"""A 股交易日历 — 提供 is_trade_day / add_trading_days 函数。

数据来源：akshare.tool_trade_date_hist_sina（新浪财经全部交易日）
缓存：BASE_DIR/trade_calendar.json（含年份标记，每年首次使用自动刷新）

解决的问题：
    _add_trading_days 单纯跳过周末，但 A 股法定节假日（春节/国庆/清明等）
    在周一到周五中，会导致 horizon_date 偏早，进而影响追踪任务自动结算的时间判断。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

from core.paths import HOME
_CACHE_FILE = HOME / 'trade_calendar.json'
_THIS_YEAR = date.today().year


def _load_cache() -> set[str]:
    """读取缓存的交易日集合（ISO 字符串）。"""
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text(encoding='utf-8'))
            # 如果缓存年份覆盖当年，直接用
            if _THIS_YEAR in data.get('years', []):
                return set(data.get('dates', []))
    except Exception:
        pass
    return set()


def _save_cache(dates: set[str], years: list[int]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({'years': years, 'dates': sorted(dates)},
                       ensure_ascii=False),
            encoding='utf-8',
        )
    except Exception:
        pass


def _fetch_from_akshare() -> set[str]:
    """从 akshare 拉取全部历史交易日，返回 ISO 字符串集合。"""
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    col = df.columns[0]
    return set(df[col].astype(str).tolist())


# 模块级缓存（进程内复用）
_TRADE_DATES: set[str] = set()


def _ensure_loaded() -> set[str]:
    """确保交易日集合已加载（优先内存 → 磁盘缓存 → akshare 网络）。"""
    global _TRADE_DATES
    if _TRADE_DATES:
        return _TRADE_DATES

    cached = _load_cache()
    if cached:
        _TRADE_DATES = cached
        return _TRADE_DATES

    # 需要网络拉取
    try:
        logger.info('[trade_calendar] 首次加载，从 akshare 拉取交易日历…')
        dates = _fetch_from_akshare()
        years = sorted({int(d[:4]) for d in dates if d[:4].isdigit()})
        _save_cache(dates, years)
        _TRADE_DATES = dates
        logger.info('[trade_calendar] 加载完成，共 %d 个交易日', len(dates))
    except Exception as e:
        logger.warning('[trade_calendar] akshare 拉取失败，fallback 到跳周末模式: %s', e)
        _TRADE_DATES = set()  # 空集 → 调用方降级

    return _TRADE_DATES


def is_trade_day(d: date) -> bool:
    """判断某日是否为 A 股交易日。

    数据不可用时降级为「非周末即交易日」。
    """
    dates = _ensure_loaded()
    if not dates:
        return d.weekday() < 5
    return d.isoformat() in dates


def add_trading_days(n: int, start: date | None = None) -> str:
    """从 start（默认今日）向后数 n 个 A 股交易日，返回 ISO 日期字符串。

    正确跳过周末 + 法定节假日。数据不可用时自动降级为纯周末跳过。
    """
    dates = _ensure_loaded()
    d = start or date.today()
    added = 0
    max_iter = n + 30  # 防止无限循环（最多跳 30 个日历日）
    while added < n and max_iter > 0:
        d += timedelta(days=1)
        max_iter -= 1
        if dates:
            if d.isoformat() in dates:
                added += 1
        else:  # fallback
            if d.weekday() < 5:
                added += 1
    return d.isoformat()


def refresh_calendar() -> bool:
    """强制刷新日历缓存（供设置界面或启动时调用）。"""
    global _TRADE_DATES
    try:
        dates = _fetch_from_akshare()
        years = sorted({int(d[:4]) for d in dates if d[:4].isdigit()})
        _save_cache(dates, years)
        _TRADE_DATES = dates
        return True
    except Exception:
        return False
