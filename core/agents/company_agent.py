# Inspired by TradingAgents v0.2.4 (Apache-2.0)
"""Agent3 — 基本面/板块分析师，输出结构化 JSON。"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import (
    build_cached_user_message, call_pro, AGENT_JSON_SCHEMA, STRICT_SOURCE_GROUNDING, parse_agent_json,
)

def _is_fund_like(code: str, name: str) -> bool:
    code_s = str(code or '').strip()
    name_s = str(name or '').upper()
    return (
        'ETF' in name_s
        or 'LOF' in name_s
        or '基金' in name_s
        or code_s.startswith(('15', '16', '50', '51', '52', '56', '58'))
    )


def _fund_fallback(
    code: str,
    name: str,
    sectors: list[str] | None,
    fund_context: str = '',
) -> dict:
    sector_text = ' / '.join((sectors or [])[:3]) or '暂无明确板块'
    evidence = [
        {
            'name': '产品属性',
            'value': 'ETF/基金不适用单家公司财报，基本面应以跟踪行业、成分股质量和板块资金验证为主',
            'weight': 'high',
        },
        {
            'name': '跟踪方向',
            'value': sector_text,
            'weight': 'medium',
        },
    ]
    if fund_context:
        evidence.append({'name': '板块环境', 'value': str(fund_context)[:120], 'weight': 'medium'})
    return {
        'analyst': '基本面',
        'stance': 'neutral',
        'summary': 'ETF不适用个股财报口径，基本面仅作行业景气参考；短线仍以技术触发和资金连续性为准。',
        'evidence': evidence[:4],
        'counter_evidence': [
            {
                'name': '基本面边界',
                'value': 'ETF短线涨跌主要由板块贝塔、资金流和技术位置驱动，不能用单家公司盈利指标判断',
                'weight': 'high',
            }
        ],
        'intel_refs': [],
        'confidence': 4,
    }


def _stock_fallback_from_summary(
    code: str,
    name: str,
    fundamental_summary: str,
    sectors: list[str] | None,
) -> dict | None:
    """LLM 返回空时，根据本地财务摘要给出保守基本面结论。"""
    import re

    text = str(fundamental_summary or '')
    if not text.strip() or '暂无数据' in text or '获取失败' in text:
        return None

    def _num(label: str):
        m = re.search(re.escape(label) + r'[:：]\s*([+-]?\d+(?:\.\d+)?)', text)
        return float(m.group(1)) if m else None

    revenue = _num('主营收入')
    profit = _num('净利润')
    margin = _num('净利率')
    roe = _num('ROE')
    _roe_ann_m = re.search(r'ROE年化估算[:：]\s*[≈约]?\s*([+-]?\d+(?:\.\d+)?)', text)
    roe_ann = float(_roe_ann_m.group(1)) if _roe_ann_m else None
    current_ratio = _num('流动比率')
    debt_ratio = _num('资产负债率')

    negatives = []
    positives = []
    evidence = []
    counter = []

    if profit is not None:
        item = {'name': '净利润', 'value': f'{profit:+.2f}亿（摘要口径）', 'weight': 'high'}
        (negatives if profit < 0 else positives).append(item)
    if margin is not None:
        item = {'name': '净利率', 'value': f'{margin:.1f}%', 'weight': 'high' if margin < 0 else 'medium'}
        (negatives if margin < 0 else positives).append(item)
    if roe is not None:
        roe_val = f'{roe:.1f}%（单期/摘要口径）'
        if roe_ann is not None:
            roe_val = f'{roe:.1f}%（单期/摘要口径，年化估算≈{roe_ann:.1f}%，勿当年报ROE）'
        item = {'name': 'ROE', 'value': roe_val, 'weight': 'medium'}
        (negatives if roe < 0 else positives).append(item)
    if current_ratio is not None:
        item = {'name': '流动比率', 'value': f'{current_ratio:.2f}', 'weight': 'medium'}
        (positives if current_ratio >= 1.2 else negatives).append(item)
    if debt_ratio is not None:
        item = {'name': '资产负债率', 'value': f'{debt_ratio:.1f}%', 'weight': 'medium'}
        (positives if debt_ratio <= 50 else negatives).append(item)
    if revenue is not None:
        evidence.append({'name': '营收规模', 'value': f'{revenue:.2f}亿（摘要口径）', 'weight': 'medium'})

    score = len(positives) - len(negatives)
    if score >= 2:
        stance = 'bullish'
        summary = '财务摘要偏稳健，但短线仍需服从技术触发与资金连续性。'
    elif score <= -1:
        stance = 'bearish'
        summary = '财务摘要偏弱，盈利能力承压；短线强势也需警惕业绩瑕疵带来的回撤。'
    else:
        stance = 'neutral'
        summary = '财务摘要多空混合，基本面不构成独立买点，短线以技术和资金验证为主。'

    if stance == 'bearish':
        evidence.extend(negatives[:3] or positives[:2])
        counter.extend(positives[:2] or [{'name': '数据边界', 'value': '财务摘要有限，需结合最新公告复核', 'weight': 'medium'}])
    else:
        evidence.extend(positives[:3] or negatives[:2])
        counter.extend(negatives[:2] or [{'name': '数据边界', 'value': '财务摘要有限，短线不应单独依赖基本面', 'weight': 'medium'}])

    if sectors:
        evidence.append({'name': '所属板块', 'value': ' / '.join(sectors[:3]), 'weight': 'low'})

    confidence = 5 if stance != 'neutral' else 4
    return {
        'analyst': '基本面',
        'stance': stance,
        'summary': summary,
        'evidence': evidence[:4],
        'counter_evidence': counter[:2],
        'intel_refs': [],
        'confidence': confidence,
    }


_SYSTEM_STOCK = """\
你是基本面分析师，服务于个人量化投资者。

