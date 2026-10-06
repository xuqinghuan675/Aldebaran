"""Small JSON cache for normalized intelligence collection output."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from core.intelligence.models import NormalizedIntelItem
from core.paths import intelligence_cache_root


_ITEM_FIELDS = {field.name for field in fields(NormalizedIntelItem)}


def load_normalized(source_id: str) -> list[NormalizedIntelItem]:
    path = _normalized_path(source_id)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    items = raw.get('items') if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[NormalizedIntelItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = {key: value for key, value in item.items() if key in _ITEM_FIELDS}
        try:
            out.append(NormalizedIntelItem(**payload))
        except TypeError:
            continue
    return out


def save_normalized(source_id: str, items: Iterable[NormalizedIntelItem | dict[str, Any]]) -> None:
    path = _normalized_path(source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'source_id': source_id,
        'saved_at': _now(),
        'items': [_item_dict(item) for item in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def load_source_status() -> dict[str, dict[str, Any]]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_source_status(status: dict[str, dict[str, Any]]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status or {}, ensure_ascii=False, indent=2), encoding='utf-8')


def update_source_status(source_id: str, ok: bool, item_count: int, error: str = '') -> dict[str, Any]:
    status = load_source_status()
    current = dict(status.get(source_id) or {})
    now = _now()
    current.update({
        'status': 'ok' if ok else 'error',
        'last_attempt_at': now,
        'last_item_count': int(item_count or 0),
        'last_error': '' if ok else str(error or ''),
    })
    if ok:
        current['last_success_at'] = now
        current.setdefault('error_count', 0)
    else:
        current['error_count'] = int(current.get('error_count') or 0) + 1
    status[source_id] = current
    save_source_status(status)
    return current


def cache_root() -> Path:
    return intelligence_cache_root()


def _normalized_path(source_id: str) -> Path:
    return cache_root() / 'normalized' / f'{_safe_source_id(source_id)}.json'


def _status_path() -> Path:
    return cache_root() / 'source_status.json'


def _item_dict(item: NormalizedIntelItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, NormalizedIntelItem):
        return asdict(item)
    return dict(item or {})


def _safe_source_id(source_id: str) -> str:
    return re.sub(r'[^0-9A-Za-z_.-]+', '__', str(source_id or 'unknown')).strip('_') or 'unknown'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')
