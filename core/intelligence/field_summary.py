"""Aggregate periodic report evidence into field-level summaries."""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable

from core.intelligence.models import NormalizedIntelItem


FIELD_SUMMARY_FIELDS = (
    'business_segments',
    'inventory',
    'customer_concentration',
    'supplier_concentration',
    'export',
    'cost',
    'financial',
    'risk',
)

_FIELD_LAYERS = {
    'business_segments': 'financial',
    'customer_concentration': 'customer_supplier',
    'supplier_concentration': 'customer_supplier',
}

_FORBIDDEN_TERMS = (
    '\u004b\u7ebf',
    '\u5747\u7ebf',
    'MACD',
    'RSI',
    'KDJ',
    '\u652f\u6491\u4f4d',
    '\u538b\u529b\u4f4d',
    '\u4e70\u70b9',
    '\u6b62\u635f',
    '\u89e6\u53d1\u4ef7',
    '\u5931\u6548\u4ef7',
    'technical-analysis',
)

_POSITIVE_TERMS = (
    '\u540c\u6bd4\u589e\u957f',
    '\u589e\u957f',
    '\u589e\u52a0',
    '\u63d0\u5347',
    '\u6539\u5584',
    '\u63d0\u9ad8',
    '\u4e0a\u5347',
    '\u6062\u590d',
    '\u6269\u5927',
    'increase',
    'increased',
    'improve',
    'improved',
    'growth',
)

_NEGATIVE_TERMS = (
    '\u540c\u6bd4\u4e0b\u964d',
    '\u4e0b\u964d',
    '\u51cf\u5c11',
    '\u4e0b\u6ed1',
    '\u6076\u5316',
    '\u964d\u4f4e',
    '\u8870\u9000',
    'decline',
    'declined',
    'decrease',
    'decreased',
    'drop',
    'deteriorate',
    'deteriorated',
)

_CHANGE_KEYS = ('yoy', 'change', 'growth', '\u540c\u6bd4', '\u589e\u51cf')
_TOTAL_TERMS = ('\u5408\u8ba1', 'total', 'top five')
_OVERSEAS_TERMS = ('\u5883\u5916', '\u6d77\u5916', '\u5916\u9500', '\u56fd\u5916', '\u51fa\u53e3', 'overseas', 'export')
_INVENTORY_TERMS = (
    '\u539f\u6750\u6599',
    '\u5e93\u5b58\u5546\u54c1',
    '\u53d1\u51fa\u5546\u54c1',
    '\u8dcc\u4ef7\u51c6\u5907',
    '\u5b58\u8d27',
)
_EXPORT_CONTEXT_TERMS = (
    '\u6536\u5165',
    '\u4e1a\u52a1',
    '\u5ba2\u6237',
    '\u9500\u552e',
    '\u5e02\u573a',
    '\u56fd\u9645',
    '\u8d38\u6613',
    '\u51fa\u53e3',
    '\u5916\u9500',
    'export',
    'overseas',
    'revenue',
    'sales',
    'market',
)
_EXPORT_NOISE_TERMS = (
    '\u7ea2\u7b79\u67b6\u6784',
    '\u67b6\u6784\u62c6\u9664',
    '\u80a1\u6743\u8f6c\u8ba9',
    '\u4ee3\u6263\u4ee3\u7f34',
)


