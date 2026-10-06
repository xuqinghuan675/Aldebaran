"""统一行情数据源（多源回退）。

目的：TickFlow 付费源可用时优先使用，失效/缺失时自动回退腾讯/新浪免费源。

可用函数：
- tencent_quotes(codes, timeout)：腾讯 qt.gtimg.cn，返回 {code: {...}}
- sina_quotes(codes, timeout)：新浪 hq.sinajs.cn，返回 {code: {...}}
- get_stock_quotes(codes, timeout)：统一入口（TickFlow -> 腾讯 -> 新浪）
- quotes_routed(codes, timeout)：同 get_stock_quotes，供新代码显式表达路由语义

适用：A 股普通行情（价格、涨跌幅、成交量、成交额、换手率）。
不适用：板块/个股资金流（仅东财独占字段 f62/f184）。
"""
from __future__ import annotations
from typing import Iterable

from core import http_client


_TENCENT_URL = 'https://qt.gtimg.cn/q='
_SINA_URL = 'https://hq.sinajs.cn/list='

_COMMON_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)
_TENCENT_HEADERS = {'User-Agent': _COMMON_UA}
_SINA_HEADERS = {
    'User-Agent': _COMMON_UA,
    'Referer': 'https://finance.sina.com.cn/',  # 必需，否则 403
}


def _normalize_code(code: str) -> str:
    """裸 6 位代码转 sh/sz/bj 前缀格式。已带前缀的原样返回。

    ETF 优先规则（必须在通用规则之前）：
      深市 ETF 15xxxx/16xxxx/18xxxx → sz
      沪市 ETF 50xxxx/51xxxx/52xxxx/56xxxx/58xxxx → sh
    股票通用规则：
      6xxxxx → sh（沪市 A）
      9xxxxx(非 92) → sh（沪市 B 等）
      92xxxx → bj（北交所新代码段）
      0xxxxx/3xxxxx → sz（深市 A/创业板）
      4xxxxx/8xxxxx → bj（北交所）
    """
    c = str(code).strip().lower()
    if c.startswith(('sh', 'sz', 'bj')):
        return c
    if len(c) == 6:
        # ETF 优先（深市 ETF 前缀 15/16/18，沪市 ETF 前缀 50/51/52/56/58）
        if c.startswith(('15', '16', '18')):
            return f'sz{c}'
        if c.startswith(('50', '51', '52', '56', '58')):
            return f'sh{c}'
        # 通用股票路由
        if c.startswith('92'):
            return f'bj{c}'
        if c.startswith(('6', '9')):
            return f'sh{c}'
        if c.startswith(('0', '3')):
            return f'sz{c}'
        if c.startswith(('4', '8')):
            return f'bj{c}'
    return f'sz{c}'


def is_etf_code(code: str) -> bool:
    """是否为 ETF/LOF 基金代码（深市 15/16/18，沪市 50/51/52/56/58）。

    兼容裸 6 位和带 sh/sz/bj 前缀两种形式。
    """
    c = str(code).strip().lower()
    if c.startswith(('sh', 'sz', 'bj')):
        c = c[2:]
    if len(c) != 6 or not c.isdigit():
        return False
    return c.startswith(('15', '16', '18', '50', '51', '52', '56', '58'))


def price_decimals(code=None, *, is_option: bool = False) -> int:
    """价格显示小数位：ETF / 期权 用 4 位，其余（股票等）用 3 位。"""
    return 4 if (is_option or (code and is_etf_code(code))) else 3


