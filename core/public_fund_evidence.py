from __future__ import annotations

import json
import logging
import math
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from core.board_rules import is_etf, normalize_code
from core.paths import CACHE_DIR

logger = logging.getLogger(__name__)

_CACHE_DIR = CACHE_DIR / 'public_evidence'
_MARKET_OPEN = dtime(9, 30)

_SEAT_WATCHLIST = (
    '中信杭州延安路',
    '东方财富拉萨',
    '东财拉萨',
    '华鑫证券上海分公司',
    '华鑫上海分公司',
    '银河证券绍兴',
    '银河绍兴',
    '国泰君安上海江苏路',
    '国君上海江苏路',
    '招商证券深圳益田路',
    '招商深圳益田路',
    '中信证券上海溧阳路',
    '中信上海溧阳路',
    '国盛证券宁波桑田路',
    '宁波桑田路',
    '方新侠',
    '章建平',
    '孙国栋',
)


def _safe_float(v: Any) -> float:
    try:
        f = float(v)
        return 0.0 if math.isnan(f) or math.isinf(f) else f
    except Exception:
        return 0.0


def _pick(row: dict, *keys: str, default: Any = '') -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != '':
            return value
    return default


def _effective_date(now: datetime | None = None) -> str:
    return next(_candidate_dates(now, limit=1))


def _candidate_dates(now: datetime | None = None, limit: int = 7):
    current = now or datetime.now()
    if current.time() < _MARKET_OPEN:
        current = current - timedelta(days=1)
    emitted = 0
    while emitted < max(1, int(limit)):
        if current.weekday() < 5:
            yield current.strftime('%Y%m%d')
            emitted += 1
        current = current - timedelta(days=1)


def _cache_path(kind: str, date_str: str) -> Path:
    return _CACHE_DIR / f'{kind}_{date_str}.json'


