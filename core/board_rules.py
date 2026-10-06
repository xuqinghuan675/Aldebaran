from __future__ import annotations

from collections.abc import Sized


ETF_ALLOWED_TAGS = {
    '疑似启动初段', '疑似破位', '数据不足', '数据异常', '无明确行为标签',
    # 纯技术信号（无主力/游资语义，适用于指数型 ETF）
    '疑似底部背离', '疑似底部放量', '疑似顶背离风险', '疑似超买回调风险',
    '疑似突破回踩确认', '疑似量价背离',
}


_ETF_PREFIXES = ('15', '16', '18', '50', '51', '52', '56', '58')
_BSE_PREFIXES = ('4', '8', '92', '83', '87', '88', '43')
_CHINEXT_PREFIXES = ('300', '301')
_STAR_PREFIXES = ('688', '689')


def normalize_code(code: str | int | None) -> str:
    text = str(code or '').strip().upper()
    if '.' in text:
        text = text.split('.')[0]
    digits = ''.join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def is_etf(code: str | int | None) -> bool:
    c = normalize_code(code)
    return len(c) == 6 and c.startswith(_ETF_PREFIXES)


def is_st(code: str | int | None = None, name: str | None = None) -> bool:
    text = str(name or '').strip().upper()
    return bool(text and ('*ST' in text or text.startswith('ST') or ' ST' in text or '退' in text))


def is_bse(code: str | int | None) -> bool:
    c = normalize_code(code)
    return len(c) == 6 and c.startswith(_BSE_PREFIXES)


def is_ex_right_name(name: str | None) -> bool:
    text = str(name or '').strip().upper()
    return text.startswith(('XD', 'XR', 'DR'))


def include_in_mainland_emotion_count(code: str | int | None, name: str | None = None) -> bool:
    """Mainland short-term emotion counts exclude BSE and ex-right noise rows."""
    return not is_bse(code) and not is_ex_right_name(name)


def count_mainland_emotion_rows(df) -> int:
    if df is None:
        return 0
    try:
        if len(df) == 0:
            return 0
        columns = list(getattr(df, 'columns', []))
        code_key = next((c for c in ('代码', '股票代码', '证券代码') if c in columns), None)
        name_key = next((c for c in ('名称', '股票名称', '证券简称') if c in columns), None)
        count = 0
        for _, row in df.iterrows():
            code = row.get(code_key) if code_key is not None else (row.iloc[1] if len(row) > 1 else '')
            name = row.get(name_key) if name_key is not None else (row.iloc[2] if len(row) > 2 else '')
            if include_in_mainland_emotion_count(code, name):
                count += 1
        return count
    except Exception:
        try:
            return int(len(df))
        except Exception:
            return 0


def same_source_mainland_emotion_count(total: int | float | str | None, df) -> int | None:
    try:
        raw_total = int(float(total))
        raw_rows = int(len(df))
    except Exception:
        return None
    if raw_rows <= 0 or raw_rows != raw_total:
        return None
    return count_mainland_emotion_rows(df)


def limit_pct(code: str | int | None, name: str | None = None) -> float:
    c = normalize_code(code)
    if is_etf(c):
        return 0.10
    if is_st(c, name):
        return 0.05
    if is_bse(c):
        return 0.30
    if c.startswith(_CHINEXT_PREFIXES) or c.startswith(_STAR_PREFIXES):
        return 0.20
    return 0.10


def is_limit_up(prev_close: float | int | None, today_close: float | int | None,
                code: str | int | None, name: str | None = None, eps: float = 0.005) -> bool:
    try:
        prev = float(prev_close or 0)
        close = float(today_close or 0)
    except Exception:
        return False
    if prev <= 0 or close <= 0:
        return False
    pct = close / prev - 1
    return pct >= limit_pct(code, name) - eps


def is_limit_down(prev_close: float | int | None, today_close: float | int | None,
                  code: str | int | None, name: str | None = None, eps: float = 0.005) -> bool:
    try:
        prev = float(prev_close or 0)
        close = float(today_close or 0)
    except Exception:
        return False
    if prev <= 0 or close <= 0:
        return False
    pct = close / prev - 1
    return pct <= -limit_pct(code, name) + eps


def is_one_word_board(open_: float | int | None, high: float | int | None,
                      low: float | int | None, close: float | int | None) -> bool:
    try:
        values = [float(open_), float(high), float(low), float(close)]
    except Exception:
        return False
    return len(set(values)) == 1


def is_t_word_board(open_: float | int | None, high: float | int | None,
                    low: float | int | None, close: float | int | None,
                    prev_close: float | int | None, code: str | int | None,
                    name: str | None = None) -> bool:
    try:
        low_f = float(low)
        close_f = float(close)
    except Exception:
        return False
    if is_one_word_board(open_, high, low, close):
        return False
    return is_limit_up(prev_close, close_f, code, name) and low_f < close_f * 0.995


def listing_days_enough(df: Sized | None, min_days: int = 60) -> bool:
    try:
        return df is not None and len(df) >= int(min_days)
    except Exception:
        return False


def etf_signal_scope() -> set[str]:
    return set(ETF_ALLOWED_TAGS)


__all__ = [
    'ETF_ALLOWED_TAGS',
    'normalize_code',
    'is_etf',
    'is_st',
    'is_bse',
    'is_ex_right_name',
    'include_in_mainland_emotion_count',
    'count_mainland_emotion_rows',
    'same_source_mainland_emotion_count',
    'limit_pct',
    'is_limit_up',
    'is_limit_down',
    'is_one_word_board',
    'is_t_word_board',
    'listing_days_enough',
    'etf_signal_scope',
]
