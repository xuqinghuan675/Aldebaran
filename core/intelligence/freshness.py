"""Freshness helpers for normalized intelligence items."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.intelligence.models import NormalizedIntelItem, SourceSpec


def is_stale(
    published_at: str | None,
    ttl_hours: int,
    *,
    now: datetime | None = None,
) -> bool | None:
    """Return True/False for dated evidence and None when no date is present."""
    dt = parse_time(published_at)
    if dt is None:
        return None
    current = now if now is not None else (datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now())
    if dt.tzinfo and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if current.tzinfo and dt.tzinfo is None:
        dt = dt.replace(tzinfo=current.tzinfo)
    return (current - dt).total_seconds() > max(1, int(ttl_hours)) * 3600


def freshness_for_item(
    item: NormalizedIntelItem,
    spec: SourceSpec,
    *,
    now: datetime | None = None,
) -> str:
    state = is_stale(item.published_at, spec.ttl_hours, now=now)
    if state is None:
        return 'unknown'
    return 'stale' if state else 'fresh'


def parse_time(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    text = text.replace('/', '-')
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    for candidate in (text, text[:19], text[:10]):
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ('%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None
