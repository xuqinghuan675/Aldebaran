"""Adapt normalized intelligence items into EvidenceGraph primitives."""
from __future__ import annotations

import re
from dataclasses import fields
from typing import Any, Iterable

from core.evidence_graph import EvidenceEdge, EvidenceNode, LAYERS, NODE_TYPES
from core.intelligence.models import NormalizedIntelItem


_ITEM_FIELDS = {field.name for field in fields(NormalizedIntelItem)}
_FORBIDDEN_TERMS = (
    'K线',
    '均线',
    'MACD',
    'RSI',
    'KDJ',
    '支撑位',
    '压力位',
    '支撑',
    '压力',
    '买点',
    '止损',
    '触发价',
    '失效价',
    'technical-analysis',
)

_LAYER_TYPE_MAP = {
    'macro': 'macro',
    'policy': 'policy',
    'financial': 'financial_metric',
    'cost': 'cost_factor',
    'demand': 'demand_factor',
    'export': 'export_factor',
    'price': 'price_factor',
    'inventory': 'inventory_factor',
    'capacity': 'capacity_factor',
    'competition': 'competition_factor',
    'order_contract': 'order_contract',
    'customer_supplier': 'customer',
    'news_event': 'news',
    'trading_behavior': 'fund_flow',
    'liquidity': 'liquidity',
    'geopolitics_trade': 'geopolitics_trade',
    'shareholder': 'shareholder_action',
    'risk': 'risk',
    'forecast': 'forecast_hypothesis',
    'missing': 'missing_evidence',
}

_TRUST_MULTIPLIER = {
    'official': 1.00,
    'primary': 0.90,
    'secondary': 0.75,
    'media': 0.62,
    'unknown': 0.45,
}


def evidence_items_to_nodes_edges(
    items: Iterable[NormalizedIntelItem | dict[str, Any]],
    *,
    subject_code: str,
    subject_name: str,
) -> tuple[list[EvidenceNode], list[EvidenceEdge]]:
    stock_id = f'stock:{_clean_code(subject_code) or _clean_text(subject_name)}'
    nodes: list[EvidenceNode] = []
    edges: list[EvidenceEdge] = []
    seen: set[str] = set()
    edge_seen: set[tuple[str, str, str]] = set()
    coerced: list[NormalizedIntelItem] = []

    for raw in items or []:
        item = _coerce_item(raw)
        if item is None:
            continue
        if _contains_forbidden(item.title, item.summary, item.evidence_type, item.metric_name):
            continue
        coerced.append(item)

    node_id_by_item_id = {
        str(item.id): _node_id(item)
        for item in coerced
        if str(item.id)
    }
    summary_links: list[tuple[NormalizedIntelItem, str, str, int, float, str]] = []

    for item in coerced:
        layer = _valid_layer(item.layer)
        node_type = _node_type_for_item(item, layer)
        node_id = _node_id(item)
        if node_id in seen:
            continue
        seen.add(node_id)
        analysis_weight = _analysis_weight(item)
        display_weight = _display_weight(item, analysis_weight)
        confidence = _clamp_int(item.confidence, 0, 10)
        summary = _clean_text(item.summary or item.title)
        source_url = _clean_text(item.url or item.source_url)

        nodes.append(EvidenceNode(
            id=node_id,
            label=_clean_text(item.title)[:44] or node_type,
            type=node_type,
            layer=layer,
            direction=_direction(item.direction),
            freshness=_freshness(item.freshness),
            display_weight=display_weight,
            analysis_weight=analysis_weight,
            confidence=confidence,
            source=_clean_text(item.source_id),
            source_url=source_url,
            published_at=item.published_at,
            summary=summary[:260],
            raw_ref=_clean_text(item.raw_ref),
            metrics=_metrics(item),
            time_windows=list(item.time_windows or []),
            expectation_gap=_expectation_gap(item.expectation_gap),
        ))
        edge = EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation=_relation_for_item(item, layer),
            layer=layer,
            direction=_direction(item.direction),
            display_weight=max(1.2, min(3.5, display_weight / 4.0)),
            analysis_weight=analysis_weight,
            confidence=confidence,
            summary=summary[:180],
        )
        edges.append(edge)
        edge_seen.add((edge.source, edge.target, edge.relation))
        if item.evidence_type == 'periodic_field_summary':
            summary_links.append((item, node_id, layer, confidence, analysis_weight, summary))

    for item, summary_node_id, layer, confidence, analysis_weight, summary in summary_links:
        for source_item_id in _summary_top_evidence_ids(item):
            source_node_id = node_id_by_item_id.get(source_item_id)
            if not source_node_id or source_node_id not in seen:
                continue
            edge_key = (source_node_id, summary_node_id, 'supports')
            if edge_key in edge_seen:
                continue
            edge_seen.add(edge_key)
            edges.append(EvidenceEdge(
                source=source_node_id,
                target=summary_node_id,
                relation='supports',
                layer=layer,
                direction='neutral',
                display_weight=1.4,
                analysis_weight=max(0.12, min(analysis_weight, 0.65)),
                confidence=confidence,
                summary=f'Raw periodic report evidence supports summary: {summary[:120]}',
            ))
    return nodes, edges


