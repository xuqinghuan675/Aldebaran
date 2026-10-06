"""PDF download/cache/text extraction helpers for disclosure sources."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from core import http_client
from core.paths import disclosure_documents_root


_WINDOWS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_DATE_RE = re.compile(r'(\d{4})\D?(\d{2})\D?(\d{2})')


@dataclass(frozen=True)
class DisclosurePdfResult:
    url: str
    cache_path: Path
    status: str
    text: str = ''
    page_count: int = 0
    error: str = ''
    fetched_at: str = ''


def build_pdf_cache_path(
    url: str,
    code: str | None = None,
    published_at: str | None = None,
    document_kind: str = 'announcement',
) -> Path:
    """Build a stable document cache path under the intelligence cache root."""
    url_text = str(url or '').strip()
    url_hash = hashlib.sha256(url_text.encode('utf-8')).hexdigest()[:16]
    stem = _safe_stem(Path(unquote(urlparse(url_text).path)).stem)
    if not stem:
        stem = 'disclosure'

    parts = []
    safe_code = _safe_stem(code or '')
    safe_date = _safe_date(published_at or '')
    if safe_code:
        parts.append(safe_code)
    if safe_date:
        parts.append(safe_date)
    parts.extend((stem[:80], url_hash))
    filename = '_'.join(part for part in parts if part) + '.pdf'
    return disclosure_documents_root(document_kind) / filename


def download_pdf(
    url: str,
    cache_path: str | Path,
    timeout_seconds: int = 10,
) -> DisclosurePdfResult:
    path = Path(cache_path)
    now = _now()
    if path.exists():
        return DisclosurePdfResult(
            url=str(url or ''),
            cache_path=path,
            status='cached',
            fetched_at=now,
        )

    try:
        response = http_client.get(str(url or ''), timeout=timeout_seconds)
        response.raise_for_status()
        payload = bytes(getattr(response, 'content', b'') or b'')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except Exception as exc:
        return DisclosurePdfResult(
            url=str(url or ''),
            cache_path=path,
            status='download_error',
            error=str(exc),
            fetched_at=now,
        )

    return DisclosurePdfResult(
        url=str(url or ''),
        cache_path=path,
        status='ok',
        fetched_at=now,
    )


def extract_pdf_text(path: str | Path) -> DisclosurePdfResult:
    cache_path = Path(path)
    now = _now()
    reader_cls, backend, load_error = _load_pdf_reader()
    if reader_cls is None:
        return DisclosurePdfResult(
            url='',
            cache_path=cache_path,
            status='unsupported',
            error=load_error or 'no supported pdf parser is installed',
            fetched_at=now,
        )
    if not cache_path.exists():
        return DisclosurePdfResult(
            url='',
            cache_path=cache_path,
            status='extract_error',
            error='pdf file does not exist',
            fetched_at=now,
        )

    try:
        text, page_count = _extract_with_reader(cache_path, reader_cls)
    except Exception as exc:
        return DisclosurePdfResult(
            url='',
            cache_path=cache_path,
            status='extract_error',
            error=str(exc),
            fetched_at=now,
        )

    if not text.strip():
        return DisclosurePdfResult(
            url='',
            cache_path=cache_path,
            status='empty_text',
            text='',
            page_count=page_count,
            error=f'{backend} returned no text',
            fetched_at=now,
        )
    return DisclosurePdfResult(
        url='',
        cache_path=cache_path,
        status='ok',
        text=text.strip(),
        page_count=page_count,
        fetched_at=now,
    )


def _extract_with_reader(path: Path, reader_cls: Callable[[Any], Any]) -> tuple[str, int]:
    with path.open('rb') as stream:
        reader = reader_cls(stream)
        pages = list(getattr(reader, 'pages', []) or [])
        chunks = []
        for page in pages:
            page_text = page.extract_text() if hasattr(page, 'extract_text') else ''
            if page_text:
                chunks.append(str(page_text).strip())
        return '\n\n'.join(chunk for chunk in chunks if chunk), len(pages)


def _load_pdf_reader() -> tuple[Callable[[Any], Any] | None, str, str]:
    try:
        from pypdf import PdfReader

        return PdfReader, 'pypdf', ''
    except Exception as first_error:
        try:
            from PyPDF2 import PdfReader

            return PdfReader, 'PyPDF2', ''
        except Exception as second_error:
            return None, '', f'{first_error}; {second_error}'


def _safe_stem(value: str) -> str:
    cleaned = _WINDOWS_UNSAFE.sub('_', str(value or '')).strip(' ._')
    cleaned = re.sub(r'_+', '_', cleaned)
    return cleaned


def _safe_date(value: str) -> str:
    match = _DATE_RE.search(str(value or ''))
    if not match:
        return ''
    return f'{match.group(1)}-{match.group(2)}-{match.group(3)}'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'DisclosurePdfResult',
    'build_pdf_cache_path',
    'download_pdf',
    'extract_pdf_text',
]
