"""Low-load optional Firecrawl helpers for web-context intelligence probes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from core.intelligence.models import RawIntelItem
from core.paths import intelligence_cache_root


EXTRACTOR_VERSION = 'firecrawl_scrape_v1'

FIRECRAWL_ALLOWED_LAYERS = {
    'policy',
    'industry',
    'export',
    'demand',
    'competition',
    'risk',
}

_TECHNICAL_PATTERNS = (
    re.compile(r'\bK[- ]?line\b', re.IGNORECASE),
    re.compile(r'\bMACD\b', re.IGNORECASE),
    re.compile(r'\bRSI\b', re.IGNORECASE),
    re.compile(r'\bKDJ\b', re.IGNORECASE),
    re.compile(r'\bMA\b'),
    re.compile(r'\bsupport\b', re.IGNORECASE),
    re.compile(r'\bresistance\b', re.IGNORECASE),
    re.compile(r'\bbuy point\b', re.IGNORECASE),
    re.compile(r'\bstop loss\b', re.IGNORECASE),
    re.compile(r'\btrigger price\b', re.IGNORECASE),
    re.compile(r'\binvalidation price\b', re.IGNORECASE),
    re.compile('\u004b\u7ebf'),
    re.compile('\u5747\u7ebf'),
    re.compile('\u652f\u6491\u4f4d'),
    re.compile('\u538b\u529b\u4f4d'),
    re.compile('\u4e70\u70b9'),
    re.compile('\u6b62\u635f'),
    re.compile('\u89e6\u53d1\u4ef7'),
    re.compile('\u5931\u6548\u4ef7'),
)

_SCRAPE_LOCK = threading.Semaphore(1)


@dataclass
class FirecrawlWebResult:
    status: str
    url: str
    layer: str
    markdown: str = ''
    title: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    error: str = ''
    elapsed_ms: int = 0
    source_status: str = ''
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ''
    cache_path: str = ''
    access_mode: str = 'optional_api'

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['source_status'] = self.source_status or self.status
        return payload


def is_firecrawl_enabled() -> bool:
    return build_firecrawl_status().get('status') == 'ready'


def build_firecrawl_status() -> dict[str, Any]:
    mode = _clean_mode(os.environ.get('FIRECRAWL_MODE'))
    api_key = str(os.environ.get('FIRECRAWL_API_KEY') or '').strip()
    base_url = str(os.environ.get('FIRECRAWL_BASE_URL') or '').strip().rstrip('/')
    access_mode = 'optional_self_hosted' if mode == 'local_lite' else 'optional_api'
    timeout_seconds = _timeout_seconds(os.environ.get('FIRECRAWL_TIMEOUT_SECONDS'))
    max_concurrent = _max_concurrent(os.environ.get('FIRECRAWL_MAX_CONCURRENT'))
    cache_ttl_hours = _positive_int(os.environ.get('FIRECRAWL_CACHE_TTL_HOURS'), 24, 1, 168)
    min_interval_hours = _positive_int(os.environ.get('FIRECRAWL_MIN_INTERVAL_HOURS'), 12, 0, 168)
    max_markdown_chars = _positive_int(os.environ.get('FIRECRAWL_MAX_MARKDOWN_CHARS'), 12000, 1000, 200000)

    status = {
        'mode': mode,
        'status': 'disabled',
        'enabled': False,
        'has_api_key': bool(api_key),
        'base_url': base_url,
        'timeout_seconds': timeout_seconds,
        'max_concurrent': max_concurrent,
        'cache_ttl_hours': cache_ttl_hours,
        'min_interval_hours': min_interval_hours,
        'max_markdown_chars': max_markdown_chars,
        'access_mode': access_mode,
        'missing_reason': 'firecrawl_disabled',
    }
    if mode == 'disabled':
        return status
    if mode == 'remote' and not api_key:
        status.update(status='missing_credentials', missing_reason='missing_credentials')
        return status
    if not base_url:
        status.update(status='missing_base_url', missing_reason='missing_base_url')
        return status
    status.update(status='ready', enabled=True, missing_reason='')
    return status


def scrape_url_to_markdown(
    url: str,
    schema: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    *,
    layer: str = 'policy',
    request_post: Callable[..., Any] | None = None,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    status = build_firecrawl_status()
    source_url = str(url or '').strip()
    clean_layer = _clean_layer(layer)
    started = time.monotonic()
    cache_dir = _cache_dir(source_url, clean_layer)

    if status.get('status') != 'ready':
        return _result(
            status=str(status.get('status') or 'disabled'),
            url=source_url,
            layer=clean_layer,
            error=str(status.get('missing_reason') or status.get('status') or ''),
            elapsed_ms=_elapsed_ms(started),
            cache_path=cache_dir,
            access_mode=str(status.get('access_mode') or 'optional_api'),
        )
    if not source_url:
        return _result('invalid_url', source_url, clean_layer, 'url is required', _elapsed_ms(started), cache_dir, str(status['access_mode']))

    if use_cache and not force_refresh:
        cached = _read_cache(cache_dir, int(status['cache_ttl_hours']))
        if cached is not None:
            cached.update(status='cached', source_status='cached', cached=True, elapsed_ms=_elapsed_ms(started))
            return cached

    if not force_refresh and _requested_recently(cache_dir, int(status['min_interval_hours'])):
        return _result(
            'skipped_recently',
            source_url,
            clean_layer,
            'request skipped by FIRECRAWL_MIN_INTERVAL_HOURS',
            _elapsed_ms(started),
            cache_dir,
            str(status['access_mode']),
        )

    timeout = int(timeout_seconds or status['timeout_seconds'])
    try:
        post = request_post or _requests_post
        with _SCRAPE_LOCK:
            response = post(
                _scrape_endpoint(str(status['base_url'])),
                json=_scrape_payload(source_url, timeout),
                headers=_headers(),
                timeout=timeout,
            )
        if hasattr(response, 'raise_for_status'):
            response.raise_for_status()
        payload = response.json() if hasattr(response, 'json') else {}
    except Exception as exc:
        failure_status = 'timeout' if _is_timeout(exc) else 'firecrawl_error'
        _write_metadata(cache_dir, _metadata_payload(source_url, clean_layer, failure_status, {}, False, str(status['access_mode'])))
        return _result(
            failure_status,
            source_url,
            clean_layer,
            str(exc),
            _elapsed_ms(started),
            cache_dir,
            str(status['access_mode']),
        )

    data = payload.get('data') if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    markdown = _truncate_markdown(str(data.get('markdown') or (payload.get('markdown') if isinstance(payload, dict) else '') or ''), int(status['max_markdown_chars']))
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    result_status = 'ok' if _clean_text(markdown) else 'empty_text'
    title = _clean_text(metadata.get('title') or _heading_title(markdown) or source_url)

    result = FirecrawlWebResult(
        status=result_status,
        source_status=result_status,
        url=source_url,
        layer=clean_layer,
        markdown=markdown,
        title=title,
        metadata=metadata,
        cached=False,
        error=_result_error(result_status),
        elapsed_ms=_elapsed_ms(started),
        raw=payload if isinstance(payload, dict) else {},
        fetched_at=_now(),
        cache_path=str(cache_dir),
        access_mode=str(status['access_mode']),
    ).to_dict()
    if result_status == 'ok':
        _write_cache(cache_dir, result)
    else:
        _write_metadata(cache_dir, _metadata_payload(source_url, clean_layer, result_status, metadata, False, str(status['access_mode'])))
    return result


def firecrawl_result_to_raw_intel(
    result: dict[str, Any],
    *,
    url: str,
    layer: str,
    query: str = '',
    title: str = '',
    source_id: str = '',
    source_name: str = '',
) -> RawIntelItem | None:
    clean_layer = _clean_layer(layer)
    if clean_layer not in FIRECRAWL_ALLOWED_LAYERS:
        raise ValueError(f'invalid Firecrawl evidence layer: {layer}')
    if str(result.get('status') or '') not in {'ok', 'cached'}:
        return None

    markdown = str(result.get('markdown') or '')
    metadata = result.get('metadata') if isinstance(result.get('metadata'), dict) else {}
    clean_title = _clean_text(title or result.get('title') or metadata.get('title') or _heading_title(markdown) or url)
    if _contains_technical_terms(f'{clean_title}\n{markdown}'):
        return None

    access_mode = str(result.get('access_mode') or 'optional_api')
    original_url = str(url or result.get('url') or '').strip()
    sid = source_id or _source_id_for_layer(clean_layer)
    return RawIntelItem(
        source_id=sid,
        source_name=source_name or _source_name_for_layer(clean_layer),
        source_url=original_url,
        fetched_at=str(result.get('fetched_at') or _now()),
        published_at=_published_at(metadata),
        title=clean_title,
        text=markdown,
        html='',
        url=original_url,
        layer=clean_layer,
        trust_level='secondary',
        raw={
            'category': 'web_context',
            'source_role': 'secondary',
            'evidence_origin': 'web_context',
            'access_mode': access_mode,
            'extractor': 'firecrawl_scrape',
            'cached': bool(result.get('cached')),
            'original_url': original_url,
            'markdown_length': len(markdown),
            'query': str(query or ''),
            'firecrawl_status': str(result.get('status') or ''),
            'metadata': metadata,
        },
    )


def _clean_mode(value: Any) -> str:
    mode = str(value or 'disabled').strip().lower()
    aliases = {'hosted': 'remote', 'self_hosted': 'local_lite', 'local': 'local_lite'}
    mode = aliases.get(mode, mode)
    return mode if mode in {'disabled', 'local_lite', 'remote'} else 'disabled'


def _clean_layer(value: Any) -> str:
    return str(value or '').strip().lower()


def _timeout_seconds(value: Any) -> int:
    return _positive_int(value, 25, 1, 60)


def _max_concurrent(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 1
    return 1 if parsed != 1 else parsed


def _positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _headers() -> dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    api_key = str(os.environ.get('FIRECRAWL_API_KEY') or '').strip()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def _scrape_endpoint(base_url: str) -> str:
    base = str(base_url or '').strip().rstrip('/')
    if base.endswith('/v1') or base.endswith('/v2'):
        return f'{base}/scrape'
    return f'{base}/v2/scrape'


def _scrape_payload(url: str, timeout_seconds: int) -> dict[str, Any]:
    return {
        'url': url,
        'formats': ['markdown'],
        'onlyMainContent': True,
        'timeout': int(max(1, min(timeout_seconds, 60)) * 1000),
        'removeBase64Images': True,
        'skipTlsVerification': False,
    }


def _requests_post(*args, **kwargs):
    import requests

    return requests.post(*args, **kwargs)


def _cache_dir(url: str, layer: str) -> Path:
    digest = hashlib.sha256(f'{url}\n{layer}\n{EXTRACTOR_VERSION}'.encode('utf-8')).hexdigest()
    return intelligence_cache_root() / 'web_context' / 'firecrawl' / digest


def _read_cache(path: Path, ttl_hours: int) -> dict[str, Any] | None:
    markdown_file = path / 'markdown.json'
    metadata_file = path / 'metadata.json'
    try:
        if not markdown_file.exists() or not metadata_file.exists():
            return None
        metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
        fetched_at = _parse_dt(metadata.get('fetched_at') or metadata.get('last_attempted_at'))
        if fetched_at is None or datetime.now() - fetched_at > timedelta(hours=ttl_hours):
            return None
        markdown_payload = json.loads(markdown_file.read_text(encoding='utf-8'))
        markdown = str(markdown_payload.get('markdown') or '')
        if not markdown:
            return None
        return {
            'status': 'cached',
            'source_status': 'cached',
            'url': str(metadata.get('url') or ''),
            'layer': str(metadata.get('layer') or ''),
            'markdown': markdown,
            'title': str(metadata.get('title') or ''),
            'metadata': metadata.get('metadata') if isinstance(metadata.get('metadata'), dict) else {},
            'cached': True,
            'error': '',
            'elapsed_ms': 0,
            'raw': {},
            'fetched_at': str(metadata.get('fetched_at') or ''),
            'cache_path': str(path),
            'access_mode': str(metadata.get('access_mode') or 'optional_api'),
        }
    except Exception:
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / 'markdown.json').write_text(
        json.dumps({'markdown': str(payload.get('markdown') or '')}, ensure_ascii=False, sort_keys=True),
        encoding='utf-8',
    )
    (path / 'metadata.json').write_text(
        json.dumps(
            _metadata_payload(
                str(payload.get('url') or ''),
                str(payload.get('layer') or ''),
                str(payload.get('status') or ''),
                payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {},
                bool(payload.get('cached')),
                str(payload.get('access_mode') or 'optional_api'),
                title=str(payload.get('title') or ''),
                fetched_at=str(payload.get('fetched_at') or _now()),
            ),
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding='utf-8',
    )


def _write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / 'metadata.json').write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')


def _metadata_payload(
    url: str,
    layer: str,
    status: str,
    metadata: dict[str, Any],
    cached: bool,
    access_mode: str,
    *,
    title: str = '',
    fetched_at: str | None = None,
) -> dict[str, Any]:
    now = _now()
    return {
        'url': url,
        'layer': layer,
        'status': status,
        'title': title or _clean_text(metadata.get('title') or ''),
        'metadata': metadata,
        'cached': cached,
        'access_mode': access_mode,
        'extractor': 'firecrawl_scrape',
        'extractor_version': EXTRACTOR_VERSION,
        'fetched_at': fetched_at or now,
        'last_attempted_at': now,
    }


def _requested_recently(path: Path, min_interval_hours: int) -> bool:
    if min_interval_hours <= 0:
        return False
    try:
        metadata = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
    except Exception:
        return False
    attempted = _parse_dt(metadata.get('last_attempted_at') or metadata.get('fetched_at'))
    return attempted is not None and datetime.now() - attempted < timedelta(hours=min_interval_hours)


def _result(
    status: str,
    url: str,
    layer: str,
    error: str,
    elapsed_ms: int,
    cache_path: Path,
    access_mode: str,
) -> dict[str, Any]:
    return FirecrawlWebResult(
        status=status,
        source_status=status,
        url=url,
        layer=layer,
        markdown='',
        title='',
        metadata={},
        cached=False,
        error=error,
        elapsed_ms=elapsed_ms,
        raw={},
        fetched_at=_now(),
        cache_path=str(cache_path),
        access_mode=access_mode,
    ).to_dict()


def _result_error(status: str) -> str:
    if status == 'ok':
        return ''
    if status == 'empty_text':
        return 'empty markdown'
    return status


def _source_id_for_layer(layer: str) -> str:
    if layer == 'industry':
        return 'firecrawl:industry_web_context'
    return f'firecrawl:{layer}_web_context'


def _source_name_for_layer(layer: str) -> str:
    return f'Firecrawl {layer.replace("_", " ")} web context'


def _contains_technical_terms(text: str) -> bool:
    return any(pattern.search(text or '') for pattern in _TECHNICAL_PATTERNS)


def _heading_title(markdown: str) -> str:
    for line in str(markdown or '').splitlines():
        cleaned = line.strip()
        if cleaned.startswith('#'):
            return cleaned.lstrip('#').strip()
    return ''


def _published_at(metadata: dict[str, Any]) -> str | None:
    for key in ('publishedTime', 'published_at', 'date', 'publishedDate'):
        value = str(metadata.get(key) or '').strip()
        if value:
            return value
    return None


def _clean_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def _truncate_markdown(markdown: str, max_chars: int) -> str:
    if len(markdown) <= max_chars:
        return markdown
    return markdown[:max_chars]


def _is_timeout(exc: Exception) -> bool:
    text = f'{type(exc).__name__}: {exc}'.lower()
    return isinstance(exc, TimeoutError) or 'timeout' in text or 'timed out' in text


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'FIRECRAWL_ALLOWED_LAYERS',
    'FirecrawlWebResult',
    'build_firecrawl_status',
    'firecrawl_result_to_raw_intel',
    'is_firecrawl_enabled',
    'scrape_url_to_markdown',
]
