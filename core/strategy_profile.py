"""Deterministic short-term strategy profile translation.

This module names the trading pattern in user-readable language and provides
strategy knowledge for Agent/F/G prompts. It does not emit buy/sell categories
and must not bypass the opportunity/blueprint contract.
"""
from __future__ import annotations


def _num(value) -> float | None:
    try:
        n = float(value)
    except Exception:
        return None
    if n <= 0:
        return None
    return n


def _flow_num(value) -> float:
    try:
        n = float(value)
    except Exception:
        return 0.0
    if n != n:
        return 0.0
    return n


def _fmt(value) -> str:
    n = _num(value)
    if n is None:
        return '—'
    if float(n).is_integer():
        return f'{n:.1f}'
    return f'{n:g}'


def _stage(status: str | None) -> str:
    if status == 'triggered':
        return 'triggered'
    if status in ('avoid', 'failed'):
        return 'failed'
    return 'forming'


_KNOWLEDGE = {
    'pullback_low_absorb': [
        '适用前提：中短期趋势仍在上行，回踩接近均线或平台支撑，成交量缩小，不能是放量破位。',
        '触发条件：回踩不破防守位后重新站回短均线或关键价，量价企稳，资金流出不能继续扩大。',
        '失效条件：跌破防守位、放量阴线、冲高回落破结构，低吸假设失效。',
        '现实约束：低吸不是左侧抄底，只允许轻仓试错，并且要服从机会等级和风险收益比。',
    ],
    'breakout_momentum': [
        '适用前提：价格在平台或趋势压力位下方蓄势，突破位清晰，突破前不能已经严重过热。',
        '触发条件：放量站上触发价并保持在突破位上方，突破后不快速跌回平台。',
        '失效条件：突破无量、长上影冲高回落、跌回突破位下方，视为假突破风险。',
        '现实约束：突破策略更怕追高，触发后也要检查换手、RSI/KDJ、事件兑现和风险收益比。',
    ],
    'oversold_rebound': [
        '适用前提：短线跌幅已经释放，但反弹必须有价格和成交量确认，不能把超卖指标直接当买点。',
        '触发条件：放量站回关键价或短均线，弱反弹转为可验证修复。',
        '失效条件：反弹无量、再创新低、跌破防守位，说明修复失败。',
        '现实约束：超跌反弹多为快进快出，趋势未修复前不按趋势买点处理。',
    ],
    'event_catalyst_watch': [
        '适用前提：存在近期相关事件或题材催化，但事件必须被价格、量能和资金验证。',
        '触发条件：事件后站上关键价，成交量放大，且未出现利好兑现后的冲高回落。',
        '失效条件：事件未获市场验证、情报过旧或相关性弱、放量下跌。',
        '现实约束：事件只能提高关注优先级，不能替代技术触发和风控线。',
    ],
    'trend_follow_momentum': [
        '适用前提：个股处于多头排列主升浪、站稳短均线，且属于当前主线/主力资金抱团方向，不是孤立拉升。',
        '触发条件：趋势延续且站稳MA5，同时主力资金净流入或板块走强或RS领先或相关情报催化之一成立，量价不背离。',
        '失效条件：跌破MA5/失效价、放量长上影冲高回落、主力资金转为持续流出、板块退潮。',
        '现实约束：趋势跟随是追强势，最怕追在见顶日，只允许轻仓跟随并严格按失效价止损，服从机会等级与盈亏比。',
    ],
    'etf_trend_follow': [
        'ETF 是一篮子、跟踪板块或指数贝塔，不具个股领涨属性。',
        '看多基于自身多头排列、站稳短均线、板块走强与量能。',
    ],
    'risk_avoid': [
        '适用前提：结构已经走弱、关键位失守、候选失败或资金/量价出现明显反证。',
        '触发条件：风险策略没有买入触发，只能等待重新站回关键价后重评。',
        '失效条件：重新站回关键价并获得量价、资金、市场环境共同验证。',
        '现实约束：回避不是看空做空建议；对无持仓用户只表达不买和等待。',
    ],
}


