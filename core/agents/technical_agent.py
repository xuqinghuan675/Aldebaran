# Inspired by TradingAgents v0.2.4 (Apache-2.0)
"""Agent4 — 技术面分析师。

输入：K 线指标摘要
输出：结构化 JSON（stance / summary / signals / intel_refs）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import (
    build_cached_user_message, call_pro, AGENT_JSON_SCHEMA, STRICT_SOURCE_GROUNDING, parse_agent_json,
)

_SYSTEM = """\
你是A股短线技术交易员，服务于个人手动交易者。你的目标不是预测神话，而是把K线、成交量、换手率、均线结构翻译成可执行的交易条件。

任务：阅读 K 线指标摘要、技术结构画像、短线交易模式，判断短线1-3日和中期5-15日的技术状态，并给出触发价、失效价、目标价与风险反证。

分析顺序必须严格遵守：
1. 先看趋势骨架：收盘价相对 MA20/MA40/MA60 的位置，MA20/40/60 是否多头排列、空头排列、粘合、发散，MA20斜率是否向上/向下。
2. 再看K线位置：当前价靠近20日高点、20日低点、近5日低点、布林上轨/下轨、平台上沿/下沿还是均线附近。
3. 再看量价：成交量、成交额、量比、可得换手率；判断放量突破、缩量回踩、放量下跌、冲高回落、无量反弹、量价背离。
4. 再看动量：RSI、KDJ、MACD、ATR。RSI/KDJ超买只代表追高风险，超卖只代表反弹候选；都不能单独构成买点。
5. 最后落到交易结构：当前属于趋势突破、箱体突破、上升回踩、超跌反弹、事件催化候选、下跌回避中的哪一种。
6. 最后检查风险收益比：(目标价-当前价)/(当前价-失效价)。低于2.0时，不允许把技术面判为可买，只能判为观察或回避。

硬纪律：
- 必须区分「短线1-3日」和「中期5-15日」；短线与中期冲突时必须明说。
- 技术结构画像状态为“候选未触发”时，不得建议买入，stance 通常为 neutral，confidence 不得高于5。
- 技术结构画像状态为“回避/失败”时不得看多，必须强调禁止新增买入。
- 只有放量站上 entry_trigger 或关键压力位，才可称为可交易；否则只能观察。
- 接近触发价但量比不足，只能叫“临界机会”，不能叫“已触发买点”。
- 已触发但 RSI≥80、KDJ-J≥90、换手率≥20% 或出现风险提示时，必须写“已触发但高风险”，不得鼓励重仓追高。
- fail_level 是风控线，不可用其他无依据价格替代。
- 目标价必须来自 target_level 或明确压力位，不可凭感觉上调。
- 若换手率字段为暂无，必须写“换手率暂无”，不得编造。
- 输出必须中文，不得出现 setup_name/status 等英文内部字段。
- 技术行为标签是代码生成的确定性证据，只能解释其“疑似”含义，不得改写为“机构正在买入/庄家控盘/洗盘完成/出货确认”。
- 公开资金证据在本 Agent 仅作为简短旁证，不得展开龙虎榜、北向、大宗交易细节；详细资金判断交给资金流向 Agent。
- 大盘情绪阶段估计只允许降低置信度，不得因为 main_up/repair 提高置信度。

""" + STRICT_SOURCE_GROUNDING + "\n" + AGENT_JSON_SCHEMA + """

analyst 固定填 "技术面"。
evidence 必须列举 5-8 个技术证据，至少覆盖：均线结构、K线位置、量价/成交额/换手率、动量指标、关键价位、风险收益比。value 必须给具体数值。
counter_evidence 必须列出 2-4 个反方向技术信号或风险，必须包含“为什么现在不能直接买/卖”的技术理由。禁止使用"暂无明确反方信号"等占位符。
intel_refs 固定为空列表 []。
summary 300-600 字，必须用交易结构语言展开：短线判断、中期判断、均线/位置/量价/动量的取舍、触发价、失效价、目标价、为什么现在可做或不可做。
额外输出 strategy_report 对象：selected_strategy、strategy_family、stage、buy_ready、reason、trigger_conditions、missing_conditions、invalidation。该字段必须来自输入的“超短线策略原型”，不能自行发明；buy_ready=false 时不得在 summary 中写“可以买”。