def build_periodic_field_summary_items(
    items: Iterable[NormalizedIntelItem | dict[str, Any] | Any],
    *,
    missing_reasons: dict[str, str] | None = None,
    top_n: int = 3,
) -> tuple[list[NormalizedIntelItem], dict[str, Any]]:
    """Return summary items plus source-status counters for periodic evidence."""
    groups: dict[str, list[NormalizedIntelItem]] = defaultdict(list)
    for raw in items or []:
        item = _coerce_item(raw)
        if item is None:
            continue
        if str(item.category or '') != 'periodic_report_body':
            continue
        field = _field_for_item(item)
        if field not in FIELD_SUMMARY_FIELDS:
            continue
        if _contains_forbidden(_render_item(item)):
            continue
        if not _is_summary_candidate(item, field):
            continue
        groups[field].append(item)

    summaries: list[NormalizedIntelItem] = []
    weak_fields: list[str] = []
    mixed_fields: list[str] = []

    for field in FIELD_SUMMARY_FIELDS:
        evidence = groups.get(field, [])
        if not evidence:
            continue
        top = _select_top_evidence(evidence, top_n=max(1, int(top_n or 1)))
        strongest = top[0]
        avg_quality = _average_quality(evidence)
        direction, conflict_reason = _direction_for_items(evidence)
        confidence = _summary_confidence(avg_quality, len(evidence), direction)
        if direction == 'mixed':
            mixed_fields.append(field)
        if confidence < 6 or avg_quality < 0.55 or len(evidence) < 2:
            weak_fields.append(field)

        source_period = _source_period(strongest)
        summary_text = _summary_text(
            field,
            evidence_count=len(evidence),
            direction=direction,
            average_quality=avg_quality,
            top=top,
            conflict_reason=conflict_reason,
        )
        metadata = {
            'field': field,
            'evidence_count': len(evidence),
            'top_evidence_ids': [str(item.id or '') for item in top if str(item.id or '')],
            'top_raw_refs': [str(item.raw_ref or '') for item in top if str(item.raw_ref or '')],
            'average_quality_score': avg_quality,
            'strongest_source_title': _source_title(strongest),
            'source_period': source_period,
            'direction': direction,
            'confidence': confidence,
            'missing_reason': '',
            'conflict_reason': conflict_reason,
            'summary_text': summary_text,
            'top_table_values': [_metadata(item).get('table_values') or {} for item in top],
        }
        summaries.append(NormalizedIntelItem(
            id=_summary_id(field, strongest, source_period),
            source_id=str(strongest.source_id or 'requests:company_announcements'),
            title=f'{field} field summary',
            summary=summary_text[:260],
            layer=_FIELD_LAYERS.get(field, field),
            direction=direction,
            related_codes=list(strongest.related_codes or []),
            related_names=list(strongest.related_names or []),
            related_sectors=[],
            evidence_type='periodic_field_summary',
            published_at=strongest.published_at,
            fetched_at=_now(),
            url=str(strongest.url or strongest.source_url or ''),
            source_url=str(strongest.source_url or strongest.url or ''),
            trust_level=strongest.trust_level or 'primary',
            confidence=confidence,
            category='periodic_field_summary',
            relevance_reason=f'field_summary:{field}',
            matched_keywords=_top_keywords(evidence),
            time_windows=['swing', 'mid', 'long'],
            raw_ref=f'field_summary:{field}:{source_period or "unknown"}',
            metadata=metadata,
        ))

    missing = _missing_fields(groups, missing_reasons or {})
    status = {
        'field_summary_count': len(summaries),
        'field_summary_by_field': dict(sorted(Counter(item.metadata.get('field') for item in summaries).items())),
        'mixed_fields': sorted(mixed_fields),
        'weak_fields': sorted(weak_fields),
        'missing_fields': dict(sorted(missing.items())),
    }
    return summaries, status


