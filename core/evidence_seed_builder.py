"""Build EvidencePack objects from existing Aldebaran stock data."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from core.evidence_graph import EvidenceEdge, EvidenceNode, EvidencePack
from core.intelligence.evidence_adapter import evidence_items_to_nodes_edges


FORBIDDEN_INTELLIGENCE_TERMS = (
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
    '失效价',
    '触发价',
    '技术突破',
    '趋势线',
)

_DEFAULT_WINDOWS = ['immediate', 'short', 'swing', 'mid', 'long']
_MISSING_LAYER_SPECS = {
    'cost': {'layer': 'cost', 'label': '成本证据缺失', 'types': {'cost_factor'}},
    'demand': {'layer': 'demand', 'label': '需求证据缺失', 'types': {'demand_factor'}},
    'export': {'layer': 'export', 'label': '出口证据缺失', 'types': {'export_factor'}},
    'customer': {'layer': 'customer_supplier', 'label': '客户证据缺失', 'types': {'customer'}},
    'supplier': {'layer': 'customer_supplier', 'label': '供应链证据缺失', 'types': {'supplier'}},
    'inventory': {'layer': 'inventory', 'label': '库存证据缺失', 'types': {'inventory_factor'}},
    'capacity': {'layer': 'capacity', 'label': '产能证据缺失', 'types': {'capacity_factor'}},
    'competition': {'layer': 'competition', 'label': '竞争格局证据缺失', 'types': {'competition_factor'}},
    'order_contract': {'layer': 'order_contract', 'label': '订单/合同/中标证据缺失', 'types': {'order_contract'}},
    'margin_financing': {'layer': 'trading_behavior', 'label': '融资融券证据缺失', 'types': {'margin_financing'}},
    'event_calendar': {'layer': 'risk', 'label': '事件日历证据缺失', 'types': {'event_calendar'}},
}
_MISSING_LAYERS = {
    'cost': '成本证据缺失',
    'demand': '需求证据缺失',
    'export': '出口证据缺失',
    'customer': '客户证据缺失',
    'supplier': '供应链证据缺失',
}


def build_evidence_pack_from_seed(seed: dict[str, Any]) -> EvidencePack:
    subject = dict(seed.get('subject') or {})
    code = _clean_code(subject.get('code') or seed.get('code') or '')
    name = _clean_text(subject.get('name') or seed.get('name') or code or '未命名股票')
    subject_type = str(subject.get('type') or 'stock')
    stock_id = f'stock:{code or name}'

    nodes: list[EvidenceNode] = []
    edges: list[EvidenceEdge] = []
    seen: set[str] = set()

    def add_node(node: EvidenceNode) -> None:
        if node.id in seen:
            return
        seen.add(node.id)
        nodes.append(node)

    def add_edge(edge: EvidenceEdge) -> None:
        if edge.source in seen and edge.target in seen:
            edges.append(edge)

    price = subject.get('price')
    summary = '当前个股情报网络中心'
    if price not in (None, ''):
        summary = f'当前个股情报网络中心，最新价 {price}'
    add_node(EvidenceNode(
        id=stock_id,
        label=name,
        type='stock',
        layer='company',
        direction='neutral',
        freshness='fresh',
        display_weight=26,
        analysis_weight=1.0,
        confidence=10,
        summary=summary,
        time_windows=list(_DEFAULT_WINDOWS),
        color='#f7fbff',
    ))

    normalized_items = list(seed.get('normalized_intel_items') or [])
    _add_sector_nodes(seed, stock_id, add_node, add_edge)
    if normalized_items:
        _add_normalized_intel_nodes(normalized_items, code or name, name, add_node, add_edge)
    else:
        _add_intel_event_nodes(seed, stock_id, add_node, add_edge)
        _add_stock_news_nodes(seed, stock_id, add_node, add_edge)
        _add_context_nodes(seed, stock_id, add_node, add_edge)
        _add_variable_nodes(seed, stock_id, add_node, add_edge)
    _add_missing_nodes(code or name, stock_id, nodes, add_node, add_edge)

    return EvidencePack.from_nodes(
        subject_code=code,
        subject_name=name,
        subject_type=subject_type,
        nodes=nodes,
        edges=edges,
    )


def _add_normalized_intel_nodes(items, subject_code, subject_name, add_node, add_edge) -> None:
    nodes, edges = evidence_items_to_nodes_edges(
        items,
        subject_code=subject_code,
        subject_name=subject_name,
    )
    for node in nodes:
        add_node(node)
    for edge in edges:
        add_edge(edge)


def _add_sector_nodes(seed, stock_id, add_node, add_edge) -> None:
    sectors = [str(s).strip() for s in (seed.get('sectors') or []) if str(s).strip()]
    for idx, sector in enumerate(sectors[:10]):
        safe = _safe_id(sector)
        node_id = f'sector:{safe}'
        add_node(EvidenceNode(
            id=node_id,
            label=_clean_text(sector),
            type='sector',
            layer='sector',
            direction='neutral',
            freshness='unknown',
            display_weight=max(8, 14 - idx),
            analysis_weight=0.35,
            confidence=4,
            summary='作为个股图中的关联板块节点，不作为 v1 主分析对象。',
            time_windows=['short', 'swing', 'mid'],
            color='#27c9ff',
        ))
        add_edge(EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation='belongs_to',
            layer='sector',
            display_weight=2.4,
            analysis_weight=0.35,
            summary='个股与关联板块关系',
            color='#27c9ff',
        ))


def _add_intel_event_nodes(seed, stock_id, add_node, add_edge) -> None:
    for idx, event in enumerate(seed.get('intel_events') or []):
        data = event.to_dict() if hasattr(event, 'to_dict') else dict(event or {})
        title = _clean_text(data.get('title') or '')
        summary = _clean_text(data.get('summary') or data.get('interpretation') or title)
        if not title or _contains_forbidden(title, summary):
            continue
        category = str(data.get('category') or 'event')
        node_type, layer = _event_type_layer(category)
        direction = _direction(data.get('direction'))
        level = str(data.get('level') or 'info')
        display_weight = {'critical': 14, 'important': 11, 'info': 8}.get(level, 8)
        analysis_weight = {'critical': 0.85, 'important': 0.65, 'info': 0.42}.get(level, 0.4)
        node_id = f'{node_type}:{_safe_id(data.get("id") or title)}'
        add_node(EvidenceNode(
            id=node_id,
            label=title[:42],
            type=node_type,
            layer=layer,
            direction=direction,
            freshness='fresh',
            display_weight=display_weight,
            analysis_weight=analysis_weight,
            confidence=max(3, min(9, int(round(analysis_weight * 10)))),
            source=str(data.get('source') or 'intel_feed'),
            published_at=str(data.get('timestamp') or ''),
            summary=summary[:220],
            raw_ref=str(data.get('id') or ''),
            time_windows=['immediate', 'short', 'swing'],
        ))
        add_edge(EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation='impacts',
            layer=layer,
            direction=direction,
            display_weight=2.8,
            analysis_weight=analysis_weight,
            confidence=max(3, min(9, int(round(analysis_weight * 10)))),
            summary=summary[:160],
        ))
        if idx >= 14:
            break


def _add_stock_news_nodes(seed, stock_id, add_node, add_edge) -> None:
    for idx, item in enumerate(seed.get('stock_news') or []):
        data = dict(item or {})
        title = _clean_text(data.get('title') or '')
        content = _clean_text(data.get('content') or '')
        if not title or _contains_forbidden(title, content):
            continue
        node_type, layer = _text_type_layer(title, content)
        is_variable_evidence = layer != 'news_event'
        node_id = f'news:{_safe_id(data.get("url") or title)}'
        summary = content[:180] if content else title
        add_node(EvidenceNode(
            id=node_id,
            label=title[:44],
            type=node_type,
            layer=layer,
            direction='neutral',
            freshness='fresh' if idx < 5 else 'stale',
            display_weight=max(5.0, 10.0 - idx * 0.5),
            analysis_weight=0.50 if is_variable_evidence else max(0.22, 0.45 - idx * 0.02),
            confidence=4,
            source=str(data.get('source') or 'stock_news_provider'),
            source_url=str(data.get('url') or ''),
            published_at=str(data.get('date') or ''),
            summary=summary,
            time_windows=['short', 'swing', 'mid'] if is_variable_evidence else ['immediate', 'short'],
        ))
        add_edge(EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation='impacts' if is_variable_evidence else 'supports',
            layer=layer,
            display_weight=1.8,
            analysis_weight=0.45 if is_variable_evidence else 0.3,
            confidence=4,
            summary='个股新闻作为事件证据进入情报图',
        ))
        if idx >= 11:
            break


def _add_context_nodes(seed, stock_id, add_node, add_edge) -> None:
    context = dict(seed.get('context') or {})
    for key in ('fundamental_summary', 'flow_profile', 'money_flow_summary', 'realtime',
                'public_fund_evidence', 'restricted_release', 'hot_rank'):
        if key in seed and key not in context:
            context[key] = seed.get(key)

    market_context = seed.get('market_context')
    if isinstance(market_context, dict):
        context.setdefault('emotion', market_context.get('emotion'))
        context.setdefault('global', market_context.get('global'))
        context.setdefault('market_phase', market_context.get('market_phase'))

    _add_financial_node(context, stock_id, add_node, add_edge)
    _add_flow_nodes(context, stock_id, add_node, add_edge)
    _add_market_context_nodes(context, stock_id, add_node, add_edge)
    _add_company_risk_nodes(context, stock_id, add_node, add_edge)


def _add_financial_node(context, stock_id, add_node, add_edge) -> None:
    summary = _clean_text(context.get('fundamental_summary') or '')
    if not summary or _contains_forbidden(summary):
        return
    node_id = f'financial:{stock_id}'
    add_node(EvidenceNode(
        id=node_id,
        label='财报经营摘要',
        type='financial_metric',
        layer='financial',
        direction='neutral',
        freshness='unknown',
        display_weight=11,
        analysis_weight=0.55,
        confidence=5,
        summary=summary[:260],
        time_windows=['swing', 'mid', 'long'],
    ))
    add_edge(EvidenceEdge(
        source=node_id,
        target=stock_id,
        relation='impacts',
        layer='financial',
        display_weight=2.4,
        analysis_weight=0.55,
        confidence=5,
        summary='财务经营摘要影响公司层判断',
    ))


def _add_flow_nodes(context, stock_id, add_node, add_edge) -> None:
    flow = context.get('flow_profile') if isinstance(context.get('flow_profile'), dict) else {}
    money_summary = _clean_text(context.get('money_flow_summary') or '')
    realtime = context.get('realtime') if isinstance(context.get('realtime'), dict) else {}
    if flow or money_summary or realtime.get('main_flow') is not None:
        available = bool(flow.get('available', True))
        days = int(flow.get('days') or 0)
        main_5d = _num(flow.get('main_5d'))
        direction = 'bullish' if main_5d and main_5d > 0 else 'bearish' if main_5d and main_5d < 0 else 'neutral'
        summary = money_summary or _format_flow_summary(flow, realtime)
        add_node(EvidenceNode(
            id=f'fund_flow:{stock_id}',
            label='资金流向',
            type='fund_flow',
            layer='trading_behavior',
            direction=direction,
            freshness='fresh',
            display_weight=15,
            analysis_weight=0.68 if available and days >= 3 else 0.36,
            confidence=7 if available and days >= 3 else 4,
            summary=summary[:240] if summary else '资金流数据已进入交易行为情报层。',
            metrics={
                'days': days,
                'main_3d': flow.get('main_3d'),
                'main_5d': flow.get('main_5d'),
                'main_10d': flow.get('main_10d'),
                'main_streak': flow.get('main_streak'),
                'divergence': flow.get('divergence'),
            },
            time_windows=['immediate', 'short'],
            color='#43f57b',
        ))
        add_edge(EvidenceEdge(
            source=f'fund_flow:{stock_id}',
            target=stock_id,
            relation='verifies',
            layer='trading_behavior',
            direction=direction,
            display_weight=3.5,
            analysis_weight=0.5,
            confidence=6,
            summary='资金流作为交易行为情报，不作为走势指标。',
            color='#43f57b',
        ))

    public_evidence = context.get('public_fund_evidence')
    if isinstance(public_evidence, dict) and public_evidence.get('available'):
        lhb = public_evidence.get('lhb') if isinstance(public_evidence.get('lhb'), dict) else {}
        hsgt = public_evidence.get('hsgt') if isinstance(public_evidence.get('hsgt'), dict) else {}
        dzjy = public_evidence.get('dzjy') if isinstance(public_evidence.get('dzjy'), dict) else {}
        if _public_payload_hit(lhb):
            _add_trading_node(
                stock_id, add_node, add_edge,
                node_id=f'hot_money:{stock_id}',
                label='游资/龙虎榜',
                node_type='hot_money_seat',
                summary=_clean_text(lhb.get('summary') or str(lhb))[:220],
                color='#00ffc8',
                direction=_trade_direction('hot_money_seat', lhb),
                metrics=_trade_metrics(lhb),
            )
        if _public_payload_hit(hsgt):
            _add_trading_node(
                stock_id, add_node, add_edge,
                node_id=f'institution:{stock_id}',
                label='机构/北向行为',
                node_type='institutional_behavior',
                summary=_clean_text(hsgt.get('summary') or str(hsgt))[:220],
                color='#ffe6a3',
                direction=_trade_direction('institutional_behavior', hsgt),
                metrics=_trade_metrics(hsgt),
            )
        if _public_payload_hit(dzjy):
            _add_trading_node(
                stock_id, add_node, add_edge,
                node_id=f'block_trade:{stock_id}',
                label='大宗交易',
                node_type='block_trade',
                summary=_block_trade_summary(dzjy),
                color='#6ef0c8',
                direction=_trade_direction('block_trade', dzjy),
                metrics=_trade_metrics(dzjy),
            )

    hot_rank = context.get('hot_rank')
    if isinstance(hot_rank, dict) and hot_rank.get('rank'):
        _add_trading_node(
            stock_id, add_node, add_edge,
            node_id=f'hot_rank:{stock_id}',
            label='市场热度排名',
            node_type='fund_flow',
            summary=f"市场热度排名 {hot_rank.get('rank')}，变化 {hot_rank.get('change') or '未知'}。",
            color='#43f57b',
            analysis_weight=0.25,
        )


def _add_trading_node(
    stock_id,
    add_node,
    add_edge,
    *,
    node_id: str,
    label: str,
    node_type: str,
    summary: str,
    color: str,
    analysis_weight: float = 0.5,
    direction: str = 'neutral',
    metrics: dict[str, Any] | None = None,
) -> None:
    if _contains_forbidden(summary):
        return
    add_node(EvidenceNode(
        id=node_id,
        label=label,
        type=node_type,
        layer='trading_behavior',
        direction=direction,
        freshness='fresh',
        display_weight=12,
        analysis_weight=analysis_weight,
        confidence=5,
        summary=summary or label,
        metrics=dict(metrics or {}),
        time_windows=['immediate', 'short'],
        color=color,
    ))
    add_edge(EvidenceEdge(
        source=node_id,
        target=stock_id,
        relation='verifies',
        layer='trading_behavior',
        direction=direction,
        display_weight=2.8,
        analysis_weight=analysis_weight,
        confidence=5,
        summary=f'{label}作为交易行为情报入图。',
        color=color,
    ))


def _add_market_context_nodes(context, stock_id, add_node, add_edge) -> None:
    emotion = context.get('emotion') if isinstance(context.get('emotion'), dict) else {}
    global_snap = context.get('global') if isinstance(context.get('global'), dict) else {}
    market_phase = _clean_text(context.get('market_phase') or '')
    if emotion or global_snap or market_phase:
        parts = []
        if emotion:
            if emotion.get('zt') is not None:
                parts.append(f"涨停 {emotion.get('zt')}")
            if emotion.get('dt') is not None:
                parts.append(f"跌停 {emotion.get('dt')}")
            if emotion.get('up') is not None and emotion.get('down') is not None:
                parts.append(f"上涨 {emotion.get('up')} / 下跌 {emotion.get('down')}")
        if global_snap:
            parts.append(_clean_text(global_snap.get('summary') or str(global_snap))[:90])
        if market_phase:
            parts.append(f'市场阶段 {market_phase}')
        summary = '；'.join(p for p in parts if p)
        if not _contains_forbidden(summary):
            add_node(EvidenceNode(
                id=f'macro:{stock_id}',
                label='外部环境',
                type='macro',
                layer='macro',
                direction='neutral',
                freshness='fresh',
                display_weight=10,
                analysis_weight=0.38,
                confidence=4,
                summary=summary or '外部环境数据已进入情报图。',
                time_windows=['immediate', 'short', 'swing'],
                color='#2368d8',
            ))
            add_edge(EvidenceEdge(
                source=f'macro:{stock_id}',
                target=stock_id,
                relation='impacts',
                layer='macro',
                display_weight=1.8,
                analysis_weight=0.35,
                confidence=4,
                summary='外部环境对个股情报阶段形成约束。',
                color='#2368d8',
            ))


def _add_company_risk_nodes(context, stock_id, add_node, add_edge) -> None:
    restricted = context.get('restricted_release')
    if isinstance(restricted, dict) and restricted.get('summary'):
        summary = _clean_text(restricted.get('summary') or '')
        if not _contains_forbidden(summary):
            add_node(EvidenceNode(
                id=f'shareholder:{stock_id}:restricted_release',
                label='解禁/股东事件',
                type='shareholder_action',
                layer='shareholder',
                direction='bearish' if _num(restricted.get('ratio')) and _num(restricted.get('ratio')) >= 5 else 'neutral',
                freshness='fresh',
                display_weight=9,
                analysis_weight=0.42,
                confidence=4,
                summary=summary[:220],
                time_windows=['short', 'swing'],
                color='#9ac7ff',
            ))
            add_edge(EvidenceEdge(
                source=f'shareholder:{stock_id}:restricted_release',
                target=stock_id,
                relation='impacts',
                layer='shareholder',
                display_weight=1.8,
                analysis_weight=0.42,
                confidence=4,
                summary='股东事件作为公司层风险或供给约束证据。',
            ))


def _add_variable_nodes(seed, stock_id, add_node, add_edge) -> None:
    snapshots = seed.get('variable_snapshots') if isinstance(seed.get('variable_snapshots'), dict) else {}
    mapping = {
        'cost': ('cost_factor', 'cost', '成本变量'),
        'demand': ('demand_factor', 'demand', '需求变量'),
        'export': ('export_factor', 'export', '出口变量'),
        'price': ('price_factor', 'price', '产品价格'),
        'inventory': ('inventory_factor', 'inventory', '库存变量'),
        'capacity': ('capacity_factor', 'capacity', '产能变量'),
        'competition': ('competition_factor', 'competition', '竞争格局'),
    }
    for key, raw in snapshots.items():
        if key not in mapping or not isinstance(raw, dict):
            continue
        node_type, layer, label = mapping[key]
        summary = _clean_text(raw.get('summary') or raw.get('text') or '')
        if not summary or _contains_forbidden(summary):
            continue
        node_id = f'{node_type}:{stock_id}:{key}'
        add_node(EvidenceNode(
            id=node_id,
            label=label,
            type=node_type,
            layer=layer,
            direction=_direction(raw.get('direction')),
            freshness=str(raw.get('freshness') or 'unknown'),
            display_weight=10,
            analysis_weight=0.55,
            confidence=5,
            summary=summary[:220],
            metrics=_variable_metrics(raw),
            time_windows=list(raw.get('window') or raw.get('time_windows') or ['swing', 'mid']),
            expectation_gap=str(raw.get('expectation_gap') or 'unknown'),
        ))
        add_edge(EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation='impacts',
            layer=layer,
            display_weight=2.2,
            analysis_weight=0.55,
            confidence=5,
            summary=f'{label}影响公司收入、成本或利润变量。',
        ))


def _add_missing_nodes(code, stock_id, nodes, add_node, add_edge) -> None:
    existing_layers = {
        node.layer for node in nodes
        if node.type != 'missing_evidence' and node.analysis_weight > 0
    }
    existing_types = {
        node.type for node in nodes
        if node.type != 'missing_evidence' and node.analysis_weight > 0
    }
    for missing_key, spec in _MISSING_LAYER_SPECS.items():
        graph_layer = str(spec['layer'])
        label = str(spec['label'])
        covered_types = set(spec.get('types') or ())
        if graph_layer in existing_layers:
            continue
        if existing_types.intersection(covered_types) or (
            missing_key in {'cost', 'demand', 'export', 'inventory', 'capacity', 'competition'}
            and graph_layer in existing_layers
        ):
            continue
        if missing_key in {'customer', 'supplier'} and graph_layer in existing_layers:
            continue
        node_id = f'missing:{code}:{missing_key}'
        add_node(EvidenceNode(
            id=node_id,
            label=label,
            type='missing_evidence',
            layer=graph_layer,
            direction='unknown',
            freshness='unknown',
            display_weight=8,
            analysis_weight=0.0,
            confidence=0,
            summary=f'未找到足够{label.replace("缺失", "")}，只能标记为缺失，不能补充事实。',
            forecast_role='missing',
            time_windows=list(_DEFAULT_WINDOWS),
            color='#657694',
        ))
        add_edge(EvidenceEdge(
            source=node_id,
            target=stock_id,
            relation='missing',
            layer=graph_layer,
            direction='unknown',
            display_weight=1.2,
            analysis_weight=0.0,
            confidence=0,
            summary='缺失证据必须显式入图。',
            color='#657694',
        ))


def _text_layer_keyword_checks() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    return (
        ('order_contract', 'order_contract', (
            '\u8ba2\u5355', '\u5408\u540c', '\u4e2d\u6807', '\u7b7e\u7ea6',
            'contract', 'order', 'bid',
        )),
        ('fund_flow', 'trading_behavior', (
            '\u5927\u5b97\u4ea4\u6613', '\u878d\u8d44\u5ba2', '\u4e3b\u529b\u8d44\u91d1',
            '\u7279\u5927\u5355', '\u9f99\u864e\u699c', '\u51c0\u6d41\u5165', '\u51c0\u6d41\u51fa',
            'block trade', 'margin financing', 'fund flow',
        )),
        ('cost_factor', 'cost', (
            '\u6210\u672c', '\u539f\u6750\u6599', '\u80fd\u6e90', '\u7535\u4ef7', '\u8fd0\u8d39',
            'cost', 'raw material',
        )),
        ('demand_factor', 'demand', (
            '\u9700\u6c42', '\u9500\u91cf', '\u51fa\u8d27', '\u7ec8\u7aef', '\u590d\u82cf',
            'demand', 'shipment', 'sales',
        )),
        ('export_factor', 'export', (
            '\u51fa\u53e3', '\u6d77\u5916', '\u5916\u8d38', '\u5173\u7a0e', '\u6d77\u5173',
            'export', 'overseas', 'tariff',
        )),
        ('inventory_factor', 'inventory', ('\u5e93\u5b58', '\u53bb\u5e93', '\u8865\u5e93', 'inventory')),
        ('capacity_factor', 'capacity', (
            '\u4ea7\u80fd', '\u6295\u4ea7', '\u6269\u4ea7', '\u5f00\u5de5\u7387',
            '\u5efa\u8bbe', '\u5de5\u5382', 'capacity',
        )),
        ('competition_factor', 'competition', (
            '\u7ade\u4e89', '\u4efd\u989d', '\u66ff\u4ee3', '\u4ef7\u683c\u6218',
            'competition', 'market share',
        )),
        ('customer', 'customer_supplier', (
            '\u5ba2\u6237', '\u4f9b\u5e94\u5546', '\u4f9b\u5e94\u94fe', '\u91c7\u8d2d',
            'customer', 'supplier',
        )),
        ('shareholder_action', 'shareholder', (
            '\u56de\u8d2d', '\u589e\u6301', '\u51cf\u6301', '\u89e3\u7981', '\u8d28\u62bc',
            'repurchase', 'shareholder',
        )),
        ('policy', 'policy', (
            '\u653f\u7b56', '\u76d1\u7ba1', '\u8865\u8d34', '\u5904\u7f5a',
            'policy', 'regulation',
        )),
    )


def _text_type_layer(*texts: str) -> tuple[str, str]:
    merged = ' '.join(str(text or '') for text in texts).lower()
    checks = _text_layer_keyword_checks() + (
        ('order_contract', 'order_contract', ('订单', '合同', '中标', '签约', 'contract', 'order', 'bid')),
        ('cost_factor', 'cost', ('成本', '原材料', '原料', '能源', '电价', '运费', '折旧', 'cost', 'raw material')),
        ('demand_factor', 'demand', ('需求', '销量', '出货', '终端', '复苏', 'demand', 'shipment', 'sales')),
        ('export_factor', 'export', ('出口', '海外', '外贸', '关税', '海关', 'export', 'overseas', 'tariff')),
        ('inventory_factor', 'inventory', ('库存', '去库', '补库', 'inventory')),
        ('capacity_factor', 'capacity', ('产能', '投产', '扩产', '开工率', 'capacity')),
        ('competition_factor', 'competition', ('竞争', '份额', '替代', '价格战', 'competition', 'market share')),
        ('customer', 'customer_supplier', ('客户', 'customer')),
        ('supplier', 'customer_supplier', ('供应商', '供应链', 'supplier')),
        ('shareholder_action', 'shareholder', ('回购', '增持', '减持', '解禁', '质押', 'repurchase', 'shareholder')),
        ('policy', 'policy', ('政策', '监管', '补贴', '处罚', 'policy', 'regulation')),
    )
    for node_type, layer, keywords in checks:
        if any(keyword in merged for keyword in keywords):
            return node_type, layer
    return 'news', 'news_event'


def _public_payload_hit(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return value.get('hit', True) is not False


def _trade_metrics(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        'rows',
        'premium_pct_avg',
        'amount_yi',
        'net_amount_yi',
        'hold_pct',
        'hold_pct_d1_change',
        'watchlist_hit',
    )
    return {key: value.get(key) for key in keys if key in value}


def _trade_direction(node_type: str, value: dict[str, Any]) -> str:
    if node_type == 'hot_money_seat':
        side = str(value.get('side') or '')
        return 'bullish' if side == 'buy' else 'bearish' if side == 'sell' else 'neutral'
    if node_type == 'institutional_behavior':
        side = str(value.get('direction') or '')
        return 'bullish' if side == 'in' else 'bearish' if side == 'out' else 'neutral'
    premium = _num(value.get('premium_pct_avg'))
    if premium is not None and premium > 2:
        return 'bullish'
    if premium is not None and premium < -2:
        return 'bearish'
    return 'neutral'


def _block_trade_summary(value: dict[str, Any]) -> str:
    summary = _clean_text(value.get('summary') or '')
    if summary:
        return summary[:220]
    rows = value.get('rows')
    premium = value.get('premium_pct_avg')
    amount = value.get('amount_yi')
    parts = ['大宗交易']
    if rows is not None:
        parts.append(f'{rows} 笔')
    if premium is not None:
        parts.append(f'平均折溢价 {premium}%')
    if amount is not None:
        parts.append(f'成交额 {amount} 亿')
    return '，'.join(parts)[:220]


def _event_type_layer(category: str) -> tuple[str, str]:
    if category in {'policy', 'regulation'}:
        return 'policy', 'policy'
    if category in {'macro', 'finance', 'overseas'}:
        return 'macro', 'macro'
    if category in {'real_estate'}:
        return 'risk', 'risk'
    return 'event', 'news_event'


def _variable_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    keys = (
        'current_value',
        'unit',
        'change_1d',
        'change_3d',
        'change_7d',
        'change_30d',
        'change_qoq',
        'change_yoy',
        'acceleration',
        'duration',
    )
    return {key: raw.get(key) for key in keys if key in raw}


def _format_flow_summary(flow: dict[str, Any], realtime: dict[str, Any]) -> str:
    parts = []
    if flow.get('days'):
        parts.append(f"{flow.get('days')} 日资金窗口")
    if flow.get('main_5d') is not None:
        parts.append(f"5日主力净额 {flow.get('main_5d')}")
    if flow.get('main_streak'):
        parts.append(f"连续方向 {flow.get('main_streak')}")
    if realtime.get('main_flow') is not None:
        parts.append(f"今日主力 {realtime.get('main_flow')}")
    return '；'.join(parts)


def _contains_forbidden(*texts: str) -> bool:
    merged = ' '.join(str(t or '') for t in texts)
    return any(term in merged for term in FORBIDDEN_INTELLIGENCE_TERMS)


def _clean_text(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def _clean_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _safe_id(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_.:-]+', '_', text)
    return text[:80] or datetime.now().strftime('%H%M%S%f')


def _direction(value: Any) -> str:
    value = str(value or 'neutral')
    if value in {'bullish', 'bearish', 'neutral', 'mixed', 'unknown'}:
        return value
    return 'neutral'


def _num(value: Any) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