def fmt_price(value, code=None, *, is_option: bool = False) -> str:
    """按标的类型格式化价格字符串：ETF / 期权 4 位小数，其余 3 位。

    无法转为数字时返回 '0.000'（与原 f'{x:.3f}' 行为一致，由调用方决定空值占位）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return f'{v:.{price_decimals(code, is_option=is_option)}f}'


def _safe_float(val) -> float:
    try:
        v = float(val)
        if v != v:  # NaN
            return 0.0
        return v
    except (TypeError, ValueError):
        return 0.0


def tencent_quotes(codes: Iterable[str], timeout: float = 6) -> dict[str, dict]:
    """从腾讯 qt.gtimg.cn 获取实时行情。返回 {6位code: dict}。

    dict 字段：name, price, pct, open, pre_close, high, low,
              volume(股), amount(元), turnover(%), source='tencent'
    """
    codes_list = [str(c).strip() for c in codes if str(c).strip()]
    if not codes_list:
        return {}
    full_codes = ','.join(_normalize_code(c) for c in codes_list)
    resp = http_client.get(
        _TENCENT_URL + full_codes,
        headers=_TENCENT_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.content.decode('gbk', 'replace')

    out: dict[str, dict] = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        val = line.split('=', 1)[1].strip().strip('"')
        fields = val.split('~')
        if len(fields) < 33:
            continue
        code = fields[2].strip()
        # Tencent 字段位置（A 股普通行情）
        out[code] = {
            'name': fields[1],
            'price': _safe_float(fields[3]),
            'pre_close': _safe_float(fields[4]),
            'open': _safe_float(fields[5]),
            'volume': _safe_float(fields[6]) * 100,  # 手 -> 股
            'pct': _safe_float(fields[32]),
            'high': _safe_float(fields[33]) if len(fields) > 33 else 0.0,
            'low': _safe_float(fields[34]) if len(fields) > 34 else 0.0,
            'amount': _safe_float(fields[37]) * 1e4 if len(fields) > 37 else 0.0,  # 万元 -> 元
            'turnover': _safe_float(fields[38]) if len(fields) > 38 else 0.0,
            'pe': _safe_float(fields[39]) if len(fields) > 39 else 0.0,
            'source': 'tencent',
        }
    return out


def sina_quotes(codes: Iterable[str], timeout: float = 6) -> dict[str, dict]:
    """从新浪 hq.sinajs.cn 获取实时行情。返回 {6位code: dict}。

    dict 字段：name, price, pct, open, pre_close, high, low,
              volume(股), amount(元), turnover(0, 新浪不直接返回), source='sina'
    """
    codes_list = [str(c).strip() for c in codes if str(c).strip()]
    if not codes_list:
        return {}
    full_codes = ','.join(_normalize_code(c) for c in codes_list)
    resp = http_client.get(
        _SINA_URL + full_codes,
        headers=_SINA_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.content.decode('gbk', 'replace')

    out: dict[str, dict] = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        # 形如 `var hq_str_sh600000="浦发银行,..."`
        key, val = line.split('=', 1)
        val = val.strip().strip('"')
        sym = key.replace('var hq_str_', '').strip()
        if sym.startswith(('sh', 'sz', 'bj')):
            code = sym[2:]
        else:
            code = sym
        fields = val.split(',')
        if len(fields) < 10:
            continue
        name = fields[0]
        today_open = _safe_float(fields[1])
        pre_close = _safe_float(fields[2])
        price = _safe_float(fields[3])
        high = _safe_float(fields[4])
        low = _safe_float(fields[5])
        volume = _safe_float(fields[8])      # 股
        amount = _safe_float(fields[9])      # 元
        pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
        out[code] = {
            'name': name,
            'price': price,
            'pre_close': pre_close,
            'open': today_open,
            'volume': volume,
            'pct': pct,
            'high': high,
            'low': low,
            'amount': amount,
            'turnover': 0.0,  # 新浪不直接返回换手率
            'pe': 0.0,
            'source': 'sina',
        }
    return out


def _free_stock_quotes(codes: Iterable[str], timeout: float = 6) -> dict[str, dict]:
    """免费源行情：腾讯 -> 新浪。任一返回非空即采用。"""
    codes_list = list(codes)
    if not codes_list:
        return {}
    last_exc: Exception | None = None
    for fn in (tencent_quotes, sina_quotes):
        try:
            result = fn(codes_list, timeout=timeout)
            if result:
                return result
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    return {}


def get_stock_quotes(codes: Iterable[str], timeout: float = 6) -> dict[str, dict]:
    """统一行情入口：TickFlow 可用时优先，失败自动回退免费源。"""
    return quotes_routed(codes, timeout=timeout)


# 批量超过此数（板块扫描、全市场刷新等）直接走免费源：TickFlow 按 40 一批串行请求，
# 几百只代码会串行卡几十秒、兜底还排在其后，拖垮整体；这类场景只需排名用的价格/涨跌幅，
# 免费源一次拿全，用不上付费精确价。
_TF_MAX_CODES = 40
# 前置 TickFlow 单次请求超时上限：即便走 TickFlow 也不让它卡太久才兜底。
_TF_FRONT_TIMEOUT = 4.0


def quotes_routed(codes: Iterable[str], timeout: float = 6, *,
                  bulk_tickflow: bool = False) -> dict[str, dict]:
    """带 TickFlow 双源路由的行情入口。返回 {6位code: dict}。

    - 未配置 TickFlow token / 批量请求（> _TF_MAX_CODES）：整体走免费源（腾讯 -> 新浪）。
    - 已配置 token 且小列表：非期权 6 位代码先走 TickFlow；
      TickFlow 缺失/失败自动用免费源兜底。
    - bulk_tickflow=True：批量也强制走 TickFlow（ETF 数据库更新等付费批量场景），
      不受 _TF_MAX_CODES 阈值与前置超时/批数上限限制。
    字段 schema 与 tencent_quotes 一致，调用方无感。
    """
    codes_list = [str(c).strip() for c in codes if str(c).strip()]
    if not codes_list:
        return {}
    try:
        from core.credentials import has_tickflow_token
        use_tf = has_tickflow_token()
    except Exception:
        use_tf = False
    if not use_tf or (len(codes_list) > _TF_MAX_CODES and not bulk_tickflow):
        return _free_stock_quotes(codes_list, timeout=timeout)

    out: dict[str, dict] = {}

    tf_quotes: dict[str, dict] = {}
    try:
        from core.etf_quote_provider import fetch_etf_quotes
        if bulk_tickflow:
            tf_quotes = fetch_etf_quotes(codes_list, timeout=timeout, max_batches=None)
        else:
            tf_quotes = fetch_etf_quotes(codes_list, timeout=min(timeout, _TF_FRONT_TIMEOUT))
    except Exception:
        tf_quotes = {}

    fallback_codes = [
        c for c in codes_list
        if _bare(c) not in tf_quotes or not tf_quotes[_bare(c)].get('price')
    ]
    if fallback_codes:
        try:
            out.update(_free_stock_quotes(fallback_codes, timeout=timeout))
        except Exception:
            pass
    # TickFlow 有效报价覆盖兜底值。
    out.update({k: v for k, v in tf_quotes.items() if v.get('price')})
    return out


def _bare(code: str) -> str:
    c = str(code).strip().lower()
    if c.startswith(('sh', 'sz', 'bj')):
        c = c[2:]
    return c
