"""A股 HTTP 兜底 provider — akshare 失败时的备用数据源。

只在 akshare 抛异常或返回空时使用。数据来自东方财富等公开 HTTP 接口。
不做缓存，不影响现有路径。
"""
from __future__ import annotations

import logging
from typing import Any

from core import http_client

logger = logging.getLogger(__name__)

_TIMEOUT = 8

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://data.eastmoney.com/',
    'Accept': 'application/json, text/plain, */*',
}


def _safe_float(v: Any) -> float:
    try:
        f = float(v)
        import math
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return 0.0


def _pick(d: dict, *keys: str, default: Any = '') -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None and v != '':
            return v
    return default


def _get_json(url: str, params: dict | None = None) -> Any:
    r = http_client.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _normalize_code(raw: str) -> str:
    """'300750' / '0300750' / '1600519' / 'SZ300750' → 纯6位数字。"""
    import re
    s = str(raw).strip()
    m = re.search(r'(\d{6})', s)
    return m.group(1) if m else s


# ── 龙虎榜 ───────────────────────────────────────────────────────

def fetch_lhb_detail(date: str) -> list[dict]:
    """HTTP 获取龙虎榜详情。date 格式 YYYYMMDD。"""
    try:
        url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
        params = {
            'sortColumns': 'BILLBOARD_NET_AMT',
            'sortTypes': '-1',
            'pageSize': '50',
            'pageNumber': '1',
            'reportName': 'RPT_DAILYBILLBOARD_DETAILSNEW',
            'columns': 'ALL',
            'source': 'WEB',
            'client': 'WEB',
            'filter': f"(TRADE_DATE='{date[:4]}-{date[4:6]}-{date[6:8]}')",
        }
        data = _get_json(url, params)
        if not data or not data.get('result'):
            return []
        rows = data['result'].get('data') or []
        results = []
        for item in rows:
            code = _normalize_code(_pick(item, 'SECURITY_CODE', 'SECUCODE', default=''))
            name = str(_pick(item, 'SECURITY_NAME_ABBR', 'SECURITY_NAME', default=''))
            if not code or not name:
                continue
            results.append({
                '代码': code,
                '名称': name,
                '解读': str(_pick(item, 'EXPLAIN', 'EXPLANATION', default='')),
                '上榜原因': str(_pick(item, 'EXPLANATION', 'EXPLAIN', default='')),
                '收盘价': _safe_float(_pick(item, 'CLOSE_PRICE', default=0)),
                '涨跌幅': _safe_float(_pick(item, 'CHANGE_RATE', default=0)),
                '龙虎榜净买额': _safe_float(_pick(item, 'BILLBOARD_NET_AMT', default=0)),
                '龙虎榜买入额': _safe_float(_pick(item, 'BILLBOARD_BUY_AMT', default=0)),
                '龙虎榜卖出额': _safe_float(_pick(item, 'BILLBOARD_SELL_AMT', default=0)),
                '龙虎榜成交额': _safe_float(_pick(item, 'BILLBOARD_DEAL_AMT', default=0)),
                '市场总成交额': _safe_float(_pick(item, 'ACCUM_AMOUNT', default=0)),
                '净买额占总成交比': _safe_float(_pick(item, 'DEAL_NET_RATIO', default=0)),
                '成交额占总成交比': _safe_float(_pick(item, 'DEAL_AMOUNT_RATIO', default=0)),
                '换手率': _safe_float(_pick(item, 'TURNOVERRATE', default=0)),
            })
        return results
    except Exception as e:
        logger.debug(f'HTTP 龙虎榜获取失败: {e}')
        return []


# ── 大宗交易 ─────────────────────────────────────────────────────