def _coerce_item(raw: NormalizedIntelItem | dict[str, Any]) -> NormalizedIntelItem | None:
    if isinstance(raw, NormalizedIntelItem):
        return raw
    if not isinstance(raw, dict):
        return None
    payload = {key: value for key, value in raw.items() if key in _ITEM_FIELDS}
    try:
        return NormalizedIntelItem(**payload)
    except TypeError:
        return None


def _node_type_for_item(item: NormalizedIntelItem, layer: str) -> str:
    evidence_type = _clean_text(item.evidence_type)
    if evidence_type == 'periodic_field_summary':
        return 'periodic_field_summary'
    if layer in _LAYER_TYPE_MAP and evidence_type in {'', 'event', 'news'}:
        return _LAYER_TYPE_MAP[layer]
    if layer in _LAYER_TYPE_MAP and layer not in {'news_event', 'trading_behavior'}:
        return _LAYER_TYPE_MAP[layer]
    if evidence_type in NODE_TYPES:
        return evidence_type
    return _LAYER_TYPE_MAP.get(layer, 'event')


def _relation_for_item(item: NormalizedIntelItem, layer: str) -> str:
    if item.evidence_type == 'periodic_field_summary':
        return 'impacts'
    if layer == 'trading_behavior':
        return 'verifies'
    direction = _direction(item.direction)
    if direction in {'bullish', 'positive'}:
        return 'supports'
    if direction in {'bearish', 'negative'}:
        return 'pressures'
    return 'impacts'


def _analysis_weight(item: NormalizedIntelItem) -> float:
    if item.source_status and item.source_status != 'ok':
        return 0.0
    confidence = _clamp_int(item.confidence, 0, 10) / 10.0
    trust = _TRUST_MULTIPLIER.get(str(item.trust_level or 'unknown'), 0.45)
    fresh = {'fresh': 1.0, 'unknown': 0.82, 'stale': 0.55}.get(str(item.freshness or 'unknown'), 0.82)
    return round(max(0.12, min(0.95, confidence * trust * fresh)), 2)


def _display_weight(item: NormalizedIntelItem, analysis_weight: float) -> float:
    confidence = _clamp_int(item.confidence, 0, 10)
    return round(max(5.0, min(15.0, 5.0 + confidence * 0.8 + analysis_weight * 3.0)), 2)


def _metrics(item: NormalizedIntelItem) -> dict[str, Any]:
    out: dict[str, Any] = {}
    metric_fields = (
        'metric_name',
        'current_value',
        'unit',
        'change_1d',
        'change_3d',
        'change_7d',
        'change_30d',
        'change_qoq',
        'change_yoy',
        'acceleration',
    )
    for key in metric_fields:
        value = getattr(item, key)
        if value in (None, '', 'unknown'):
            continue
        out[key] = value
    metadata = getattr(item, 'metadata', {})
    if item.evidence_type == 'periodic_field_summary' and isinstance(metadata, dict):
        out['field_summary'] = dict(metadata)
    return out


def _node_id(item: NormalizedIntelItem) -> str:
    base = item.id or f'{item.source_id}:{item.title}:{item.published_at or ""}'
    return f'intel:{_safe_id(item.source_id)}:{_safe_id(base)}'


def _valid_layer(value: str) -> str:
    text = _clean_text(value)
    return text if text in LAYERS else 'news_event'


def _direction(value: str) -> str:
    text = str(value or 'neutral')
    return text if text in {'bullish', 'bearish', 'positive', 'negative', 'neutral', 'mixed', 'unknown'} else 'neutral'


def _freshness(value: str) -> str:
    text = str(value or 'unknown')
    return text if text in {'fresh', 'stale', 'unknown'} else 'unknown'


def _expectation_gap(value: str) -> str:
    text = _clean_text(value or 'unknown')
    return text or 'unknown'


def _contains_forbidden(*texts: str) -> bool:
    merged = ' '.join(str(text or '') for text in texts)
    upper = merged.upper()
    return any(term in merged or term.upper() in upper for term in _FORBIDDEN_TERMS)


def _clean_text(value: Any) -> str:
    text = str(value or '').strip()
    return re.sub(r'\s+', ' ', text)


def _clean_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _safe_id(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r'[^0-9A-Za-z_.:-]+', '_', text)
    return text.strip('_')[:96] or 'item'


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _summary_top_evidence_ids(item: NormalizedIntelItem) -> list[str]:
    metadata = getattr(item, 'metadata', {})
    if not isinstance(metadata, dict):
        return []
    values = metadata.get('top_evidence_ids')
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value) for value in values if str(value)]