def _coerce_item(raw: NormalizedIntelItem | dict[str, Any] | Any) -> NormalizedIntelItem | None:
    if isinstance(raw, NormalizedIntelItem):
        return raw
    if isinstance(raw, dict):
        try:
            return NormalizedIntelItem(**raw)
        except TypeError:
            return None
    field = str(getattr(raw, 'field', '') or '')
    if not field:
        return None
    layer = str(getattr(raw, 'layer', '') or _FIELD_LAYERS.get(field, field))
    return NormalizedIntelItem(
        id=str(getattr(raw, 'raw_ref', '') or f'periodic:{field}'),
        source_id='requests:company_announcements',
        title=str(getattr(raw, 'title', '') or f'{field} evidence'),
        summary=str(getattr(raw, 'summary', '') or getattr(raw, 'evidence_text', '') or ''),
        layer=layer,
        direction='neutral',
        related_codes=[str(getattr(raw, 'code', '') or '')] if str(getattr(raw, 'code', '') or '') else [],
        related_names=[str(getattr(raw, 'name', '') or '')] if str(getattr(raw, 'name', '') or '') else [],
        related_sectors=[],
        evidence_type='periodic_report',
        published_at=getattr(raw, 'published_at', None),
        fetched_at=_now(),
        url=str(getattr(raw, 'source_url', '') or ''),
        trust_level='primary',
        confidence=int(getattr(raw, 'confidence', 0) or 0),
        category='periodic_report_body',
        raw_ref=str(getattr(raw, 'raw_ref', '') or ''),
        metadata={
            'field': field,
            'source_title': str(getattr(raw, 'title', '') or ''),
            'table_quality_score': float(getattr(raw, 'table_quality_score', 0.0) or 0.0),
            'table_values': dict(getattr(raw, 'table_values', {}) or {}),
            'table_row_index': int(getattr(raw, 'table_row_index', 0) or 0),
        },
    )


def _field_for_item(item: NormalizedIntelItem) -> str:
    metadata = _metadata(item)
    field = str(metadata.get('field') or '')
    if field:
        return field
    raw_ref = str(item.raw_ref or '')
    parts = raw_ref.split(':')
    if len(parts) >= 6 and parts[0] == 'periodic_report':
        return parts[-2]
    return str(item.layer or '')


def _metadata(item: NormalizedIntelItem) -> dict[str, Any]:
    value = getattr(item, 'metadata', {})
    return value if isinstance(value, dict) else {}


def _select_top_evidence(items: list[NormalizedIntelItem], *, top_n: int) -> list[NormalizedIntelItem]:
    by_table: dict[str, list[NormalizedIntelItem]] = defaultdict(list)
    for item in items:
        by_table[_table_key(item)].append(item)
    selected: list[NormalizedIntelItem] = []
    for group in by_table.values():
        selected.extend(sorted(group, key=_rank_key)[:top_n])
    return sorted(selected, key=_rank_key)[:top_n]


def _is_summary_candidate(item: NormalizedIntelItem, field: str) -> bool:
    metadata = _metadata(item)
    page = int(metadata.get('page') or 0)
    if page > 0 or _raw_ref_page_ref(item).startswith('page'):
        return True
    section = _raw_ref_section(item)
    rendered = _clean_text(_render_item(item))
    if field == 'inventory':
        return section == 'inventory'
    if field == 'export':
        if _has_any(rendered, _EXPORT_NOISE_TERMS):
            return False
        return _has_any(rendered, _OVERSEAS_TERMS) and _has_any(rendered, _EXPORT_CONTEXT_TERMS)
    return True


def _raw_ref_section(item: NormalizedIntelItem) -> str:
    parts = str(item.raw_ref or '').split(':')
    if len(parts) >= 6 and parts[0] == 'periodic_report':
        return parts[2]
    return ''


def _raw_ref_page_ref(item: NormalizedIntelItem) -> str:
    parts = str(item.raw_ref or '').split(':')
    if len(parts) >= 6 and parts[0] == 'periodic_report':
        return parts[3]
    return ''


def _rank_key(item: NormalizedIntelItem) -> tuple[float, int, str]:
    score = _quality(item) + _structured_bonus(item) + _row_bonus(item)
    row_index = _metadata(item).get('table_row_index')
    try:
        index = int(row_index or 0)
    except (TypeError, ValueError):
        index = 0
    return (-score, index, str(item.id or item.raw_ref or ''))


def _structured_bonus(item: NormalizedIntelItem) -> float:
    metadata = _metadata(item)
    page = int(metadata.get('page') or 0)
    header = metadata.get('table_header')
    values = metadata.get('table_values')
    if page > 0 and isinstance(values, dict) and values:
        return 0.12
    if page > 0 and isinstance(header, list) and header:
        return 0.06
    return 0.0


