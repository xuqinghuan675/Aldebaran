# Inspired by TradingAgents v0.2.4 (Apache-2.0)
"""Agent1 — 市场环境分析师。

输入：宏观文本 / 大盘情绪 / 全球指数 / 板块环境
输出：结构化 JSON（stance / summary / signals / intel_refs）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import (
    build_cached_user_message, call_pro, AGENT_JSON_SCHEMA, STRICT_SOURCE_GROUNDING, parse_agent_json,
)

_SYSTEM = """\
你是市场环境分析师，服务于个人量化投资者。

任务：只评估大盘情绪、全球市场、行业/板块环境对目标股的外部影响，不分析个股公告和个股新闻催化。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "市场环境"。
evidence 列举 2-4 个支持当前方向的关键信号，必须是目标股所在行业/板块相关的大盘因子（如：行业政策、利率、汇率、商品价格、板块资金流向），value 含具体数值或量化描述。禁止仅列全球股指涨跌而不说明对目标股行业的传导逻辑。若确实无行业特定数据，至少分析大盘情绪对该股所属板块的典型影响路径。
counter_evidence 必须列出 1-2 个反方向风险信号（含具体数值或可量化描述）。禁止使用"暂无明确反方信号"等占位符。即使当前宏观数据全面向好，也必须指出1个潜在风险（如：政策转向、海外市场联动下跌、汇率波动、地缘冲突升级等），并说明该风险在当前数据中尚未体现但不可忽视的原因。不可留空。
intel_refs 列出本次引用的情报标题（≤30 字，最多 3 条）。
summary ≤80 字，语气客观，说明综合偏向及核心理由。\
"""

# 稳定前缀模板（不含变化数据）
_STABLE_PREFIX_TEMPLATE = """\
[Agent1 · 市场环境分析师]
请基于下方"当前数据"区域提供的信息进行分析。\
"""

_LVL_LBL = {'critical': '重大', 'important': '重要', 'info': '一般'}
_DIR_LBL = {'bullish': '利多▲', 'bearish': '利空▼', 'neutral': '中性→'}


def _format_global_brief(global_snap: dict) -> str:
    indices = global_snap.get('indices') if isinstance(global_snap, dict) else None
    if isinstance(indices, list) and indices:
        parts = []
        for item in indices[:4]:
            if not isinstance(item, dict):
                continue
            name = item.get('name') or item.get('symbol') or '指数'
            pct = item.get('pct')
            if isinstance(pct, (int, float)):
                parts.append(f'{name}{pct:+.2f}%')
            else:
                price = item.get('price')
                parts.append(f'{name}{price}' if price is not None else str(name))
        if parts:
            return '，'.join(parts)
    return str(global_snap)[:120]


def _build_variable_data(
    code: str,
    name: str,
    current_price: float | None,
    context: dict,
    intel_events: list,
) -> str:
    """构造变化数据后缀（每次调用都不同）。"""
    parts = [
        f'【当前数据】',
        f'对象：{name}（{code}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]

    # 宏观文本（可选）
    try:
        from core.macro_state import get_macro_context_text
        macro_text = get_macro_context_text()
        if macro_text:
            parts.append(macro_text)
    except Exception:
        pass

    if current_price:
        parts.append(f'▶ 当前价格：{current_price:.3f} 元')

    emotion = context.get('emotion')

    # 情绪锐化：仅在大盘情绪偏多 + 股价已破位时追加背离警示
    # （bullish 判定：涨停>=跌停*2 或 上涨家数>=下跌家数*1.5）
    if current_price and emotion:
        try:
            zt = int(emotion.get('zt') or 0)
            dt = int(emotion.get('dt') or 0)
            up = int(emotion.get('up') or 0)
            down = int(emotion.get('down') or 0)
            bullish = (zt >= max(dt * 2, 1)) or (up >= max(down * 3 // 2, 1))
            if bullish:
                import re
                kline_summary = context.get('kline_summary', '') or ''
                m5 = re.search(r'MA5(?!\d)[^\d.]*([\d.]+)', kline_summary)
                m20 = re.search(r'MA20(?!\d)[^\d.]*([\d.]+)', kline_summary)
                ma5 = float(m5.group(1)) if m5 else None
                ma20 = float(m20.group(1)) if m20 else None
                if ma5 and ma20 and current_price < ma5 and current_price < ma20:
                    parts.append(
                        f'▶ ⚠️ 情绪/价格背离：大盘情绪偏多，但股价已跌破 MA5'
                        f'（{ma5:.3f}）和 MA20（{ma20:.3f}），个股趋势独立走弱，回调风险较高'
                    )
                elif ma20 and current_price < ma20:
                    parts.append(
                        f'▶ ⚠️ 情绪/价格背离：大盘情绪偏多，但股价（{current_price:.3f}）'
                        f'已跌破 MA20（{ma20:.3f}），需警惕个股趋势转弱'
                    )
        except Exception:
            pass

    if emotion:
        try:
            from core.market_context_provider import format_emotion_text
            etxt = format_emotion_text(emotion)
            if etxt:
                parts.append(etxt)
        except Exception:
            pass

    global_snap = context.get('global')
    if global_snap:
        try:
            from core.market_context_provider import format_global_text
            gtxt = format_global_text(global_snap)
            if gtxt:
                parts.append(gtxt)
        except Exception:
            pass

    hot_rank = context.get('hot_rank')
    if hot_rank and isinstance(hot_rank, dict) and hot_rank.get('rank'):
        rank = int(hot_rank['rank'])
        change = str(hot_rank.get('change', '') or '').strip()
        change_note = f'（较昨日{change}）' if change and change != '0' else ''
        parts.append(f'▶ 东财人气榜：第 {rank} 名{change_note}（越小热度越高）')

    # 相关情报（最多 5 条）：越相关、越近、等级越高，越靠前。
    try:
        from core.intel_matcher import sort_intel_events_for_stock
        sorted_events = sort_intel_events_for_stock(
            intel_events or [], code, name,
            sector_hints=context.get('sectors') or [],
            max_n=5,
        )
    except Exception:
        sorted_events = sorted(
            intel_events or [],
            key=lambda e: {'critical': 0, 'important': 1, 'info': 2}.get(
                getattr(e, 'level', 'info'), 2
            ),
        )[:5]
    if sorted_events:
        parts.append(f'\n相关情报（{len(sorted_events)} 条）：')
        for ev in sorted_events:
            lvl = _LVL_LBL.get(getattr(ev, 'level', ''), '?')
            drct = _DIR_LBL.get(getattr(ev, 'direction', ''), '?')
            title = (getattr(ev, 'title', '') or '')[:60]
            tip = (getattr(ev, 'trading_tip', '') or '')[:40]
            day = getattr(ev, 'timestamp', '') or '未知日期'
            parts.append(f'  [{day}][{lvl}][{drct}] {title}')
            if tip:
                parts.append(f'    操作提示：{tip}')
    else:
        parts.append('\n相关情报：暂无')

    return '\n'.join(parts)


def run(
    api_key: str,
    code: str,
    name: str,
    current_price: float | None,
    context: dict,
    intel_events: list,
    **_: Any,
) -> dict:
    """运行 Agent1，返回结构化分析 dict。"""
    variable = _build_variable_data(code, name, current_price, context, intel_events)
    user_msg = build_cached_user_message(_STABLE_PREFIX_TEMPLATE, variable)
    out, _reasoning = call_pro(_SYSTEM, user_msg, api_key, max_tokens=3000,
                               response_format={'type': 'json_object'})
    parsed = parse_agent_json(out, '市场环境')
    has_local_context = bool(
        context.get('emotion') or context.get('global') or context.get('hot_rank')
        or context.get('fund_context') or context.get('kline_summary')
    )
    if parsed.get('confidence', 0) > 0 or not has_local_context:
        return parsed

    evidence = []
    emotion = context.get('emotion') or {}
    if isinstance(emotion, dict) and emotion:
        emotion_parts = []
        if emotion.get('zt') is not None:
            emotion_parts.append(f"涨停{emotion.get('zt')}家")
        if emotion.get('dt') is not None:
            emotion_parts.append(f"跌停{emotion.get('dt')}家")
        if emotion.get('_fallback') and not emotion.get('up') and not emotion.get('down'):
            emotion_parts.append('上涨/下跌家数缺失')
        else:
            if emotion.get('up') is not None:
                emotion_parts.append(f"上涨{emotion.get('up')}家")
            if emotion.get('down') is not None:
                emotion_parts.append(f"下跌{emotion.get('down')}家")
        evidence.append({
            'name': '大盘情绪',
            'value': '，'.join(emotion_parts) if emotion_parts else '大盘情绪数据暂不完整',
            'weight': 'medium',
        })
    global_snap = context.get('global') or {}
    if isinstance(global_snap, dict) and global_snap:
        evidence.append({
            'name': '外围市场',
            'value': _format_global_brief(global_snap),
            'weight': 'low',
        })
    hot_rank = context.get('hot_rank') or {}
    if isinstance(hot_rank, dict) and hot_rank.get('rank'):
        evidence.append({
            'name': '个股热度',
            'value': f"东财人气榜第{hot_rank.get('rank')}名，变化{hot_rank.get('change') or '暂无'}",
            'weight': 'low',
        })
    if context.get('fund_context'):
        evidence.append({'name': '板块环境', 'value': str(context.get('fund_context'))[:120], 'weight': 'medium'})
    counter = []
    if context.get('kline_summary'):
        counter.append({'name': '个股独立走势', 'value': '市场环境只能作为背景，仍需服从个股技术触发与资金验证', 'weight': 'high'})
    else:
        counter.append({'name': '数据边界', 'value': '缺少个股K线交叉验证，市场环境不单独构成买点', 'weight': 'high'})
    return {
        'analyst': '市场环境',
        'stance': 'neutral',
        'summary': '市场环境仅作背景参考，需结合个股技术触发、资金连续性和事件验证后再决策。',
        'evidence': evidence[:4],
        'counter_evidence': counter,
        'intel_refs': [],
        'confidence': 3,
    }
