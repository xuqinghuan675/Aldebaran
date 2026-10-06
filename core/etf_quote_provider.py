"""
独立 TickFlow 实时行情 provider（历史文件名保留 etf）—— 付费接口接入层。

仅在用户配置了 TickFlow token 时启用（设置里填 key）；未配置则不调用。
返回字段与 core.data_source.tencent_quotes 对齐，便于调用方无感替换/回退。

未来替换 QMT / 券商 API 时，只需修改本模块内部实现，
UI 和业务方只调用 fetch_etf_quotes()（兼容旧名，实际可服务非期权 6 位代码）。
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

from core.credentials import load_tickflow_token

logger = logging.getLogger(__name__)

_BASE_URL = 'https://api.tickflow.org'
_QUOTES_PATH = '/v1/quotes'
_BATCH = 40
# 硬保护：单次最多请求的批数，防止大列表把 TickFlow 串行请求拖到几十秒。
# 超出的代码由调用方（quotes_routed）回退免费源补齐。
_MAX_BATCHES = 2


def _to_tf_symbol(code: str) -> str:
    """裸 6 位代码 -> TickFlow 带交易所后缀格式（510300 -> 510300.SH）。"""
    c = str(code).strip().lower()
    if c.startswith(('sh', 'sz', 'bj')):
        suffix = c[:2]
        c = c[2:]
    elif len(c) == 6 and c.startswith(('15', '16', '18')):
        suffix = 'sz'
    elif len(c) == 6 and c.startswith(('50', '51', '52', '56', '58')):
        suffix = 'sh'
    else:
        suffix = 'sh' if c[:1] in ('6', '9', '5') else 'sz'
    return f'{c}.{suffix.upper()}'


def _safe_float(val) -> float:
    try:
        v = float(val)
        if v != v:  # NaN
            return 0.0
        return v
    except (TypeError, ValueError):
        return 0.0


def _first(d: dict, *keys):
    """返回 d 中第一个存在且非空的键值（容忍 TickFlow 字段命名差异）。"""
    for k in keys:
        if k in d and d[k] not in (None, '', 0):
            return d[k]
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _extract_rows(body) -> list[dict]:
    """从 TickFlow 响应里取出 quote 列表（兼容多种包裹结构）。"""
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if not isinstance(body, dict):
        return []
    data = body.get('data', body)
    if isinstance(data, dict):
        for key in ('quotes', 'items', 'rows', 'list'):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # data 本身就是单条
        if any(k in data for k in ('symbol', 'code', 'last_price', 'price')):
            return [data]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _normalize_row(row: dict) -> tuple[str, dict] | None:
    """把一条 TickFlow quote 归一成 (裸6位code, tencent 同构字段 dict)。

    TickFlow 实时报价结构（实测）：顶层有 last_price/prev_close/open/high/low/
    volume/amount，名称与涨跌幅/换手率在嵌套的 ext 里，且 change_pct/
    turnover_rate 是**小数比例**（-0.0205 = -2.05%），需 ×100 对齐 tencent。
    """
    sym = str(_first(row, 'symbol', 'code', 'instrument', 'ticker') or '').strip()
    if not sym:
        return None
    bare = sym.split('.')[0].strip()
    if bare.lower().startswith(('sh', 'sz', 'bj')):
        bare = bare[2:]
    if len(bare) != 6 or not bare.isdigit():
        return None

    ext = row.get('ext') if isinstance(row.get('ext'), dict) else {}

    price = _safe_float(_first(row, 'last_price', 'price', 'last', 'close'))
    pre_close = _safe_float(_first(row, 'prev_close', 'pre_close', 'preclose',
                                   'yesterday_close', 'prev_settlement'))

    # 涨跌幅：优先 ext.change_pct（小数比例 → ×100）；缺失则用价差现算
    pct_raw = _first(ext, 'change_pct', 'pct') if ext else None
    if pct_raw is None:
        pct_raw = _first(row, 'pct', 'change_pct', 'pct_change', 'change_percent', 'chg_pct')
        pct = _safe_float(pct_raw) if pct_raw is not None else None
    else:
        pct = _safe_float(pct_raw) * 100.0
    if pct is None:
        pct = (price - pre_close) / pre_close * 100 if (pre_close > 0 and price > 0) else 0.0

    # 换手率：ext.turnover_rate 同为小数比例 → ×100
    turnover_raw = _first(ext, 'turnover_rate', 'turnover') if ext else None
    if turnover_raw is None:
        turnover = _safe_float(_first(row, 'turnover', 'turnover_rate', 'turnover_ratio'))
    else:
        turnover = _safe_float(turnover_raw) * 100.0

    name = str((_first(ext, 'name', 'symbol_name', 'cn_name') if ext else None)
               or _first(row, 'name', 'symbol_name', 'cn_name') or '').strip()

    quote = {
        'name': name,
        'price': price,
        'pre_close': pre_close,
        'open': _safe_float(_first(row, 'open', 'open_price')),
        'high': _safe_float(_first(row, 'high', 'high_price')),
        'low': _safe_float(_first(row, 'low', 'low_price')),
        'volume': _safe_float(_first(row, 'volume', 'vol')),
        'amount': _safe_float(_first(row, 'amount', 'turnover_amount', 'value')),
        'turnover': turnover,
        'pct': _safe_float(pct),
        'pe': 0.0,
        'source': 'tickflow',
    }
    return bare, quote


def fetch_etf_quotes(codes: Iterable[str], timeout: float = 6,
                     max_batches: int | None = _MAX_BATCHES) -> dict[str, dict]:
    """批量获取 TickFlow 实时行情。返回 {6位code: dict}。

    dict 字段与 tencent_quotes 对齐：
        name, price, pre_close, open, high, low, volume, amount,
        turnover, pct, pe, source='tickflow'
    无 token / 请求失败 / 无有效数据 → 返回 {}（由调用方回退免费源），不抛异常。
    max_batches=None 时不限批数（付费批量场景，如 ETF 数据库更新）。
    """
    codes_list = [str(c).strip() for c in codes if str(c).strip()]
    if not codes_list:
        return {}
    token = load_tickflow_token()
    if not token:
        # 未配置第三方 Token 时不调用 TickFlow，由调用方回退免费公开源。
        return {}
    headers = {
        'X-API-Key': token,
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    limit = len(codes_list) if max_batches is None else min(len(codes_list), _BATCH * max_batches)
    out: dict[str, dict] = {}
    for i in range(0, limit, _BATCH):
        batch = codes_list[i:i + _BATCH]
        symbols = ','.join(_to_tf_symbol(c) for c in batch)
        try:
            resp = requests.get(
                _BASE_URL + _QUOTES_PATH,
                headers=headers,
                params={'symbols': symbols},
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            logger.warning('[tickflow] ETF 行情请求失败: %s', e)
            continue
        for row in _extract_rows(body):
            norm = _normalize_row(row)
            if norm and norm[1].get('price'):
                out[norm[0]] = norm[1]
    return out