_NAMES = {
    'pullback_low_absorb': ('强趋势缩量回踩低吸', '龙回头/强趋势回踩低吸一类打法'),
    'breakout_momentum': ('放量平台/趋势突破', '平台突破/箱体突破一类打法'),
    'oversold_rebound': ('超跌反弹确认', '超跌修复/反弹试错一类打法'),
    'event_catalyst_watch': ('事件催化观察', '事件驱动候选一类打法'),
    'trend_follow_momentum': ('强趋势延续/主线龙头跟随', '龙头趋势跟随/主升浪持有一类打法'),
    'etf_trend_follow': ('板块趋势延续/指数贝塔跟随', '跟踪板块或指数趋势的一篮子持有'),
    'risk_avoid': ('下跌回避/失败结构', '破位回避一类纪律'),
}


_NAMES_ETF = {
    'etf_trend_follow': ('板块趋势延续/指数贝塔跟随', '跟踪板块或指数趋势的一篮子持有'),
    'trend_follow_momentum': ('板块趋势延续/指数贝塔跟随', '跟踪板块或指数趋势的一篮子持有'),
    'pullback_low_absorb': ('板块缩量回踩低吸', '指数/板块强趋势回踩低吸一类打法'),
}

_KNOWLEDGE_ETF = {
    'etf_trend_follow': [
        '适用前提：ETF 是一篮子、跟踪板块或指数贝塔，不具个股领涨属性。',
        '看多基于自身多头排列、站稳短均线、板块走强与量能。',
    ],
    'trend_follow_momentum': [
        '适用前提：ETF 是一篮子、跟踪板块或指数贝塔，不具个股领涨属性。',
        '看多基于自身多头排列、站稳短均线、板块走强与量能。',
    ],
    'pullback_low_absorb': [
        'ETF 缩量回踩至支撑且板块未走坏可低吸，不套用个股资金抱团语义。',
    ],
}


def _candidate(
    family: str,
    *,
    stage: str,
    score: int,
    reason: str,
    trigger_conditions: list[str],
    invalidation: str,
    missing_conditions: list[str] | None = None,
    buy_ready: bool = False,
) -> dict:
    name, similar = _NAMES[family]
    return {
        'strategy_family': family,
        'strategy_name': name,
        'similar_to': similar,
        'stage': stage,
        'match_score': max(0, min(100, int(score))),
        'buy_ready': bool(buy_ready),
        'reason': reason,
        'missing_conditions': [x for x in (missing_conditions or []) if x],
        'trigger_conditions': [x for x in trigger_conditions if x],
        'invalidation': invalidation,
        'strategy_knowledge': _KNOWLEDGE[family],
    }


def _flow_is_negative(flow: dict) -> bool:
    if not isinstance(flow, dict):
        return False
    main_flow = _flow_num(flow.get('main_5d') or flow.get('main_3d') or flow.get('main_net'))
    small_flow = _flow_num(flow.get('small_5d') or flow.get('small_3d') or flow.get('small_net'))
    return main_flow < 0 and small_flow > 0


def _flow_is_strong_inflow(flow: dict) -> bool:
    if not isinstance(flow, dict):
        return False
    main_flow = _flow_num(flow.get('main_5d') or flow.get('main_3d') or flow.get('main_net'))
    return main_flow > 0


