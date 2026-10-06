"""虚拟持仓 — CRUD + 结算 + 胜率统计。

注意：此模块已被追踪任务（tracking.py）取代，保留仅为向后兼容。
落盘位置：BASE_DIR/virtual_portfolio.json
结算触发：挂在 MainWindow.market_timer（每 60s），与面板激活解耦。
invalid 结果（失效条件触发）不计入胜率统计。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from core.cache import load_virtual_portfolio, save_virtual_portfolio

# ---- 工具函数 ----

def wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval，手写实现，不引入 scipy。"""
    if total == 0:
        return (0.0, 0.0)
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = (z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _next_seq(positions: list) -> str:
    """生成今日序号，格式 vp_{YYYYMMDD}_{seq:03d}。"""
    today = date.today().strftime('%Y%m%d')
    prefix = f'vp_{today}_'
    existing = [p['id'] for p in positions if isinstance(p.get('id'), str) and p['id'].startswith(prefix)]
    seq = len(existing) + 1
    return f'{prefix}{seq:03d}'


# ---- 持仓管理 ----

def add_virtual_position(pred: dict) -> str:
    """根据预测记录新增一条虚拟持仓，返回新建 id。

    注意：此函数已被追踪任务取代，保留仅为向后兼容。
    pred 必须包含：code / name / direction / entry_price /
                   target_pct / stop_pct / horizon_days / invalidation
    """
    positions = load_virtual_portfolio()
    entry_price = float(pred.get('entry_price') or pred.get('entry_ref') or 0)
    target_pct = float(pred.get('target_pct') if pred.get('target_pct') is not None else 5.0)
    stop_pct   = float(pred.get('stop_pct')   if pred.get('stop_pct')   is not None else 2.5)
    direction = pred.get('direction', 'bullish')

    # 优先用 predictor 已计算好的 horizon_date（含节假日修正）
    # 否则回退到 trade_calendar
    if pred.get('horizon_date'):
        deadline = pred['horizon_date']
    else:
        try:
            from core.trade_calendar import add_trading_days
            deadline = add_trading_days(max(1, int(pred.get('horizon_days', 3))))
        except Exception:
            deadline = (date.today() + timedelta(days=max(1, int(pred.get('horizon_days', 3))))).isoformat()

    # 方向感知的目标价/止损价
    if entry_price:
        if direction == 'bearish':
            target_price = round(entry_price * (1 - abs(target_pct) / 100), 3)
            stop_price   = round(entry_price * (1 + abs(stop_pct)   / 100), 3)
        else:  # bullish / neutral
            target_price = round(entry_price * (1 + abs(target_pct) / 100), 3)
            stop_price   = round(entry_price * (1 - abs(stop_pct)   / 100), 3)
    else:
        target_price = stop_price = 0

    # 预测振幅：bullish 为正，bearish 表示回避风险幅度。
    predicted_amplitude = round(
        target_pct if direction != 'bearish' else -abs(target_pct), 2,
    )

    pos = {
        'id': _next_seq(positions),
        'prediction_id': pred.get('prediction_id', ''),
        'code': pred.get('code', ''),
        'name': pred.get('name', ''),
        'sectors': list(pred.get('sectors') or []),
        'direction': direction,
        'entry_price': entry_price,
        'entry_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'target_price': target_price,
        'stop_price': stop_price,
        'horizon_days': int(pred.get('horizon_days', 3)),
        'deadline': deadline,
        'invalidation': pred.get('invalidation', ''),
        'confidence': pred.get('confidence', 0),
        'status': 'open',
        'current_price': entry_price,
        'current_pct': 0.0,
        'exit_price': None,
        'exit_reason': None,
        'result': None,
        'predicted_amplitude': predicted_amplitude,
        'actual_amplitude': None,                    # 结算时回写
        'precision_score': None,                     # 结算时回写
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    positions.append(pos)
    save_virtual_portfolio(positions)
    return pos['id']


def _compute_precision(pos: dict) -> None:
    """计算 actual_amplitude 和 precision_score 并就地写入 pos。

    actual_amplitude：结算时刻的实际涨跌幅（已含方向，与 current_pct 一致）
    precision_score：0.0 - 1.0
      - 方向预测错误 → 0.0
      - 方向对了 → min(实际幅度, 预测幅度) / 预测幅度
      - 超过目标 → 1.0（封顶）
    """
    predicted = pos.get('predicted_amplitude')
    actual = pos.get('current_pct')
    if predicted is None or actual is None:
        return

    pos['actual_amplitude'] = round(float(actual), 2)

    try:
        p = float(predicted)
        a = float(actual)
    except (TypeError, ValueError):
        return

    # 方向是否一致（同号视为对，0 视为方向不明）
    same_direction = (p > 0 and a > 0) or (p < 0 and a < 0)

    if not same_direction:
        pos['precision_score'] = 0.0
        return

    abs_p = abs(p)
    if abs_p < 0.01:
        pos['precision_score'] = 0.0
        return

    ratio = min(abs(a), abs_p) / abs_p
    pos['precision_score'] = round(max(0.0, min(1.0, ratio)), 3)


def _close_rag_memory_loop(pos: dict) -> None:
    """把结算结果回写到 RAG 记忆。失败不影响主流程。"""
    try:
        from core.analysis_memory import update_outcome
        update_outcome(
            pos.get('code', ''),
            pos.get('prediction_id', ''),
            {
                'name': pos.get('name', ''),
                'result': pos.get('result', '?'),
                'actual_pct': pos.get('actual_amplitude'),
                'precision_score': pos.get('precision_score'),
            },
        )
    except Exception:
        pass


def check_and_settle(
    positions: list,
    current_prices: dict,
    invalidation_flags: dict | None = None,
) -> list[dict]:
    """检查持仓并结算，返回本次触发结算的持仓列表（供触发 alert 用）。

    Args:
        positions: 完整持仓列表（会被就地修改）
        current_prices: {code: {'price': float, 'pct': float, ...}}
        invalidation_flags: {vp_id: bool}，True 表示失效条件已触发

    Returns:
        本次结算的持仓列表（result 刚被设置的）
    """
    today_str = date.today().isoformat()
    settled = []
    flags = invalidation_flags or {}

    for pos in positions:
        if pos.get('status') != 'open':
            continue

        code = pos.get('code', '')
        vp_id = pos.get('id', '')
        q = current_prices.get(code) or {}
        cur_price = q.get('price') or pos.get('current_price') or pos.get('entry_price')

        if cur_price:
            pos['current_price'] = cur_price
            entry = pos.get('entry_price', cur_price)
            pos['current_pct'] = round((cur_price - entry) / entry * 100, 2) if entry else 0.0

        # 失效条件
        if flags.get(vp_id):
            pos['status'] = 'closed'
            pos['exit_price'] = cur_price
            pos['exit_reason'] = '失效条件触发'
            pos['result'] = 'invalid'
            settled.append(pos)
            continue

        direction = pos.get('direction', 'bullish')
        target = pos.get('target_price', 0)
        stop = pos.get('stop_price', 0)
        deadline = pos.get('deadline', '')

        # 方向感知：目标达成 / 止损触发
        if direction == 'bearish':
            target_hit = bool(target and cur_price and cur_price <= target)
            stop_hit   = bool(stop   and cur_price and cur_price >= stop)
        else:  # bullish / neutral
            target_hit = bool(target and cur_price and cur_price >= target)
            stop_hit   = bool(stop   and cur_price and cur_price <= stop)

        if target_hit:
            pos['status'] = 'closed'
            pos['exit_price'] = cur_price
            pos['exit_reason'] = '目标达成'
            pos['result'] = 'win'
            _compute_precision(pos)
            _close_rag_memory_loop(pos)
            settled.append(pos)
        elif stop_hit:
            pos['status'] = 'closed'
            pos['exit_price'] = cur_price
            pos['exit_reason'] = '止损触发'
            pos['result'] = 'loss'
            _compute_precision(pos)
            _close_rag_memory_loop(pos)
            settled.append(pos)
        elif deadline and today_str >= deadline:
            # 仅在 A 股交易日结算，避免节假日/休市期间误触发
            try:
                from core.trade_calendar import is_trade_day
                if not is_trade_day(date.today()):
                    continue
            except Exception:
                pass
            entry = pos.get('entry_price', 0)
            pos['status'] = 'closed'
            pos['exit_price'] = cur_price
            pos['exit_reason'] = '到期结算'
            if direction == 'bearish':
                pos['result'] = 'win' if (cur_price and entry and cur_price < entry) else 'loss'
            else:
                pos['result'] = 'win' if (cur_price and entry and cur_price > entry) else 'loss'
            _compute_precision(pos)
            _close_rag_memory_loop(pos)
            settled.append(pos)

    return settled


def get_stats(positions: list) -> dict:
    """计算胜率统计 + 平均预测精度。invalid 不计入。

    Returns:
        {wins, losses, timeouts, invalids, total_valid, total_all,
         rate (None if <30), ci_low, ci_high, show_rate (bool),
         avg_precision (None if <5 samples), precision_samples}
    """
    wins = losses = timeouts = invalids = 0
    precision_scores: list[float] = []
    for p in positions:
        r = p.get('result')
        if r == 'win':
            wins += 1
        elif r == 'loss':
            losses += 1
        elif r == 'timeout':
            timeouts += 1
        elif r == 'invalid':
            invalids += 1
        # 收集精度（仅 valid 结果）
        if r in ('win', 'loss', 'timeout'):
            ps = p.get('precision_score')
            if ps is not None:
                try:
                    precision_scores.append(float(ps))
                except (TypeError, ValueError):
                    pass

    total_valid = wins + losses + timeouts
    total_all = total_valid + invalids
    show_rate = total_valid >= 30
    rate = wins / total_valid if total_valid > 0 else None
    ci_low, ci_high = wilson_ci(wins, total_valid) if total_valid > 0 else (0.0, 0.0)

    # 平均精度（至少 5 个样本才显示）
    avg_precision = None
    if len(precision_scores) >= 5:
        avg_precision = round(sum(precision_scores) / len(precision_scores), 3)

    return {
        'wins': wins,
        'losses': losses,
        'timeouts': timeouts,
        'invalids': invalids,
        'total_valid': total_valid,
        'total_all': total_all,
        'rate': rate,
        'ci_low': ci_low,
        'ci_high': ci_high,
        'show_rate': show_rate,
        'avg_precision': avg_precision,
        'precision_samples': len(precision_scores),
    }