def _read_cache(kind: str, date_str: str) -> list[dict] | None:
    path = _cache_path(kind, date_str)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        rows = data.get('rows') if isinstance(data, dict) else data
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _write_cache(kind: str, date_str: str, rows: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {'date': date_str, 'rows': rows, 'fetched_at': datetime.now().isoformat(timespec='seconds')}
        _cache_path(kind, date_str).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception as e:
        logger.debug('[public_evidence] 写缓存失败 %s %s: %s', kind, date_str, e)


def _rows_from_df(df: Any) -> list[dict]:
    if df is None:
        return []
    if hasattr(df, 'to_dict'):
        try:
            return list(df.to_dict(orient='records'))
        except Exception:
            return []
    if isinstance(df, list):
        return [x for x in df if isinstance(x, dict)]
    return []


def _fetch_lhb_rows(date_str: str) -> list[dict]:
    try:
        import akshare as ak
        df = ak.stock_lhb_detail_em(start_date=date_str, end_date=date_str)
        rows = _rows_from_df(df)
        if rows:
            return rows
    except Exception as e:
        logger.debug('[public_evidence] akshare 龙虎榜失败: %s', e)
    try:
        from core.astock_http_provider import fetch_lhb_detail
        return fetch_lhb_detail(date_str)
    except Exception as e:
        logger.debug('[public_evidence] HTTP 龙虎榜失败: %s', e)
        return []


def _fetch_hsgt_rows(date_str: str) -> list[dict]:
    try:
        import akshare as ak
        df = ak.stock_hsgt_hold_stock_em(market='北向', indicator='今日排行')
        return _rows_from_df(df)
    except Exception as e:
        logger.debug('[public_evidence] akshare 北向失败: %s', e)
        return []


def _fetch_dzjy_rows(date_str: str) -> list[dict]:
    try:
        import akshare as ak
        df = ak.stock_dzjy_mrtj(start_date=date_str, end_date=date_str)
        rows = _rows_from_df(df)
        if rows:
            return rows
    except Exception as e:
        logger.debug('[public_evidence] akshare 大宗交易失败: %s', e)
    try:
        from core.astock_http_provider import fetch_dzjy_detail
        return fetch_dzjy_detail(date_str)
    except Exception as e:
        logger.debug('[public_evidence] HTTP 大宗交易失败: %s', e)
        return []


def _load_rows(kind: str, date_str: str, fetcher) -> list[dict]:
    try:
        base = datetime.strptime(f'{date_str} 15:00', '%Y%m%d %H:%M')
    except Exception:
        base = datetime.now()
    last_rows: list[dict] = []
    max_dates = 1 if kind == 'hsgt' else 7
    for candidate in _candidate_dates(base, limit=max_dates):
        cached = _read_cache(kind, candidate)
        if cached:
            return cached
        if cached == []:
            last_rows = cached
            continue
        rows = fetcher(candidate)
        _write_cache(kind, candidate, rows)
        if rows:
            return rows
        last_rows = rows
    return last_rows


def _row_code(row: dict) -> str:
    return normalize_code(_pick(row, '代码', '证券代码', '股票代码', 'SECURITY_CODE', 'SECUCODE', '代码_Code', default=''))


def _row_name(row: dict) -> str:
    return str(_pick(row, '名称', '证券简称', '股票简称', 'SECURITY_NAME_ABBR', 'SECURITY_NAME', default=''))


def _seat_text(row: dict) -> str:
    parts = []
    for key in ('营业部名称', '买方营业部', '卖方营业部', 'BUYER_NAME', 'SELLER_NAME', '解读', '上榜原因'):
        text = str(row.get(key) or '').strip()
        if text:
            parts.append(text)
    return ' '.join(parts)


def _seat_watchlist_hit(text: str) -> bool:
    return any(seat in text for seat in _SEAT_WATCHLIST)


def _build_lhb(code: str, rows: list[dict]) -> dict:
    matched = [row for row in rows if _row_code(row) == code]
    if not matched:
        return {'hit': False}
    buy = sum(_safe_float(_pick(row, '龙虎榜买入额', 'BILLBOARD_BUY_AMT', '买入金额', default=0)) for row in matched)
    sell = sum(_safe_float(_pick(row, '龙虎榜卖出额', 'BILLBOARD_SELL_AMT', '卖出金额', default=0)) for row in matched)
    net = sum(_safe_float(_pick(row, '龙虎榜净买额', 'BILLBOARD_NET_AMT', '净买额', default=0)) for row in matched)
    if net == 0 and (buy or sell):
        net = buy - sell
    side = 'mixed'
    if net > 0:
        side = 'buy'
    elif net < 0:
        side = 'sell'
    seat_texts = [_seat_text(row) for row in matched]
    seats = []
    for text in seat_texts:
        for seat in _SEAT_WATCHLIST:
            if seat in text and seat not in seats:
                seats.append(seat)
    if not seats:
        seats = [text[:30] for text in seat_texts if text][:5]
    return {
        'hit': True,
        'side': side,
        'seats': seats[:5],
        'watchlist_hit': any(_seat_watchlist_hit(text) for text in seat_texts),
        'net_amount_yi': round(net / 100000000, 2),
    }


def _build_hsgt(code: str, rows: list[dict]) -> dict:
    matched = [row for row in rows if _row_code(row) == code]
    if not matched:
        return {'hit': False, 'reason': '非北向标的或暂无持仓数据'}
    row = matched[0]
    hold_pct = _safe_float(_pick(
        row,
        '今日持股-占总股本比',
        '今日持股-占流通股比',
        '持股占比',
        '持股占A股百分比',
        '持股占流通A股比例',
        'HOLD_RATIO',
        default=0,
    ))
    change = _safe_float(_pick(
        row,
        '今日增持估计-占总股本比',
        '今日增持估计-占流通股比',
        '持股比例增减',
        '持股占比增减',
        '5日增持估计-占总股本比',
        '5日增持估计-占流通股比',
        'CHANGE_RATE',
        default=0,
    ))
    if abs(change) < 0.5:
        direction = 'flat'
    else:
        direction = 'in' if change > 0 else 'out'
    return {'hit': True, 'hold_pct': round(hold_pct, 3), 'hold_pct_d1_change': round(change, 3), 'direction': direction}


def _build_dzjy(code: str, rows: list[dict]) -> dict:
    matched = [row for row in rows if _row_code(row) == code]
    if not matched:
        return {'hit': False}
    premiums = [_safe_float(_pick(row, '折溢率', '折溢价率', '溢价率', 'PREMIUM_RATIO', default=0)) for row in matched]
    amounts = [_safe_float(_pick(row, '成交额', '成交金额', 'DEAL_AMT', default=0)) for row in matched]
    premium_avg = sum(premiums) / len(premiums) if premiums else 0.0
    return {'hit': True, 'rows': len(matched), 'premium_pct_avg': round(premium_avg, 2), 'amount_yi': round(sum(amounts) / 100000000, 2)}


def get_public_evidence(code: str, name: str | None = None) -> dict:
    pure_code = normalize_code(code)
    if is_etf(pure_code):
        return {'available': False, 'reason': 'ETF 不适用公开席位证据'}
    date_str = _effective_date()
    result = {'available': True, 'date': f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'}
    try:
        result['lhb'] = _build_lhb(pure_code, _load_rows('lhb', date_str, _fetch_lhb_rows))
    except Exception as e:
        result['lhb'] = {'hit': False, 'error': str(e)[:80]}
    try:
        result['hsgt'] = _build_hsgt(pure_code, _load_rows('hsgt', date_str, _fetch_hsgt_rows))
    except Exception as e:
        result['hsgt'] = {'hit': False, 'error': str(e)[:80]}
    try:
        result['dzjy'] = _build_dzjy(pure_code, _load_rows('dzjy', date_str, _fetch_dzjy_rows))
    except Exception as e:
        result['dzjy'] = {'hit': False, 'error': str(e)[:80]}
    result['fetched_at'] = datetime.now().isoformat(timespec='seconds')
    return result


__all__ = ['get_public_evidence']
