"""Normalize existing stock seed data into intelligence item contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.intelligence.models import NormalizedIntelItem


def normalized_items_from_seed(seed: dict[str, Any]) -> list[NormalizedIntelItem]:
    subject = seed.get('subject') if isinstance(seed.get('subject'), dict) else {}
    code = _clean_code(subject.get('code') or seed.get('code') or '')
    name = str(subject.get('name') or seed.get('name') or code)
    fetched_at = datetime.now().isoformat(timespec='seconds')
    items: list[NormalizedIntelItem] = []

    for idx, event in enumerate(seed.get('intel_events') or []):
        data = event.to_dict() if hasattr(event, 'to_dict') else dict(event or {})
        title = str(data.get('title') or '').strip()
        if not title:
            continue
        layer = _layer_from_text(title, str(data.get('summary') or data.get('interpretation') or ''))
        items.append(NormalizedIntelItem(
            id=f'event:{data.get("id") or idx}',
            source_id='existing:intel_feed',
            title=title,
            summary=str(data.get('summary') or data.get('interpretation') or title),
            layer=layer or _event_layer(str(data.get('category') or '')),
            direction=_direction(data.get('direction')),
            related_codes=[code] if code else [],
            related_names=[name] if name else [],
            related_sectors=list(data.get('related_sectors') or []),
            evidence_type='event',
            published_at=str(data.get('timestamp') or '') or None,
            fetched_at=fetched_at,
            url='',
            trust_level='secondary',
            confidence=_confidence(data.get('level')),
            time_windows=['immediate', 'short', 'swing'],
            raw_ref=str(data.get('id') or ''),
        ))

    for idx, item in enumerate(seed.get('stock_news') or []):
        data = dict(item or {})
        title = str(data.get('title') or '').strip()
        if not title:
            continue
        text = str(data.get('content') or title)
        layer = _layer_from_text(title, text) or 'news_event'
        items.append(NormalizedIntelItem(
            id=f'news:{_safe_id(data.get("url") or title or idx)}',
            source_id='existing:stock_news',
            title=title,
            summary=text[:260],
            layer=layer,
            direction='neutral',
            related_codes=[code] if code else [],
            related_names=[name] if name else [],
            related_sectors=[],
            evidence_type='news',
            published_at=str(data.get('date') or '') or None,
            fetched_at=fetched_at,
            url=str(data.get('url') or ''),
            trust_level='media',
            confidence=4,
            time_windows=['immediate', 'short'] if layer == 'news_event' else ['short', 'swing', 'mid'],
        ))

    context = seed.get('context') if isinstance(seed.get('context'), dict) else {}
    public_evidence = context.get('public_fund_evidence') if isinstance(context.get('public_fund_evidence'), dict) else {}
    for evidence_type, payload in (
        ('hot_money_seat', public_evidence.get('lhb')),
        ('institutional_behavior', public_evidence.get('hsgt')),
        ('block_trade', public_evidence.get('dzjy')),
    ):
        if not _payload_hit(payload):
            continue
        data = dict(payload or {})
        current_value = _num(data.get('amount_yi')) if evidence_type == 'block_trade' else _num(data.get('net_amount_yi'))
        items.append(NormalizedIntelItem(
            id=f'{evidence_type}:{code}',
            source_id='existing:public_fund_evidence',
            title=evidence_type,
            summary=str(data.get('summary') or data),
            layer='trading_behavior',
            direction=_trade_direction(evidence_type, data),
            related_codes=[code] if code else [],
            related_names=[name] if name else [],
            related_sectors=[],
            evidence_type=evidence_type,
            published_at=public_evidence.get('date'),
            fetched_at=str(public_evidence.get('fetched_at') or fetched_at),
            url='',
            trust_level='secondary',
            confidence=5,
            metric_name='amount_yi' if evidence_type == 'block_trade' else 'net_amount_yi',
            current_value=current_value,
            unit='yi' if current_value is not None else '',
            time_windows=['immediate', 'short'],
        ))

    restricted = context.get('restricted_release') if isinstance(context.get('restricted_release'), dict) else {}
    if restricted.get('summary'):
        items.append(NormalizedIntelItem(
            id=f'shareholder:restricted_release:{code}',
            source_id='existing:restricted_release',
            title='restricted_release',
            summary=str(restricted.get('summary') or ''),
            layer='shareholder',
            direction='neutral',
            related_codes=[code] if code else [],
            related_names=[name] if name else [],
            related_sectors=[],
            evidence_type='shareholder_action',
            published_at=str(restricted.get('release_date') or '') or None,
            fetched_at=fetched_at,
            url='',
            trust_level='secondary',
            confidence=4,
            time_windows=['short', 'swing'],
        ))

    summary = str((context.get('fundamental_summary') or seed.get('fundamental_summary') or '')).strip()
    if summary:
        items.append(NormalizedIntelItem(
            id=f'financial:{code}',
            source_id='existing:fundamentals',
            title='fundamental_summary',
            summary=summary[:260],
            layer='financial',
            direction='neutral',
            related_codes=[code] if code else [],
            related_names=[name] if name else [],
            related_sectors=[],
            evidence_type='financial_metric',
            published_at=None,
            fetched_at=fetched_at,
            url='',
            trust_level='secondary',
            confidence=5,
            time_windows=['swing', 'mid', 'long'],
        ))

    return items


def _event_layer(category: str) -> str:
    if category in {'policy', 'regulation'}:
        return 'policy'
    if category in {'macro', 'finance', 'overseas'}:
        return 'macro'
    if category == 'real_estate':
        return 'risk'
    return 'news_event'


def _layer_keyword_checks() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return (
        ('order_contract', (
            '\u8ba2\u5355', '\u5408\u540c', '\u4e2d\u6807', '\u7b7e\u7ea6',
            'contract', 'order', 'bid',
        )),
        ('trading_behavior', (
            '\u5927\u5b97\u4ea4\u6613', '\u878d\u8d44\u5ba2', '\u4e3b\u529b\u8d44\u91d1',
            '\u7279\u5927\u5355', '\u9f99\u864e\u699c', '\u51c0\u6d41\u5165', '\u51c0\u6d41\u51fa',
            'block trade', 'margin financing', 'fund flow',
        )),
        ('cost', (
            '\u6210\u672c', '\u539f\u6750\u6599', '\u80fd\u6e90', '\u7535\u4ef7', '\u8fd0\u8d39',
            'cost', 'raw material',
        )),
        ('demand', (
            '\u9700\u6c42', '\u9500\u91cf', '\u51fa\u8d27', '\u7ec8\u7aef', '\u590d\u82cf',
            'demand', 'shipment', 'sales',
        )),
        ('export', (
            '\u51fa\u53e3', '\u6d77\u5916', '\u5916\u8d38', '\u5173\u7a0e', '\u6d77\u5173',
            'export', 'overseas', 'tariff',
        )),
        ('inventory', ('\u5e93\u5b58', '\u53bb\u5e93', '\u8865\u5e93', 'inventory')),
        ('capacity', (
            '\u4ea7\u80fd', '\u6295\u4ea7', '\u6269\u4ea7', '\u5f00\u5de5\u7387',
            '\u5efa\u8bbe', '\u5de5\u5382', 'capacity',
        )),
        ('competition', (
            '\u7ade\u4e89', '\u4efd\u989d', '\u66ff\u4ee3', '\u4ef7\u683c\u6218',
            'competition', 'market share',
        )),
        ('customer_supplier', (
            '\u5ba2\u6237', '\u4f9b\u5e94\u5546', '\u4f9b\u5e94\u94fe', '\u91c7\u8d2d',
            'customer', 'supplier',
        )),
        ('shareholder', (
            '\u56de\u8d2d', '\u589e\u6301', '\u51cf\u6301', '\u89e3\u7981', '\u8d28\u62bc',
            'repurchase', 'shareholder',
        )),
        ('policy', (
            '\u653f\u7b56', '\u76d1\u7ba1', '\u8865\u8d34', '\u5904\u7f5a',
            'policy', 'regulation',
        )),
    )


def _layer_from_text(*texts: str) -> str:
    merged = ' '.join(str(text or '') for text in texts).lower()
    checks = _layer_keyword_checks() + (
        ('order_contract', ('订单', '合同', '中标', '签约', 'contract', 'order', 'bid')),
        ('cost', ('成本', '原材料', '原料', '能源', '电价', '运费', '折旧', 'cost', 'raw material')),
        ('demand', ('需求', '销量', '出货', '终端', '复苏', 'demand', 'shipment', 'sales')),
        ('export', ('出口', '海外', '外贸', '关税', '海关', 'export', 'overseas', 'tariff')),
        ('inventory', ('库存', '去库', '补库', 'inventory')),
        ('capacity', ('产能', '投产', '扩产', '开工率', 'capacity')),
        ('competition', ('竞争', '份额', '替代', '价格战', 'competition', 'market share')),
        ('customer_supplier', ('客户', '供应商', '供应链', 'customer', 'supplier')),
        ('shareholder', ('回购', '增持', '减持', '解禁', '质押', 'repurchase', 'shareholder')),
        ('policy', ('政策', '监管', '补贴', '处罚', 'policy', 'regulation')),
    )
    for layer, keywords in checks:
        if any(keyword in merged for keyword in keywords):
            return layer
    return ''


def _payload_hit(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return value.get('hit', True) is not False


def _trade_direction(evidence_type: str, data: dict[str, Any]) -> str:
    if evidence_type == 'hot_money_seat':
        side = str(data.get('side') or '')
        return 'bullish' if side == 'buy' else 'bearish' if side == 'sell' else 'neutral'
    if evidence_type == 'institutional_behavior':
        side = str(data.get('direction') or '')
        return 'bullish' if side == 'in' else 'bearish' if side == 'out' else 'neutral'
    premium = _num(data.get('premium_pct_avg'))
    if premium is not None and premium > 2:
        return 'bullish'
    if premium is not None and premium < -2:
        return 'bearish'
    return 'neutral'


def _direction(value: Any) -> str:
    value = str(value or 'neutral')
    return value if value in {'bullish', 'bearish', 'neutral', 'mixed', 'unknown'} else 'neutral'


def _confidence(level: Any) -> int:
    return {'critical': 8, 'important': 6, 'info': 4}.get(str(level or ''), 4)


def _num(value: Any) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _safe_id(value: Any) -> str:
    text = str(value or '').strip()
    return ''.join(ch if ch.isalnum() or ch in '_.:-' else '_' for ch in text)[:80] or 'item'
