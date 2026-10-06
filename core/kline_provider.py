"""K 线数据拉取 + 技术指标计算 + prompt 摘要。

数据源：mootdx (通达信协议)
指标套装（1B 中等）：
  核心：MA5/10/20/60  RSI14  MACD  布林带(20,2)  KDJ(9,3)  ATR14（止损刚需）
  可选：OBV 5日趋势

缓存：CACHE_DIR/kline/{code}.json
  — 当日 09:30 前复用前一日缓存；09:30 后缓存当日数据到次日 09:30
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

from core.paths import CACHE_DIR as _BASE_CACHE
from core.board_rules import (
    ETF_ALLOWED_TAGS,
    is_etf as _board_is_etf,
    is_limit_down,
    is_limit_up,
    is_one_word_board,
    is_t_word_board,
)
from core.js_runtime import js_runtime_diagnostic
_CACHE_DIR = _BASE_CACHE / 'kline'
_MARKET_OPEN = dtime(9, 30)


def _js_diag_suffix() -> str:
    diag = js_runtime_diagnostic()
    return f' ({diag})' if diag else ''


def _latest_available_trade_date(today: date | None = None) -> str:
    now = datetime.now()
    d = today or now.date()
    if today is None and (now.hour, now.minute) < (9, 30):
        d -= timedelta(days=1)
    try:
        from core.trade_calendar import is_trade_day
        for _ in range(20):
            if is_trade_day(d):
                return d.isoformat()
            d -= timedelta(days=1)
    except Exception:
        pass
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _row_get(row, key: str):
    try:
        return row.get(key)
    except AttributeError:
        return None


def _finite_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _is_bad_tail_bar(row) -> bool:
    prices = [_finite_float(_row_get(row, k)) for k in ('open', 'high', 'low', 'close')]
    if any(v is None or v <= 0 for v in prices):
        return False
    if max(prices) - min(prices) > max(abs(prices[-1]) * 1e-8, 1e-8):
        return False
    volume = _finite_float(_row_get(row, 'volume'))
    amount = _finite_float(_row_get(row, 'amount'))
    if volume is None:
        return False
    return abs(volume) <= 1e-6 and (amount is None or abs(amount) <= 1e-6)


def _drop_bad_tail_bars(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    out = df.copy()
    while not out.empty and _is_bad_tail_bar(out.iloc[-1]):
        out = out.iloc[:-1]
    return out


def _cache_last_bar_date(meta: dict) -> str | None:
    rows = meta.get('rows') or []
    if not rows:
        return None
    for last in reversed(rows):
        if not isinstance(last, dict) or _is_bad_tail_bar(last):
            continue
        raw = last.get('datetime') or last.get('date')
        if not raw:
            year, month, day = last.get('year'), last.get('month'), last.get('day')
            if year and month and day:
                return f'{int(year):04d}-{int(month):02d}-{int(day):02d}'
            continue
        text = str(raw)
        if len(text) >= 10:
            return text[:10]
    return None


def _read_cached_kline(code: str) -> pd.DataFrame | None:
    meta = json.loads(_cache_path(code).read_text(encoding='utf-8'))
    df = pd.DataFrame(meta['rows'], columns=meta['columns'])
    df.index = pd.to_datetime(df['datetime'])
    return _drop_bad_tail_bars(df)


def _cache_path(code: str) -> Path:
    return _CACHE_DIR / f'{code}.json'


def _cache_is_fresh(code: str) -> bool:
    """当日 09:30 前：复用前一日缓存；09:30 后：当日缓存有效。"""
    p = _cache_path(code)
    if not p.exists():
        return False
    try:
        meta = json.loads(p.read_text(encoding='utf-8'))
        expected = _latest_available_trade_date()
        return (_cache_last_bar_date(meta) or '') >= expected
    except Exception:
        return False


def _norm_code(code: str) -> tuple[str, str]:
    """6 位代码 → (mootdx 市场 id, 纯代码)。

    ETF 优先（与 data_source._normalize_code 保持一致）：
      深市 ETF 15x/16x/18x → market=0
      沪市 ETF 50x/51x/52x/56x/58x → market=1
    股票：6x/9x → 1(sh)；其余 → 0(sz)
    """
    c = str(code).strip()[-6:]
    if c.startswith(('15', '16', '18')):
        return '0', c   # 深市 ETF
    if c.startswith(('50', '51', '52', '56', '58')):
        return '1', c   # 沪市 ETF
    if c.startswith('92'):
        return '0', c   # 北交所新代码段
    if c.startswith(('6', '9')):
        return '1', c   # 沪市股票
    return '0', c       # 深市股票/其他


def _index_secid(code: str) -> str:
    c = str(code).strip()[-6:]
    market = '1' if c.startswith(('000', '001', '880', '881', '883', '884', '885', '886')) else '0'
    return f'{market}.{c}'


def _is_etf(code: str) -> bool:
    """判断是否为 ETF（场内基金）。"""
    return _board_is_etf(code)


def _fetch_via_mootdx(code: str, days: int) -> pd.DataFrame | None:
    """通过 mootdx 拉取 K 线；使用 _norm_code 显式路由 ETF/股票市场。"""
    try:
        from mootdx.quotes import Quotes
        market_id, sym = _norm_code(code)
        client = Quotes.factory(market='std')
        df = client.bars(symbol=sym, frequency=9, start=0, offset=max(days, 60))
        if df is None or df.empty:
            logger.debug('[kline] mootdx 返回空 %s', code)
            return None
        df.index = pd.to_datetime(df['datetime'])
        return df.sort_index()
    except Exception as e:
        logger.debug('[kline] mootdx 失败 %s: %s', code, e)
        return None


def _fetch_via_eastmoney_http(code: str, days: int) -> pd.DataFrame | None:
    """直接请求东方财富 HTTP K 线 API（纯 HTTPS，零额外依赖）。

    这是最稳的兜底：只依赖 requests + pandas，不经过 mootdx/akshare，
    不会被非标准端口防火墙拦截。
    """
    try:
        import requests
        market_id, sym = _norm_code(code)
        secid = f'{market_id}.{sym}'
        today = datetime.now().strftime('%Y%m%d')
        start = (datetime.now().replace(hour=0, minute=0) -
                 pd.Timedelta(days=days + 30)).strftime('%Y%m%d')
        url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        params = {
            'secid': secid,
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
            'klt': '101',      # 日 K
            'fqt': '1',        # 前复权
            'beg': start,
            'end': today,
            'lmt': str(max(days, 60) + 10),
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://quote.eastmoney.com/',
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data or data.get('rc') != 0:
            logger.debug('[kline] 东方财富 HTTP 返回异常 %s: %s', code, data)
            return None
        klines = (data.get('data') or {}).get('klines') or []
        if not klines:
            logger.debug('[kline] 东方财富 HTTP 返回空 %s', code)
            return None
        rows = []
        for line in klines:
            parts = str(line).split(',')
            if len(parts) < 8:
                continue
            rows.append({
                'datetime': parts[0],
                'open': float(parts[1]),
                'close': float(parts[2]),
                'high': float(parts[3]),
                'low': float(parts[4]),
                'volume': float(parts[5]),
                'amount': float(parts[6]),
                'turnover': float(parts[10]) if len(parts) > 10 else 0.0,
            })
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.set_index('datetime').sort_index()
        return df.tail(max(days, 60))
    except Exception as e:
        logger.debug('[kline] 东方财富 HTTP 失败 %s: %s', code, e)
        return None


def _tickflow_kline_token() -> str:
    try:
        from core.credentials import load_tickflow_token, load_tickflow_free_token
        tok = load_tickflow_token()
        if tok:
            return tok
        return load_tickflow_free_token()
    except Exception:
        return ''


def fetch_tickflow_kline(symbol: str, count: int = 600) -> pd.DataFrame | None:
    """TickFlow /v1/klines 日 K（symbol 形如 000985.SH / 600000.SH / 300750.SZ）。"""
    token = _tickflow_kline_token()
    if not token:
        return None
    try:
        import requests
        r = requests.get(
            'https://api.tickflow.org/v1/klines',
            headers={'X-API-Key': token, 'Authorization': f'Bearer {token}'},
            params={'symbol': symbol, 'period': '1d', 'count': str(max(count, 60))},
            timeout=12,
        )
        r.raise_for_status()
        data = (r.json() or {}).get('data') or {}
        ts = data.get('timestamp') or []
        if not ts:
            return None
        df = pd.DataFrame({
            'open': data.get('open') or [],
            'high': data.get('high') or [],
            'low': data.get('low') or [],
            'close': data.get('close') or [],
            'volume': data.get('volume') or [],
            'amount': data.get('amount') or [],
        })
        df.index = (
            pd.to_datetime(ts, unit='ms', utc=True)
            .tz_convert('Asia/Shanghai')
            .tz_localize(None)
        )
        return df.sort_index()
    except Exception as e:
        logger.debug('[kline] tickflow 失败 %s: %s', symbol, e)
        return None


def _fetch_index_via_akshare(code: str, days: int) -> pd.DataFrame | None:
    """指数日 K 的 akshare 兜底（东财 HTTP 不通时用）。"""
    try:
        import akshare as ak
        c = str(code).strip()[-6:]
        mkt = 'sh' if c.startswith(('000', '001', '880', '881', '883', '884', '885', '886')) else 'sz'
        raw = ak.stock_zh_index_daily_em(symbol=f'{mkt}{c}')
        if raw is None or raw.empty:
            logger.debug('[kline] 指数 akshare 返回空 %s%s', code, _js_diag_suffix())
            return None
        raw = raw.rename(columns={'date': 'datetime'})
        raw['datetime'] = pd.to_datetime(raw['datetime'])
        return raw.set_index('datetime').sort_index().tail(max(days, 60))
    except Exception as e:
        logger.debug('[kline] 指数 akshare 兜底失败 %s: %s%s', code, e, _js_diag_suffix())
        return None


def fetch_index_kline(code: str = '000300', days: int = 600) -> pd.DataFrame | None:
    """Fetch index daily K-line via Eastmoney secid, e.g. CSI300 -> 1.000300。
    东财 HTTP 失败时回退 akshare。"""
    try:
        import requests
        secid = _index_secid(code)
        today = datetime.now().strftime('%Y%m%d')
        start = (datetime.now().replace(hour=0, minute=0) -
                 pd.Timedelta(days=days + 30)).strftime('%Y%m%d')
        url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        params = {
            'secid': secid,
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
            'klt': '101',
            'fqt': '1',
            'beg': start,
            'end': today,
            'lmt': str(max(days, 60) + 10),
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://quote.eastmoney.com/',
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        klines = (data.get('data') or {}).get('klines') or []
        rows = []
        for line in klines:
            parts = str(line).split(',')
            if len(parts) < 8:
                continue
            rows.append({
                'datetime': parts[0],
                'open': float(parts[1]),
                'close': float(parts[2]),
                'high': float(parts[3]),
                'low': float(parts[4]),
                'volume': float(parts[5]),
                'amount': float(parts[6]),
                'turnover': float(parts[10]) if len(parts) > 10 else 0.0,
            })
        if not rows:
            return _index_fallback(code, days)
        df = pd.DataFrame(rows)
        df['datetime'] = pd.to_datetime(df['datetime'])
        return df.set_index('datetime').sort_index().tail(max(days, 60))
    except Exception as e:
        logger.debug('[kline] 指数K线失败 %s: %s', code, e)
        return _index_fallback(code, days)


def _index_fallback(code: str, days: int) -> pd.DataFrame | None:
    """东财指数失败时的兜底：TickFlow(免费节点) -> akshare。"""
    df = fetch_tickflow_kline(f'{str(code).strip()[-6:]}.SH', count=days)
    if df is not None and not df.empty:
        return df.tail(max(days, 60))
    return _fetch_index_via_akshare(code, days)


def _fetch_via_akshare(code: str, days: int) -> pd.DataFrame | None:
    """通过 akshare 拉取 K 线（mootdx 失败时的兜底）。

    ETF → ak.fund_etf_hist_em；股票 → ak.stock_zh_a_hist
    """
    try:
        import akshare as ak
        period = 'daily'
        adjust = 'qfq'
        if _is_etf(code):
            raw = ak.fund_etf_hist_em(
                symbol=code[-6:], period=period, adjust=adjust
            )
        else:
            raw = ak.stock_zh_a_hist(
                symbol=code[-6:], period=period, adjust=adjust
            )
        if raw is None or raw.empty:
            logger.debug('[kline] akshare 返回空 %s%s', code, _js_diag_suffix())
            return None
        raw = raw.rename(columns={
            '日期': 'datetime', '开盘': 'open', '最高': 'high',
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
            '成交额': 'amount', '换手率': 'turnover',
        })
        need = [c for c in ('datetime', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover') if c in raw.columns]
        raw = raw[need].copy()
        raw['datetime'] = pd.to_datetime(raw['datetime'])
        raw = raw.set_index('datetime').sort_index()
        return raw.tail(max(days, 60))
    except Exception as e:
        logger.debug('[kline] akshare 失败 %s: %s%s', code, e, _js_diag_suffix())
        return None


def _price_sanity_ok(df: pd.DataFrame, code: str, tol: float = 0.35) -> bool:
    """校验 K 线最后一根 close 是否与路由实时价吻合（偏离 > tol 视为坏数据）。

    tol=0.35 允许 35% 偏差（非交易时段可能有合理差距）。
    网络失败时放行（返回 True），避免双重失败。
    """
    try:
        close = float(df['close'].iloc[-1])
        if close <= 0:
            return False
        from core.data_source import quotes_routed
        q = quotes_routed([code])
        rt_price = (q.get(code) or {}).get('price')
        if rt_price is None or rt_price <= 0:
            return True  # 实时价拉不到，放行
        ratio = abs(close - rt_price) / rt_price
        return ratio <= tol
    except Exception:
        return True


def _has_paid_tickflow_token() -> bool:
    """检查是否配置了 TickFlow token。"""
    try:
        from core.credentials import load_tickflow_token
        return bool(load_tickflow_token())
    except Exception:
        return False


def fetch_daily_kline(code: str, days: int = 120) -> pd.DataFrame | None:
    """拉取日 K 数据，自动走缓存。失败返回 None。

    优先级：新鲜缓存 → TickFlow付费（已配置时）→ mootdx → 东方财富 HTTP → akshare
    价格合理性校验：末根 close 与路由实时价偏离 > 35% 时尝试下一源补救。
    """
    if _cache_is_fresh(code):
        try:
            df = _read_cached_kline(code)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass

    # 真正串行短路：付费源有效即停止，不再额外打免费接口。
    source_fetchers = []
    if _has_paid_tickflow_token():
        def _fetch_paid_tickflow():
            from core.data_source import _normalize_code
            c = _normalize_code(code)
            return fetch_tickflow_kline(f'{c[2:]}.{c[:2].upper()}', count=days)
        source_fetchers.append(('tickflow-paid', _fetch_paid_tickflow))
    source_fetchers.extend([
        ('mootdx', lambda: _fetch_via_mootdx(code, days)),
        ('eastmoney', lambda: _fetch_via_eastmoney_http(code, days)),
        ('akshare', lambda: _fetch_via_akshare(code, days)),
    ])

    df = None
    for source_name, fetcher in source_fetchers:
        try:
            candidate = _drop_bad_tail_bars(fetcher())
        except Exception as e:
            logger.debug('[kline] %s 拉取失败 %s: %s', source_name, code, e)
            continue
        if candidate is not None and not candidate.empty:
            if _price_sanity_ok(candidate, code):
                df = candidate
                logger.debug('[kline] 使用 %s 数据 %s', source_name, code)
                break
            logger.debug('[kline] %s 价格异常 %s，继续尝试下一源', source_name, code)

    df = _drop_bad_tail_bars(df)
    if df is None or df.empty:
        return None

    # 写缓存
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        meta = _kline_cache_payload(df)
        _cache_path(code).write_text(
            json.dumps(meta, ensure_ascii=False, default=str), encoding='utf-8'
        )
    except Exception as e:
        logger.warning('[kline] 缓存写入失败 %s: %s', code, e)
    return df


def _kline_cache_payload(df: pd.DataFrame) -> dict:
    """Build JSON cache payload without duplicating an existing datetime column."""
    cache_df = df.copy()
    cache_df.columns = [str(c) for c in cache_df.columns]
    if 'datetime' in cache_df.columns:
        cache_df = cache_df.reset_index(drop=True)
    else:
        cache_df = cache_df.reset_index()
        first = str(cache_df.columns[0])
        if first != 'datetime':
            cache_df = cache_df.rename(columns={first: 'datetime'})
    cache_df = cache_df.loc[:, ~cache_df.columns.duplicated()]
    return {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'columns': cache_df.columns.tolist(),
        'rows': cache_df.to_dict(orient='records'),
    }


def _safe(val) -> float | None:
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except Exception:
        return None


def _round2(val) -> float | None:
    v = _safe(val)
    return round(v, 3) if v is not None else None


def _pct(a, b) -> float | None:
    a = _safe(a)
    b = _safe(b)
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / b * 100, 2)


def _levels(values, close: float, above: bool, limit: int = 3) -> list[float]:
    out = []
    for v in values:
        x = _round2(v)
        if x is None:
            continue
        if (above and x > close) or ((not above) and x < close):
            out.append(x)
    out = sorted(set(out), reverse=not above)
    return out[:limit]


_ST_TARGET_CAP = {2: 0.07, 3: 0.09, 4: 0.11, 5: 0.13}
_ST_TRIGGER_CAP = {2: 0.04, 3: 0.05, 4: 0.06, 5: 0.07}


def _st_horizon(hz) -> int:
    try:
        h = int(hz)
    except (TypeError, ValueError):
        return 5
    return min(5, max(2, h))


def short_term_target_cap_pct(hz) -> float:
    return _ST_TARGET_CAP[_st_horizon(hz)] * 100


def short_term_band(atr_pct, hz):
    h = _st_horizon(hz)
    a = atr_pct if (atr_pct and atr_pct > 0) else 0.03
    tgt = min(a * h * 0.8, _ST_TARGET_CAP[h])
    stop = min(tgt / 2.0, 1.2 * a)
    return round(tgt, 4), round(stop, 4)


def _tag(tag: str, confidence: int, evidence: list[str] | None = None,
         counter_evidence: list[str] | None = None, data_quality: str = 'good') -> dict:
    return {
        'tag': tag,
        'confidence': max(0, min(10, int(confidence))),
        'evidence': evidence or [],
        'counter_evidence': counter_evidence or [],
        'data_quality': data_quality,
    }


def _detect_divergence(df: pd.DataFrame) -> str | None:
    """MACD 柱与价格的经典背离检测。

    返回 'bottom'（底背离：价创新低、MACD柱低点抬高）
        / 'top'（顶背离：价创新高、MACD柱高点走低）/ None。
    用近 30 根 K 线，分为前半段(-30..-12)与后半段(-12..)比较极值。
    """
    try:
        if df is None or len(df) < 35 or 'close' not in df.columns:
            return None
        import talib
        close_arr = df['close'].astype(float).to_numpy()
        _, _, hist_arr = talib.MACD(
            close_arr,
            fastperiod=12,
            slowperiod=26,
            signalperiod=9,
        )
        hist = pd.Series(hist_arr, index=df.index).reset_index(drop=True)
        close = df['close'].reset_index(drop=True)
        high = (df['high'] if 'high' in df.columns else df['close']).reset_index(drop=True)
        low = (df['low'] if 'low' in df.columns else df['close']).reset_index(drop=True)
        n = len(close)
        prev = slice(n - 30, n - 12)   # 前段
        recent = slice(n - 12, n)      # 后段
        h_prev, h_recent = hist[prev], hist[recent]
        if h_prev.isna().all() or h_recent.isna().all():
            return None
        # 底背离：后段价格更低，但 MACD 柱低点抬高
        if low[recent].min() < low[prev].min() and h_recent.min() > h_prev.min():
            return 'bottom'
        # 顶背离：后段价格更高，但 MACD 柱高点走低
        if high[recent].max() > high[prev].max() and h_recent.max() < h_prev.max():
            return 'top'
        return None
    except Exception:
        return None


def _series_rank_latest(series: pd.Series, window: int = 60) -> float | None:
    vals = series.dropna().tail(window)
    if vals.empty:
        return None
    latest = vals.iloc[-1]
    return float((vals <= latest).sum() / len(vals))


def _count_consec_shrink(volume: pd.Series, ratio: float = 0.7) -> int:
    vals = volume.dropna()
    if len(vals) < 21:
        return 0
    avg20 = vals.rolling(20).mean()
    count = 0
    for idx in range(len(vals) - 1, -1, -1):
        base = avg20.iloc[idx]
        if not base or pd.isna(base) or vals.iloc[idx] > base * ratio:
            break
        count += 1
    return count


def _vcp_contractions(df: pd.DataFrame) -> int:
    amps = []
    for window in (40, 20, 10):
        if len(df) < window or not {'high', 'low', 'close'}.issubset(df.columns):
            return 0
        part = df.tail(window)
        close = float(part['close'].iloc[-1] or 0)
        if close <= 0:
            return 0
        amps.append((float(part['high'].max()) - float(part['low'].min())) / close)
    return sum(1 for prev, cur in zip(amps, amps[1:]) if cur < prev)


def _days_since_breakout(df: pd.DataFrame) -> int | None:
    if len(df) < 21 or not {'close', 'high'}.issubset(df.columns):
        return None
    for back in range(0, min(20, len(df) - 20)):
        idx = len(df) - 1 - back
        prior = df.iloc[:idx].tail(20)
        if prior.empty:
            continue
        if float(df['close'].iloc[idx]) > float(prior['high'].max()):
            return back
    return None


def _public_evidence_hit(public_evidence: dict | None) -> bool:
    if not isinstance(public_evidence, dict) or not public_evidence.get('available', True):
        return False
    return bool(
        public_evidence.get('lhb', {}).get('hit')
        or public_evidence.get('hsgt', {}).get('direction') == 'in'
        or (public_evidence.get('dzjy', {}).get('premium_pct_avg') or 0) > 2
    )


def _build_behavior_tags(
    code: str,
    name: str | None,
    df: pd.DataFrame,
    ind: dict,
    profile: dict,
    flow_profile: dict | None = None,
    public_evidence: dict | None = None,
    market_phase: str | None = None,
) -> list[dict]:
    if df is None or df.empty:
        return [_tag('数据不足', 4, ['K线数据缺失'], data_quality='insufficient')]
    df = df.copy()
    df.columns = df.columns.str.lower()
    if 'vol' in df.columns and 'volume' not in df.columns:
        df['volume'] = df['vol']
    if len(df) < 60:
        return [_tag('数据不足', 4, [f'K线少于60根（{len(df)}根）'], data_quality='insufficient')]

    is_etf_code = _is_etf(code)
    close = float(df['close'].iloc[-1])
    open_ = float(df['open'].iloc[-1]) if 'open' in df.columns else close
    high = float(df['high'].iloc[-1]) if 'high' in df.columns else close
    low = float(df['low'].iloc[-1]) if 'low' in df.columns else close
    prev_close = float(df['close'].iloc[-2]) if len(df) >= 2 else close
    volume = float(df['volume'].iloc[-1]) if 'volume' in df.columns else 0.0

    if len(df) >= 2 and is_one_word_board(open_, high, low, close):
        return [_tag('数据异常', 4, ['一字板，无可执行突破'], data_quality='abnormal')]
    if len(df) >= 2 and is_t_word_board(open_, high, low, close, prev_close, code, name):
        return [_tag('数据异常', 4, ['T字板，量能假象'], data_quality='abnormal')]

    if 'volume' in df.columns and len(df) >= 6:
        recent = df.tail(5)
        if (recent['volume'].iloc[:-1] == 0).any():
            prev_vol = float(recent['volume'].iloc[-2] or 0)
            if prev_vol == 0 and volume > 0:
                return [_tag('数据异常', 4, ['复牌首日量能失真'], data_quality='abnormal')]

    latest_limit_up = len(df) >= 2 and is_limit_up(prev_close, close, code, name)
    latest_limit_down = len(df) >= 2 and is_limit_down(prev_close, close, code, name)

    vol_series = df['volume'] if 'volume' in df.columns else pd.Series(dtype=float)
    vol_60d_rank = _series_rank_latest(vol_series, 60) if not vol_series.empty else None
    vol_ma20 = _safe(df.iloc[:-1].tail(20)['volume'].mean()) if 'volume' in df.columns and len(df) > 20 else None
    vol_ma5 = _safe(df.tail(5)['volume'].mean()) if 'volume' in df.columns and len(df) >= 5 else None
    volume_ratio = (volume / vol_ma20) if vol_ma20 and vol_ma20 > 0 else (profile.get('volume') or {}).get('volume_ratio')
    consec_shrink_days = _count_consec_shrink(vol_series) if not vol_series.empty else 0
    vcp = _vcp_contractions(df)
    ma = profile.get('ma') or {}
    ma5, ma20, ma40, ma60 = ma.get('ma5'), ma.get('ma20'), ma.get('ma40'), ma.get('ma60')
    close_series = df['close']
    ma10_series = close_series.rolling(10).mean()
    ma20_series = close_series.rolling(20).mean()
    ma5_series = close_series.rolling(5).mean()
    triple_open = bool(
        len(df) >= 25
        and ma5_series.iloc[-1] > ma10_series.iloc[-1] > ma20_series.iloc[-1]
        and ma5_series.iloc[-1] > ma5_series.iloc[-5]
        and ma10_series.iloc[-1] > ma10_series.iloc[-5]
        and ma20_series.iloc[-1] > ma20_series.iloc[-5]
    )
    days_breakout = _days_since_breakout(df)
    cum_pct_5d = _pct(close, df['close'].iloc[-6]) if len(df) >= 6 else None
    cum_pct_10d = _pct(close, df['close'].iloc[-11]) if len(df) >= 11 else None
    cum_pct_20d = _pct(close, df['close'].iloc[-21]) if len(df) >= 21 else None
    volume_dry = bool(vol_60d_rank is not None and vol_60d_rank < 0.10)
    volume_extreme = bool(vol_60d_rank is not None and vol_60d_rank > 0.95)
    trend_stage = profile.get('trend_stage')
    long_upper = bool((profile.get('volume') or {}).get('long_upper'))
    long_lower = bool((profile.get('volume') or {}).get('long_lower'))
    volume_down = bool((profile.get('volume') or {}).get('volume_down'))
    high20 = (profile.get('range') or {}).get('high20')
    low20 = (profile.get('range') or {}).get('low20')
    low5 = (profile.get('range') or {}).get('low5')
    breakdown = bool(low20 and close < low20)
    flow_profile = flow_profile or {}
    main_3d = _safe(flow_profile.get('main_3d')) or 0.0
    main_streak = int(flow_profile.get('main_streak') or 0)
    public_hit = _public_evidence_hit(public_evidence)

    tags: list[dict] = []
    limit_counter = ['当日涨停，T+1 无法买入'] if latest_limit_up else []
    if latest_limit_down and not is_etf_code:
        tags.append(_tag('疑似破位', 5, ['当日跌停，弱势信号明确'], data_quality='good'))

    body_mid_strong = high > low and close >= (high + low) / 2 + (high - low) * 0.3
    amount_ok = False
    if 'amount' in df.columns and len(df) >= 6:
        amount_today = _safe(df['amount'].iloc[-1]) or 0
        amount_ma5 = _safe(df['amount'].iloc[:-1].tail(5).mean()) or 0
        amount_ok = bool(amount_ma5 > 0 and amount_today >= amount_ma5 * 1.5)
    breakout_now = bool(high20 and close > high20)
    startup_conditions = [
        breakout_now,
        bool((volume_ratio or 0) >= (1.3 if is_etf_code else 1.8)),
        body_mid_strong,
        amount_ok,
        triple_open,
        bool(main_3d > 0 and main_streak >= 1),
        public_hit and not is_etf_code,
    ]
    can_start = (
        (days_breakout is None or days_breakout == 0 or days_breakout >= 5)
        and (cum_pct_10d is None or cum_pct_10d <= 30)
        and trend_stage in ('uptrend', 'recovery', 'sideways')
        and not latest_limit_down
    )
    accelerate = (cum_pct_10d is not None and cum_pct_10d > 30) or (
        len(df) >= 4 and all(_pct(df['close'].iloc[i], df['close'].iloc[i - 1]) and _pct(df['close'].iloc[i], df['close'].iloc[i - 1]) > 4 for i in range(len(df) - 3, len(df)))
    )
    acceleration_conditions = [
        volume_extreme,
        long_upper,
        days_breakout is not None and days_breakout <= 3,
        bool(main_3d < 0),
    ]
    if not is_etf_code and accelerate and sum(bool(x) for x in acceleration_conditions) >= 2:
        tags.append(_tag('疑似加速派发风险', 7, ['短期涨幅过大', '高位量价出现分歧'], ['高位冲高不等于新启动']))
    elif can_start and sum(bool(x) for x in startup_conditions) >= 3:
        cap = 6
        if public_hit and not is_etf_code:
            cap = 7
        if triple_open and main_3d > 0 and not is_etf_code:
            cap = 8
        confidence = min(cap, 5 + sum(bool(x) for x in startup_conditions))
        if latest_limit_up:
            confidence = max(0, confidence - 2)
        evidence = ['突破平台或接近关键高点', f'量比{(volume_ratio or 0):.2f}', '收盘靠近上沿']
        if triple_open:
            evidence.append('三线开花')
        if public_hit and not is_etf_code:
            evidence.append('公开证据命中')
        tags.append(_tag('疑似启动初段', confidence, evidence, ['若次日不能持续放量站稳触发价，启动失败'] + limit_counter))

    if not is_etf_code:
        # 疑似拉升中段：已突破启动之后、均线多头排列延续上行、涨幅尚未过度透支
        # （介于"疑似启动初段"与"疑似加速派发风险"之间，是 A 股主升浪跟随窗口）
        markup_conditions = [
            triple_open,
            bool(days_breakout is not None and 1 <= days_breakout <= 8),
            bool(ma20 and close > ma20 and (ma.get('ma20_slope_pct') or 0) > 0),
            bool((volume_ratio or 0) >= 1.2),
            bool(main_3d > 0 or main_streak >= 1),
        ]
        if (
            trend_stage == 'uptrend' and not latest_limit_up and not latest_limit_down
            and (cum_pct_10d is None or cum_pct_10d <= 30)
            and sum(bool(x) for x in markup_conditions) >= 4
        ):
            # 证据按实际命中的条件动态生成，不硬编“主力资金延续”（避免与净流出打架）
            markup_ev = ['均线多头排列上行', f'量比{(volume_ratio or 0):.2f}']
            if ma20 and close > ma20 and (ma.get('ma20_slope_pct') or 0) > 0:
                markup_ev.append(f'站稳MA20（斜率+{(ma.get("ma20_slope_pct") or 0):.1f}%）')
            if main_3d > 0:
                markup_ev.append('主力资金近3日净流入延续')
            elif main_streak >= 1:
                markup_ev.append('主力资金连续净流入')
            tags.append(_tag(
                '疑似拉升中段', 6,
                markup_ev,
                ['拉升中途随时可能转洗盘或高位见顶，需带保护性止损跟随；一旦放量滞涨/跌破MA20即降级'],
            ))
        if trend_stage in ('sideways', 'recovery') and cum_pct_20d is not None and -15 <= cum_pct_20d <= 10:
            absorb_conditions = [
                vcp >= 2,
                consec_shrink_days >= 3 or (vol_ma5 and vol_ma20 and vol_ma5 / vol_ma20 < 0.7),
                bool((ma.get('ma20_slope_pct') or 0) >= -0.5),
                volume_dry,
                main_streak >= 0,
            ]
            if sum(bool(x) for x in absorb_conditions) >= 3:
                tags.append(_tag('疑似吸筹', 6, ['平台收缩', '缩量整理', '均线走平或上拐'], ['横盘可能转为破位，吸筹仅是概率判断']))
        if trend_stage == 'uptrend' and not latest_limit_down:
            pullback_conditions = [
                bool(ma20 and ma20 * 0.92 <= close <= ma20 * 1.03),
                long_lower,
                bool(low5 and low20 and low < low5 and low >= low20),
                bool(close > open_),
            ]
            if ma40 and close < ma40:
                tags.append(_tag('疑似破位', 5, ['MA40 已破，洗盘假设失效']))
            elif sum(bool(x) for x in pullback_conditions) >= 2:
                tags.append(_tag('疑似洗盘', 6, ['上升趋势回踩', '下影线或关键位收复'], ['若继续跌破 MA40，则洗盘假设失效']))
        day_close_pos = ((close - low) / (high - low)) if high > low else 0.5
        range20_pos = ((close - low20) / (high20 - low20)) if (high20 and low20 and high20 > low20) else None
        recent_limit_ups = 0
        for i in range(max(1, len(df) - 4), len(df)):
            try:
                if is_limit_up(float(df['close'].iloc[i - 1]), float(df['close'].iloc[i]), code, name):
                    recent_limit_ups += 1
            except Exception:
                pass
        post_board_weak = bool(
            recent_limit_ups >= 1 and not latest_limit_up
            and day_close_pos <= 0.45
            and (range20_pos is not None and range20_pos >= 0.65)
        )
        lhb_sell = bool((public_evidence or {}).get('lhb', {}).get('side') == 'sell')
        distribution_conditions = [
            bool(range20_pos is not None and range20_pos >= 0.70),
            bool((volume_ratio or 0) >= 1.8 or (vol_60d_rank is not None and vol_60d_rank >= 0.85)),
            bool(long_upper or day_close_pos <= 0.40),
            bool(ma20 and close < ma20),
            bool(main_3d < 0 or main_streak <= -2),
            post_board_weak,
            lhb_sell,
        ]
        hard_distribution = [
            bool(ma20 and close < ma20),
            bool(main_3d < 0 or main_streak <= -2),
            post_board_weak,
            lhb_sell,
        ]
        distribution_score = sum(bool(x) for x in distribution_conditions)
        if distribution_score >= 4 and any(hard_distribution):
            ev = [f'出货相共振{distribution_score}/7']
            if volume_ratio is not None:
                ev.append(f'量比{volume_ratio:.1f}')
            if range20_pos is not None:
                ev.append(f'20日位置{range20_pos*100:.0f}%')
            ev.append(f'日内收位{day_close_pos*100:.0f}%')
            if recent_limit_ups:
                ev.append(f'近4日涨停{recent_limit_ups}次后转弱')
            if main_3d < 0 or main_streak <= -2:
                ev.append('主力资金转弱')
            tags.append(_tag(
                '出货相', min(9, 5 + distribution_score), ev,
                ['若次日放量收复MA20/前高且主力资金同步回流，则出货相失效'],
            ))

        ship_conditions = [
            bool((volume_ratio or 0) >= 1.5 and abs(_pct(close, prev_close) or 0) < 1),
            long_upper,
            bool(ma20 and close < ma20),
            volume_down,
            main_3d < 0,
            lhb_sell,
        ]
        if trend_stage in ('uptrend', 'recovery') and sum(bool(x) for x in ship_conditions) >= 3:
            tags.append(_tag('疑似出货', 7 if lhb_sell else 6, ['高位放量滞涨', '上影线/破位/资金流出'], ['可能只是换手，需观察次日承接']))

    if breakdown or trend_stage == 'downtrend':
        tags.append(_tag('疑似破位', 5, ['跌破20日平台低点' if breakdown else '日线空头排列']))

    # 看多形态与派发/破位互斥：同屏出现出货/破位/加速派发时，剔除启动/拉升类标签，
    # 防止“拉升中段”与“出货/破位”自相矛盾地同时呈现（跌停+主力净流出日的典型冲突）。
    if {'出货相', '疑似出货', '疑似破位', '疑似加速派发风险'} & {t.get('tag') for t in tags}:
        tags = [t for t in tags if t.get('tag') not in ('疑似启动初段', '疑似拉升中段')]

    # ── 指标驱动核心判断：背离 / 超买超卖 / 突破回踩（个股+ETF 通用纯技术）──
    rsi = ind.get('rsi14')
    kdj_j = ind.get('kdj_j')
    macd_trend = ind.get('macd_trend') or ''
    divergence = _detect_divergence(df)
    oversold = bool((rsi is not None and rsi < 35) or (kdj_j is not None and kdj_j < 20))
    overbought = bool((rsi is not None and rsi > 72) or (kdj_j is not None and kdj_j > 95))
    low_zone = bool(oversold or trend_stage == 'downtrend' or (cum_pct_20d is not None and cum_pct_20d < -10))
    high_zone = bool(high20 and close >= high20 * 0.97)

    # 1) 疑似底部背离（填补 predictor 悬空引用，抄底/反弹信号）
    if divergence == 'bottom' and (oversold or low_zone):
        ev = ['MACD柱底背离：价创新低而指标未创新低']
        if rsi is not None:
            ev.append(f'RSI14={rsi:.0f}{"(超卖)" if rsi < 35 else ""}')
        if kdj_j is not None:
            ev.append(f'KDJ_J={kdj_j:.0f}')
        tags.append(_tag('疑似底部背离', 6, ev,
                         ['底背离≠立即反转，需放量阳线确认；跌破前低则背离失效']))

    # 2) 疑似底部放量
    bottom_vol = bool((vol_60d_rank is not None and vol_60d_rank > 0.85) or (volume_ratio or 0) >= 1.5)
    stabilize = bool(long_lower or close > prev_close or (ma5 and close > ma5))
    if low_zone and bottom_vol and stabilize:
        ev = [f'量比{(volume_ratio or 0):.2f}、60日量能分位偏高', '长下影或收阳/收复MA5']
        if rsi is not None and rsi < 40:
            ev.append(f'RSI14={rsi:.0f}低位')
        tags.append(_tag('疑似底部放量', 6, ev,
                         ['低位放量可能是恐慌出逃也可能是资金承接，需次日不破低确认']))

    # 3) 疑似顶背离风险
    if divergence == 'top' or (high_zone and '红柱收缩' in macd_trend and rsi is not None and rsi < 70):
        ev = ['MACD柱顶背离：价创新高而指标走低' if divergence == 'top' else '高位红柱收缩、指标走弱']
        if rsi is not None:
            ev.append(f'RSI14={rsi:.0f}')
        tags.append(_tag('疑似顶背离风险', 6, ev,
                         ['顶背离需跌破颈线确认，强趋势中可钝化延续']))

    # 4) 疑似超买回调风险
    if overbought and (cum_pct_10d is not None and cum_pct_10d > 20):
        ev = [f'涨幅透支（近10日{cum_pct_10d:.1f}%）']
        if rsi is not None:
            ev.append(f'RSI14={rsi:.0f}超买')
        if kdj_j is not None and kdj_j > 95:
            ev.append(f'KDJ_J={kdj_j:.0f}超买')
        tags.append(_tag('疑似超买回调风险', 5, ev,
                         ['超买可钝化，强势股高位可维持，需配合滞涨/破位确认']))

    # 5) 疑似突破回踩确认（A股经典二次上车点）
    if (days_breakout is not None and 2 <= days_breakout <= 10
            and ma20 and ma20 * 0.97 <= close <= ma20 * 1.03
            and (volume_down or volume_dry)
            and not breakdown):
        tags.append(_tag('疑似突破回踩确认', 6,
                         [f'突破后第{days_breakout}日回踩MA20附近', '缩量回踩、未破突破位'],
                         ['回踩须在突破位上方止跌，缩量为健康；放量跌破则突破失败']))

    # 6) 疑似量价背离（仅在未触发出货/破位/派发时补充）
    if not ({'出货相', '疑似出货', '疑似破位', '疑似加速派发风险'} & {t.get('tag') for t in tags}):
        if close > prev_close and volume_down and trend_stage in ('uptrend', 'recovery'):
            tags.append(_tag('疑似量价背离', 5,
                             ['价涨量缩，上涨动能不足'],
                             ['量价背离是预警非定论，放量突破可证伪']))
        elif close < prev_close and (vol_60d_rank is not None and vol_60d_rank > 0.7):
            tags.append(_tag('疑似量价背离', 5,
                             ['价跌量增，抛压释放'],
                             ['放量下跌后若快速收复则为洗盘，需观察承接']))

    if market_phase in ('ice', 'decline', 'retreat'):
        cut = 2 if market_phase in ('ice', 'decline') else 1
        for item in tags:
            if item.get('tag') == '疑似启动初段':
                item['confidence'] = max(0, int(item.get('confidence') or 0) - cut)
                item.setdefault('counter_evidence', []).append(f'大盘情绪阶段估计为{market_phase}，启动类信号降级')

    if is_etf_code:
        tags = [item for item in tags if item.get('tag') in ETF_ALLOWED_TAGS]
    return tags or [_tag('无明确行为标签', 3, ['未满足启动/吸筹/洗盘/出货/破位的组合条件'])]


def _fetch_intraday_kline(code: str, days: int = 80) -> pd.DataFrame | None:
    """60分钟K线增强：失败返回 None，不影响日线主流程。"""
    try:
        from mootdx.quotes import Quotes
        _, sym = _norm_code(code)
        client = Quotes.factory(market='std')
        df = client.bars(symbol=sym, frequency=3, start=0, offset=max(days, 60))
        if df is None or df.empty:
            return None
        df.index = pd.to_datetime(df['datetime'])
        return df.sort_index()
    except Exception as e:
        logger.debug('[kline] 60m失败 %s: %s', code, e)
        return None


def build_technical_profile(
    code: str,
    df: pd.DataFrame | None = None,
    intraday_df: pd.DataFrame | None = None,
    *,
    name: str | None = None,
    flow_profile: dict | None = None,
    public_evidence: dict | None = None,
    market_phase: str | None = None,
) -> dict:
    """短线量化用结构化技术画像。

    只做确定性归纳，不替代 Agent 判断；失败时返回可识别的 low_data 状态。
    """
    if df is None:
        df = fetch_daily_kline(code)
    if df is None or df.empty:
        return {'available': False, 'reason': 'K线数据缺失'}

    df = df.copy()
    df.columns = df.columns.str.lower()
    if 'vol' in df.columns and 'volume' not in df.columns:
        df['volume'] = df['vol']
    if 'turnover_rate' in df.columns and 'turnover' not in df.columns:
        df['turnover'] = df['turnover_rate']
    ind = compute_indicators(df)
    close = float(df['close'].iloc[-1])
    open_ = float(df['open'].iloc[-1]) if 'open' in df.columns else close
    high = float(df['high'].iloc[-1]) if 'high' in df.columns else close
    low = float(df['low'].iloc[-1]) if 'low' in df.columns else close
    volume = float(df['volume'].iloc[-1]) if 'volume' in df.columns else 0.0
    amount = _safe(df['amount'].iloc[-1]) if 'amount' in df.columns else None
    turnover = _safe(df['turnover'].iloc[-1]) if 'turnover' in df.columns else None

    ma5, ma20, ma40, ma60 = ind.get('ma5'), ind.get('ma20'), ind.get('ma40'), ind.get('ma60')
    ma20_series = df['close'].rolling(20).mean()
    ma20_prev = _safe(ma20_series.iloc[-6]) if len(ma20_series.dropna()) >= 6 else None
    ma20_slope_pct = _pct(ma20, ma20_prev) if ma20 and ma20_prev else None

    prior = df.iloc[:-1] if len(df) > 1 else df
    prior20 = prior.tail(20)
    high20 = _round2(prior20['high'].max()) if 'high' in prior20.columns and not prior20.empty else None
    low20 = _round2(prior20['low'].min()) if 'low' in prior20.columns and not prior20.empty else None
    high60 = _round2(prior.tail(60)['high'].max()) if 'high' in prior.columns and len(prior) else None
    low5 = _round2(prior.tail(5)['low'].min()) if 'low' in prior.columns and len(prior) else None
    vol_ma20 = _safe(prior.tail(20)['volume'].mean()) if 'volume' in prior.columns and len(prior) else None
    volume_ratio = round(volume / vol_ma20, 2) if vol_ma20 and vol_ma20 > 0 else None
    ret20 = round((close / float(df['close'].iloc[-21]) - 1) * 100, 2) if len(df) >= 21 else None

    uptrend = bool(ma5 and ma20 and ma40 and ma60 and close > ma20 and ma5 > ma20 > ma40 > ma60 and (ma20_slope_pct or 0) >= 0)
    downtrend = bool(ma5 and ma20 and ma40 and ma60 and close < ma20 and ma5 < ma20 < ma40 < ma60)
    if uptrend:
        trend_stage = 'uptrend'
    elif downtrend:
        trend_stage = 'downtrend'
    elif ma20 and close > ma20 and (ma20_slope_pct or 0) >= 0:
        trend_stage = 'recovery'
    else:
        trend_stage = 'sideways'

    rsi = ind.get('rsi14')
    kdj_j = ind.get('kdj_j')
    bb_lower, bb_mid, bb_upper = ind.get('bb_lower'), ind.get('bb_mid'), ind.get('bb_upper')
    atr = ind.get('atr14')
    oversold = bool((rsi is not None and rsi < 35) or (kdj_j is not None and kdj_j < 20))
    near_lower = bool(bb_lower and close <= bb_lower * 1.03)
    breakout = bool(high20 and close > high20 and (volume_ratio or 0) >= 1.5)
    pullback = bool(uptrend and ma20 and ma20 * 0.97 <= close <= ma20 * 1.03 and (volume_ratio or 0) <= 1.2)

    body_top = max(open_, close)
    body_bottom = min(open_, close)
    day_range = high - low
    long_upper = bool(day_range > 0 and (high - body_top) / day_range >= 0.45)
    long_lower = bool(day_range > 0 and (body_bottom - low) / day_range >= 0.45)
    volume_down = bool(close < open_ and (volume_ratio or 0) >= 1.5)
    breakdown = bool(low20 and close < low20)
    # 强趋势延续：三条路径，任一成立即视为趋势跟随
    #  ① 标准：多头排列 + 站稳MA5（不靠回踩或新鲜突破的主升浪）
    #  ② 主升浪龙头（放宽死指标）：中长期结构在上（站上MA60、MA20在MA60上方）且贴近60日新高，
    #     即便被大盘性回调短暂打穿MA5也算延续，避免用"干净多头排列"一刀切掉政策主线龙头
    #  ③ 收复均线（修复转强）：收盘站上MA20>MA40>MA60且MA20上行、站上MA5，仅MA5因快速上攻
    #     滞后未>MA20，也算延续——避免龙头收复所有中长均线却因MA5滞后被打成event候选/C；
    #     是否给买点由下游机会分层(资金/RS二选一)把关，本处只认形态
    trend_follow_strict = bool(uptrend and ma5 and close >= ma5)
    leader_uptrend = bool(
        ma20 and ma60 and high60
        and close > ma60 and ma20 > ma60
        and close >= high60 * 0.93
    )
    trend_reclaim = bool(
        ma5 and ma20 and ma40 and ma60
        and close > ma20 > ma40 > ma60
        and (ma20_slope_pct or 0) >= 0
        and close >= ma5
    )
    trend_follow = bool((trend_follow_strict or leader_uptrend or trend_reclaim) and not breakdown and not (long_upper and volume_down))

    risk_flags = []
    if downtrend:
        risk_flags.append('日线空头排列')
    if breakdown:
        risk_flags.append('跌破20日平台低点')
    if long_upper:
        risk_flags.append('长上影冲高回落')
    if volume_down:
        risk_flags.append('放量阴线')

    # 强势股回调低吸：唯一真相源，无条件先判，由 evaluate_dipbuy 接管 pullback 的
    # status 与支撑系价位，彻底消灭候选低吸落进阻力位 entry 的旧 bug。
    from core.dipbuy import evaluate_dipbuy
    dip = evaluate_dipbuy(df, ind=ind, code=code, name=name)

    setup_candidates = []

    def _add_setup(name_: str, status_: str, reason_: str):
        if any(item.get('setup_name') == name_ for item in setup_candidates):
            return
        setup_candidates.append({
            'setup_name': name_,
            'status': status_,
            'reason': reason_,
        })

    if dip.get('status'):
        _add_setup('pullback_buy', dip['status'], 'dipbuy')
    if breakout:
        _add_setup('box_breakout', 'triggered', 'breakout')
    if trend_follow:
        _add_setup('trend_continuation', 'triggered', 'trend_follow')
    if pullback:
        _add_setup('pullback_buy', 'candidate', 'pullback')
    if oversold and (near_lower or downtrend):
        _add_setup('oversold_rebound', 'candidate', 'oversold')
    if downtrend or breakdown:
        _add_setup('downtrend_avoid', 'avoid', 'downtrend_or_breakdown')
    if trend_stage in ('uptrend', 'recovery') and high20 and close >= high20 * 0.97:
        _add_setup('trend_breakout', 'candidate', 'near_breakout')
    if not setup_candidates:
        _add_setup('event_momentum', 'candidate', 'fallback')

    setup_name = setup_candidates[0]['setup_name']
    status = setup_candidates[0]['status']
    secondary_setups = [item['setup_name'] for item in setup_candidates[1:]]

    resistances = _levels([ma5, ma20, high20, bb_mid, bb_upper], close, True)
    supports = _levels([low5, low20, bb_lower, ma20], close, False)
    if dip.get('status'):
        # 低吸：支撑系 entry（≈close），不走阻力位
        entry_trigger, fail_level, target_level = _round2(dip['entry']), _round2(dip['stop']), _round2(dip['target'])
    else:
        st_hz = 3 if setup_name in ('oversold_rebound', 'event_momentum') else 5
        atr_pct = (atr / close) if (atr and close) else 0.03
        trig_cap = _ST_TRIGGER_CAP[st_hz]
        if status == 'triggered':
            entry_trigger = close
        elif resistances:
            entry_trigger = min(resistances[0], close * (1 + trig_cap))
        else:
            entry_trigger = close * (1 + trig_cap)
        entry_trigger = _round2(entry_trigger)
        target_dist, stop_dist = short_term_band(atr_pct, st_hz)
        fail_level = _round2(entry_trigger * (1 - stop_dist))
        target_level = _round2(entry_trigger * (1 + target_dist))

    if status == 'candidate' and entry_trigger and high >= entry_trigger and close < entry_trigger:
        if '冲击触发位失败' not in risk_flags:
            risk_flags.append('冲击触发位失败')

    if status == 'triggered':
        position_hint = '轻仓试错，跌破失效价退出'
    elif status == 'candidate':
        position_hint = '观察，不触发不买'
    else:
        position_hint = '禁止新开仓，已有仓位优先风控'

    intraday_available = intraday_df is not None and not intraday_df.empty
    profile = {
        'available': True,
        'code': str(code).strip()[-6:],
        'asof': df.index[-1].strftime('%Y-%m-%d'),
        'close': _round2(close),
        'ma': {'ma5': _round2(ma5), 'ma20': _round2(ma20), 'ma40': _round2(ma40), 'ma60': _round2(ma60), 'ma20_slope_pct': ma20_slope_pct},
        'range': {'high20': high20, 'low20': low20, 'low5': low5},
        'volume': {
            'volume': _round2(volume),
            'amount': _round2(amount),
            'turnover': _round2(turnover),
            'volume_ratio': volume_ratio,
            'long_upper': long_upper,
            'long_lower': long_lower,
            'volume_down': volume_down,
        },
        'indicators': {'rsi14': _round2(rsi), 'kdj_j': _round2(kdj_j), 'macd_trend': ind.get('macd_trend'), 'atr14': _round2(atr)},
        'trend_stage': trend_stage,
        'ret20': ret20,
        'setup_name': setup_name,
        'secondary_setups': secondary_setups,
        'setup_candidates': setup_candidates,
        'status': status,
        'entry_trigger': _round2(entry_trigger),
        'fail_level': fail_level,
        'target_level': _round2(target_level),
        'support_levels': supports,
        'resistance_levels': resistances,
        'risk_flags': risk_flags,
        'position_hint': position_hint,
        'intraday_available': intraday_available,
    }
    profile['behavior_tags'] = _build_behavior_tags(
        code, name, df, ind, profile,
        flow_profile=flow_profile,
        public_evidence=public_evidence,
        market_phase=market_phase,
    )
    return profile


def compute_indicators(df: pd.DataFrame) -> dict:
    """计算 1B 核心套装；TA-Lib 负责标准指标，KDJ 使用原 pandas-ta 同公式。"""
    try:
        import talib
    except ImportError:
        return {}

    df = df.copy()
    df.columns = df.columns.str.lower()
    if 'vol' in df.columns and 'volume' not in df.columns:
        df['volume'] = df['vol']
    if 'turnover_rate' in df.columns and 'turnover' not in df.columns:
        df['turnover'] = df['turnover_rate']

    ind: dict = {}
    n = len(df)
    if n < 20:
        return ind

    close = df['close'].astype(float)
    high = (df['high'] if 'high' in df.columns else close).astype(float)
    low = (df['low'] if 'low' in df.columns else close).astype(float)
    close_arr = close.to_numpy()
    high_arr = high.to_numpy()
    low_arr = low.to_numpy()

    # ── MA ────────────────────────────────────────────
    for period in (5, 10, 20, 40, 60):
        if n >= period:
            ind[f'ma{period}'] = _safe(close.rolling(period).mean().iloc[-1])

    # ── RSI 14 ────────────────────────────────────────
    rsi = talib.RSI(close_arr, timeperiod=14)
    if len(rsi):
        ind['rsi14'] = _safe(rsi[-1])

    # ── MACD (12,26,9) ───────────────────────────────
    _, _, hist_arr = talib.MACD(
        close_arr,
        fastperiod=12,
        slowperiod=26,
        signalperiod=9,
    )
    if len(hist_arr):
        ind['macd_hist'] = _safe(hist_arr[-1])
        hist = pd.Series(hist_arr).dropna()
        if len(hist) >= 3:
            a, b, c_ = float(hist.iloc[-3]), float(hist.iloc[-2]), float(hist.iloc[-1])
            if c_ > b > a:
                ind['macd_trend'] = '红柱扩张'
            elif c_ < b < a and c_ > 0:
                ind['macd_trend'] = '红柱收缩'
            elif c_ < b < a:
                ind['macd_trend'] = '绿柱扩张'
            elif c_ > b:
                ind['macd_trend'] = '绿柱收缩'
            else:
                ind['macd_trend'] = '震荡'

    # ── Bollinger 布林带 (20,2) ─────────────────────
    upper, mid, lower = talib.BBANDS(
        close_arr,
        timeperiod=20,
        nbdevup=2.0,
        nbdevdn=2.0,
        matype=0,
    )
    if len(upper):
        upper_v = _safe(upper[-1])
        mid_v = _safe(mid[-1])
        lower_v = _safe(lower[-1])
        ind.update(bb_upper=upper_v, bb_lower=lower_v, bb_mid=mid_v)
        if upper_v is not None and lower_v is not None and (upper_v - lower_v) > 0:
            pos = (float(close.iloc[-1]) - lower_v) / (upper_v - lower_v)
            if pos > 0.8:
                ind['bb_pos'] = '近上轨，有压力'
            elif pos < 0.2:
                ind['bb_pos'] = '近下轨，有支撑'
            else:
                ind['bb_pos'] = '带内中部'

    # ── KDJ (9,3) ───────────────────────────────────
    # 精确复刻 pandas-ta: fastk -> pd_rma(n=3) -> pd_rma(n=3)。
    highest_high = high.rolling(9).max()
    lowest_low = low.rolling(9).min()
    denom = (highest_high - lowest_low).replace(0, float('nan'))
    fastk = 100.0 * (close - lowest_low) / denom
    k = fastk.ewm(alpha=1.0 / 3.0, min_periods=3).mean()
    d = k.ewm(alpha=1.0 / 3.0, min_periods=3).mean()
    j = 3.0 * k - 2.0 * d
    ind['kdj_k'] = _safe(k.iloc[-1]) if len(k) else None
    ind['kdj_d'] = _safe(d.iloc[-1]) if len(d) else None
    ind['kdj_j'] = _safe(j.iloc[-1]) if len(j) else None

    # ── ATR 14 ───────────────────────────────────────
    atr = talib.ATR(high_arr, low_arr, close_arr, timeperiod=14)
    if len(atr):
        ind['atr14'] = _safe(atr[-1])

    # ── OBV 5 日趋势 ─────────────────────────────────
    if 'volume' in df.columns:
        volume = df['volume'].astype(float).to_numpy()
        obv_arr = talib.OBV(close_arr, volume)
        if len(obv_arr) >= 5:
            v0 = float(obv_arr[-5])
            v1 = float(obv_arr[-1])
            ind['obv_trend'] = (
                '量价齐升' if v1 > v0 * 1.02
                else '量价背离' if v1 < v0 * 0.98
                else '量能稳定'
            )

    return ind


def summarize(code: str, df: pd.DataFrame | None = None,
              ind: dict | None = None) -> str:
    """返回适合 AI prompt 注入的 K 线段文本（含 ATR 止损建议）。"""
    if df is None:
        df = fetch_daily_kline(code)
    if df is None or df.empty:
        return '▶ K 线数据：获取失败，跳过技术分析'
    if ind is None:
        ind = compute_indicators(df)
    df = df.copy()
    df.columns = df.columns.str.lower()
    if 'vol' in df.columns and 'volume' not in df.columns:
        df['volume'] = df['vol']
    if 'turnover_rate' in df.columns and 'turnover' not in df.columns:
        df['turnover'] = df['turnover_rate']

    close = float(df['close'].iloc[-1]) if not df.empty else 0.0
    lines = [f'▶ 技术指标（{code}，截至 {df.index[-1].strftime("%Y-%m-%d")}）：']

    # MA 排列
    ma5 = ind.get('ma5')
    ma20 = ind.get('ma20')
    ma40 = ind.get('ma40')
    ma60 = ind.get('ma60')
    if ma5 and ma20 and ma40 and ma60:
        close_r = round(close, 3)
        if ma5 > ma20 > ma40 > ma60:
            arr = f'多头排列（收{close_r} / MA5={ma5} > MA20={ma20} > MA40={ma40} > MA60={ma60}）'
        elif ma5 < ma20 < ma40 < ma60:
            arr = f'空头排列（收{close_r} / MA5={ma5} < MA20={ma20} < MA40={ma40} < MA60={ma60}）'
        else:
            arr = f'混乱排列（收{close_r} / MA5={ma5} / MA20={ma20} / MA40={ma40} / MA60={ma60}）'
    else:
        arr = f'收盘={round(close,3)}'
    lines.append(f'  均线: {arr}')

    # 量能 / 成交额 / 换手率
    try:
        vol = float(df['volume'].iloc[-1]) if 'volume' in df.columns else None
        vol_ma20 = float(df['volume'].iloc[-21:-1].mean()) if 'volume' in df.columns and len(df) >= 21 else None
        vol_ratio = round(vol / vol_ma20, 2) if vol and vol_ma20 and vol_ma20 > 0 else None
        amount = float(df['amount'].iloc[-1]) if 'amount' in df.columns else None
        turnover = float(df['turnover'].iloc[-1]) if 'turnover' in df.columns else None
        vol_parts = []
        if vol_ratio is not None:
            vol_parts.append(f'量比={vol_ratio}')
        if amount is not None:
            vol_parts.append(f'成交额={amount / 100000000:.2f}亿')
        if turnover is not None:
            vol_parts.append(f'换手率={turnover:.2f}%')
        if vol_parts:
            lines.append('  量能: ' + ' / '.join(vol_parts))
    except Exception:
        pass

    # RSI
    rsi = ind.get('rsi14')
    if rsi is not None:
        rsi_note = ''
        if rsi > 70:
            rsi_note = ' ⚠️超买区'
        elif rsi < 30:
            rsi_note = ' ⚠️超卖区'
        lines.append(f'  RSI14: {rsi:.1f}{rsi_note}')

    # MACD
    macd_h = ind.get('macd_hist')
    macd_t = ind.get('macd_trend', '')
    if macd_h is not None:
        lines.append(f'  MACD柱: {macd_h:.4f}（{macd_t}）')

    # 布林带
    bb_pos = ind.get('bb_pos')
    if bb_pos:
        lines.append(f'  布林带: 当前价{bb_pos}')

    # KDJ
    j = ind.get('kdj_j')
    if j is not None:
        j_note = ''
        if j > 80:
            j_note = ' 高位钝化'
        elif j < 20:
            j_note = ' 低位超卖'
        lines.append(f'  KDJ: J={j:.1f}{j_note}')

    # ATR（核心止损参考）
    atr = ind.get('atr14')
    if atr is not None and close > 0:
        stop_ref = round(2 * atr / close * 100, 1)
        lines.append(f'  ATR14: {atr:.3f} 元 → 参考止损 2×ATR ≈ -{stop_ref}%')
    else:
        lines.append('  ATR14: 无法计算（数据不足）')

    # OBV
    obv_t = ind.get('obv_trend')
    if obv_t:
        lines.append(f'  OBV趋势: {obv_t}')

    return '\n'.join(lines)


# ─────────────────────────────────────────────
# 板块指数日 K（东财，概念 / 行业）
# ─────────────────────────────────────────────

def fetch_sector_kline(board_name: str, days: int = 60) -> 'pd.DataFrame | None':
    """拉取板块指数日 K 线。

    按板块名称依次尝试概念板块 → 行业板块。
    返回 DataFrame: DatetimeIndex + open/high/low/close/volume 列；失败返回 None。
    """
    try:
        import akshare as ak
        import pandas as pd
    except Exception:
        return None

    start = (pd.Timestamp.today() - pd.Timedelta(days=days + 30)).strftime('%Y%m%d')
    end = pd.Timestamp.today().strftime('%Y%m%d')

    def _clean(raw) -> 'pd.DataFrame | None':
        if raw is None or raw.empty:
            return None
        col_map = {
            '日期': 'datetime', '开盘': 'open', '最高': 'high',
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
        }
        raw = raw.rename(columns=col_map)
        need = [c for c in ('datetime', 'open', 'high', 'low', 'close', 'volume') if c in raw.columns]
        if 'datetime' not in need or 'close' not in need:
            return None
        raw = raw[need].copy()
        raw['datetime'] = pd.to_datetime(raw['datetime'])
        raw = raw.set_index('datetime').sort_index()
        return raw.tail(days) if not raw.empty else None

    # 概念板块
    try:
        raw = ak.stock_board_concept_index_em(symbol=board_name, start_date=start, end_date=end)
        result = _clean(raw)
        if result is not None:
            return result
    except Exception:
        pass

    # 行业板块
    try:
        raw = ak.stock_board_industry_index_em(symbol=board_name, start_date=start, end_date=end)
        result = _clean(raw)
        if result is not None:
            return result
    except Exception:
        pass

    return None
