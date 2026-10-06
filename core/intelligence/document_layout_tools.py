"""Optional document-layout comparison helpers for periodic-report probes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.MULTILINE)


def extract_markdown_with_pymupdf4llm(path: str | Path) -> dict[str, Any]:
    source_path = Path(path)
    module, load_error = _load_pymupdf4llm()
    if module is None:
        return _layout_result('unsupported', source_path, markdown='', error=load_error)
    if not source_path.exists():
        return _layout_result('extract_error', source_path, markdown='', error='pdf file does not exist')
    if not hasattr(module, 'to_markdown'):
        return _layout_result('unsupported', source_path, markdown='', error='pymupdf4llm.to_markdown unavailable')

    try:
        markdown = module.to_markdown(str(source_path))
    except Exception as exc:
        return _layout_result('extract_error', source_path, markdown='', error=str(exc))
    return _layout_result('ok', source_path, markdown=str(markdown or ''), error='')


def extract_json_with_pymupdf4llm(path: str | Path) -> dict[str, Any]:
    source_path = Path(path)
    module, load_error = _load_pymupdf4llm()
    if module is None:
        return {'status': 'unsupported', 'path': str(source_path), 'json': None, 'error': load_error}
    if not source_path.exists():
        return {'status': 'extract_error', 'path': str(source_path), 'json': None, 'error': 'pdf file does not exist'}
    if not hasattr(module, 'to_markdown'):
        return {'status': 'unsupported', 'path': str(source_path), 'json': None, 'error': 'pymupdf4llm.to_markdown unavailable'}

    try:
        payload = module.to_markdown(str(source_path), page_chunks=True)
    except TypeError:
        return {'status': 'unsupported', 'path': str(source_path), 'json': None, 'error': 'page_chunks mode unavailable'}
    except Exception as exc:
        return {'status': 'extract_error', 'path': str(source_path), 'json': None, 'error': str(exc)}
    return {'status': 'ok', 'path': str(source_path), 'json': payload, 'error': ''}


def compare_section_index_with_markdown(
    pdf_path: str | Path,
    current_sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    markdown_result = extract_markdown_with_pymupdf4llm(pdf_path)
    json_result = extract_json_with_pymupdf4llm(pdf_path)
    markdown = str(markdown_result.get('markdown') or '')
    headings = _markdown_headings(markdown)
    return {
        'pdf_path': str(pdf_path),
        'markdown_status': markdown_result['status'],
        'json_status': json_result['status'],
        'markdown_error': markdown_result.get('error', ''),
        'json_error': json_result.get('error', ''),
        'heading_count': len(headings),
        'table_markdown_count': _count_markdown_tables(markdown),
        'section_overlap_count': _section_overlap_count(headings, current_sections or []),
        'reading_order_warning_count': _reading_order_warning_count(headings),
    }


def _load_pymupdf4llm() -> tuple[Any | None, str]:
    try:
        import pymupdf4llm

        return pymupdf4llm, ''
    except Exception as exc:
        return None, f'pymupdf4llm missing; layout comparison disabled: {exc}'


def _layout_result(status: str, path: Path, *, markdown: str, error: str) -> dict[str, Any]:
    return {'status': status, 'path': str(path), 'markdown': markdown, 'error': error}


def _markdown_headings(markdown: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for match in _HEADING_RE.finditer(markdown or ''):
        title = _clean_heading(match.group(2))
        headings.append({
            'level': len(match.group(1)),
            'title': title,
            'key': _norm(title),
            'line': (markdown[:match.start()].count('\n') + 1),
        })
    return headings


def _count_markdown_tables(markdown: str) -> int:
    count = 0
    in_table = False
    for line in str(markdown or '').splitlines():
        stripped = line.strip()
        is_table_line = stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2
        if is_table_line and not in_table:
            count += 1
            in_table = True
        elif not is_table_line:
            in_table = False
    return count


def _section_overlap_count(headings: list[dict[str, Any]], sections: list[dict[str, Any]]) -> int:
    heading_keys = {str(heading.get('key') or '') for heading in headings}
    count = 0
    for section in sections:
        keys = {
            _norm(section.get('title')),
            _norm(str(section.get('section_id') or '').replace('_', ' ')),
        }
        if heading_keys & {key for key in keys if key}:
            count += 1
    return count


def _reading_order_warning_count(headings: list[dict[str, Any]]) -> int:
    warnings = 0
    last_level = 0
    seen: set[str] = set()
    for heading in headings:
        level = int(heading.get('level') or 0)
        key = str(heading.get('key') or '')
        if last_level and level > last_level + 1:
            warnings += 1
        if key and key in seen:
            warnings += 1
        seen.add(key)
        last_level = level
    return warnings


def _clean_heading(value: Any) -> str:
    text = ' '.join(str(value or '').split()).strip()
    return text.strip('#').strip()


def _norm(value: Any) -> str:
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]+', ' ', str(value or '').lower()).strip()


def dumps_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = [
    'compare_section_index_with_markdown',
    'extract_json_with_pymupdf4llm',
    'extract_markdown_with_pymupdf4llm',
]
