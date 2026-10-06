"""Optional table-extractor comparison helpers for periodic-report probes."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from core.intelligence.periodic_report_documents import TableExtractionResult


def extract_tables_with_pdfplumber(path: str | Path) -> TableExtractionResult:
    from core.intelligence.periodic_report_documents import extract_periodic_report_tables

    source_path = Path(path)
    doc_id = f'table_compare_{_path_digest(source_path)}'
    return extract_periodic_report_tables(doc_id, source_path)


def extract_tables_with_camelot(path: str | Path, pages: str = 'all') -> TableExtractionResult:
    source_path = Path(path)
    camelot, load_error = _load_camelot()
    if camelot is None:
        return TableExtractionResult(status='unsupported', path=source_path, tables=[], error=load_error)
    if not source_path.exists():
        return TableExtractionResult(status='extract_error', path=source_path, tables=[], error='pdf file does not exist')

    try:
        table_list = camelot.read_pdf(str(source_path), pages=pages)
        tables = [
            block
            for table in (table_list or [])
            for block in [_camelot_table_to_block(table)]
            if block
        ]
    except Exception as exc:
        return TableExtractionResult(status='extract_error', path=source_path, tables=[], error=str(exc))
    return TableExtractionResult(status='ok', path=source_path, tables=tables, error='')


def compare_table_extractors(pdf_path: str | Path) -> dict[str, Any]:
    pdfplumber_result = extract_tables_with_pdfplumber(pdf_path)
    camelot_result = extract_tables_with_camelot(pdf_path)
    pdfplumber_tables = list(pdfplumber_result.tables or [])
    camelot_tables = list(camelot_result.tables or [])
    candidate_counts = _candidate_counts(pdfplumber_tables + camelot_tables)

    return {
        'pdf_path': str(pdf_path),
        'pdfplumber_status': pdfplumber_result.status,
        'camelot_status': camelot_result.status,
        'pdfplumber_error': pdfplumber_result.error,
        'camelot_error': camelot_result.error,
        'pdfplumber_table_count': len(pdfplumber_tables),
        'camelot_table_count': len(camelot_tables),
        'matched_header_count': _matched_header_count(pdfplumber_tables, camelot_tables),
        'customer_supplier_candidate_count': candidate_counts['customer_supplier'],
        'inventory_candidate_count': candidate_counts['inventory'],
        'cost_candidate_count': candidate_counts['cost'],
        'business_segment_candidate_count': candidate_counts['business_segments'],
    }


def _load_camelot() -> tuple[Any | None, str]:
    try:
        import camelot

        return camelot, ''
    except Exception as exc:
        return None, f'camelot missing; table comparison disabled: {exc}'


def _camelot_table_to_block(table: Any) -> dict[str, Any] | None:
    rows = _rows_from_table(table)
    cleaned_rows = [
        [_clean_text(cell) for cell in row]
        for row in rows
        if isinstance(row, (list, tuple)) and any(_clean_text(cell) for cell in row)
    ]
    if not cleaned_rows:
        return None
    header = cleaned_rows[0]
    body_rows = cleaned_rows[1:]
    return {
        'page': _int_value(getattr(table, 'page', 0)),
        'bbox': _bbox_value(getattr(table, '_bbox', [])),
        'section_guess': _guess_table_section(header, body_rows),
        'header': header,
        'rows': body_rows,
        'confidence': _camelot_quality(getattr(table, 'parsing_report', None)),
        'extractor': 'camelot',
        'parsing_report': dict(getattr(table, 'parsing_report', {}) or {}),
    }


def _rows_from_table(table: Any) -> list[list[Any]]:
    df = getattr(table, 'df', None)
    values = getattr(df, 'values', None)
    if values is not None and hasattr(values, 'tolist'):
        rows = values.tolist()
        if isinstance(rows, list):
            return rows
    data = getattr(table, 'data', None)
    if isinstance(data, list):
        return data
    return []


def _candidate_counts(tables: list[dict[str, Any]]) -> Counter[str]:
    from core.intelligence.periodic_report_parser import parse_periodic_report_text

    evidence = parse_periodic_report_text('', table_blocks=tables)
    counts = Counter(str(item.field or '') for item in evidence)
    return Counter({
        'customer_supplier': (
            counts.get('customer_supplier', 0)
            + counts.get('customer_concentration', 0)
            + counts.get('supplier_concentration', 0)
        ),
        'inventory': counts.get('inventory', 0),
        'cost': counts.get('cost', 0),
        'business_segments': counts.get('business_segments', 0),
    })


def _matched_header_count(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> int:
    left_headers = {_header_key(table) for table in left if _header_key(table)}
    right_headers = {_header_key(table) for table in right if _header_key(table)}
    return len(left_headers & right_headers)


def _header_key(table: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_clean_text(value).lower() for value in (table.get('header') or []) if _clean_text(value))


def _camelot_quality(report: Any) -> float:
    if not isinstance(report, dict):
        return 0.0
    try:
        accuracy = float(report.get('accuracy') or 0.0) / 100.0
    except Exception:
        accuracy = 0.0
    try:
        whitespace = float(report.get('whitespace') or 0.0) / 100.0
    except Exception:
        whitespace = 0.0
    return round(max(0.0, min(1.0, accuracy - whitespace * 0.25)), 3)


def _guess_table_section(header: list[str], rows: list[list[str]]) -> str:
    text = ' '.join(header + [cell for row in rows for cell in row])
    if _has_any(text, ('\u524d\u4e94\u5927\u5ba2\u6237', '\u5ba2\u6237\u540d\u79f0', '\u4f9b\u5e94\u5546')):
        return 'customer_supplier'
    if _has_any(text, ('\u5b58\u8d27', '\u8dcc\u4ef7\u51c6\u5907', '\u5e93\u5b58')):
        return 'inventory'
    if _has_any(text, ('\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u539f\u6750\u6599')):
        return 'cost'
    if _has_any(text, ('\u5206\u5730\u533a', '\u5883\u5916', '\u6d77\u5916', '\u5206\u884c\u4e1a', '\u5206\u4ea7\u54c1')):
        return 'business_segments'
    return 'table'


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _bbox_value(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[float] = []
    for item in value[:4]:
        try:
            out.append(float(item))
        except Exception:
            return []
    return out


def _path_digest(path: Path) -> str:
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _clean_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


__all__ = [
    'compare_table_extractors',
    'extract_tables_with_camelot',
    'extract_tables_with_pdfplumber',
]