def _row_bonus(item: NormalizedIntelItem) -> float:
    field = _field_for_item(item)
    metadata = _metadata(item)
    values = metadata.get('table_values') if isinstance(metadata.get('table_values'), dict) else {}
    rendered = _clean_text(' '.join([
        str(item.summary or ''),
        str(item.raw_ref or ''),
        ' '.join(str(value) for value in values.values()),
    ]))
    if field in {'customer_concentration', 'supplier_concentration'} and _has_any(rendered, _TOTAL_TERMS):
        return 0.10
    if field == 'business_segments' and {'revenue', 'cost', 'gross_margin'} <= set(values):
        return 0.08
    if field == 'export' and _has_any(rendered, _OVERSEAS_TERMS):
        return 0.08
    if field == 'inventory' and _has_any(rendered, _INVENTORY_TERMS):
        return 0.08
    return 0.0


def _quality(item: NormalizedIntelItem) -> float:
    metadata = _metadata(item)
    value = metadata.get('table_quality_score')
    try:
        quality = float(value)
    except (TypeError, ValueError):
        quality = 0.0
    if quality > 0:
        return max(0.0, min(1.0, quality))
    try:
        confidence = float(item.confidence or 0) / 10.0
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, confidence * 0.8))


def _average_quality(items: list[NormalizedIntelItem]) -> float:
    if not items:
        return 0.0
    return round(sum(_quality(item) for item in items) / len(items), 3)


def _direction_for_items(items: list[NormalizedIntelItem]) -> tuple[str, str]:
    positives = 0
    negatives = 0
    for item in items:
        rendered = _render_item(item)
        if _has_any(rendered, _POSITIVE_TERMS) or _numeric_direction(item) == 'positive':
            positives += 1
        if _has_any(rendered, _NEGATIVE_TERMS) or _numeric_direction(item) == 'negative':
            negatives += 1
    if positives and negatives:
        return 'mixed', 'mixed positive and negative evidence'
    if positives:
        return 'positive', ''
    if negatives:
        return 'negative', ''
    return 'unknown', ''


def _numeric_direction(item: NormalizedIntelItem) -> str:
    values = _metadata(item).get('table_values')
    if not isinstance(values, dict):
        return ''
    positive = False
    negative = False
    for key, value in values.items():
        key_text = str(key or '').lower()
        value_text = str(value or '')
        if not any(marker in key_text for marker in _CHANGE_KEYS):
            continue
        if _has_any(value_text, _POSITIVE_TERMS):
            positive = True
        if _has_any(value_text, _NEGATIVE_TERMS):
            negative = True
        match = re.search(r'[-+]?\d+(?:\.\d+)?', value_text.replace(',', ''))
        if not match:
            continue
        number = float(match.group(0))
        if number > 0:
            positive = True
        elif number < 0:
            negative = True
    if positive and negative:
        return 'mixed'
    if positive:
        return 'positive'
    if negative:
        return 'negative'
    return ''


def _summary_confidence(average_quality: float, evidence_count: int, direction: str) -> int:
    base = int(round(average_quality * 8))
    count_bonus = min(2, max(0, int(evidence_count) - 1))
    direction_bonus = 1 if direction in {'positive', 'negative', 'mixed'} else 0
    return max(3, min(10, base + count_bonus + direction_bonus))


def _summary_text(
    field: str,
    *,
    evidence_count: int,
    direction: str,
    average_quality: float,
    top: list[NormalizedIntelItem],
    conflict_reason: str,
) -> str:
    top_bits = []
    for item in top[:3]:
        label = _label_for_item(item)
        if label:
            top_bits.append(label)
    detail = '; '.join(top_bits)
    conflict = f' conflict={conflict_reason}.' if conflict_reason else ''
    return _clip_summary(
        f'{field}: {evidence_count} periodic report evidence rows; '
        f'direction={direction}; average_quality_score={average_quality:.3f}. '
        f'Top evidence: {detail or "none"}.{conflict}'
    )


