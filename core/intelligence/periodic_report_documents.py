"""Document artifact helpers for periodic report PDF parsing."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.paths import extracted_text_cache_root, pdf_index_path


EXTRACTOR_VERSION = 'periodic_report_documents:v1'


@dataclass(frozen=True)
class SectionIndexResult:
    status: str
    path: Path
    sections: list[dict[str, Any]] = field(default_factory=list)
    error: str = ''


@dataclass(frozen=True)
class TableExtractionResult:
    status: str
    path: Path
    tables: list[dict[str, Any]] = field(default_factory=list)
    error: str = ''


def build_periodic_report_doc_id(
    *,
    code: str,
    title: str,
    url: str,
    local_path: str | Path,
) -> str:
    digest = hashlib.sha256(
        '|'.join((
            _clean_code(code),
            _clean_text(title),
            str(url or '').strip(),
            str(local_path or ''),
        )).encode('utf-8')
    ).hexdigest()[:16]
    prefix = _clean_code(code) or 'periodic'
    return f'{prefix}_{digest}'


def record_periodic_report_pdf_index(
    *,
    code: str,
    name: str,
    report_type: str,
    title: str,
    url: str,
    local_path: str | Path,
    downloaded_at: str,
    parse_status: str,
    error: str = '',
    doc_id: str = '',
    extractor_version: str = EXTRACTOR_VERSION,
) -> dict[str, Any]:
    path = Path(local_path)
    record = {
        'doc_id': str(doc_id or build_periodic_report_doc_id(
            code=code,
            title=title,
            url=url,
            local_path=path,
        )),
        'code': _clean_code(code),
        'name': _clean_text(name),
        'report_type': str(report_type or 'unknown'),
        'title': _clean_text(title),
        'url': str(url or '').strip(),
        'local_path': str(path),
        'sha256': _file_sha256(path),
        'downloaded_at': str(downloaded_at or _now()),
        'extractor_version': str(extractor_version or EXTRACTOR_VERSION),
        'parse_status': str(parse_status or 'unknown'),
        'error': str(error or ''),
    }
    index_path = pdf_index_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    return record


def write_periodic_report_sections(doc_id: str, text: str) -> SectionIndexResult:
    from core.intelligence.periodic_report_parser import build_periodic_report_section_index

    path = _sections_path(doc_id)
    try:
        sections = build_periodic_report_section_index(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sections, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:
        return SectionIndexResult(status='error', path=path, sections=[], error=str(exc))
    return SectionIndexResult(status='ok', path=path, sections=sections, error='')


def write_periodic_report_tables(doc_id: str, tables: list[dict[str, Any]]) -> Path:
    path = _tables_path(doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as handle:
        for table in tables:
            handle.write(json.dumps(_normalise_table_block(table), ensure_ascii=False, sort_keys=True) + '\n')
    return path


def extract_periodic_report_tables(doc_id: str, pdf_path: str | Path) -> TableExtractionResult:
    path = _tables_path(doc_id)
    pdfplumber, load_error = _load_pdfplumber()
    if pdfplumber is None:
        return TableExtractionResult(status='unsupported', path=path, tables=[], error=load_error)

    source_path = Path(pdf_path)
    if not source_path.exists():
        return TableExtractionResult(status='extract_error', path=path, tables=[], error='pdf file does not exist')

    tables: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(str(source_path)) as pdf:
            for page_number, page in enumerate(getattr(pdf, 'pages', []) or [], start=1):
                tables.extend(_extract_page_tables(page, page_number))
    except Exception as exc:
        return TableExtractionResult(status='extract_error', path=path, tables=[], error=str(exc))

    output_path = write_periodic_report_tables(doc_id, tables)
    return TableExtractionResult(status='ok', path=output_path, tables=tables, error='')


def _extract_page_tables(page: Any, page_number: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if hasattr(page, 'find_tables'):
        for table_obj in page.find_tables() or []:
            rows = table_obj.extract() if hasattr(table_obj, 'extract') else []
            block = _table_from_rows(
                rows,
                page_number=page_number,
                bbox=getattr(table_obj, 'bbox', None),
                extractor='pdfplumber',
            )
            if block:
                out.append(block)
        return out

    if hasattr(page, 'extract_tables'):
        for rows in page.extract_tables() or []:
            block = _table_from_rows(
                rows,
                page_number=page_number,
                bbox=[],
                extractor='pdfplumber',
            )
            if block:
                out.append(block)
    return out


def _table_from_rows(
    rows: Any,
    *,
    page_number: int,
    bbox: Any,
    extractor: str,
) -> dict[str, Any] | None:
    cleaned_rows = [
        [_clean_text(cell) for cell in row]
        for row in (rows or [])
        if isinstance(row, (list, tuple)) and any(_clean_text(cell) for cell in row)
    ]
    if not cleaned_rows:
        return None
    header = cleaned_rows[0]
    body_rows = cleaned_rows[1:]
    payload = {
        'page': int(page_number or 0),
        'bbox': _clean_bbox(bbox),
        'section_guess': _guess_table_section(header, body_rows),
        'header': header,
        'rows': body_rows,
        'confidence': _table_confidence(header, body_rows),
        'extractor': extractor,
    }
    return _normalise_table_block(payload)


def _normalise_table_block(table: dict[str, Any]) -> dict[str, Any]:
    header = [_clean_text(value) for value in table.get('header') or [] if _clean_text(value)]
    rows = [
        [_clean_text(value) for value in row if _clean_text(value)]
        for row in (table.get('rows') or [])
        if isinstance(row, (list, tuple)) and any(_clean_text(value) for value in row)
    ]
    return {
        'page': int(table.get('page') or 0),
        'bbox': _clean_bbox(table.get('bbox')),
        'section_guess': _clean_text(table.get('section_guess') or _guess_table_section(header, rows)),
        'header': header,
        'rows': rows,
        'confidence': float(table.get('confidence') or _table_confidence(header, rows)),
        'extractor': _clean_text(table.get('extractor') or 'pdfplumber'),
    }


def _guess_table_section(header: list[str], rows: list[list[str]]) -> str:
    text = _table_text(header, rows)
    if _has_any(text, ('\u524d\u4e94\u5927\u5ba2\u6237', '\u5ba2\u6237\u540d\u79f0', '\u4f9b\u5e94\u5546')):
        return 'customer_supplier'
    if _has_any(text, ('\u5b58\u8d27', '\u8dcc\u4ef7\u51c6\u5907', '\u5e93\u5b58')):
        return 'inventory'
    if _has_any(text, ('\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u539f\u6750\u6599')):
        return 'cost'
    if _has_any(text, ('\u5206\u5730\u533a', '\u5883\u5916', '\u6d77\u5916', '\u5206\u884c\u4e1a', '\u5206\u4ea7\u54c1')):
        return 'business_segments'
    if _has_any(text, ('\u8d22\u52a1\u62a5\u8868', '\u8d44\u4ea7\u8d1f\u503a\u8868')):
        return 'financial_statements'
    return 'table'


def _table_confidence(header: list[str], rows: list[list[str]]) -> float:
    score = 0.55
    if header:
        score += 0.15
    if rows:
        score += 0.15
    if _guess_table_section(header, rows) != 'table':
        score += 0.1
    return min(0.95, round(score, 2))


def _sections_path(doc_id: str) -> Path:
    return extracted_text_cache_root('periodic_report') / f'{_safe_doc_id(doc_id)}.sections.json'


def _tables_path(doc_id: str) -> Path:
    return extracted_text_cache_root('periodic_report') / f'{_safe_doc_id(doc_id)}.tables.jsonl'


def _load_pdfplumber() -> tuple[Any | None, str]:
    try:
        import pdfplumber

        return pdfplumber, ''
    except Exception as exc:
        return None, f'pdfplumber missing; table extraction disabled: {exc}'


def _file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ''
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _clean_bbox(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[float] = []
    for item in value[:4]:
        try:
            out.append(float(item))
        except Exception:
            return []
    return out


def _table_text(header: list[str], rows: list[list[str]]) -> str:
    return ' '.join(header + [cell for row in rows for cell in row])


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _clean_code(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _clean_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def _safe_doc_id(value: str) -> str:
    cleaned = ''.join(ch if ch.isalnum() or ch in {'_', '-'} else '_' for ch in str(value or '').strip())
    return cleaned or 'periodic_report'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'EXTRACTOR_VERSION',
    'SectionIndexResult',
    'TableExtractionResult',
    'build_periodic_report_doc_id',
    'extract_periodic_report_tables',
    'record_periodic_report_pdf_index',
    'write_periodic_report_sections',
    'write_periodic_report_tables',
]