def derive_strategy_profile(
    context: dict | None,
    trade_setup: dict | None = None,
    opportunity_profile: dict | None = None,
) -> dict:
    """Build a non-authoritative strategy profile for prompts, reports, and tracking."""
    context = context or {}
    trade_setup = trade_setup or {}
    tech = context.get('technical_profile') or {}
    flow = context.get('flow_profile') or {}
    opportunity_profile = opportunity_profile or {}

    setup_name = trade_setup.get('setup_name') or tech.get('setup_name') or 'event_momentum'
    status = trade_setup.get('status') or tech.get('status') or 'candidate'
    stage = _stage(status)
    entry = trade_setup.get('entry_trigger') or tech.get('entry_trigger')
    fail = trade_setup.get('fail_level') or tech.get('fail_level')
    target = trade_setup.get('target_level') or tech.get('target_level')
    volume_ratio = tech.get('volume_ratio')
    flow_negative = _flow_is_negative(flow)
    grade = str(opportunity_profile.get('opportunity_grade') or '').upper()
    is_etf_flag = bool((context.get('mainline_fit') or {}).get('is_etf'))

    missing: list[str] = []
    current: list[str] = []
    triggers: list[str] = []
    candidates: list[dict] = []

    # 失败/回避结构统一归 risk_avoid，避免 oversold/pullback 等形态名残留污染分策略统计
    if stage == 'failed':
        setup_name = 'downtrend_avoid'

    if setup_name == 'pullback_buy':
        family = 'pullback_low_absorb'
        current = ['强趋势回踩']
        if volume_ratio:
            current.append(f'量比{volume_ratio}，缩量回踩不等于突破放量')
        if stage == 'triggered':
            current.append('企稳确认')
            action = '已触发但仍需证据闸确认；只允许轻仓试错，不得重仓追高'
        elif stage == 'failed':
            action = '回避，低吸结构已经失败'
        else:
            action = '观察，形成中不买，等待企稳触发'
            missing.append(f'回踩不破{_fmt(fail)}并出现企稳确认')
        if flow_negative:
            missing.append('资金流出未解除')
        triggers = [f'回踩不破{_fmt(fail)}，收回MA5或放量转强']
        invalid = f'跌破{_fmt(fail)}或出现放量阴线/长上影冲高回落'
        buy_ready = stage == 'triggered' and grade not in ('C', 'D') and not flow_negative
        candidates.append(_candidate(
            family,
            stage=stage,
            score=92 if stage == 'triggered' else 72,
            reason='强趋势缩量回踩且企稳确认' if stage == 'triggered' else '强趋势回踩正在形成，触发条件尚未完整',
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing,
            buy_ready=buy_ready,
        ))
        candidates.append(_candidate(
            'breakout_momentum',
            stage='forming',
            score=55,
            reason='若回踩后再放量越过前高，可转为突破动量策略观察',
            trigger_conditions=[f'放量站上{_fmt(entry)}且不跌回触发位'],
            invalidation=f'跌破{_fmt(fail)}或突破后快速回落',
            missing_conditions=[f'尚未形成放量站上{_fmt(entry)}的突破确认'],
            buy_ready=False,
        ))
        evidence_level = 'strong' if buy_ready else 'medium'
    elif setup_name in ('trend_breakout', 'box_breakout'):
        family = 'breakout_momentum'
        current = ['接近或站上关键突破位']
        if volume_ratio:
            current.append(f'量比{volume_ratio}')
        if stage == 'triggered':
            action = '已触发；仍需量能、资金和风险收益比确认'
            missing = []
        elif stage == 'failed':
            action = '回避，突破结构失败'
            missing = []
        else:
            action = '观察，未放量站上触发位前不买'
            missing = [f'放量站上{_fmt(entry)}']
        triggers = [f'放量站上{_fmt(entry)}，且不跌回触发位']
        invalid = f'跌破{_fmt(fail)}或突破后快速跌回平台'
        buy_ready = stage == 'triggered' and grade not in ('C', 'D')
        candidates.append(_candidate(
            family,
            stage=stage,
            score=90 if stage == 'triggered' else 70,
            reason='关键价突破获得价格确认' if stage == 'triggered' else '突破位清晰但尚未完成放量确认',
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing,
            buy_ready=buy_ready,
        ))
        candidates.append(_candidate(
            'pullback_low_absorb',
            stage='forming',
            score=52,
            reason='若突破失败但回踩不破支撑，后续可转为回踩低吸观察',
            trigger_conditions=[f'回踩不破{_fmt(fail)}后重新转强'],
            invalidation=f'跌破{_fmt(fail)}',
            missing_conditions=[f'尚未出现回踩不破{_fmt(fail)}后的企稳确认'],
            buy_ready=False,
        ))
        evidence_level = 'strong' if buy_ready else 'medium'
    elif setup_name == 'trend_continuation':
        mf = context.get('mainline_fit') if isinstance(context.get('mainline_fit'), dict) else {}
        if is_etf_flag:
            family = 'etf_trend_follow'
            current = ['板块多头排列', '站稳短均线']
            if volume_ratio:
                current.append(f'量比{volume_ratio}')
            fit_signals: list[str] = []
            if mf.get('sector_strong'):
                fit_signals.append('板块走强')
            if mf.get('rs_lead'):
                fit_signals.append('相对强势跑赢大盘')
            if _flow_is_strong_inflow(flow):
                fit_signals.append('资金净流入')
            trend_ok = bool(fit_signals) or bool(tech.get('trend_stage') in ('uptrend', 'recovery'))
            if fit_signals:
                current.append('趋势确认：' + '、'.join(fit_signals))
            triggers = [f'站稳MA5与{_fmt(entry)}，板块未走坏']
            invalid = f'跌破{_fmt(fail)}或MA5、板块转弱、放量长上影回落'
            buy_ready = stage == 'triggered' and grade not in ('C', 'D') and not flow_negative and trend_ok
            if stage == 'triggered' and not trend_ok:
                missing.append('板块趋势确认未成立（需板块走强/相对强势/资金净流入/自身多头结构之一）')
            if flow_negative:
                missing.append('资金流出未解除')
            if stage == 'triggered':
                action = ('板块趋势延续确认，可轻仓跟随ETF，仍需盈亏比与F层确认'
                          if trend_ok else 'ETF走强但板块趋势确认未成立，先观察不追高')
            else:
                action = '观察，板块趋势延续未确认前不买'
            reason = '板块趋势延续且确认成立' if buy_ready else '板块趋势延续但确认或证据待补'
        else:
            family = 'trend_follow_momentum'
            current = ['多头排列主升浪', '站稳短均线']
            if volume_ratio:
                current.append(f'量比{volume_ratio}')
            fit_signals = []
            if mf.get('major_mainline'):
                fit_signals.append('大主线AI算力链')
            elif mf.get('minor_mainline'):
                fit_signals.append('小主线题材')
            if _flow_is_strong_inflow(flow):
                fit_signals.append('主力资金净流入')
            if mf.get('rs_lead'):
                fit_signals.append('RS领先大盘')
            if mf.get('sector_strong'):
                fit_signals.append('板块走强')
            if mf.get('intel_hit'):
                fit_signals.append('相关情报催化')
            mainline_fit = bool(fit_signals)
            if mainline_fit:
                current.append('主线契合：' + '、'.join(fit_signals))
            triggers = [f'站稳MA5与{_fmt(entry)}，主力资金不转为流出']
            invalid = f'跌破{_fmt(fail)}或MA5、放量长上影冲高回落、主力资金持续流出'
            buy_ready = stage == 'triggered' and grade not in ('C', 'D') and not flow_negative and mainline_fit
            if stage == 'triggered' and not mainline_fit:
                missing.append('主线契合未确认（需主力净流入/板块走强/RS领先/相关情报之一）')
            if flow_negative:
                missing.append('资金流出未解除')
            if stage == 'triggered':
                action = ('已触发强趋势延续；主线契合，可轻仓跟随，仍需盈亏比与F层确认'
                          if mainline_fit else '形态已走强但主线契合未确认，先观察不追高')
            else:
                action = '观察，趋势延续未确认前不买'
            reason = '强趋势延续且主线契合' if buy_ready else '强趋势延续但主线契合或证据待确认'
        candidates.append(_candidate(
            family,
            stage=stage,
            score=91 if buy_ready else 74,
            reason=reason,
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing,
            buy_ready=buy_ready,
        ))
        candidates.append(_candidate(
            'pullback_low_absorb',
            stage='forming',
            score=50,
            reason='若趋势中回踩不破支撑，可转为回踩低吸观察',
            trigger_conditions=[f'回踩不破{_fmt(fail)}后重新站上MA5'],
            invalidation=f'跌破{_fmt(fail)}',
            missing_conditions=['尚未出现回踩企稳确认'],
            buy_ready=False,
        ))
        evidence_level = 'strong' if buy_ready else 'medium'
    elif setup_name == 'oversold_rebound':
        family = 'oversold_rebound'
        current = ['超跌反弹候选']
        action = '观察，超跌不等于买点，必须等待量价确认'
        missing = [f'站回{_fmt(entry)}并获得量能确认']
        triggers = [f'放量站回{_fmt(entry)}']
        invalid = f'跌破{_fmt(fail)}或弱反弹后继续破位'
        buy_ready = stage == 'triggered' and grade not in ('C', 'D')
        candidates.append(_candidate(
            family,
            stage=stage,
            score=82 if stage == 'triggered' else 62,
            reason='超跌后出现可验证修复' if stage == 'triggered' else '仅是超跌候选，缺少量价触发',
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing if not buy_ready else [],
            buy_ready=buy_ready,
        ))
        candidates.append(_candidate(
            'risk_avoid',
            stage='forming',
            score=50,
            reason='若反弹无量或再创新低，应切换为回避纪律',
            trigger_conditions=[f'重新站回{_fmt(entry)}后再评估'],
            invalidation=f'跌破{_fmt(fail)}',
            missing_conditions=['反弹质量仍需验证'],
            buy_ready=False,
        ))
        evidence_level = 'weak' if not buy_ready else 'medium'
    elif setup_name == 'downtrend_avoid' or stage == 'failed':
        family = 'risk_avoid'
        current = list(trade_setup.get('risk_flags') or tech.get('risk_flags') or ['结构偏弱'])
        action = '回避，不新增买入，等待重新站回关键位'
        missing = ['技术结构修复并重新获得量价验证']
        triggers = [f'重新站回{_fmt(entry)}并获得资金/量能确认']
        invalid = '持续弱势或资金持续流出'
        buy_ready = False
        candidates.append(_candidate(
            family,
            stage='failed',
            score=88,
            reason='短线结构已失败或处于回避状态',
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing,
            buy_ready=False,
        ))
        candidates.append(_candidate(
            'event_catalyst_watch',
            stage='forming',
            score=35,
            reason='即便有事件，也必须等待价格重新验证',
            trigger_conditions=[f'事件后站回{_fmt(entry)}且量能转强'],
            invalidation='事件未获市场验证或继续放量下跌',
            missing_conditions=['缺少价格修复和量能确认'],
            buy_ready=False,
        ))
        evidence_level = 'strong'
    else:
        family = 'event_catalyst_watch'
        current = ['事件或结构候选']
        action = '观察，事件不替代量价和资金验证'
        missing = [f'站上{_fmt(entry)}并获得市场验证']
        triggers = [f'站上{_fmt(entry)}且资金不继续流出']
        invalid = f'跌破{_fmt(fail)}或事件未获市场验证'
        buy_ready = stage == 'triggered' and grade not in ('C', 'D') and not flow_negative
        candidates.append(_candidate(
            family,
            stage=stage,
            score=76 if stage == 'triggered' else 58,
            reason='事件线索需要市场验证' if stage != 'triggered' else '事件后获得初步价格确认',
            trigger_conditions=triggers,
            invalidation=invalid,
            missing_conditions=missing if not buy_ready else [],
            buy_ready=buy_ready,
        ))
        candidates.append(_candidate(
            'breakout_momentum',
            stage='forming',
            score=48,
            reason='若事件推动放量越过关键价，可转为突破策略',
            trigger_conditions=[f'放量站上{_fmt(entry)}'],
            invalidation=f'跌破{_fmt(fail)}或冲高回落',
            missing_conditions=[f'尚未放量站上{_fmt(entry)}'],
            buy_ready=False,
        ))
        evidence_level = 'medium' if buy_ready else 'weak'

    if is_etf_flag:
        for cand in candidates:
            fam = cand.get('strategy_family')
            if fam in _NAMES_ETF:
                cand['strategy_name'], cand['similar_to'] = _NAMES_ETF[fam]
            if fam in _KNOWLEDGE_ETF:
                cand['strategy_knowledge'] = _KNOWLEDGE_ETF[fam]
    candidates = sorted(candidates, key=lambda item: item.get('match_score', 0), reverse=True)
    selected = candidates[0]
    if opportunity_profile.get('opportunity_grade') in ('C', 'D'):
        action = opportunity_profile.get('not_holding_plan') or action
        selected['buy_ready'] = False
        buy_ready = False

    return {
        'strategy_family': selected['strategy_family'],
        'strategy_name': selected['strategy_name'],
        'similar_to': selected['similar_to'],
        'stage': selected['stage'],
        'buy_ready': bool(selected.get('buy_ready')),
        'evidence_level': evidence_level,
        'current_match': [x for x in current if x],
        'missing_conditions': [x for x in missing if x],
        'trigger_conditions': [x for x in triggers if x],
        'invalidation': invalid,
        'action_hint': action,
        'strategy_candidates': candidates[:4],
        'strategy_knowledge': selected.get('strategy_knowledge') or _KNOWLEDGE[selected['strategy_family']],
        'data_required': ['daily_kline', 'flow_profile', 'market_phase', 'intel_events'],
    }
