"""Display wording helpers for tracking tasks."""
from __future__ import annotations

from core.data_source import fmt_price


def is_bearish_direction(direction: str) -> bool:
    return str(direction or '') in ('bearish', 'short', '看空', '空')


def is_bullish_direction(direction: str) -> bool:
    return str(direction or '') in ('bullish', 'long', '看多', '多')


def reconcile_view_to_category(
    category: str,
    bias_label: str,
    readiness: str,
    evaluation_scope: str,
    grade: str = '',
    f_layer_veto: bool = False,
) -> tuple[str, str, str]:
    if f_layer_veto:
        # 跳过 category 强还原，返回调用方已压好的值（非绕过所有归一）
        return bias_label, readiness, evaluation_scope
    grade = str(grade or '').strip().upper()
    if category == 'avoid':
        return '看跌回避', 'avoid', 'avoid_watch'
    if category == 'watch':
        return '中性震荡', 'weak', 'neutral_watch'
    if category == 'buy':
        if bias_label not in ('看多', '风险看多'):
            bias_label = '风险看多' if readiness == 'risk_triggered' else '看多'
        if readiness not in ('triggered', 'risk_triggered'):
            readiness = 'triggered'
        return bias_label, readiness, 'trade'
    if category == 'bullish_watch':
        if readiness in ('triggered', 'risk_triggered'):
            if bias_label not in ('看多', '风险看多'):
                bias_label = '风险看多' if readiness == 'risk_triggered' else '看多'
            return bias_label, readiness, 'trade'
        if bias_label not in ('临界看多', '看多观察'):
            bias_label = '临界看多' if grade == 'A' else '看多观察'
        if readiness not in ('near_trigger', 'candidate'):
            readiness = 'near_trigger' if grade == 'A' else 'candidate'
        return bias_label, readiness, 'direction_watch'
    return bias_label, readiness, evaluation_scope


def sample_type_label(sample_type: str) -> str:
    return '虚拟观察单'


def tracking_price_label(task: dict, detail: dict | None = None) -> str:
    """Label the stored entry_price according to tracking semantics."""
    detail = detail or {}
    if task.get('converted_from_watch'):
        return '入场价'
    return '入场价' if _effective_task_class(detail, task) == 'buy' else '分析时价'


def tracking_reference_price(record: dict) -> object:
    """Return the user-facing price baseline without changing stored semantics."""
    for key in ('entry_ref', 'entry_price', 'analysis_price', 'current_price'):
        value = record.get(key)
        if value not in (None, ''):
            return value
    return None


def format_price_or_dash(value, code=None) -> str:
    if value in (None, ''):
        return '—'
    try:
        if float(value) <= 0:
            return '—'
    except (TypeError, ValueError):
        return '—'
    return fmt_price(value, code)


def _pct(value) -> str:
    try:
        return f'{float(value):+.2f}%'
    except (TypeError, ValueError):
        return '+0.00%'