def _label_for_item(item: NormalizedIntelItem) -> str:
    field = _field_for_item(item)
    values = _metadata(item).get('table_values')
    if isinstance(values, dict) and values:
        if field in {'business_segments', 'export'}:
            return _clip_label(values.get('segment_name') or '')
        if field in {'inventory', 'cost'}:
            return _clip_label(values.get('item_name') or '')
        if field in {'customer_concentration', 'supplier_concentration'}:
            share = _clean_text(values.get('share') or '')
            amount = _clean_text(values.get('amount') or '')
            if share and amount:
                return _clip_label(f'{amount} / {share}')
            return _clip_label(share or amount)
    return _clip_label(item.summary)


def _clip_label(value: Any, limit: int = 34) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + '...'


def _clip_summary(value: str, limit: int = 220) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)].rstrip() + '...'


def _top_keywords(items: list[NormalizedIntelItem]) -> list[str]:
    counts = Counter(
        str(keyword)
        for item in items
        for keyword in (item.matched_keywords or [])
        if str(keyword)
    )
    return [keyword for keyword, _count in counts.most_common(8)]


def _missing_fields(
    groups: dict[str, list[NormalizedIntelItem]],
    missing_reasons: dict[str, str],
) -> dict[str, str]:
    missing = {
        str(field): str(reason)
        for field, reason in (missing_reasons or {}).items()
        if str(field) and str(reason)
    }
    for field in FIELD_SUMMARY_FIELDS:
        if field not in groups and field not in missing:
            missing[field] = 'no_periodic_field_evidence'
    return missing


def _source_title(item: NormalizedIntelItem) -> str:
    metadata = _metadata(item)
    return _clean_text(metadata.get('source_title') or item.title or '')


def _source_period(item: NormalizedIntelItem) -> str:
    if item.published_at:
        return str(item.published_at)[:10]
    title = _source_title(item)
    match = re.search(r'20\d{2}', title)
    if match:
        return match.group(0)
    return ''


def _summary_id(field: str, item: NormalizedIntelItem, source_period: str) -> str:
    identity = f'{field}|{source_period}|{item.source_id}|{item.source_url or item.url}'
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    code = (item.related_codes or [''])[0]
    return f'periodic_field_summary:{_safe_id(code or "stock")}:{field}:{digest}'


def _table_key(item: NormalizedIntelItem) -> str:
    metadata = _metadata(item)
    header = metadata.get('table_header')
    header_text = '|'.join(str(value) for value in header) if isinstance(header, list) else ''
    return '|'.join([
        str(item.source_url or item.url or ''),
        str(_field_for_item(item)),
        str(metadata.get('page') or ''),
        header_text,
    ])


def _render_item(item: NormalizedIntelItem) -> str:
    metadata = _metadata(item)
    values = metadata.get('table_values') if isinstance(metadata.get('table_values'), dict) else {}
    return ' '.join(str(value or '') for value in (
        item.title,
        item.summary,
        item.layer,
        item.evidence_type,
        item.metric_name,
        item.raw_ref,
        ' '.join(str(value) for value in values.values()),
    ))


def _contains_forbidden(text: str) -> bool:
    upper = str(text or '').upper()
    return any(term in text or term.upper() in upper for term in _FORBIDDEN_TERMS)


def _has_any(text: str, terms: Iterable[str]) -> bool:
    value = str(text or '')
    lower = value.lower()
    return any(str(term) in value or str(term).lower() in lower for term in terms)


def _clean_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _safe_id(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r'[^0-9A-Za-z_.:-]+', '_', text)
    return text.strip('_')[:96] or 'item'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'FIELD_SUMMARY_FIELDS',
    'build_periodic_field_summary_items',
]
