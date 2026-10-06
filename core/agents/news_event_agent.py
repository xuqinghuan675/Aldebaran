"""Agent6 — 事件催化分析师。

输入：个股专属新闻列表
输出：结构化 JSON（stance / summary / evidence / counter_evidence / intel_refs）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import (
    build_cached_user_message, call_pro, AGENT_JSON_SCHEMA, STRICT_SOURCE_GROUNDING, parse_agent_json,
)

def _event_fallback(
    stock_news: list[dict] | None,
    context: dict | None,
) -> dict:
    context = context or {}
    tech = context.get('technical_profile') or {}
    flow = context.get('flow_profile') or {}
    evidence = []
    counter = []
    refs = []

    if stock_news:
        for item in stock_news[:3]:
            title = (item.get('title') or '').strip()
            day = (item.get('date') or '').strip()
            if title:
                refs.append(title[:30])
                evidence.append({
                    'name': '近期事件',
                    'value': f'{day} {title}'.strip(),
                    'weight': 'medium',
                })
    else:
        evidence.append({
            'name': '个股事件',
            'value': '本地新闻源未返回可验证的个股专属新闻/公告；不能据此断言现实无公告',
            'weight': 'low',
        })

    status = tech.get('status')
    if status == 'candidate':
        counter.append({
            'name': '量价验证不足',
            'value': '技术模式仍为候选未触发，事件或板块信息不能直接转化为买点',
            'weight': 'high',
        })
    elif status in ('avoid', 'failed'):
        counter.append({
            'name': '交易结构偏弱',
            'value': '技术模式禁止买入或已失败，事件需等待市场重新验证',
            'weight': 'high',
        })
    elif tech:
        counter.append({
            'name': '事件边界',
            'value': '事件面只作催化验证，不替代技术失效价和资金连续性',
            'weight': 'medium',
        })
    else:
        counter.append({
            'name': '数据边界',
            'value': '缺少技术与资金验证，事件面不单独构成交易依据',
            'weight': 'high',
        })

    days = flow.get('days') if isinstance(flow, dict) else None
    if days is not None and days < 3:
        counter.append({
            'name': '资金验证不足',
            'value': f'资金流仅{days}日样本，事件催化缺少连续资金确认',
            'weight': 'medium',
        })

    if stock_news:
        summary = '有近期个股新闻但未构成可独立驱动交易的催化；事件面需等待价格、量能和资金连续性验证。'
    else:
        summary = '本地新闻源未返回可独立驱动交易的个股事件；事件面需等待价格、量能和资金连续性验证。'
    return {
        'analyst': '事件催化',
        'stance': 'neutral',
        'summary': summary,
        'evidence': evidence[:3],
        'counter_evidence': counter[:2],
        'intel_refs': refs[:3],
        'confidence': 3,
    }


_SYSTEM = """\
你是事件催化分析师，服务于个人量化投资者。

任务：只阅读个股近期专属新闻、公告与资产重组等事件，判断近30天内是否有可交易催化，并检查是否被价格/量能/资金验证。

短线事件纪律：
1. 不只判断题材好坏，还要判断市场是否验证：股价是否跟随、成交量是否放大、同概念是否联动。
2. 公告已发酵但价格/量能未跟随时，标记为“故事强、交易弱”，降低 confidence。
3. 事件催化必须落到1-5个交易日的短线影响，不要只写长期愿景。
4. 若当前数据没有提供价格、量能或资金验证，confidence 最高为5，summary 必须写“待市场验证”。
5. 若 technical_profile.status 为 candidate/avoid，事件利好只能判为“催化候选”，不得直接写“股价有望上涨”。
6. 事件层只做验证，不改写资金周期：资金验证数据写“仅1日”就只能说1日，禁止把单日净流入表述为3日/5日连续流入或趋势确认。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "事件催化"。
evidence 引用最多 3 条具体新闻标题和日期，说明其对股价的催化意义。若个股专属新闻为空，stance 填 neutral，summary 写“本地新闻源未返回可交易催化”，evidence 填“本地新闻源未返回可验证的个股专属新闻/公告”，不要写成现实中“无公告/无新闻”，不要借用宏观或板块信息。
counter_evidence 必须引用 1-2 条负面/风险新闻（含具体日期和影响描述）。若近期无直接负面新闻，写明“缺少反向新闻，但催化仍需量价验证”，不要借用宏观或板块风险。
intel_refs 列出引用的新闻标题（≤30 字，最多 3 条）。
summary ≤120 字，包含最重要事件、是否已被价格/量能/资金验证，以及关键风险。\
"""

_STABLE_PREFIX = """\
[Agent6 · 事件催化分析师]
请基于下方"当前数据"做个股事件催化分析。不要分析宏观环境、板块情绪或大盘风险。\
"""


def _build_variable_data(
    code: str,
    name: str,
    stock_news: list[dict] | None,
    context: dict | None = None,
) -> str:
    context = context or {}
    parts = [
        '【当前数据】',
        f'对象：{name}（{code}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]
    tech = context.get('technical_profile')
    if tech:
        parts.append('\n▶ 技术验证数据：')
        parts.append(str(tech))
    flow = context.get('flow_profile')
    if flow:
        parts.append('\n▶ 资金验证数据：')
        parts.append(str(flow))

    if stock_news:
        parts.append('\n▶ 个股新闻（最近，按时间倒序）：')
        for item in stock_news[:8]:
            t = (item.get('title') or '').strip()[:80]
            d = (item.get('date') or '').strip()
            if t:
                parts.append(f'  · [{d}] {t}')
    else:
        parts.append('\n▶ 个股新闻：本地新闻源未返回')

    parts.append('\n请输出新闻与事件分析：')
    return '\n'.join(parts)


def run(
    api_key: str,
    code: str,
    name: str,
    stock_news: list[dict] | None = None,
    context: dict | None = None,
    **_: Any,
) -> dict:
    variable = _build_variable_data(code, name, stock_news, context)
    user_msg = build_cached_user_message(_STABLE_PREFIX, variable)
    out, _reasoning = call_pro(_SYSTEM, user_msg, api_key, max_tokens=1500,
                               response_format={'type': 'json_object'})
    parsed = parse_agent_json(out, '事件催化')
    if parsed.get('confidence', 0) <= 0:
        return _event_fallback(stock_news, context)
    return parsed