def _float_value(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRIGGER_LABEL = {
    'target': '止盈触发',
    'stop': '止损出局',
    'stop_same_day': '止损出局',
    'expire': '持有到期',
}


def _is_bullish_watch(task: dict) -> bool:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    return (bp.get('category') == 'bullish_watch'
            and not task.get('converted_from_watch')
            and not bp.get('f_layer_veto'))


def _effective_task_class(detail: dict, task: dict) -> str:
    tc = str(detail.get('task_class') or task.get('task_class') or '')
    if tc:
        return tc
    direction = str(detail.get('direction') or task.get('direction') or '')
    if is_bearish_direction(direction):
        return 'avoid'
    if is_bullish_direction(direction):
        return 'buy'
    return 'watch'


def format_directional_metric_lines(detail: dict, task: dict) -> list[str]:
    task_class = _effective_task_class(detail, task)
    final_pct = detail.get('final_pct', 0)
    peak_pct = detail.get('peak_pct', 0)
    trough_pct = detail.get('trough_pct', 0)
    try:
        fp = float(final_pct)
    except (TypeError, ValueError):
        fp = 0.0

    if task_class == 'buy':
        trigger = _TRIGGER_LABEL.get(str(detail.get('trigger') or ''), '持有到期')
        return [
            f'看多兑现: {_pct(final_pct)}（{trigger}）',
            f'期间最高: {_pct(peak_pct)}  最深回撤: {_pct(trough_pct)}',
        ]
    if task_class == 'avoid':
        tier = str(detail.get('outcome_tier') or '')
        if tier == 'drop_target':
            verdict = '盘中跌幅达标'
        elif tier == 'drop_half':
            verdict = '跌幅半程达标'
        else:
            verdict = '回避正确' if fp <= -1.5 else ('踏空' if fp >= 1.5 else '基本持平')
        return [
            f'看跌回避 → 实际 {_pct(final_pct)} → {verdict}',
            f'期间最高: {_pct(peak_pct)}  期间最低: {_pct(trough_pct)}',
        ]
    if _is_bullish_watch(task):
        grade = str(detail.get('grade') or '')
        verdict = {
            'B': '等对了（已上涨）', 'C': '横盘持平', 'D': '跌破失效（看错）',
        }.get(grade) or ('已上涨' if fp >= 1.5 else ('下跌' if fp <= -1.5 else '横盘'))
        return [
            f'看多观察 → 实际 {_pct(final_pct)} → {verdict}',
            f'期间最高: {_pct(peak_pct)}  最深回撤: {_pct(trough_pct)}',
        ]
    verdict = '躲过下跌' if fp <= -1.5 else ('错过机会' if fp >= 1.5 else '横盘合理')
    return [
        f'观望 → 实际 {_pct(final_pct)} → {verdict}',
        f'期间最高: {_pct(peak_pct)}  期间最低: {_pct(trough_pct)}',
    ]


def target_pct_label(direction: str) -> str:
    return '目标涨幅'


def stop_pct_label(direction: str) -> str:
    return '止损线'


def format_prediction_plan_line(task: dict) -> str:
    task_class = _effective_task_class({}, task)
    horizon = _int_value(task.get('horizon_days'), 0)
    code = task.get('code')
    if task_class == 'avoid':
        return f'看跌回避信号（不按目标/止损达标；实际跌=避对 / 涨=踏空）  周期: {horizon}日'
    if _is_bullish_watch(task):
        bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
        trigger = format_price_or_dash(bp.get('entry_trigger'), code)
        fail = format_price_or_dash(bp.get('fail_level'), code)
        return f'看多观察（涨破触发价 {trigger} 即买入；跌破失效价 {fail} 看错）  周期: {horizon}日'
    if task_class == 'watch':
        return f'中性观望（按预期区间/到期真实涨跌评观察是否合理）  周期: {horizon}日'
    target_pct = _float_value(task.get('target_pct'), 0.0)
    stop_pct = _float_value(task.get('stop_pct'), 0.0)
    if target_pct > 0 and stop_pct != 0:
        return f'目标涨幅: +{target_pct:.1f}%  止损线: -{abs(stop_pct):.1f}%  周期: {horizon}日'
    return f'看多观察（未设目标/止损，按到期收盘评看多是否兑现）  周期: {horizon}日'


def _plain_pct(value, *, down: bool = False, signed: bool = False) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return '—'
    if down:
        return f'↓{abs(f):.1f}%'
    if signed:
        return f'{f:+.1f}%'
    return f'{f:.1f}%'


def _range_pct(value) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            lo = float(value[0])
            hi = float(value[1])
            return f'{lo:+.1f}% ~ {hi:+.1f}%'
        except (TypeError, ValueError):
            return _value_or_dash(value)
    if isinstance(value, dict):
        lo = value.get('low', value.get('min'))
        hi = value.get('high', value.get('max'))
        if lo is not None and hi is not None:
            try:
                return f'{float(lo):+.1f}% ~ {float(hi):+.1f}%'
            except (TypeError, ValueError):
                pass
    return _plain_pct(value, signed=True)


def _value_or_dash(value) -> str:
    return '—' if value in (None, '') else str(value)


def _triggered_target_pct(bp: dict):
    value = bp.get('triggered_target_pct')
    if value not in (None, ''):
        return value
    trigger = bp.get('entry_trigger')
    target = bp.get('target_level')
    try:
        trigger_f = float(trigger)
        target_f = float(target)
    except (TypeError, ValueError):
        return None
    if trigger_f <= 0:
        return None
    return (target_f - trigger_f) / trigger_f * 100


def _mapped_value(kind: str, value) -> str:
    maps = {
        'direction': {'bullish': '看多', 'bearish': '看跌回避', 'neutral': '中性'},
        'rating': {'strong_buy': '强烈买入', 'buy': '买入', 'hold': '观望', 'sell': '卖出', 'strong_sell': '强烈卖出'},
        'action': {'buy': '买入', 'sell': '卖出', 'hold': '持有/观望', 'watch_only': '仅观察', 'not_suitable': '不适合'},
    }
    return maps.get(kind, {}).get(value, _value_or_dash(value))


def format_prediction_advice_lines(
    pred: dict,
    *,
    period_label: str,
    rating_text: str,
    entry_text: str,
    suit_text: str,
    action_text: str,
) -> list[str]:
    task_class = _effective_task_class({}, pred)
    if task_class == 'avoid':
        direction_text = '看跌回避 ↓'
        plan_line = (
            f'回避基准: {entry_text}  下跌风险: {_plain_pct(pred.get("target_pct"), down=True)}  '
            f'反弹失效: {_plain_pct(pred.get("stop_pct"), signed=True)}'
        )
    elif task_class == 'watch':
        direction_text = '中性观望 →'
        expected = pred.get('expected_range_pct')
        plan_line = (
            f'观察基准: {entry_text}  预期区间: {_range_pct(expected) if expected is not None else _range_pct(pred.get("target_pct"))}  '
            f'失效条件: {_value_or_dash(pred.get("invalidation"))}'
        )
    else:
        direction_text = '看多 ↑'
        plan_line = (
            f'入场参考: {entry_text}  目标涨幅: {_plain_pct(pred.get("target_pct"), signed=True)}  '
            f'止损线: {_plain_pct(pred.get("stop_pct"), signed=True)}'
        )
    return [
        f'📌 周期: {period_label}  |  方向: {direction_text}  |  评级: {rating_text}',
        plan_line,
        f'适配度: {suit_text}  |  建议操作: {action_text}',
    ]


def format_prediction_copy_field_lines(pred: dict) -> list[str]:
    task_class = _effective_task_class({}, pred)
    code = pred.get('code')
    ref_price = tracking_reference_price(pred)
    opportunity = pred.get('_opportunity_profile') or {}
    if not isinstance(opportunity, dict):
        opportunity = {}
    common = [
        f'方向：{_mapped_value("direction", pred.get("direction"))}',
        f'置信度：{_value_or_dash(pred.get("confidence"))}/10',
        f'综合评级：{_mapped_value("rating", pred.get("final_rating"))}',
        f'建议动作：{_mapped_value("action", pred.get("final_action"))}',
        f'机会等级：{pred.get("opportunity_label") or opportunity.get("opportunity_label") or "—"}',
        f'当前阶段：{pred.get("setup_phase") or opportunity.get("setup_phase") or "—"}',
        f'未持仓策略：{pred.get("not_holding_plan") or opportunity.get("not_holding_plan") or "—"}',
        f'已持仓策略：{pred.get("holding_plan") or opportunity.get("holding_plan") or "—"}',
        f'进攻条件：{pred.get("attack_level") or opportunity.get("attack_level") or "—"}',
        f'防守条件：{pred.get("defense_level") or opportunity.get("defense_level") or "—"}',
    ]
    if task_class == 'avoid':
        direction_fields = [
            f'回避基准：{format_price_or_dash(ref_price, code)}',
            f'下跌风险幅度：{_plain_pct(pred.get("target_pct"), down=True)}',
            f'反弹失效幅度：{_plain_pct(pred.get("stop_pct"), signed=True)}',
        ]
    elif task_class == 'watch':
        expected = pred.get('expected_range_pct')
        direction_fields = [
            f'观察基准：{format_price_or_dash(ref_price, code)}',
            f'预期区间：{_range_pct(expected) if expected is not None else _range_pct(pred.get("target_pct"))}',
            f'观察失效幅度：{_plain_pct(pred.get("stop_pct"), signed=True)}',
        ]
    else:
        direction_fields = [
            f'风险收益比：{pred.get("risk_reward") if pred.get("risk_reward") is not None else (opportunity.get("risk_reward") if opportunity.get("risk_reward") is not None else "—")}',
            f'入场参考：{format_price_or_dash(ref_price, code)}',
            f'目标涨幅：{_plain_pct(pred.get("target_pct"), signed=True)}',
            f'止损幅度：{_plain_pct(pred.get("stop_pct"), signed=True)}',
        ]
    tail = [
        f'周期：{_value_or_dash(pred.get("horizon_days"))}个交易日',
        f'失效条件：{_value_or_dash(pred.get("invalidation"))}',
        f'交易限制：{_value_or_dash(pred.get("_trade_block_reason"))}',
        f'持仓建议：{_value_or_dash(pred.get("_holding_advice"))}',
    ]
    return common + direction_fields + tail


def format_open_review_rule_html(task: dict) -> str:
    task_class = _effective_task_class({}, task)
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    is_bullish_watch = _is_bullish_watch(task)
    code = task.get('code')
    if task_class == 'avoid':
        body = (
            '<b style="color:#7ec8e3;">看跌回避观察</b><br>'
            '站在“你没买”的角度，按 AI 预测跌幅的兑现度评分。<br>'
            'A：实际跌幅 ≥ 预测跌幅；B：跌幅过半；C：未跌够半程且涨 &lt;2%；'
            'D：涨 ≥2% 但未破失效价（小踏空）；F：未先跌达标且收盘确认涨破失效价。'
        )
    elif is_bullish_watch:
        trigger = format_price_or_dash(bp.get('entry_trigger'), code)
        fail = format_price_or_dash(bp.get('fail_level'), code)
        target = _plain_pct(_triggered_target_pct(bp), signed=True)
        body = (
            '<b style="color:#7ec8e3;">看多观察候选</b><br>'
            f'方向看多但未触发买入：涨破触发价 {trigger} 即变身买入单按看多单评分；'
            f'失效价 {fail}；触发后预计涨幅 {target}。'
            f'到期仍未触发，按观察基准价的最终涨跌评分。<br>'
            '触发后：盘中触及目标价=A；审判日收盘跌破止损线（个股10%/ETF5%）才=F，过程中跌破不算。<br>'
            'B：最终涨幅 ≥ 0；C：-6% < 最终涨幅 < 0；D：最终涨幅 ≤ -6%。'
        )
    elif task_class == 'watch':
        body = (
            '<b style="color:#7ec8e3;">中性观望观察</b><br>'
            '散户不踏空即正确，涨幅是唯一扣分项。<br>'
            'A：涨幅 ≤ +4%（下跌躲过 / 横盘合理）；D：涨 +4%~+6% 踏空；F：涨 &gt; +6% 大幅踏空。'
        )
    else:
        body = (
            '<b style="color:#7ec8e3;">看多买入验证</b><br>'
            '追踪期内盘中触及目标价=A；止损只认审判日：审判日收盘跌破止损线（个股10%/ETF5%）才=F，过程中跌破不算。<br>'
            '到期未触发：≥目标50%=B，0~目标50%=C，小亏未破止损=D；'
            '无目标/止损时按到期涨跌回退评分。'
        )
    return (
        '<b style="color:#aabbcc;">评分规则（审判日自动执行）</b><br><br>'
        f'{body}<br><br>'
        '<span style="color:#8899aa;">路径质量：看多单按回撤压力评估；'
        '看跌回避/中性观望为观察路径，不按持仓止损线评分。</span>'
    )


def format_open_review_rule_text(task: dict) -> str:
    task_class = _effective_task_class({}, task)
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    is_bullish_watch = _is_bullish_watch(task)
    code = task.get('code')

    if task_class == 'avoid':
        lines = [
            '评分规则（审判日自动执行）',
            '',
            '看跌回避观察',
            '站在“你没买”的角度，按 AI 预测跌幅的兑现度评分。',
            'A：实际跌幅 ≥ 预测跌幅；B：跌幅过半；C：未跌够半程且涨 <2%；D：涨 ≥2% 但未破失效价；F：未先跌达标且收盘确认涨破失效价。',
        ]
    elif is_bullish_watch:
        trigger = format_price_or_dash(bp.get('entry_trigger'), code)
        fail = format_price_or_dash(bp.get('fail_level'), code)
        target = _plain_pct(_triggered_target_pct(bp), signed=True)
        lines = [
            '评分规则（审判日自动执行）',
            '',
            '看多观察候选',
            f'触发价：{trigger}',
            f'失效价：{fail}',
            f'触发后预计涨幅：{target}',
            '触发后按买入验证评分：盘中触及目标价=A；审判日收盘跌破止损线（个股10%/ETF5%）=F（过程中跌破不算）；否则按审判日收益分 B/C/D。',
            '未触发到期按最终涨跌评分：B=最终涨幅≥0；C=-6%<最终涨幅<0；D=最终涨幅≤-6%。',
        ]
    elif task_class == 'watch':
        lines = [
            '评分规则（审判日自动执行）',
            '',
            '中性观望观察',
            '散户不踏空即正确，涨幅是唯一扣分项。',
            'A：涨幅 ≤ +4%（下跌躲过 / 横盘合理）；D：涨 +4%~+6% 踏空；F：涨 > +6% 大幅踏空。',
        ]
    else:
        lines = [
            '评分规则（审判日自动执行）',
            '',
            '看多买入验证',
            '追踪期内盘中触及目标价=A；止损只认审判日：审判日收盘跌破止损线（个股10%/ETF5%）才=F，过程中跌破不算。',
            '到期未触发：≥目标50%=B，0~目标50%=C，小亏未破止损=D；无目标/止损时按到期涨跌回退评分。',
        ]

    lines.extend([
        '',
        '审判日评级标准：审判日自动执行；路径质量看多单按回撤压力评估，看跌回避/中性观望为观察路径，不按持仓止损线评分。',
    ])
    return '\n'.join(lines)


def format_agent_hit_lines(detail: dict) -> list[str]:
    hit_map = detail.get('agent_hit_map') or {}
    lines: list[str] = []
    for item in hit_map.values():
        label = item.get('label') or 'Agent'
        status = item.get('status') or '样本不足'
        stance = _mapped_value('direction', item.get('stance')) if item.get('stance') else '未表态'
        lines.append(f'{label}: {status}（立场: {stance}）')
    return lines


def format_retrospective_metric_lines(detail: dict, task: dict) -> str:
    task_class = _effective_task_class(detail, task)
    plan_label = (detail.get('plan_quality') or {}).get('label', '未知')
    if task_class == 'buy':
        return (
            f"  R 倍数(最终/风险): {_value_or_dash(detail.get('r_multiple'))}  |  "
            f"MAE(最深不利/风险): {_value_or_dash(detail.get('mae_r'))}  |  "
            f"MFE(最高有利/风险): {_value_or_dash(detail.get('mfe_r'))}\n"
            f"  收益捕获率(最终/最高): {_value_or_dash(detail.get('capture_ratio'))}  |  "
            f"计划完整度: {plan_label}"
        )
    label = '回避评价' if task_class == 'avoid' else '观望评价'
    quality = detail.get('execution_quality') or detail.get('watch_quality') or '—'
    return (
        f"  真实涨跌: {_pct(detail.get('final_pct'))}  |  "
        f"期间最高: {_pct(detail.get('peak_pct'))}  |  "
        f"期间最低: {_pct(detail.get('trough_pct'))}\n"
        f"  {label}: {quality}  |  计划完整度: {plan_label}"
    )


def format_skill_kpi_html(skill_stats: dict) -> str:
    ss = skill_stats or {}
    buckets = ss.get('buckets') or {}

    def _excess(cls: str) -> str:
        e = (buckets.get(cls) or {}).get('excess')
        return f'{e:+.1f}%' if e is not None else '—'

    spread = ss.get('spread')
    if spread is None:
        buy_n = (buckets.get('buy') or {}).get('n', 0)
        avoid_n = (buckets.get('avoid') or {}).get('n', 0)
        return (
            '<span style="color:#aab;">系统判断力</span> '
            f'<span style="color:#8899bb;">积累中（多{buy_n}/避{avoid_n}，每方向需≥5）</span>'
        )

    color = '#2ecc71' if spread >= 0 else '#e74c3c'
    bench = ss.get('benchmark')
    bench_txt = f'{bench:+.1f}%' if bench is not None else '—'
    bench_name = '中证全指' if ss.get('benchmark_kind') == 'market' else '组合均值'
    return (
        '<span style="color:#aab;">系统判断力</span> '
        f'<span style="color:{color};font-weight:bold;">{spread:+.2f}%</span>'
        '  <span style="color:#445566;">|</span>  '
        f'<span style="color:#aab;">超额(基准{bench_name} {bench_txt})</span> '
        f'<span style="color:#8899bb;">多 {_excess("buy")} / 避 {_excess("avoid")} / 观 {_excess("watch")}</span>'
    )