特别注意：当 K 线数据缺失时，stance 填 neutral，evidence 填入"数据缺失无法判断"，基于当前价格给出合理的技术估值区间。不可输出"获取失败"或空 JSON。\
"""

_STABLE_PREFIX = """\
[Agent4 · 技术面分析师]
请基于下方"当前数据"做技术面分析。重点使用K线结构、成交量/成交额/换手率、MA20/MA40/MA60、支撑压力、触发价和失效价；所有价位都要给具体数值。\
"""


def _build_variable_data(
    code: str,
    name: str,
    current_price: float | None,
    context: dict,
) -> str:
    parts = [
        '【当前数据】',
        f'对象：{name}（{code}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]
    if current_price:
        parts.append(f'▶ 当前价格：{current_price:.3f} 元')

    kline = context.get('kline_summary')
    if kline:
        parts.append(kline)
    else:
        parts.append('▶ 技术指标：暂无 K 线数据')

    profile = context.get('technical_profile')
    if profile:
        parts.append('\n▶ 技术结构画像：')
        parts.append(str(profile))

    tags = (profile or {}).get('behavior_tags') if isinstance(profile, dict) else None
    if tags:
        # 按置信度降序，确保高把握标签（含背离/回踩等指标信号）不被低分标签挤出
        sorted_tags = sorted(
            tags, key=lambda t: int(t.get('confidence') or 0) if isinstance(t, dict) else 0,
            reverse=True,
        )
        parts.append('\n▶ 技术行为标签（代码生成，仅作疑似解释，不是买卖结论）：')
        parts.append(str(sorted_tags[:6]))

    market_phase = context.get('market_phase')
    if market_phase:
        parts.append(f'\n▶ 大盘情绪阶段估计：{market_phase}（仅用于降低置信度，不用于加分）')

    public_evidence = context.get('public_fund_evidence') or {}
    if isinstance(public_evidence, dict) and public_evidence.get('available'):
        lhb = public_evidence.get('lhb') or {}
        hsgt = public_evidence.get('hsgt') or {}
        brief = []
        if lhb.get('hit'):
            brief.append(f"龙虎榜{lhb.get('side') or 'mixed'}")
        if hsgt.get('hit'):
            brief.append(f"北向{hsgt.get('direction') or 'flat'}")
        if brief:
            parts.append('\n▶ 公开资金证据简述：' + '，'.join(brief) + '；仅作技术旁证，详细判断见资金流向 Agent。')

    trade_setup = context.get('trade_setup')
    if trade_setup:
        parts.append('\n▶ 短线交易模式：')
        parts.append(str(trade_setup))

    strategy_profile = context.get('strategy_profile')
    if strategy_profile:
        parts.append('\n▶ 超短线策略原型（代码生成，只解释形态阶段，不直接生成买卖结论）：')
        parts.append(str(strategy_profile))

    parts.append('\n请输出技术面分析：')
    return '\n'.join(parts)


def _strategy_report_from_context(context: dict) -> dict | None:
    profile = context.get('strategy_profile')
    if not isinstance(profile, dict) or not profile:
        return None
    candidates = profile.get('strategy_candidates') if isinstance(profile.get('strategy_candidates'), list) else []
    selected = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    report = {
        'selected_strategy': profile.get('strategy_name') or selected.get('strategy_name'),
        'strategy_family': profile.get('strategy_family') or selected.get('strategy_family'),
        'stage': profile.get('stage') or selected.get('stage'),
        'buy_ready': bool(profile.get('buy_ready') or selected.get('buy_ready')),
        'reason': selected.get('reason') or profile.get('action_hint'),
        'trigger_conditions': profile.get('trigger_conditions') or selected.get('trigger_conditions') or [],
        'missing_conditions': profile.get('missing_conditions') or selected.get('missing_conditions') or [],
        'invalidation': profile.get('invalidation') or selected.get('invalidation'),
    }
    return {k: v for k, v in report.items() if v is not None}


def _fallback_from_context(current_price: float | None, context: dict) -> dict:
    """LLM 输出不可解析时，用本地技术画像生成中文兜底，避免误报数据获取失败。"""
    setup_map = {
        'trend_breakout': '趋势突破',
        'box_breakout': '箱体突破',
        'pullback_buy': '上升回踩',
        'oversold_rebound': '超跌反弹',
        'event_momentum': '事件催化',
        'downtrend_avoid': '下跌回避',
        'trend_continuation': '强趋势延续',
    }
    status_map = {'candidate': '候选未触发', 'triggered': '已触发', 'failed': '已失败', 'avoid': '禁止买入'}
    trend_map = {'uptrend': '上升趋势', 'downtrend': '下降趋势', 'recovery': '修复趋势', 'sideways': '震荡'}
    profile = context.get('technical_profile') or {}
    price = current_price or profile.get('close')
    if profile.get('available'):
        setup = profile.get('setup_name') or 'event_momentum'
        status = profile.get('status') or 'candidate'
        trend_stage = profile.get('trend_stage')
        setup_cn = setup_map.get(setup, setup)
        status_cn = status_map.get(status, status)
        trend_cn = trend_map.get(trend_stage, trend_stage or '不明')
        entry = profile.get('entry_trigger') or '—'
        fail = profile.get('fail_level') or '—'
        target = profile.get('target_level') or '—'
        risk_flags = profile.get('risk_flags') or []
        ma = profile.get('ma') or {}
        indicators = profile.get('indicators') or {}
        volume = profile.get('volume') or {}
        ranges = profile.get('range') or {}
        support = profile.get('support_levels') or []
        resistance = profile.get('resistance_levels') or []
        tags = profile.get('behavior_tags') or []
        tag_names = [str(item.get('tag') or '') for item in tags if isinstance(item, dict)]
        market_phase = context.get('market_phase', '')
        has_data_issue = any(name in tag_names for name in ('数据不足', '数据异常'))
        is_decline_env = market_phase in ('ice', 'decline', 'retreat')

        stance = 'neutral'
        confidence = 4
        if status == 'triggered':
            stance, confidence = 'bullish', 5
        elif status in ('avoid', 'failed') or setup == 'downtrend_avoid':
            stance, confidence = 'bearish', 5
        elif status == 'candidate' and not has_data_issue and not is_decline_env:
            if trend_stage in ('uptrend', 'recovery') and setup != 'downtrend_avoid':
                stance, confidence = 'bullish', 5
        amount = volume.get('amount')
        amount_text = f'{amount / 100000000:.2f}亿' if isinstance(amount, (int, float)) else '暂无'
        if stance == 'bullish' and status == 'candidate':
            summary = f'短线{status_cn}，中期{trend_cn}；偏多但未触发，不能直接买。触发价{entry}，失效价{fail}，目标{target}。'
        else:
            summary = f'短线{status_cn}，中期{trend_cn}；触发价{entry}，失效价{fail}，目标{target}。'
        evidence = [
            {'name': '短线模式', 'value': f'{setup_cn}/{status_cn}', 'weight': 'high'},
            {
                'name': '趋势结构',
                'value': (
                    f'收盘{profile.get("close")}，MA5={ma.get("ma5")}，MA20={ma.get("ma20")}，'
                    f'MA40={ma.get("ma40")}，MA60={ma.get("ma60")}，MA20斜率={ma.get("ma20_slope_pct")}%'
                ),
                'weight': 'high',
            },
            {
                'name': '动量指标',
                'value': (
                    f'RSI14={indicators.get("rsi14")}，KDJ-J={indicators.get("kdj_j")}，'
                    f'MACD={indicators.get("macd_trend")}，ATR14={indicators.get("atr14")}'
                ),
                'weight': 'medium',
            },
            {
                'name': '量价结构',
                'value': (
                    f'量比={volume.get("volume_ratio")}，成交额={amount_text}，换手率={volume.get("turnover") or "暂无"}，'
                    f'20日高点={ranges.get("high20")}，'
                    f'20日低点={ranges.get("low20")}，近5日低点={ranges.get("low5")}'
                ),
                'weight': 'medium',
            },
            {'name': '关键价位', 'value': f'触发{entry}，失效{fail}，目标{target}', 'weight': 'high'},
        ]
        counter = []
        if risk_flags:
            counter.append({'name': '风险标记', 'value': '、'.join(risk_flags), 'weight': 'medium'})
        if status == 'candidate':
            counter.append({
                'name': '未触发风险',
                'value': f'当前尚未放量站上触发价{entry}，候选形态不能直接当作买点',
                'weight': 'high',
            })
        elif status in ('avoid', 'failed'):
            counter.append({
                'name': '禁止买入',
                'value': f'模式状态为{status_cn}，新增买入需要等待结构重新修复',
                'weight': 'high',
            })
        rsi = indicators.get('rsi14')
        kdj_j = indicators.get('kdj_j')
        if isinstance(rsi, (int, float)) and rsi >= 70:
            counter.append({'name': '短线超买', 'value': f'RSI14={rsi}，追高回撤风险上升', 'weight': 'medium'})
        elif isinstance(kdj_j, (int, float)) and kdj_j >= 85:
            counter.append({'name': 'KDJ高位', 'value': f'KDJ-J={kdj_j}，短线可能钝化或回落', 'weight': 'medium'})
        vol_ratio = volume.get('volume_ratio')
        if isinstance(vol_ratio, (int, float)) and vol_ratio < 0.8 and status == 'candidate':
            counter.append({'name': '量能未确认', 'value': f'量比={vol_ratio}，突破前缺少放量确认', 'weight': 'medium'})
        if resistance:
            counter.append({'name': '上方压力', 'value': '、'.join(map(str, resistance[:3])), 'weight': 'medium'})
        if support:
            evidence.append({'name': '下方支撑', 'value': '、'.join(map(str, support[:3])), 'weight': 'medium'})
        return {
            'analyst': '技术面',
            'stance': stance,
            'summary': summary[:200],
            'evidence': evidence,
            'counter_evidence': counter,
            'intel_refs': [],
            'confidence': confidence,
            'strategy_report': _strategy_report_from_context(context) or {},
        }
    ref = f'{price:.3f}' if isinstance(price, (int, float)) else '未知'
    return {
        'analyst': '技术面',
        'stance': 'neutral',
        'summary': f'技术画像不可用，当前价{ref}，短线不生成买点，等待K线恢复后重评。',
        'evidence': [{'name': '技术数据状态', 'value': profile.get('reason') or 'K线数据缺失', 'weight': 'high'}],
        'counter_evidence': [{'name': '交易风险', 'value': '缺少触发价与失效价，不能作为买入依据', 'weight': 'high'}],
        'intel_refs': [],
        'confidence': 0,
        'strategy_report': _strategy_report_from_context(context) or {},
    }


def _contradicts_local_ma5(text: str, current_price: float | None, context: dict) -> bool:
    tp = context.get('technical_profile') or {}
    ma = tp.get('ma') if isinstance(tp.get('ma'), dict) else {}
    try:
        ma5 = float(ma.get('ma5'))
        close = float(current_price if current_price else tp.get('close'))
    except (TypeError, ValueError):
        return False
    return close >= ma5 and ('未站上MA5' in text or '未能站上MA5' in text or '跌破MA5' in text)


def _merge_local_profile(parsed: dict, current_price: float | None, context: dict) -> dict:
    """技术 Agent 以本地画像为锚，避免 LLM 与确定性短线模式互相打架。"""
    local = _fallback_from_context(current_price, context)
    if not (context.get('technical_profile') or {}).get('available'):
        return local if parsed.get('confidence', 0) <= 0 else parsed
    if parsed.get('confidence', 0) <= 0 or not str(parsed.get('summary') or '').strip():
        return local

    merged = dict(parsed)
    merged['analyst'] = '技术面'
    parsed_summary = str(parsed.get('summary') or '').strip()
    local_summary = str(local.get('summary') or '').strip()
    if (parsed_summary and parsed_summary not in local_summary
            and 'setup_name' not in parsed_summary and 'status' not in parsed_summary
            and not _contradicts_local_ma5(parsed_summary, current_price, context)):
        merged['summary'] = f'{local_summary} {parsed_summary}'[:650]
    else:
        merged['summary'] = local_summary[:650]
    merged['stance'] = local['stance']
    merged['confidence'] = max(int(parsed.get('confidence') or 0), int(local.get('confidence') or 0))
    status = (context.get('technical_profile') or {}).get('status')
    if status == 'candidate':
        merged['confidence'] = min(int(merged.get('confidence') or 0), 5)
    elif status in ('avoid', 'failed'):
        merged['confidence'] = min(int(merged.get('confidence') or 0), 6)

    seen = set()
    evidence = []
    for item in (local.get('evidence') or []) + (parsed.get('evidence') or parsed.get('signals') or []):
        name = str(item.get('name', '')).strip()
        if name and name not in seen:
            evidence.append(item)
            seen.add(name)
    merged['evidence'] = evidence[:8]

    seen = set()
    counter = []
    for item in (local.get('counter_evidence') or []) + (parsed.get('counter_evidence') or []):
        name = str(item.get('name', '')).strip()
        value = str(item.get('value', '')).strip()
        risk_key = name
        if 'RSI' in (name + value):
            risk_key = 'RSI风险'
        elif 'KDJ' in (name + value):
            risk_key = 'KDJ风险'
        elif '量比' in value or '放量' in value or '缩量' in value:
            risk_key = '量能风险'
        placeholder = (
            '未见明确风险标记' in value
            or '暂无明确反方信号' in value
            or '暂无明确风险' in value
        )
        if name and risk_key not in seen and not placeholder:
            counter.append(item)
            seen.add(risk_key)
    merged['counter_evidence'] = counter[:4]
    merged['intel_refs'] = []
    merged['strategy_report'] = _strategy_report_from_context(context) or parsed.get('strategy_report') or {}
    return merged


def _clean_behavior_words(value: Any) -> Any:
    replacements = {
        '机构正在买入': '资金行为仍需验证',
        '庄家': '资金行为仍需验证',
        '洗盘完成': '疑似洗盘仍需验证',
        '游资确认买入': '席位线索仍需验证',
        '知名游资': '席位观察名单',
    }
    if isinstance(value, dict):
        return {k: _clean_behavior_words(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_behavior_words(v) for v in value]
    if isinstance(value, str):
        text = value
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text
    return value


def _apply_behavior_tag_limits(parsed: dict, context: dict) -> dict:
    out = _clean_behavior_words(dict(parsed or {}))
    profile = context.get('technical_profile') or {}
    tags = profile.get('behavior_tags') or []
    tag_names = [str(item.get('tag') or '') for item in tags if isinstance(item, dict)]
    if '疑似启动初段' in tag_names:
        max_conf = max((int(item.get('confidence') or 0) for item in tags if isinstance(item, dict) and item.get('tag') == '疑似启动初段'), default=6)
        out['confidence'] = min(int(out.get('confidence') or 0), max_conf)
    if any(name in tag_names for name in ('数据不足', '数据异常')):
        out['stance'] = 'neutral'
        out['confidence'] = min(int(out.get('confidence') or 0), 4)
    if context.get('market_phase') in ('ice', 'decline', 'retreat'):
        out['confidence'] = min(int(out.get('confidence') or 0), 6)
        if out.get('stance') == 'bullish':
            out['stance'] = 'neutral'
    return out


def run(
    api_key: str,
    code: str,
    name: str,
    current_price: float | None,
    context: dict,
    **_: Any,
) -> dict:
    variable = _build_variable_data(code, name, current_price, context)
    user_msg = build_cached_user_message(_STABLE_PREFIX, variable)
    out, reasoning = call_pro(_SYSTEM, user_msg, api_key, max_tokens=3000, temperature=0.1,
                              response_format={'type': 'json_object'})
    parsed = parse_agent_json(out, '技术面')
    if reasoning:
        parsed['_reasoning'] = reasoning
    profile_available = bool((context.get('technical_profile') or {}).get('available'))
    low_quality = (
        parsed.get('confidence', 0) <= 0
        or not str(parsed.get('summary') or '').strip()
        or not (parsed.get('evidence') or parsed.get('signals'))
    )
    if low_quality and (profile_available or '获取失败' in str(parsed.get('summary', ''))):
        return _apply_behavior_tag_limits(_fallback_from_context(current_price, context), context)
    if profile_available:
        return _apply_behavior_tag_limits(_merge_local_profile(parsed, current_price, context), context)
    return _apply_behavior_tag_limits(parsed, context)
