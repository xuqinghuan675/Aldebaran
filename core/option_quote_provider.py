"""
独立期权行情 provider —— AKShare 免费接口验证层。

未来替换 TickFlow / QMT / 券商 API 时，只需修改本模块内部实现，
UI 和业务方只调用 fetch_option_quotes()。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Optional

import akshare as ak

from core.js_runtime import js_runtime_diagnostic

logger = logging.getLogger(__name__)

_CONTRACT_METADATA_CACHE_DATE: date | None = None
_CONTRACT_METADATA_CACHE: dict[str, dict] = {}
_CONTRACT_METADATA_LOADED_SOURCES: set[str] = set()
_SSE_FINANCE_BOARD_CACHE_DATE: date | None = None
_SSE_FINANCE_BOARD_CACHE: dict[tuple[str, str], dict[str, float]] = {}

_SSE_FINANCE_SYMBOL_BY_UNDERLYING = {
    '510050': '华夏上证50ETF期权',
    '510300': '华泰柏瑞沪深300ETF期权',
    '510500': '南方中证500ETF期权',
    '588000': '华夏科创50ETF期权',
    '588080': '易方达科创50ETF期权',
}
_PRE_CLOSE_SOURCE_FINANCE_BOARD = 'akshare_option_finance_board'


def _js_diag_suffix() -> str:
    diag = js_runtime_diagnostic()
    return f' ({diag})' if diag else ''


def _is_stale_quote(quote_time: str) -> bool:
    """行情时间日期早于今天 → 陈旧（已到期合约/非实时/盘前旧价）。"""
    if not quote_time:
        return False
    try:
        qd = datetime.strptime(quote_time.strip()[:10], '%Y-%m-%d').date()
        return qd < date.today()
    except Exception:
        return False

# ── 公共入口 ──

def fetch_option_quotes(
    codes: list[str],
    timeout: float = 8.0,
    max_workers: int = 5,
) -> dict[str, dict]:
    """批量获取期权实时行情（并发请求每个合约）。

    Args:
        codes: 8 位期权合约代码列表
        timeout: 保留参数（AKShare 不支持单次超时设置，预留未来接口使用）
        max_workers: 并发线程数

    Returns:
        {code: {code, name, underlying_code, option_type, strike_price,
                expiry_date, contract_unit, current_price, pre_close,
                open, high, low, pct, volume, amount,
                bid_price, ask_price, bid_volume, ask_volume,
                quote_time, source, ok, error}, ...}
    """
    if not codes:
        return {}

    results: dict[str, dict] = {}
    exchange_codes = [c for c in codes if _is_exchange_option_code(c)]
    other_codes = [c for c in codes if not _is_exchange_option_code(c)]

    # 沪深交易所：并发逐个查询新浪实时快照
    if exchange_codes:
        contract_metadata = _fetch_exchange_contract_metadata(exchange_codes)
        sse_results = _fetch_sse_batch(exchange_codes, max_workers, contract_metadata)
        results.update(sse_results)

    # 非 8 位代码：尝试东财全市场
    if other_codes:
        em_results = _fetch_szse_em(other_codes)
        results.update(em_results)

    for code in codes:
        if code not in results:
            results[code] = _error_result(code, '不在已知交易所代码范围内')

    return results


# ── 沪深交易所：新浪实时快照（逐合约并发） ──

# AKShare option_sse_spot_price_sina 返回的 key-value 字段映射
# 注：新浪接口的买卖盘字段名为"买一量价/买一量量"等复合形式，
# v1 不做拆解，bid/ask 留空，后续版本可补充解析
_SSE_FIELD_MAP = {
    '最新价': 'current_price',
    '开盘价': 'open',
    '最高价': 'high',
    '最低价': 'low',
    '昨收价': 'snapshot_pre_close',
    '持仓量': 'open_interest',
    '涨幅': 'pct',
    '行权价': 'strike_price',
    '行情时间': 'quote_time',
    '标的股票': 'underlying_code',
    '期权合约简称': 'name',
    '成交量': 'volume',
    '成交额': 'amount',
}


def _fetch_exchange_contract_metadata(codes: list[str]) -> dict[str, dict]:
    global _CONTRACT_METADATA_CACHE_DATE

    today = date.today()
    if _CONTRACT_METADATA_CACHE_DATE != today:
        _CONTRACT_METADATA_CACHE.clear()
        _CONTRACT_METADATA_LOADED_SOURCES.clear()
        _CONTRACT_METADATA_CACHE_DATE = today

    missing = [code for code in codes if code not in _CONTRACT_METADATA_CACHE]
    needed_sources = set()
    for code in missing:
        if code.startswith('100'):
            needed_sources.add('sse')
        elif code.startswith('900'):
            needed_sources.add('szse')

    for source in sorted(needed_sources):
        if source in _CONTRACT_METADATA_LOADED_SOURCES:
            continue
        try:
            if source == 'sse':
                _load_sse_contract_metadata()
            elif source == 'szse':
                _load_szse_contract_metadata()
            _CONTRACT_METADATA_LOADED_SOURCES.add(source)
        except Exception as exc:
            logger.warning(
                'Option contract metadata load failed for %s: %s%s',
                source,
                exc,
                _js_diag_suffix(),
            )

    return {
        code: _CONTRACT_METADATA_CACHE[code]
        for code in codes
        if code in _CONTRACT_METADATA_CACHE
    }


def _load_sse_contract_metadata() -> None:
    df = ak.option_current_day_sse()
    if df is None or df.empty:
        return
    for _, row in df.iterrows():
        code = _normalize_contract_code(_row_value(row, 0))
        if not code:
            continue
        _CONTRACT_METADATA_CACHE[code] = {
            'name': str(_row_value(row, 2)).strip(),
            'finance_trade_code': str(_row_value(row, 1)).strip(),
            'underlying_code': _extract_underlying_code(_row_value(row, 3)),
            'option_type': _normalize_option_type(_row_value(row, 4)),
            'strike_price': _safe_float(_row_value(row, 5)),
            'contract_unit': _safe_int(_row_value(row, 6)) or 10000,
            'expiry_date': _parse_contract_date(_row_value(row, 9)),
        }


def _load_szse_contract_metadata() -> None:
    df = ak.option_current_day_szse()
    if df is None or df.empty:
        return
    for _, row in df.iterrows():
        code = _normalize_contract_code(_row_value(row, 1))
        if not code:
            continue
        _CONTRACT_METADATA_CACHE[code] = {
            'name': str(_row_value(row, 3)).strip(),
            'finance_trade_code': '',
            'underlying_code': _extract_underlying_code(_row_value(row, 4)),
            'option_type': _normalize_option_type(_row_value(row, 5)),
            'strike_price': _safe_float(_row_value(row, 6)),
            'contract_unit': _safe_int(_row_value(row, 7)) or 10000,
            'expiry_date': _parse_contract_date(_row_value(row, 10)),
        }


def _row_value(row, index: int):
    if len(row) <= index:
        return ''
    return row.iloc[index]


def _normalize_contract_code(value) -> str:
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    return text if len(text) == 8 and text.isdigit() else ''


def _extract_underlying_code(value) -> str:
    match = re.search(r'\d{6}', str(value))
    return match.group(0) if match else ''


def _normalize_option_type(value) -> str:
    text = str(value).strip().lower()
    if 'put' in text or '\u8ba4\u6cbd' in text:
        return 'put'
    if 'call' in text or '\u8ba4\u8d2d' in text:
        return 'call'
    return ''


def _parse_contract_date(value) -> str:
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        try:
            return value.strftime('%Y-%m-%d')
        except Exception:
            pass
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            try:
                return datetime.fromtimestamp(value / 1000).date().isoformat()
            except Exception:
                return ''
        value = int(value)
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    if len(text) >= 10 and text[4] == '-' and text[7] == '-':
        return text[:10]
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, '%Y%m%d').date().isoformat()
        except Exception:
            return ''
    return ''


def _fetch_sse_batch(
    codes: list[str],
    max_workers: int,
    contract_metadata: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """并发获取沪深交易所期权实时行情。"""
    results: dict[str, dict] = {}
    contract_metadata = contract_metadata or {}
    pre_settlements = _fetch_sse_pre_settlement_prices(codes, contract_metadata)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _fetch_sse_one,
                code,
                contract_metadata.get(code),
                pre_settlements.get(code),
            ): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = _error_result(code, f'并发异常: {exc}')
    return results


def _fetch_sse_pre_settlement_prices(
    codes: list[str],
    contract_metadata: dict[str, dict],
) -> dict[str, float]:
    """从上交所行情看板取前结价；现价仍由实时/分钟线接口负责。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for code in codes:
        if not code.startswith('100'):
            continue
        meta = contract_metadata.get(code) or {}
        symbol = _SSE_FINANCE_SYMBOL_BY_UNDERLYING.get(
            _clean_underlying_code(meta.get('underlying_code'))
        )
        end_month = _option_end_month(meta.get('expiry_date'))
        if not symbol or not end_month:
            continue
        groups.setdefault((symbol, end_month), []).append(code)

    out: dict[str, float] = {}
    for (symbol, end_month), group_codes in groups.items():
        board = _load_sse_finance_board_settlements(symbol, end_month)
        for code in group_codes:
            meta = contract_metadata.get(code) or {}
            trade_code = str(meta.get('finance_trade_code') or '').strip()
            value = board.get(trade_code, 0.0) or board.get(code, 0.0)
            if value > 0:
                out[code] = value
    return out


