"""Agent5 — 资金流向分析师。

输入：主力资金净流入/净流出摘要 + 板块资金上下文
输出：结构化 JSON（stance / summary / evidence / counter_evidence / intel_refs）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import (
    build_cached_user_message, call_pro, AGENT_JSON_SCHEMA, STRICT_SOURCE_GROUNDING, parse_agent_json,
)
from core.agents.company_agent import _is_fund_like

_SYSTEM = """\
你是资金流向分析师，服务于个人量化投资者。

任务：阅读主力资金净流入/净流出数据 + 公开资金证据 + 板块资金共振，判断资金面偏向。

短线资金纪律：
1. 数据天数 ≥3 日时，看 3/5/10 日主力趋势和连续性；数据仅 1-2 日时，禁止生成"连续3日/5日/近5日"等多日趋势结论，只能写"单日线索，趋势待确认"。
2. 主力流出、散户流入是短线风险信号；主力流入、散户流出才是更强确认。
3. 历史样本不足时 confidence 不得高于5。
4. 若仅有 1 日数据，evidence 中禁止出现"近5日""连续N日""N日趋势"等字段；只能写当日主力净流入数值和方向，value 必须标注"仅1日"。
5. 龙虎榜席位只能写“席位命中观察名单/未命中观察名单”，不得写“知名游资确认买入”。
6. ETF 若公开证据 unavailable，不得强行分析龙虎榜、游资席位或北向。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "资金流向"。
evidence 重点关注：连续净流入/净流出天数、主力vs散户背离、龙虎榜席位观察名单命中、北向方向、大宗交易折溢价、板块资金共振方向，value 给具体数值或方向描述。
counter_evidence 必须列出 1-2 个反向资金信号（含具体数值或可量化描述）。禁止使用"暂无明确反方信号"等占位符。即使当前资金面全面向好，也必须指出1个潜在风险（如：主力诱多出货、单日数据不代表趋势、散户跟风过热、北向资金反向操作等），并说明该风险在当前数据中尚未体现但不可忽视的原因。不可留空。
intel_refs 固定为空列表 []。
summary ≤80 字，包含主力净流入数值和连续天数结论。\
"""

_STABLE_PREFIX = """\
[Agent5 · 资金流向分析师]
请基于下方"当前数据"做资金流向分析。重点判断主力参与意愿，给出资金面立场。\
"""

# ETF/基金 专用：禁用个股投机术语，改用机构资金口径
_SYSTEM_ETF = """\
你是ETF/基金资金流向分析师，服务于个人量化投资者。

任务：阅读ETF成交额、份额申赎、板块资金共振数据，判断ETF资金面偏向。

