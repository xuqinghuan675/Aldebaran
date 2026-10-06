"""EvidenceGraph data contract for the intelligence page.

The graph is evidence-first: display weight controls visual emphasis, while
analysis weight controls how much downstream reasoning may trust the item.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


NODE_TYPES = {
    'stock',
    'sector',
    'macro',
    'liquidity',
    'geopolitics_trade',
    'policy',
    'financial_metric',
    'cost_factor',
    'demand_factor',
    'export_factor',
    'price_factor',
    'inventory_factor',
    'capacity_factor',
    'competition_factor',
    'order_contract',
    'customer',
    'supplier',
    'event',
    'news',
    'fund_flow',
    'hot_money_seat',
    'institutional_behavior',
    'margin_financing',
    'block_trade',
    'shareholder_action',
    'event_calendar',
    'forecast_hypothesis',
    'risk',
    'periodic_field_summary',
    'missing_evidence',
}

RELATION_TYPES = {
    'belongs_to',
    'impacts',
    'supports',
    'pressures',
    'verifies',
    'contradicts',
    'depends_on',
    'revenue_from',
    'cost_from',
    'customer_of',
    'supplier_of',
    'leads_to',
    'derived_from',
    'lags',
    'amplifies',
    'weakens',
    'blocks',
    'confirms',
    'missing',
}

LAYERS = {
    'macro',
    'sector',
    'company',
    'financial',
    'cost',
    'demand',
    'policy',
    'export',
    'customer_supplier',
    'news_event',
    'trading_behavior',
    'liquidity',
    'geopolitics_trade',
    'price',
    'inventory',
    'capacity',
    'competition',
    'order_contract',
    'shareholder',
    'risk',
    'expectation',
    'forecast',
    'missing',
}

TYPE_COLORS = {
    'stock': '#f7fbff',
    'sector': '#27c9ff',
    'macro': '#2368d8',
    'liquidity': '#43f57b',
    'geopolitics_trade': '#7d6cff',
    'policy': '#b46cff',
    'financial_metric': '#ffd166',
    'cost_factor': '#ff9f43',
    'demand_factor': '#20e3a2',
    'export_factor': '#7d6cff',
    'price_factor': '#ffcf70',
    'inventory_factor': '#5fd3ff',
    'capacity_factor': '#9dd5ff',
    'competition_factor': '#a7b7d8',
    'order_contract': '#ffe082',
    'customer': '#5ce1e6',
    'supplier': '#5ce1e6',
    'event': '#ff5fa2',
    'news': '#ff5fa2',
    'fund_flow': '#43f57b',
    'hot_money_seat': '#00ffc8',
    'institutional_behavior': '#ffe6a3',
    'margin_financing': '#62d6ff',
    'block_trade': '#6ef0c8',
    'shareholder_action': '#9ac7ff',
    'event_calendar': '#93a4c8',
    'forecast_hypothesis': '#dffcff',
    'risk': '#ff4d5e',
    'periodic_field_summary': '#ffd166',
    'missing_evidence': '#657694',
}

LAYER_COLORS = {
    'macro': '#2368d8',
    'sector': '#27c9ff',
    'company': '#f7fbff',
    'financial': '#ffd166',
    'cost': '#ff9f43',
    'demand': '#20e3a2',
    'policy': '#b46cff',
    'export': '#7d6cff',
    'customer_supplier': '#5ce1e6',
    'news_event': '#ff5fa2',
    'trading_behavior': '#43f57b',
    'liquidity': '#43f57b',
    'geopolitics_trade': '#7d6cff',
    'price': '#ffcf70',
    'inventory': '#5fd3ff',
    'capacity': '#9dd5ff',
    'competition': '#a7b7d8',
    'order_contract': '#ffe082',
    'shareholder': '#9ac7ff',
    'risk': '#ff4d5e',
    'expectation': '#dffcff',
    'forecast': '#dffcff',
    'missing': '#657694',
}

DIRECTION_COLORS = {
    'bullish': '#43f57b',
    'bearish': '#ff4d5e',
    'positive': '#43f57b',
    'negative': '#ff4d5e',
    'neutral': '#93a4c8',
    'mixed': '#ffd166',
    'unknown': '#657694',
}


@dataclass
class EvidenceNode:
    id: str
    label: str
    type: str
    layer: str
    direction: str = 'neutral'
    freshness: str = 'unknown'
    display_weight: float = 1.0
    analysis_weight: float = 0.0
    confidence: int = 0
    source: str = ''
    source_url: str = ''
    published_at: str | None = None
    summary: str = ''
    raw_ref: str = ''
    metrics: dict[str, Any] = field(default_factory=dict)
    time_windows: list[str] = field(default_factory=list)
    expectation_gap: str = 'unknown'
    forecast_role: str = 'fact'
    color: str = ''

    def to_graph_node(self) -> dict[str, Any]:
        color = self.color or TYPE_COLORS.get(self.type) or LAYER_COLORS.get(self.layer) or '#93a4c8'
        return {
            'id': self.id,
            'name': self.label,
            'label': self.label,
            'type': self.type,
            'layer': self.layer,
            'val': max(1.0, float(self.display_weight or 1.0)),
            'color': color,
            'direction': self.direction,
            'freshness': self.freshness,
            'display_weight': float(self.display_weight or 0.0),
            'analysis_weight': float(self.analysis_weight or 0.0),
            'confidence': int(self.confidence or 0),
            'summary': self.summary,
            'source': self.source,
            'source_url': self.source_url,
            'published_at': self.published_at,
            'metrics': dict(self.metrics or {}),
            'time_windows': list(self.time_windows or []),
            'expectation_gap': self.expectation_gap,
            'forecast_role': self.forecast_role,
        }


@dataclass
class EvidenceEdge:
    source: str
    target: str
    relation: str
    layer: str
    direction: str = 'neutral'
    display_weight: float = 1.0
    analysis_weight: float = 0.0
    confidence: int = 0
    summary: str = ''
    lag: str = ''
    mechanism: str = ''
    color: str = ''

    def to_graph_link(self) -> dict[str, Any]:
        color = self.color or DIRECTION_COLORS.get(self.direction) or LAYER_COLORS.get(self.layer) or '#2d4675'
        return {
            'source': self.source,
            'target': self.target,
            'relation': self.relation,
            'layer': self.layer,
            'direction': self.direction,
            'value': max(0.5, float(self.display_weight or 1.0)),
            'color': color,
            'display_weight': float(self.display_weight or 0.0),
            'analysis_weight': float(self.analysis_weight or 0.0),
            'confidence': int(self.confidence or 0),
            'summary': self.summary,
            'lag': self.lag,
            'mechanism': self.mechanism,
        }


@dataclass
class EvidencePack:
    subject_code: str
    subject_name: str
    subject_type: str
    created_at: str
    nodes: list[EvidenceNode]
    edges: list[EvidenceEdge]
    graph_json: dict[str, Any]
    layer_scores: dict[str, dict[str, Any]]
    missing_layers: list[str]
    dragging_layers: list[str]
    contradiction_report: list[str]
    ai_context_text: str

    @classmethod
    def from_nodes(
        cls,
        *,
        subject_code: str,
        subject_name: str,
        subject_type: str,
        nodes: list[EvidenceNode],
        edges: list[EvidenceEdge],
        created_at: str | None = None,
        contradiction_report: list[str] | None = None,
    ) -> 'EvidencePack':
        created = created_at or datetime.now().isoformat(timespec='seconds')
        graph_json = build_graph_json(
            subject_code=subject_code,
            subject_name=subject_name,
            subject_type=subject_type,
            nodes=nodes,
            edges=edges,
            created_at=created,
        )
        layer_scores = build_layer_scores(nodes)
        missing_layers = sorted({
            node.layer for node in nodes
            if node.type == 'missing_evidence' or node.forecast_role == 'missing'
        })
        dragging_layers = [
            layer for layer, score in layer_scores.items()
            if score.get('missing') or score.get('direction') in {'bearish', 'unknown'}
        ]
        return cls(
            subject_code=subject_code,
            subject_name=subject_name,
            subject_type=subject_type,
            created_at=created,
            nodes=nodes,
            edges=edges,
            graph_json=graph_json,
            layer_scores=layer_scores,
            missing_layers=missing_layers,
            dragging_layers=dragging_layers,
            contradiction_report=list(contradiction_report or []),
            ai_context_text=build_ai_context_text(subject_code, subject_name, nodes),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['nodes'] = [asdict(node) for node in self.nodes]
        data['edges'] = [asdict(edge) for edge in self.edges]
        return data


def build_graph_json(
    *,
    subject_code: str,
    subject_name: str,
    subject_type: str,
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    created_at: str,
) -> dict[str, Any]:
    return {
        'nodes': [node.to_graph_node() for node in nodes],
        'links': [edge.to_graph_link() for edge in edges],
        'meta': {
            'subject_code': subject_code,
            'subject_name': subject_name,
            'subject_type': subject_type,
            'created_at': created_at,
            'node_count': len(nodes),
            'link_count': len(edges),
        },
    }


def build_layer_scores(nodes: list[EvidenceNode]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[EvidenceNode]] = {}
    for node in nodes:
        grouped.setdefault(node.layer, []).append(node)

    scores: dict[str, dict[str, Any]] = {}
    for layer, layer_nodes in grouped.items():
        evidence_nodes = [n for n in layer_nodes if n.type != 'missing_evidence']
        directions = {n.direction for n in evidence_nodes if n.direction}
        if not evidence_nodes:
            direction = 'unknown'
        elif 'bullish' in directions and 'bearish' in directions:
            direction = 'mixed'
        elif 'bearish' in directions:
            direction = 'bearish'
        elif 'bullish' in directions:
            direction = 'bullish'
        else:
            direction = 'neutral'

        confidence = 0
        if evidence_nodes:
            confidence = min(10, int(round(sum(n.analysis_weight for n in evidence_nodes) * 3)))

        missing = any(n.type == 'missing_evidence' or n.forecast_role == 'missing' for n in layer_nodes)
        summaries = [n.summary or n.label for n in evidence_nodes[:3]]
        scores[layer] = {
            'direction': direction,
            'confidence': confidence,
            'freshness': _freshness(layer_nodes),
            'evidence_count': len(evidence_nodes),
            'missing': bool(missing),
            'dominant_variables': [n.label for n in sorted(
                evidence_nodes,
                key=lambda item: item.analysis_weight,
                reverse=True,
            )[:3]],
            'expectation_gap': _dominant_expectation_gap(evidence_nodes),
            'time_window': _dominant_window(evidence_nodes),
            'summary': '；'.join(s for s in summaries if s)[:280],
            'drag_reason': '证据缺失' if missing and not evidence_nodes else '',
        }
    return scores


def build_ai_context_text(subject_code: str, subject_name: str, nodes: list[EvidenceNode]) -> str:
    lines = [f'分析对象：{subject_code} {subject_name}']
    for node in sorted(nodes, key=lambda n: (n.layer, -n.analysis_weight, -n.display_weight)):
        role = '缺失' if node.type == 'missing_evidence' else '证据'
        summary = node.summary or node.label
        lines.append(
            f'- [{node.layer}/{role}] {node.label} | 方向={node.direction} '
            f'| 可信={node.analysis_weight:.2f} | {summary}'
        )
    return '\n'.join(lines)


def _freshness(nodes: list[EvidenceNode]) -> str:
    values = {n.freshness for n in nodes}
    if 'fresh' in values:
        return 'fresh'
    if 'stale' in values:
        return 'stale'
    return 'unknown'


def _dominant_expectation_gap(nodes: list[EvidenceNode]) -> str:
    for node in nodes:
        if node.expectation_gap and node.expectation_gap != 'unknown':
            return node.expectation_gap
    return 'unknown'


def _dominant_window(nodes: list[EvidenceNode]) -> str:
    windows: list[str] = []
    for node in nodes:
        windows.extend(node.time_windows or [])
    if not windows:
        return 'mixed'
    for candidate in ('immediate', 'short', 'swing', 'mid', 'long'):
        if candidate in windows:
            return candidate
    return 'mixed'