任务：阅读季报财务数据 + 板块归属，综合判断公司基本面偏向。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "基本面"。
evidence 列举 2-4 个支持方向的财务数据（ROE/净利润/营收增速/负债率/PE），value 含具体数值和同期对比。
counter_evidence 必须列出 1-2 个财务风险因素（含具体数值或可量化描述）。禁止使用"暂无明确反方信号"等占位符。即使当前财务数据全面健康，也必须指出1个潜在风险（如：最新报告期数据距今已滞后、行业周期下行、原材料成本上升、竞争加剧侵蚀毛利率等），并说明该风险在当前数据中尚未体现但不可忽视的原因。不可留空。
intel_refs 固定为空列表 []。
summary ≤80 字，包含综合偏向及核心财务特征。\
"""

_SYSTEM_SECTOR = """\
你是板块景气度分析师，服务于个人量化投资者。

任务：阅读板块成分股摘要 + 板块资金 + 行业基本面，综合判断板块景气度偏向。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "板块景气度"。
evidence 列举 2-4 个支持方向的关键因素（景气度/龙头动态/资金共振），value 含关键描述。
counter_evidence 必须列出 1-2 个风险因素（含具体数值或可量化描述）。禁止使用"暂无明确反方信号"等占位符。即使当前板块数据全面向好，也必须指出1个潜在风险（如：政策调控、需求下滑、龙头业绩变脸、资金轮动流出等），并说明该风险在当前数据中尚未体现但不可忽视的原因。不可留空。
intel_refs 固定为空列表 []。
summary ≤80 字，包含综合偏向及核心理由，还需提供 target_pct（板块预期涨跌幅%）参考。\
"""

_STABLE_PREFIX_STOCK = """\
[Agent3 · 基本面分析师]
请基于下方"当前数据"做基本面分析。\
"""

_STABLE_PREFIX_SECTOR = """\
[Agent3 · 板块景气度分析师]
请基于下方"当前数据"做板块分析。\
"""


def _build_stock_variable_data(
    code: str,
    name: str,
    fundamental_summary: str,
    sectors: list[str] | None,
    restricted_release: dict | None = None,
) -> str:
    parts = [
        '【当前数据】',
        f'对象：{name}（{code}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]
    if sectors:
        parts.append(f'所属板块：{" / ".join(sectors[:3])}')

    if fundamental_summary:
        parts.append('')
        parts.append(fundamental_summary)
    else:
        parts.append('\n▶ 基本面：暂无数据')

    if restricted_release and isinstance(restricted_release, dict):
        parts.append(f'\n▶ {restricted_release["summary"]}')

    parts.append('\n请输出基本面分析：')
    return '\n'.join(parts)


def _build_sector_variable_data(
    sector_name: str,
    kind: str,
    fund_summary: str,
    constituents: list[dict] | None,
) -> str:
    parts = [
        '【当前数据】',
        f'板块：{sector_name}（{kind}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]
    if fund_summary:
        parts.append('')
        parts.append(fund_summary)
    if constituents:
        parts.append('\n▶ 主要成分股：')
        for c in constituents[:6]:
            cd = (c.get('code') or '').strip()
            nm = (c.get('name') or '').strip()
            if cd:
                parts.append(f'  · {nm}（{cd}）')
    parts.append('\n请输出板块景气度分析：')
    return '\n'.join(parts)


def run(
    api_key: str,
    kind: str = 'stock',
    *,
    code: str = '',
    name: str = '',
    fundamental_summary: str = '',
    sectors: list[str] | None = None,
    sector_name: str = '',
    fund_summary: str = '',
    constituents: list[dict] | None = None,
    restricted_release: dict | None = None,
    fund_context: str = '',
    **_: Any,
) -> dict:
    """运行 Agent3。kind='stock' 或 'sector'。返回结构化 dict。"""
    if kind == 'sector':
        variable = _build_sector_variable_data(sector_name or name, kind, fund_summary, constituents)
        user_msg = build_cached_user_message(_STABLE_PREFIX_SECTOR, variable)
        out, _reasoning = call_pro(_SYSTEM_SECTOR, user_msg, api_key, max_tokens=3000,
                                   response_format={'type': 'json_object'})
        return parse_agent_json(out, '板块景气度')
    if _is_fund_like(code, name):
        return _fund_fallback(code, name, sectors, fund_context)

    variable = _build_stock_variable_data(code, name, fundamental_summary, sectors, restricted_release)
    user_msg = build_cached_user_message(_STABLE_PREFIX_STOCK, variable)
    out, _reasoning = call_pro(_SYSTEM_STOCK, user_msg, api_key, max_tokens=3000,
                               response_format={'type': 'json_object'})
    parsed = parse_agent_json(out, '基本面')
    if parsed.get('confidence', 0) <= 0 and _is_fund_like(code, name):
        return _fund_fallback(code, name, sectors, fund_context)
    if parsed.get('confidence', 0) <= 0:
        fallback = _stock_fallback_from_summary(code, name, fundamental_summary, sectors)
        if fallback:
            return fallback
    return parsed