ETF资金纪律（必须严格遵守）：
1. ETF不是个股，禁止使用"游资""主力""庄家""龙虎榜""北向席位""知名游资席位"等个股投机术语。
2. ETF资金分析口径：成交额放大/萎缩、份额净申购/净赎回、板块资金共振方向、贝塔联动。
3. 数据仅1-2日时，禁止生成"连续3日/5日"等多日趋势结论，只能写"单日线索，趋势待确认"。
4. 历史样本不足或公开证据 unavailable 时 confidence 不得高于4。
5. ETF短线涨跌由板块贝塔与资金共振驱动，不能套用个股主力席位逻辑。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "资金流向"。
evidence 重点关注：成交额放大/萎缩倍数、份额净申购/净赎回方向、板块资金共振方向、跟踪指数资金联动，value 给具体数值或方向描述。禁用游资/主力/龙虎榜/北向席位等个股术语。
counter_evidence 必须列出 1-2 个反向资金信号（含具体数值或可量化描述），如：单日数据不代表趋势、板块资金轮动流出、份额申购可能是套利而非看多等。不可留空。
intel_refs 固定为空列表 []。
summary ≤80 字，包含成交/份额方向结论。\
"""


def _fund_fallback(code: str, name: str, context: dict) -> dict:
    """ETF/基金 资金数据缺失/为0 时的中性兜底，绕开 LLM，杜绝个股术语硬失败。"""
    fund_context = context.get('fund_context') or ''
    evidence = [
        {
            'name': '产品属性',
            'value': 'ETF/基金资金以成交活跃度、份额申赎和板块资金共振为口径，不适用个股主力/游资/龙虎榜分析',
            'weight': 'high',
        },
    ]
    if fund_context:
        evidence.append({'name': '板块资金环境', 'value': str(fund_context)[:120], 'weight': 'medium'})
    return {
        'analyst': '资金流向',
        'stance': 'neutral',
        'summary': 'ETF资金明细数据缺失，资金面仅作中性参考；短线以板块资金共振和技术触发为准。',
        'evidence': evidence[:4],
        'counter_evidence': [
            {
                'name': '资金口径边界',
                'value': 'ETF无个股主力净流入口径，份额申赎可能含套利成分，不能等同看多/看空',
                'weight': 'high',
            }
        ],
        'intel_refs': [],
        'confidence': 4,
    }


def _fmt_yi(value: Any) -> str:
    try:
        return f'{float(value) / 100000000:+.2f} 亿'
    except (TypeError, ValueError):
        return '资料未提供'


def _stock_fund_from_context(code: str, name: str, context: dict) -> dict:
    """LLM 解析失败但本地已有资金摘要时，保留现有资料边界，不误报数据缺失。"""
    flow = context.get('flow_profile') or {}
    money_flow = str(context.get('money_flow_summary') or '').strip()
    days = int(flow.get('days') or 0) if isinstance(flow, dict) else 0
    day_label = f'仅{days}日' if days and days < 3 else (f'{days}日' if days else '样本不足')

    if isinstance(flow, dict) and flow.get('available'):
        main = flow.get('main_5d')
        small = flow.get('small_5d')
        xlarge = flow.get('xlarge_5d')
        evidence_value = (
            f'主力净流入: {_fmt_yi(main)}（{day_label}），'
            f'超大单: {_fmt_yi(xlarge)}，散户: {_fmt_yi(small)}'
        )
        summary = (
            f'{name}资金有{day_label}线索：主力{_fmt_yi(main)}，'
            f'超大单{_fmt_yi(xlarge)}，散户{_fmt_yi(small)}；'
            '样本不足，趋势待确认。'
        )
    else:
        evidence_value = money_flow[:180] if money_flow else '资料未提供'
        summary = f'{name}资金只有摘要线索，无法验证多日趋势；资金面维持中性观察。'

    return {
        'analyst': '资金流向',
        'stance': 'neutral',
        'summary': summary,
        'evidence': [
            {
                'name': '资金线索',
                'value': evidence_value,
                'weight': 'medium',
            },
        ],
        'counter_evidence': [
            {
                'name': '历史样本不足',
                'value': f'{day_label}资金数据，不能确认3/5/10日趋势，不得当作连续流入或连续流出',
                'weight': 'high',
            },
        ],
        'intel_refs': [],
        '_diagnostics': ['模型解析失败：LLM 输出不可用，本轮只按本地已取得资金资料保守兜底'],
        'confidence': 4 if days and days < 3 else 5,
    }


def _stock_fund_insufficient(code: str, name: str, context: dict) -> dict:
    """个股资金明细数据不足时的诚实兜底——只陈述数据缺失，绝不编造主力净流入数值。"""
    return {
        'analyst': '资金流向',
        'stance': 'neutral',
        'summary': f'{name}资金明细数据不足，无法判断主力方向；资金面本轮不作为可交易依据，以技术触发和量价验证为准。',
        'evidence': [
            {
                'name': '数据缺失',
                'value': '本次未取得有效的主力/游资/龙虎榜资金明细，无法量化净流入方向',
                'weight': 'high',
            },
        ],
        'counter_evidence': [
            {
                'name': '无数据即无法排除风险',
                'value': '资金缺失状态下既不能确认主力流入，也无法排除主力出货/诱多，不得据此看多',
                'weight': 'high',
            }
        ],
        'intel_refs': [],
        'confidence': 2,
    }


def _build_variable_data(
    code: str,
    name: str,
    context: dict,
) -> str:
    parts = [
        '【当前数据】',
        f'对象：{name}（{code}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]

    money_flow = context.get('money_flow_summary')
    if money_flow:
        parts.append(money_flow)
    else:
        parts.append('▶ 资金流：暂无数据')

    fund_context = context.get('fund_context')
    if fund_context:
        parts.append(f'▶ {fund_context}')

    flow_profile = context.get('flow_profile')
    if flow_profile:
        parts.append('\n▶ 资金流结构画像：')
        parts.append(str(flow_profile))

    public_evidence = context.get('public_fund_evidence')
    if public_evidence:
        parts.append('\n▶ 公开资金证据（详细资金判断使用）：')
        parts.append(str(public_evidence))

    parts.append('\n请输出资金流向分析：')
    return '\n'.join(parts)


def run(
    api_key: str,
    code: str,
    name: str,
    context: dict,
    **_: Any,
) -> dict:
    is_fund = _is_fund_like(code, name)
    money_flow = context.get('money_flow_summary')
    has_money_data = bool(money_flow) and '暂无' not in str(money_flow)

    variable = _build_variable_data(code, name, context)
    user_msg = build_cached_user_message(_STABLE_PREFIX, variable)

    if is_fund:
        # ETF 无资金明细 → 直接中性兜底，绕开 LLM 杜绝个股术语硬失败
        if not has_money_data:
            return _fund_fallback(code, name, context)
        # ETF 有资金数据 → 用 ETF 口径系统提示
        out, _reasoning = call_pro(_SYSTEM_ETF, user_msg, api_key, max_tokens=1500,
                                   response_format={'type': 'json_object'})
        parsed = parse_agent_json(out, '资金流向')
        if int(parsed.get('confidence') or 0) <= 0:
            result = _fund_fallback(code, name, context)
            if isinstance(result, dict) and out:
                result.setdefault('_diagnostics', []).append(
                    f'ETF LLM 返回内容但解析失败（首128字符: {str(out)[:128]}），回退兜底'
                )
            return result
        return parsed

    out, _reasoning = call_pro(_SYSTEM, user_msg, api_key, max_tokens=1500,
                               response_format={'type': 'json_object'})
    parsed = parse_agent_json(out, '资金流向')
    if int(parsed.get('confidence') or 0) <= 0 or not (parsed.get('evidence') or []):
        result = _stock_fund_from_context(code, name, context) if has_money_data else _stock_fund_insufficient(code, name, context)
        if isinstance(result, dict) and '_diagnostics' in result and out:
            result['_diagnostics'] = [f'LLM 返回内容但 JSON 解析失败（首128字符: {str(out)[:128]}），回退本地提取']
        return result
    flow = context.get('flow_profile') or {}
    days = int(flow.get('days') or 0) if isinstance(flow, dict) else 0
    if days and days < 3:
        parsed['confidence'] = min(int(parsed.get('confidence') or 0), 5)
        parsed.setdefault('counter_evidence', [])
        has_sample_warning = any(
            '样本' in str(item.get('name', '')) or '样本' in str(item.get('value', ''))
            for item in parsed.get('counter_evidence') or []
            if isinstance(item, dict)
        )
        if not has_sample_warning:
            parsed['counter_evidence'].append({
                'name': '历史样本不足',
                'value': f'仅{days}日资金数据，不能确认3/5/10日趋势',
                'weight': 'high',
            })
    return parsed
