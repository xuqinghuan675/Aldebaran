"""Vectorized technical-analysis helpers used by Aldebaran.

This module is an original implementation maintained as part of Aldebaran.
It exposes a compact set of formula-style functions for compatibility with
the rest of the application. The implementation is based on standard
technical-analysis definitions and NumPy/pandas primitives.

Project license: MIT (see repository LICENSE).
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


ArrayLike = Iterable[float] | np.ndarray | pd.Series

__all__ = [
    "RD", "RET", "ABS", "LN", "POW", "SQRT", "SIN", "COS", "TAN",
    "MAX", "MIN", "IF", "REF", "DIFF", "STD", "SUM", "CONST",
    "HHV", "LLV", "HHVBARS", "LLVBARS", "MA", "EMA", "SMA", "WMA",
    "DMA", "AVEDEV", "SLOPE", "FORCAST", "LAST", "COUNT", "EVERY",
    "EXIST", "FILTER", "BARSLAST", "BARSLASTCOUNT", "BARSSINCEN",
    "CROSS", "LONGCROSS", "VALUEWHEN", "BETWEEN", "TOPRANGE",
    "LOWRANGE", "MACD", "KDJ", "RSI", "WR", "BIAS", "BOLL", "PSY",
    "CCI", "ATR", "BBI", "DMI", "TAQ", "KTN", "TRIX", "VR", "CR",
    "EMV", "DPO", "BRAR", "DFMA", "MTM", "MASS", "ROC", "EXPMA",
    "OBV", "MFI", "ASI", "XSII",
]


def _arr(values: ArrayLike, *, dtype=float) -> np.ndarray:
    return np.asarray(values, dtype=dtype)


def _series(values: ArrayLike) -> pd.Series:
    return pd.Series(values)


def _positive_window(n: int) -> int:
    n = int(n)
    if n <= 0:
        raise ValueError("window length must be positive")
    return n


def _divide(num, den):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.divide(num, den)


def RD(values, D: int = 3):
    return np.round(values, int(D))


def RET(values: ArrayLike, N: int = 1):
    n = int(N)
    if n <= 0:
        raise ValueError("N must be positive")
    return np.asarray(values)[-n]


def ABS(values):
    return np.abs(values)


def LN(values):
    return np.log(values)


def POW(values, exponent):
    return np.power(values, exponent)


def SQRT(values):
    return np.sqrt(values)


def SIN(values):
    return np.sin(values)


def COS(values):
    return np.cos(values)


def TAN(values):
    return np.tan(values)


def MAX(left, right):
    return np.maximum(left, right)


def MIN(left, right):
    return np.minimum(left, right)


def IF(condition, when_true, when_false):
    return np.where(condition, when_true, when_false)


def REF(values: ArrayLike, N: int = 1) -> np.ndarray:
    return _series(values).shift(int(N)).to_numpy()


def DIFF(values: ArrayLike, N: int = 1) -> np.ndarray:
    return _series(values).diff(int(N)).to_numpy()


def STD(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).std(ddof=0).to_numpy()


def SUM(values: ArrayLike, N: int) -> np.ndarray:
    n = int(N)
    series = _series(values)
    if n > 0:
        return series.rolling(n).sum().to_numpy()
    return series.cumsum().to_numpy()


def CONST(values: ArrayLike) -> np.ndarray:
    data = np.asarray(values)
    if data.size == 0:
        return data.copy()
    return np.full(data.shape[0], data[-1])


def HHV(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).max().to_numpy()


def LLV(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).min().to_numpy()


def HHVBARS(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).apply(
        lambda window: float(np.argmax(window[::-1])),
        raw=True,
    ).to_numpy()


def LLVBARS(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).apply(
        lambda window: float(np.argmin(window[::-1])),
        raw=True,
    ).to_numpy()


def MA(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).mean().to_numpy()


def EMA(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).ewm(span=n, adjust=False).mean().to_numpy()


def SMA(values: ArrayLike, N: int, M: int = 1) -> np.ndarray:
    n = _positive_window(N)
    m = float(M)
    if not 0 < m <= n:
        raise ValueError("M must satisfy 0 < M <= N")
    return _series(values).ewm(alpha=m / n, adjust=False).mean().to_numpy()


def WMA(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    weights = np.arange(1.0, n + 1.0)
    denominator = float(weights.sum())
    return _series(values).rolling(n).apply(
        lambda window: float(np.dot(window, weights) / denominator),
        raw=True,
    ).to_numpy()


def DMA(values: ArrayLike, alpha) -> np.ndarray:
    data = _arr(values)
    if data.size == 0:
        return data.copy()

    if np.isscalar(alpha):
        a = float(alpha)
        if not 0 < a <= 1:
            raise ValueError("scalar alpha must be in (0, 1]")
        return pd.Series(data).ewm(alpha=a, adjust=False).mean().to_numpy()

    factors = _arr(alpha)
    if len(factors) != len(data):
        raise ValueError("alpha sequence must match input length")
    factors = np.where(np.isnan(factors), 1.0, factors)
    factors = np.clip(factors, 0.0, 1.0)

    result = np.empty_like(data, dtype=float)
    result[0] = data[0]
    for idx in range(1, len(data)):
        a = factors[idx]
        result[idx] = a * data[idx] + (1.0 - a) * result[idx - 1]
    return result


def AVEDEV(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    return _series(values).rolling(n).apply(
        lambda window: float(np.mean(np.abs(window - np.mean(window)))),
        raw=True,
    ).to_numpy()


def SLOPE(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    x = np.arange(n, dtype=float)
    return _series(values).rolling(n).apply(
        lambda window: float(np.polyfit(x, window, deg=1)[0]),
        raw=True,
    ).to_numpy()


def FORCAST(values: ArrayLike, N: int) -> np.ndarray:
    n = _positive_window(N)
    x = np.arange(n, dtype=float)
    return _series(values).rolling(n).apply(
        lambda window: float(np.polyval(np.polyfit(x, window, deg=1), n - 1)),
        raw=True,
    ).to_numpy()


def LAST(condition, A: int, B: int) -> np.ndarray:
    """Return whether condition held for every bar from A bars ago to B bars ago."""
    a, b = int(A), int(B)
    if a < b or b < 0:
        raise ValueError("LAST requires A >= B >= 0")
    flags = _arr(condition, dtype=bool)
    result = np.zeros(len(flags), dtype=bool)
    for idx in range(len(flags)):
        start = idx - a
        end = idx - b
        if start >= 0 and end >= start:
            result[idx] = bool(np.all(flags[start : end + 1]))
    return result


def COUNT(condition, N: int) -> np.ndarray:
    return SUM(_arr(condition, dtype=float), N)


def EVERY(condition, N: int) -> np.ndarray:
    n = _positive_window(N)
    return COUNT(condition, n) == n


def EXIST(condition, N: int) -> np.ndarray:
    return COUNT(condition, N) > 0


def FILTER(condition, N: int) -> np.ndarray:
    """Keep a true signal and suppress the following N bars."""
    gap = max(0, int(N))
    source = _arr(condition, dtype=bool)
    result = source.copy()
    blocked_until = -1
    for idx, active in enumerate(source):
        if idx <= blocked_until:
            result[idx] = False
            continue
        if active:
            blocked_until = idx + gap
    return result


def BARSLAST(condition) -> np.ndarray:
    flags = _arr(condition, dtype=bool)
    result = np.zeros(len(flags), dtype=int)
    distance = 0
    seen = False
    for idx, active in enumerate(flags):
        if active:
            distance = 0
            seen = True
        elif seen:
            distance += 1
        else:
            distance = idx + 1
        result[idx] = distance
    return result


def BARSLASTCOUNT(condition) -> np.ndarray:
    flags = _arr(condition, dtype=bool)
    result = np.zeros(len(flags), dtype=int)
    streak = 0
    for idx, active in enumerate(flags):
        streak = streak + 1 if active else 0
        result[idx] = streak
    return result


def BARSSINCEN(condition, N: int) -> np.ndarray:
    """Bars since the first true value inside the trailing N-bar window."""
    n = _positive_window(N)
    flags = _arr(condition, dtype=bool)
    result = np.zeros(len(flags), dtype=int)
    for idx in range(len(flags)):
        start = max(0, idx - n + 1)
        window = flags[start : idx + 1]
        hits = np.flatnonzero(window)
        if hits.size:
            result[idx] = len(window) - 1 - int(hits[0])
    return result


def CROSS(left, right) -> np.ndarray:
    a, b = np.broadcast_arrays(_arr(left), _arr(right))
    if a.size == 0:
        return np.asarray([], dtype=bool)
    result = np.zeros(a.shape, dtype=bool)
    result[1:] = (a[:-1] <= b[:-1]) & (a[1:] > b[1:])
    return result


def LONGCROSS(left, right, N: int) -> np.ndarray:
    a = _arr(left)
    b = _arr(right)
    n = _positive_window(N)
    if len(a) != len(b):
        raise ValueError("inputs must have the same length")
    result = np.zeros(len(a), dtype=bool)
    for idx in range(1, len(a)):
        start = idx - n
        if start < 0:
            continue
        result[idx] = bool(np.all(a[start:idx] < b[start:idx]) and a[idx] > b[idx])
    return result


def VALUEWHEN(condition, values) -> np.ndarray:
    flags = _arr(condition, dtype=bool)
    data = _arr(values)
    if len(flags) != len(data):
        raise ValueError("condition and values must have the same length")
    return pd.Series(np.where(flags, data, np.nan)).ffill().to_numpy()


def BETWEEN(values, left, right) -> np.ndarray:
    values = _arr(values)
    left = np.asarray(left)
    right = np.asarray(right)
    return ((left < values) & (values < right)) | ((left > values) & (values > right))


def TOPRANGE(values: ArrayLike) -> np.ndarray:
    """Bars since the nearest previous value greater than or equal to current."""
    data = _arr(values)
    result = np.zeros(len(data), dtype=int)
    for idx in range(1, len(data)):
        previous = np.flatnonzero(data[:idx] >= data[idx])
        result[idx] = idx - int(previous[-1]) if previous.size else idx
    return result


def LOWRANGE(values: ArrayLike) -> np.ndarray:
    """Bars since the nearest previous value less than or equal to current."""
    data = _arr(values)
    result = np.zeros(len(data), dtype=int)
    for idx in range(1, len(data)):
        previous = np.flatnonzero(data[:idx] <= data[idx])
        result[idx] = idx - int(previous[-1]) if previous.size else idx
    return result


def MACD(CLOSE, SHORT: int = 12, LONG: int = 26, M: int = 9):
    close = _arr(CLOSE)
    dif = EMA(close, SHORT) - EMA(close, LONG)
    dea = EMA(dif, M)
    hist = (dif - dea) * 2.0
    return RD(dif), RD(dea), RD(hist)


def KDJ(CLOSE, HIGH, LOW, N: int = 9, M1: int = 3, M2: int = 3):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    lowest = LLV(low, N)
    highest = HHV(high, N)
    rsv = _divide(close - lowest, highest - lowest) * 100.0
    k = EMA(rsv, M1 * 2 - 1)
    d = EMA(k, M2 * 2 - 1)
    j = 3.0 * k - 2.0 * d
    return k, d, j


def RSI(CLOSE, N: int = 24):
    close = _arr(CLOSE)
    delta = close - REF(close, 1)
    gains = SMA(MAX(delta, 0.0), N)
    moves = SMA(ABS(delta), N)
    return RD(_divide(gains, moves) * 100.0)


def WR(CLOSE, HIGH, LOW, N: int = 10, N1: int = 6):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    high_n, low_n = HHV(high, N), LLV(low, N)
    high_n1, low_n1 = HHV(high, N1), LLV(low, N1)
    wr = _divide(high_n - close, high_n - low_n) * 100.0
    wr1 = _divide(high_n1 - close, high_n1 - low_n1) * 100.0
    return RD(wr), RD(wr1)


def BIAS(CLOSE, L1: int = 6, L2: int = 12, L3: int = 24):
    close = _arr(CLOSE)
    outputs = []
    for n in (L1, L2, L3):
        avg = MA(close, n)
        outputs.append(_divide(close - avg, avg) * 100.0)
    return tuple(RD(item) for item in outputs)


def BOLL(CLOSE, N: int = 20, P: float = 2):
    close = _arr(CLOSE)
    mid = MA(close, N)
    sigma = STD(close, N)
    return RD(mid + sigma * P), RD(mid), RD(mid - sigma * P)


def PSY(CLOSE, N: int = 12, M: int = 6):
    close = _arr(CLOSE)
    psy = COUNT(close > REF(close, 1), N) / float(N) * 100.0
    return RD(psy), RD(MA(psy, M))


def CCI(CLOSE, HIGH, LOW, N: int = 14):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    typical = (high + low + close) / 3.0
    average = MA(typical, N)
    return _divide(typical - average, 0.015 * AVEDEV(typical, N))


def ATR(CLOSE, HIGH, LOW, N: int = 20):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    previous_close = REF(close, 1)
    true_range = MAX(MAX(high - low, ABS(high - previous_close)), ABS(low - previous_close))
    return MA(true_range, N)


def BBI(CLOSE, M1: int = 3, M2: int = 6, M3: int = 12, M4: int = 20):
    close = _arr(CLOSE)
    return (MA(close, M1) + MA(close, M2) + MA(close, M3) + MA(close, M4)) / 4.0


def DMI(CLOSE, HIGH, LOW, M1: int = 14, M2: int = 6):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    previous_close = REF(close, 1)
    true_range = SUM(MAX(MAX(high - low, ABS(high - previous_close)), ABS(low - previous_close)), M1)
    up_move = high - REF(high, 1)
    down_move = REF(low, 1) - low
    plus_dm = SUM(IF((up_move > 0) & (up_move > down_move), up_move, 0.0), M1)
    minus_dm = SUM(IF((down_move > 0) & (down_move > up_move), down_move, 0.0), M1)
    pdi = _divide(plus_dm, true_range) * 100.0
    mdi = _divide(minus_dm, true_range) * 100.0
    adx = MA(_divide(ABS(mdi - pdi), pdi + mdi) * 100.0, M2)
    adxr = (adx + REF(adx, M2)) / 2.0
    return pdi, mdi, adx, adxr


def TAQ(HIGH, LOW, N: int):
    high, low = _arr(HIGH), _arr(LOW)
    upper = HHV(high, N)
    lower = LLV(low, N)
    return upper, (upper + lower) / 2.0, lower


def KTN(CLOSE, HIGH, LOW, N: int = 20, M: int = 10):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    mid = EMA((high + low + close) / 3.0, N)
    atr = ATR(close, high, low, M)
    return mid + 2.0 * atr, mid, mid - 2.0 * atr


def TRIX(CLOSE, M1: int = 12, M2: int = 20):
    close = _arr(CLOSE)
    triple = EMA(EMA(EMA(close, M1), M1), M1)
    trix = _divide(triple - REF(triple, 1), REF(triple, 1)) * 100.0
    return trix, MA(trix, M2)


def VR(CLOSE, VOL, M1: int = 26):
    close, volume = _arr(CLOSE), _arr(VOL)
    previous = REF(close, 1)
    up = SUM(IF(close > previous, volume, 0.0), M1)
    other = SUM(IF(close <= previous, volume, 0.0), M1)
    return _divide(up, other) * 100.0


def CR(CLOSE, HIGH, LOW, N: int = 20):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    midpoint = REF(high + low + close, 1) / 3.0
    numerator = SUM(MAX(0.0, high - midpoint), N)
    denominator = SUM(MAX(0.0, midpoint - low), N)
    return _divide(numerator, denominator) * 100.0


def EMV(HIGH, LOW, VOL, N: int = 14, M: int = 9):
    high, low, volume = _arr(HIGH), _arr(LOW), _arr(VOL)
    volume_factor = _divide(MA(volume, N), volume)
    midpoint_move = 100.0 * _divide(high + low - REF(high + low, 1), high + low)
    range_average = MA(high - low, N)
    emv = MA(midpoint_move * volume_factor * _divide(high - low, range_average), N)
    return emv, MA(emv, M)


def DPO(CLOSE, M1: int = 20, M2: int = 10, M3: int = 6):
    close = _arr(CLOSE)
    dpo = close - REF(MA(close, M1), M2)
    return dpo, MA(dpo, M3)


def BRAR(OPEN, CLOSE, HIGH, LOW, M1: int = 26):
    open_, close, high, low = _arr(OPEN), _arr(CLOSE), _arr(HIGH), _arr(LOW)
    ar = _divide(SUM(high - open_, M1), SUM(open_ - low, M1)) * 100.0
    previous = REF(close, 1)
    br = _divide(SUM(MAX(0.0, high - previous), M1), SUM(MAX(0.0, previous - low), M1)) * 100.0
    return ar, br


def DFMA(CLOSE, N1: int = 10, N2: int = 50, M: int = 10):
    close = _arr(CLOSE)
    dif = MA(close, N1) - MA(close, N2)
    return dif, MA(dif, M)


def MTM(CLOSE, N: int = 12, M: int = 6):
    close = _arr(CLOSE)
    momentum = close - REF(close, N)
    return momentum, MA(momentum, M)


def MASS(HIGH, LOW, N1: int = 9, N2: int = 25, M: int = 6):
    high, low = _arr(HIGH), _arr(LOW)
    span = high - low
    ratio = _divide(MA(span, N1), MA(MA(span, N1), N1))
    mass = SUM(ratio, N2)
    return mass, MA(mass, M)


def ROC(CLOSE, N: int = 12, M: int = 6):
    close = _arr(CLOSE)
    previous = REF(close, N)
    roc = _divide(close - previous, previous) * 100.0
    return roc, MA(roc, M)


def EXPMA(CLOSE, N1: int = 12, N2: int = 50):
    close = _arr(CLOSE)
    return EMA(close, N1), EMA(close, N2)


def OBV(CLOSE, VOL):
    close, volume = _arr(CLOSE), _arr(VOL)
    previous = REF(close, 1)
    signed = IF(close > previous, volume, IF(close < previous, -volume, 0.0))
    return SUM(signed, 0) / 10000.0


def MFI(CLOSE, HIGH, LOW, VOL, N: int = 14):
    close, high, low, volume = _arr(CLOSE), _arr(HIGH), _arr(LOW), _arr(VOL)
    typical = (high + low + close) / 3.0
    previous = REF(typical, 1)
    positive = SUM(IF(typical > previous, typical * volume, 0.0), N)
    negative = SUM(IF(typical < previous, typical * volume, 0.0), N)
    money_ratio = _divide(positive, negative)
    return 100.0 - 100.0 / (1.0 + money_ratio)


def ASI(OPEN, CLOSE, HIGH, LOW, M1: int = 26, M2: int = 10):
    open_, close, high, low = _arr(OPEN), _arr(CLOSE), _arr(HIGH), _arr(LOW)
    previous_close = REF(close, 1)
    previous_open = REF(open_, 1)
    previous_low = REF(low, 1)

    aa = ABS(high - previous_close)
    bb = ABS(low - previous_close)
    cc = ABS(high - previous_low)
    dd = ABS(previous_close - previous_open)

    r = IF(
        (aa > bb) & (aa > cc),
        aa + bb / 2.0 + dd / 4.0,
        IF((bb > cc) & (bb > aa), bb + aa / 2.0 + dd / 4.0, cc + dd / 4.0),
    )
    x = close - previous_close + (close - open_) / 2.0 + previous_close - previous_open
    si = _divide(16.0 * x * MAX(aa, bb), r)
    asi = SUM(si, M1)
    return asi, MA(asi, M2)


def XSII(CLOSE, HIGH, LOW, N: int = 102, M: int = 7):
    close, high, low = _arr(CLOSE), _arr(HIGH), _arr(LOW)
    composite = (2.0 * close + high + low) / 4.0
    center = MA(composite, 5)
    td1 = center * float(N) / 100.0
    td2 = center * (200.0 - float(N)) / 100.0

    ma20 = MA(close, 20)
    adaptive = ABS(composite - ma20)
    alpha = _divide(adaptive, ma20)
    smoothed = DMA(close, alpha)
    td3 = (1.0 + float(M) / 100.0) * smoothed
    td4 = (1.0 - float(M) / 100.0) * smoothed
    return td1, td2, td3, td4