def _load_sse_finance_board_settlements(symbol: str, end_month: str) -> dict[str, float]:
    global _SSE_FINANCE_BOARD_CACHE_DATE

    today = date.today()
    if _SSE_FINANCE_BOARD_CACHE_DATE != today:
        _SSE_FINANCE_BOARD_CACHE.clear()
        _SSE_FINANCE_BOARD_CACHE_DATE = today

    key = (symbol, end_month)
    if key in _SSE_FINANCE_BOARD_CACHE:
        return _SSE_FINANCE_BOARD_CACHE[key]

    settlements: dict[str, float] = {}
    try:
        df = ak.option_finance_board(symbol=symbol, end_month=end_month)
    except Exception as exc:
        logger.warning(
            'Option finance board load failed for %s %s: %s%s',
            symbol,
            end_month,
            exc,
            _js_diag_suffix(),
        )
        _SSE_FINANCE_BOARD_CACHE[key] = settlements
        return settlements

    if df is None or df.empty:
        _SSE_FINANCE_BOARD_CACHE[key] = settlements
        return settlements

    cols = df.columns.tolist()
    code_col = _find_col(cols, ['合约交易代码', '合约编码', '合约代码', '代码'])
    pre_col = _find_col(cols, ['前结价', '前结算价', '昨结算价'])
    if not code_col and len(cols) >= 2:
        code_col = cols[1]
    if not pre_col and len(cols) >= 5:
        pre_col = cols[4]
    if not code_col or not pre_col:
        _SSE_FINANCE_BOARD_CACHE[key] = settlements
        return settlements

    for _, row in df.iterrows():
        code = str(row.get(code_col, '')).strip()
        pre_close = _safe_float(row.get(pre_col))
        if code and pre_close > 0:
            settlements[code] = pre_close

    _SSE_FINANCE_BOARD_CACHE[key] = settlements
    return settlements