def fetch_dzjy_detail(date: str) -> list[dict]:
    """HTTP 获取大宗交易明细。date 格式 YYYYMMDD。"""
    try:
        url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
        params = {
            'sortColumns': 'DEAL_AMT',
            'sortTypes': '-1',
            'pageSize': '50',
            'pageNumber': '1',
            'reportName': 'RPT_DATA_BLOCKTRADE',
            'columns': 'ALL',
            'source': 'WEB',
            'client': 'WEB',
            'filter': f"(TRADE_DATE='{date[:4]}-{date[4:6]}-{date[6:8]}')",
        }
        data = _get_json(url, params)
        if not data or not data.get('result'):
            return []
        rows = data['result'].get('data') or []
        results = []
        for item in rows:
            code = _normalize_code(_pick(item, 'SECURITY_CODE', 'SECUCODE', default=''))
            name = str(_pick(item, 'SECURITY_NAME_ABBR', 'SECURITY_NAME', default=''))
            if not code or not name:
                continue
            close_price = _safe_float(_pick(item, 'CLOSE_PRICE', default=0))
            deal_price = _safe_float(_pick(item, 'DEAL_PRICE', default=0))
            raw_prem = _safe_float(_pick(item, 'PREMIUM_RATIO', 'PREMIUM_RATE', default=0))
            # API 返回比率(如 -0.035)，分类器期望百分比(如 -3.5)
            if -1 < raw_prem < 1 and raw_prem != 0:
                premium = round(raw_prem * 100, 2)
            else:
                premium = raw_prem
            if premium == 0 and close_price > 0 and deal_price > 0:
                premium = round((deal_price / close_price - 1) * 100, 2)
            results.append({
                '证券代码': code,
                '证券简称': name,
                '成交价': deal_price,
                '成交价格': deal_price,
                '收盘价': close_price,
                '折溢率': premium,
                '折溢价率': premium,
                '成交量': _safe_float(_pick(item, 'DEAL_VOLUME', 'DEAL_VOL', default=0)),
                '成交额': _safe_float(_pick(item, 'DEAL_AMT', default=0)),
                '买方营业部': str(_pick(item, 'BUYER_NAME', default='')),
                '卖方营业部': str(_pick(item, 'SELLER_NAME', default='')),
            })
        return results
    except Exception as e:
        logger.debug(f'HTTP 大宗交易获取失败: {e}')
        return []


# ── 热门排名 ─────────────────────────────────────────────────────

def fetch_hot_rank() -> list[dict]:
    """HTTP 获取东方财富人气榜排名。"""
    try:
        url = 'https://emappdata.eastmoney.com/stockrank/getAllCurrentList'
        payload = {
            'appId': 'appId01',
            'globalId': '786e4c21-70dc-435a-93bb-38',
            'marketType': '',
            'pageNo': 1,
            'pageSize': 100,
        }
        r = http_client.post(
            url, json=payload, headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get('data') or []
        results = []
        for item in rows:
            sc = str(_pick(item, 'sc', 'code', default=''))
            code = _normalize_code(sc)
            if not code or len(code) != 6:
                continue
            results.append({
                '代码': code,
                '名称': str(_pick(item, 'name', 'sn', default='')),
                '排名': int(_safe_float(_pick(item, 'rk', 'rank', default=0))),
                '变化': str(_pick(item, 'rc', 'rankChange', default='0')),
            })
        return results
    except Exception:
        pass
    # 备用：东方财富 web 版人气榜
    try:
        url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
        params = {
            'reportName': 'RPT_STOCK_POPULARITY',
            'columns': 'ALL',
            'sortColumns': 'POPULARITY_RANK',
            'sortTypes': '1',
            'pageSize': '100',
            'pageNumber': '1',
            'source': 'WEB',
            'client': 'WEB',
        }
        data = _get_json(url, params)
        if not data or not data.get('result'):
            return []
        rows = data['result'].get('data') or []
        results = []
        for item in rows:
            code = _normalize_code(_pick(item, 'SECURITY_CODE', 'SECUCODE', default=''))
            if not code or len(code) != 6:
                continue
            results.append({
                '代码': code,
                '名称': str(_pick(item, 'SECURITY_NAME_ABBR', 'SECURITY_NAME', default='')),
                '排名': int(_safe_float(_pick(item, 'POPULARITY_RANK', 'RANK', default=0))),
                '变化': str(_pick(item, 'RANK_CHANGE', 'RC', default='0')),
            })
        return results
    except Exception as e:
        logger.debug(f'HTTP 热门排名获取失败: {e}')
        return []
