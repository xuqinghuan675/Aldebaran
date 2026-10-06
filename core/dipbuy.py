"""强势股回调低吸（北京炒家）—— 纯个股 K 线判定，唯一真相源。

设计要点：
  · 纯函数：只看个股日 K，不接 market_phase / flow / bench / RS（情绪降级在 predictor 约束层做，
    避免 technical_profile 缓存与最新情绪打架）。
  · 与 build_technical_profile 同口径：MA/量比/支撑/K 线形态公式与 kline_provider 一致，
    生产（build_technical_profile）与回测（tools/_backtest_dipbuy.py）共用本函数，杜绝逻辑漂移。
  · 价位为「支撑系」：entry≈close（企稳即买），stop=回调低点/ATR，target=2:1 构造，
    永不使用阻力位 entry。
"""
from __future__ import annotations

import pandas as pd


def _f(val) -> float | None:
    """转 float；NaN/inf/不可转 → None。"""
    try:
        x = float(val)
    except Exception:
        return None
    if x != x or x in (float('inf'), float('-inf')):
        return None
    return x


def evaluate_dipbuy(df: pd.DataFrame | None, *, ind: dict | None = None,
                    code: str = '', name: str = '') -> dict:
    """判定个股当前是否为强势股回调低吸点。

    Returns dict:
      status: 'triggered' | 'candidate' | None
      entry / stop / target: float | None（支撑系价位）
      sub_signals: {'强势','缩量回踩','企稳','无否决'} bool
      veto: list[str]；reason: list[str]
    """
    out = {
        'status': None,
        'entry': None, 'stop': None, 'target': None,
        'sub_signals': {'强势': False, '缩量回踩': False, '企稳': False, '无否决': False},
        'veto': [], 'reason': [],
    }
    if df is None or len(df) < 60:
        return out

    df = df.copy()
    df.columns = df.columns.str.lower()
    if 'vol' in df.columns and 'volume' not in df.columns:
        df['volume'] = df['vol']

    if ind is None:
        try:
            from core.kline_provider import compute_indicators
            ind = compute_indicators(df)
        except Exception:
            ind = {}
    ind = ind or {}

    close = _f(df['close'].iloc[-1])
    if close is None or close <= 0:
        return out
    open_ = _f(df['open'].iloc[-1]) if 'open' in df.columns else close
    high = _f(df['high'].iloc[-1]) if 'high' in df.columns else close
    low = _f(df['low'].iloc[-1]) if 'low' in df.columns else close
    volume = _f(df['volume'].iloc[-1]) if 'volume' in df.columns else 0.0
    if open_ is None:
        open_ = close
    if high is None:
        high = close
    if low is None:
        low = close

    # MA：优先用 ind（与生产同源），缺失则 rolling 兜底
    close_series = df['close']
    ma5 = _f(ind.get('ma5')) or _f(close_series.rolling(5).mean().iloc[-1])
    ma20 = _f(ind.get('ma20')) or _f(close_series.rolling(20).mean().iloc[-1])
    ma40 = _f(ind.get('ma40')) or _f(close_series.rolling(40).mean().iloc[-1])
    ma60 = _f(ind.get('ma60')) or _f(close_series.rolling(60).mean().iloc[-1])
    atr = _f(ind.get('atr14'))

    ma20_series = close_series.rolling(20).mean()
    ma20_prev = _f(ma20_series.iloc[-6]) if len(ma20_series.dropna()) >= 6 else None
    ma20_slope_pct = ((ma20 / ma20_prev - 1) * 100) if (ma20 and ma20_prev) else None

    prior = df.iloc[:-1] if len(df) > 1 else df
    low20 = _f(prior.tail(20)['low'].min()) if 'low' in prior.columns and len(prior) else None
    low5 = _f(prior.tail(5)['low'].min()) if 'low' in prior.columns and len(prior) else None
    vol_ma20 = _f(prior.tail(20)['volume'].mean()) if 'volume' in prior.columns and len(prior) else None
    volume_ratio = round(volume / vol_ma20, 2) if (vol_ma20 and vol_ma20 > 0) else None

    # 趋势（与 build_technical_profile:755 同口径）
    uptrend = bool(
        ma5 and ma20 and ma40 and ma60
        and close > ma20 and ma5 > ma20 > ma40 > ma60
        and (ma20_slope_pct or 0) >= 0
    )

    # K 线形态（与 build_technical_profile:775-781 同口径）
    body_top = max(open_, close)
    body_bottom = min(open_, close)
    day_range = high - low
    long_upper = bool(day_range > 0 and (high - body_top) / day_range >= 0.45)
    long_lower = bool(day_range > 0 and (body_bottom - low) / day_range >= 0.45)
    volume_down = bool(close < open_ and (volume_ratio or 0) >= 1.5)  # 放量阴线
    breakdown = bool(low20 and close < low20)

    # 缩量回踩 MA20（与 build_technical_profile:773 同口径，保证全部 pullback 都由本函数接管）
    shrink_pullback = bool(
        uptrend and ma20 and ma20 * 0.97 <= close <= ma20 * 1.03
        and (volume_ratio or 0) <= 1.2
    )
    # 企稳确认（任一）
    stabilize = bool(long_lower or close > open_ or (ma5 and close >= ma5) or (low5 and low >= low5))
    # 否决
    veto: list[str] = []
    if breakdown:
        veto.append('跌破20日平台低点')
    if long_upper:
        veto.append('长上影冲高回落')
    if volume_down:
        veto.append('放量阴线')

    out['sub_signals'] = {
        '强势': uptrend, '缩量回踩': shrink_pullback,
        '企稳': stabilize, '无否决': not veto,
    }
    out['veto'] = veto

    if not (uptrend and shrink_pullback):
        return out  # 非低吸 → status 保持 None，build_technical_profile 走原有 setup 链

    # 支撑系价位
    atr_stop = (close - 2 * atr) if atr else None
    fail_candidates = [v for v in (low5, atr_stop, low20) if v is not None and v < close]
    stop = round(max(fail_candidates), 2) if fail_candidates else None
    entry = round(close, 2)
    target = round(entry + 2 * (entry - stop), 2) if (stop and entry > stop) else None
    out['entry'], out['stop'], out['target'] = entry, stop, target

    base_reason = ['强势股缩量回踩MA20', f'量比{volume_ratio}']
    if stabilize and not veto and stop is not None:
        out['status'] = 'triggered'
        out['reason'] = base_reason + ['企稳确认（低吸触发）']
    else:
        out['status'] = 'candidate'
        out['reason'] = base_reason + (['待企稳确认'] if not stabilize else ['存在瑕疵，降为候选'])
    return out