def _clean_underlying_code(value) -> str:
    text = str(value or '').strip()
    if text[:2].lower() in ('sh', 'sz'):
        text = text[2:]
    return _extract_underlying_code(text) or text


def _option_end_month(value) -> str:
    text = str(value or '').strip()
    if len(text) >= 7 and text[4] == '-' and text[7:8] == '-':
        return f'{text[2:4]}{text[5:7]}'
    if len(text) >= 6 and text[:6].isdigit():
        return text[2:6]
    return ''


def _is_healthy_sse_raw(raw: dict) -> bool:
    name = raw.get('期权合约简称', '')
    if not name or '�' in name:
        return False
    return '行权价' in raw and '最新价' in raw


def _fetch_sse_raw_with_retry(code: str, max_retries: int = 5, retry_interval: float = 0.3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            df = ak.option_sse_spot_price_sina(symbol=code)
        except Exception as exc:
            last_exc = exc
            time.sleep(retry_interval)
            continue
        if df is None or df.empty:
            time.sleep(retry_interval)
            continue
        parsed: dict[str, str] = {}
        for _, row in df.iterrows():
            key = str(row.iloc[0]).strip()
            val = str(row.iloc[1]).strip()
            if key:
                parsed[key] = val
        if _is_healthy_sse_raw(parsed):
            return parsed
        if attempt < max_retries - 1:
            time.sleep(retry_interval)
    if last_exc is not None:
        logger.warning(
            'Option SSE quote load failed for %s after retries: %s%s',
            code,
            last_exc,
            _js_diag_suffix(),
        )
    else:
        logger.debug('Option SSE quote returned empty for %s%s', code, _js_diag_suffix())
    return {}


def _fetch_sse_one(
    code: str,
    contract_metadata: dict | None = None,
    pre_settlement: float | None = None,
) -> dict:
    """获取单只交易所期权实时行情（覆盖沪深 8 位代码）。"""
    raw = _fetch_sse_raw_with_retry(code)
    if not raw:
        fallback = _fetch_sse_one_via_kline(code, contract_metadata, pre_settlement)
        if fallback is not None:
            return fallback
        return _error_result(code, '交易所新浪快照乱码或残缺（重试后仍无完整数据）')

    mapped: dict[str, object] = {}
    for cn_field, en_field in _SSE_FIELD_MAP.items():
        if cn_field in raw:
            mapped[en_field] = raw[cn_field]

    contract_metadata = contract_metadata or {}
    name = str(mapped.get('name') or contract_metadata.get('name') or '')
    underlying_code = str(mapped.get('underlying_code') or contract_metadata.get('underlying_code') or '')
    # 标的股票字段可能是纯数字（如 '510050'），去掉 'sh' 前缀
    if underlying_code.startswith('sh') or underlying_code.startswith('sz'):
        underlying_code = underlying_code[2:]
    if underlying_code.startswith('SH') or underlying_code.startswith('SZ'):
        underlying_code = underlying_code[2:]

    strike_price = _safe_float(mapped.get('strike_price')) or _safe_float(contract_metadata.get('strike_price'))
    contract_unit = _safe_int(contract_metadata.get('contract_unit')) or 10000
    expiry_date = str(contract_metadata.get('expiry_date') or _infer_expiry_from_code(code))
    option_type = contract_metadata.get('option_type') or _infer_option_type_from_name(name)
    current_price = _safe_float(mapped.get('current_price'))
    pre_close = pre_settlement if pre_settlement and pre_settlement > 0 else 0.0
    pct = 0.0
    if pre_close > 0 and current_price > 0:
        pct = round((current_price - pre_close) / pre_close * 100, 4)
    pre_close_source = (
        _PRE_CLOSE_SOURCE_FINANCE_BOARD
        if pre_settlement and pre_settlement > 0
        else ''
    )

    return {
        'code': code,
        'name': name,
        'underlying_code': underlying_code,
        'option_type': option_type,
        'strike_price': strike_price,
        'expiry_date': expiry_date,
        'contract_unit': contract_unit,
        'current_price': current_price,
        'pre_close': pre_close,
        'pre_close_source': pre_close_source,
        'open': _safe_float(mapped.get('open')),
        'high': _safe_float(mapped.get('high')),
        'low': _safe_float(mapped.get('low')),
        'pct': pct,
        'volume': _safe_int(mapped.get('volume')),
        'amount': _safe_float(mapped.get('amount')),
        'bid_price': _safe_float(mapped.get('bid_price')),
        'ask_price': _safe_float(mapped.get('ask_price')),
        'bid_volume': _safe_int(mapped.get('bid_volume')),
        'ask_volume': _safe_int(mapped.get('ask_volume')),
        'quote_time': str(mapped.get('quote_time', '')),
        'stale': _is_stale_quote(str(mapped.get('quote_time', ''))),
        'source': 'akshare_sse_sina',
        'ok': True,
        'error': '',
    }


def _fetch_sse_minute_price(code: str) -> tuple[float, str]:
    """分钟线兜底取现价（hq.sinajs.cn 被 403 时改用此 host）。返回 (现价, 行情时间)。"""
    try:
        df = ak.option_sse_minute_sina(symbol=code)
    except Exception as exc:
        logger.debug('Option SSE minute fallback failed for %s: %s%s', code, exc, _js_diag_suffix())
        return 0.0, ''
    if df is None or df.empty:
        logger.debug('Option SSE minute fallback returned empty for %s%s', code, _js_diag_suffix())
        return 0.0, ''
    last = df.iloc[-1]
    price = _safe_float(last.iloc[2])
    quote_time = f'{last.iloc[0]} {last.iloc[1]}'.strip()
    return price, quote_time


def _fetch_sse_one_via_kline(
    code: str,
    contract_metadata: dict | None = None,
    pre_settlement: float | None = None,
) -> dict | None:
    """spot 接口不可用（如 hq.sinajs.cn 403）时，用分钟线取现价；前结价仍只用交易所看板。"""
    price, quote_time = _fetch_sse_minute_price(code)
    pre_close = pre_settlement if pre_settlement and pre_settlement > 0 else 0.0
    if price <= 0 and pre_close <= 0:
        return None

    contract_metadata = contract_metadata or {}
    name = str(contract_metadata.get('name') or '')
    underlying_code = str(contract_metadata.get('underlying_code') or '')
    if underlying_code[:2].lower() in ('sh', 'sz'):
        underlying_code = underlying_code[2:]

    pct = round((price - pre_close) / pre_close * 100, 4) if price > 0 and pre_close > 0 else 0.0
    pre_close_source = (
        _PRE_CLOSE_SOURCE_FINANCE_BOARD
        if pre_settlement and pre_settlement > 0
        else ''
    )

    return {
        'code': code,
        'name': name,
        'underlying_code': underlying_code,
        'option_type': contract_metadata.get('option_type') or _infer_option_type_from_name(name),
        'strike_price': _safe_float(contract_metadata.get('strike_price')),
        'expiry_date': str(contract_metadata.get('expiry_date') or _infer_expiry_from_code(code)),
        'contract_unit': _safe_int(contract_metadata.get('contract_unit')) or 10000,
        'current_price': price,
        'pre_close': pre_close,
        'pre_close_source': pre_close_source,
        'open': 0.0, 'high': 0.0, 'low': 0.0, 'pct': pct,
        'volume': 0, 'amount': 0.0,
        'bid_price': 0.0, 'ask_price': 0.0, 'bid_volume': 0, 'ask_volume': 0,
        'quote_time': quote_time,
        'stale': _is_stale_quote(quote_time),
        'source': 'akshare_sse_sina_kline',
        'ok': True,
        'error': '',
    }


def _infer_option_type_from_name(name: str) -> str:
    """从合约简称推断认购/认沽。"""
    if '购' in name:
        return 'call'
    if '沽' in name:
        return 'put'
    name_upper = name.upper()
    if 'C' in name_upper and 'P' not in name_upper:
        return 'call'
    if 'P' in name_upper and 'C' not in name_upper:
        return 'put'
    return 'call'


def _infer_expiry_from_code(code: str) -> str:
    """从交易所 8 位期权代码推算到期月份（第 7 位 A=1月...L=12月）。

    仅适用于字母编码的旧式合约，纯数字编码合约返回空字符串。
    """
    if len(code) != 8:
        return ''
    expiry_char = code[6].upper()
    month_map = {
        'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5, 'F': 6,
        'G': 7, 'H': 8, 'I': 9, 'J': 10, 'K': 11, 'L': 12,
    }
    month = month_map.get(expiry_char)
    if month is None:
        return ''
    return f'{month:02d}月'


# ── 深交所：东财全市场接口（不保证可用） ──

def _fetch_szse_em(codes: list[str]) -> dict[str, dict]:
    """通过东财全市场期权接口获取深交所/非标准代码实时行情。

    option_current_em() 全量分页易遇 ProxyError/RemoteDisconnected，做有限重试。
    """
    results: dict[str, dict] = {}
    df = None
    last_exc = None
    for attempt in range(3):
        try:
            df = ak.option_current_em()
            break
        except Exception as exc:
            last_exc = exc
            logger.warning('东财期权全市场接口第 %d 次失败: %s%s', attempt + 1, exc, _js_diag_suffix())
            time.sleep(0.8 * (attempt + 1))
    if df is None:
        for code in codes:
            results[code] = _error_result(code, f'东财全市场接口不可用: {last_exc}')
        return results

    if df is None or df.empty:
        logger.debug('东财期权全市场接口返回空数据%s', _js_diag_suffix())
        for code in codes:
            results[code] = _error_result(code, '东财全市场接口返回空数据')
        return results

    # 动态探测列名
    cols = df.columns.tolist()
    code_col = _find_col(cols, ['代码', '合约代码', '期权代码'])
    name_col = _find_col(cols, ['名称', '期权名称', '合约简称'])
    price_col = _find_col(cols, ['最新价', '最新价格'])
    pre_close_col = _find_col(cols, ['昨收', '前收盘'])
    bid_col = _find_col(cols, ['买一价', '买入价', '买价'])
    ask_col = _find_col(cols, ['卖一价', '卖出价', '卖价'])
    pct_col = _find_col(cols, ['涨跌幅', '涨跌幅度'])
    vol_col = _find_col(cols, ['成交量'])
    strike_col = _find_col(cols, ['行权价', '执行价'])
    und_col = _find_col(cols, ['标的代码', '标的证券代码', '标的', '标的物代码'])

    for _, row in df.iterrows():
        code = str(row.get(code_col, '')).strip() if code_col else ''
        if not code or code not in codes:
            continue

        results[code] = {
            'code': code,
            'name': str(row.get(name_col, '')).strip() if name_col else '',
            'underlying_code': str(row.get(und_col, '')).strip() if und_col else '',
            'option_type': '',
            'strike_price': _safe_float(row.get(strike_col)) if strike_col else 0.0,
            'expiry_date': '',
            'contract_unit': 10000,
            'current_price': _safe_float(row.get(price_col)) if price_col else 0.0,
            'pre_close': _safe_float(row.get(pre_close_col)) if pre_close_col else 0.0,
            'pre_close_source': 'akshare_em' if pre_close_col else '',
            'open': 0.0, 'high': 0.0, 'low': 0.0,
            'pct': _safe_float(row.get(pct_col)) if pct_col else 0.0,
            'volume': _safe_int(row.get(vol_col)) if vol_col else 0,
            'amount': 0.0,
            'bid_price': _safe_float(row.get(bid_col)) if bid_col else 0.0,
            'ask_price': _safe_float(row.get(ask_col)) if ask_col else 0.0,
            'bid_volume': 0, 'ask_volume': 0,
            'quote_time': '',
            'stale': False,
            'source': 'akshare_em',
            'ok': True,
            'error': '',
        }

    for code in codes:
        if code not in results:
            results[code] = _error_result(code, '东财全市场快照中未找到该合约')

    return results


def _find_col(columns: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in columns:
            return c
    return None


# ── helpers ──

def _is_exchange_option_code(code: str) -> bool:
    """沪深交易所 8 位纯数字期权代码（上交所 100xxxxx / 深交所 900xxxxx）。"""
    return len(code) == 8 and code.isdigit()


def _error_result(code: str, msg: str) -> dict:
    return {
        'code': code, 'name': '', 'underlying_code': '', 'option_type': '',
        'strike_price': 0.0, 'expiry_date': '', 'contract_unit': 10000,
        'current_price': 0.0, 'pre_close': 0.0, 'open': 0.0, 'high': 0.0,
        'low': 0.0, 'pct': 0.0, 'volume': 0, 'amount': 0.0,
        'pre_close_source': '',
        'bid_price': 0.0, 'ask_price': 0.0, 'bid_volume': 0, 'ask_volume': 0,
        'quote_time': '', 'stale': False, 'source': '', 'ok': False, 'error': msg,
    }


def _safe_float(val) -> float:
    try:
        v = float(val)
        if v != v:
            return 0.0
        return v
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0
