"""基本面数据拉取 + 新鲜度检查 + prompt 摘要。

数据源：
  主源：mootdx finance()（通达信协议静态财务快照）
  补充：akshare stock_financial_analysis_indicator()（毛利率/营收增速/净利润增速）
新鲜度：若 updated_date 字段超过 180 天，返回安全占位文本，不送误导性数字给 AI

缓存：CACHE_DIR/fundamentals/{code}.json（当日缓存）
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'fundamentals'

_STALE_TEXT = '▶ 基本面：财报数据不足或过时（距今超 6 个月），跳过基本面分析'
_NO_DATA_TEXT = '▶ 基本面：暂无数据，跳过基本面分析'


def _cache_path(code: str) -> Path:
    return _CACHE_DIR / f'{code}.json'


def _cache_is_fresh(code: str) -> bool:
    p = _cache_path(code)
    if not p.exists():
        return False
    try:
        meta = json.loads(p.read_text(encoding='utf-8'))
        return meta.get('date', '') == datetime.now().strftime('%Y-%m-%d')
    except Exception:
        return False


def _load_cache(code: str) -> dict | None:
    try:
        meta = json.loads(_cache_path(code).read_text(encoding='utf-8'))
        return meta.get('data')
    except Exception:
        return None


def _save_cache(code: str, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'data': data,
    }
    _cache_path(code).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding='utf-8'
    )


def _safe_float(val) -> float | None:
    try:
        v = float(val)
        import math
        return None if math.isnan(v) or math.isinf(v) else v
    except Exception:
        return None


def _fetch_akshare_supplement(code: str) -> dict:
    """用 akshare 获取补充财务指标（毛利率/营收增速/净利润增速）。失败返回空 dict。"""
    try:
        import akshare as ak
        symbol = code[-6:]
        df = ak.stock_financial_analysis_indicator(
            symbol=symbol, start_year=str(date.today().year - 1)
        )
        if df is None or df.empty:
            return {}
        row = df.iloc[0]
        result: dict = {}
        for col in df.columns:
            col_s = str(col)
            v = _safe_float(row.get(col))
            if v is None:
                continue
            if '毛利率' in col_s and 'gross_margin' not in result:
                result['gross_margin'] = v
            elif ('营业收入同比增长率' in col_s or '主营收入同比增长率' in col_s) \
                    and 'revenue_growth' not in result:
                result['revenue_growth'] = v
            elif ('净利润同比增长率' in col_s or '净利润同比' in col_s) \
                    and 'profit_growth' not in result:
                result['profit_growth'] = v
        return result
    except Exception as e:
        logger.debug(f'akshare 财务指标获取失败 [{code}]: {e}')
        return {}


def fetch_finance(code: str) -> dict | None:
    """从 mootdx 获取财务快照，返回 dict；失败返回 None。"""
    if _cache_is_fresh(code):
        return _load_cache(code)

    try:
        from mootdx.quotes import Quotes
        c = Quotes.factory(market='std')
        df = c.finance(symbol=code[-6:])
        if df is None or df.empty:
            return None
        row = df.iloc[0].to_dict()
        result = {k: (v if not hasattr(v, '__float__') else _safe_float(v))
                  for k, v in row.items()}
        # 合并 akshare 补充指标（失败返回空 dict，当日也缓存避免重试）
        result.update(_fetch_akshare_supplement(code))
        _save_cache(code, result)
        return result
    except Exception:
        return None


def _check_freshness(data: dict) -> bool:
    """判断财务数据是否新鲜（updated_date 在 180 天内）。"""
    raw = data.get('updated_date')
    if raw is None:
        return False
    try:
        date_str = str(int(raw))
        d = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
        return (date.today() - d).days <= 180
    except Exception:
        return False


def _fmt_yuan(val: float | None, unit: str = '亿') -> str:
    """格式化金额。mootdx finance() 财务报表字段单位为角(0.1元)，已内置转换。"""
    if val is None:
        return 'N/A'
    val_yuan = val / 10
    if unit == '亿':
        return f'{val_yuan / 1e8:.2f} 亿'
    if unit == '万亿':
        return f'{val_yuan / 1e12:.2f} 万亿'
    return f'{val_yuan:.2f}'


def _quarter_tag_for_month(month: int) -> str:
    """数据更新月 → 当月最新【已披露】报告期。A股披露窗口：年报次年1-4月、Q1四月、
    半年报7-8月、三季报10月。故6月最新仍是Q1(半年报未出)、9月仍是半年报(三季报未出)、
    11-12月仍是前三季(年报未出)。旧逻辑把6月误判半年报→Q1数据被×2年化(应×4)。"""
    if month <= 3:
        return '年报'
    if month <= 6:
        return 'Q1'
    if month <= 9:
        return '半年报'
    return '前三季'


def _period_label(quarter_tag: str) -> str:
    return {
        'Q1': 'Q1一季报（单期）',
        '半年报': '半年报（上半年累计）',
        '前三季': '前三季报（前三季累计）',
        '年报': '年报（年度）',
    }.get(quarter_tag, quarter_tag)


def summarize(code: str) -> str:
    """生成适合 AI prompt 注入的基本面摘要文本。"""
    data = fetch_finance(code)
    if data is None:
        return _NO_DATA_TEXT
    if not _check_freshness(data):
        return _STALE_TEXT

    upd_raw = data.get('updated_date')
    quarter_tag = ''
    if upd_raw:
        quarter_tag = _quarter_tag_for_month(int(str(int(upd_raw))[4:6]))
    period_hint = f'（{quarter_tag}单期）' if quarter_tag and quarter_tag != '年报' else ''

    lines = [f'▶ 基本面快照（{code}）：']
    if quarter_tag:
        lines.append(f'  报告期口径：最新已披露=【{_period_label(quarter_tag)}】（按数据更新月推导），引用财务数字须严格按此口径，禁止改用其他报告期称谓')

    revenue = _safe_float(data.get('zhuyingshouru'))
    net_profit = _safe_float(data.get('jinglirun'))
    net_asset = _safe_float(data.get('jingzichan'))
    total_asset = _safe_float(data.get('zongzichan'))
    cur_asset = _safe_float(data.get('liudongzichan'))
    cur_liab = _safe_float(data.get('liudongfuzhai'))
    lt_liab = _safe_float(data.get('changqifuzhai'))
    eps_bv = _safe_float(data.get('meigujingzichan'))
    shares_circ = _safe_float(data.get('liutongguben'))

    if revenue is not None:
        lines.append(f'  主营收入: {_fmt_yuan(revenue)}{period_hint}')
    if net_profit is not None:
        lines.append(f'  净利润: {_fmt_yuan(net_profit)}{period_hint}')

    # 净利率（金融类公司净利润可能远超主营收入，标注不适用）
    if revenue and net_profit and revenue > 0:
        margin = net_profit / revenue * 100
        if margin > 100:
            lines.append(f'  净利率: 不适用（净利润>{_fmt_yuan(net_profit)}远超主营收入{_fmt_yuan(revenue)}，该公司可能有大量非主营收入如投资/利息收入）')
        else:
            lines.append(f'  净利率: {margin:.1f}%')

    # ROE（复用顶部 quarter_tag）
    if net_profit is not None and net_asset and net_asset > 0:
        roe = net_profit / net_asset * 100
        roe_hint = period_hint or '（单期，非TTM）'
        lines.append(f'  ROE: {roe:.1f}%{roe_hint}')
        # 非年报时给出年化估算，防止 LLM 自行乘算
        _q_mult = {'Q1': 4, '半年报': 2, '前三季': 4 / 3}
        if quarter_tag in _q_mult:
            roe_ann = roe * _q_mult[quarter_tag]
            lines.append(f'  ROE年化估算: ≈{roe_ann:.1f}%（简单年化，仅供参考，勿当作实际年报ROE）')

    # 流动比率
    if cur_asset and cur_liab and cur_liab > 0:
        ratio = cur_asset / cur_liab
        lines.append(f'  流动比率: {ratio:.2f}（<1.0 注意短期偿债压力）' if ratio < 1 else f'  流动比率: {ratio:.2f}')

    # 资产负债率：优先用 总资产-净资产（银行/金融的流动/非流动负债科目不全，cur+lt 会算出≈0 的假值）
    if total_asset and total_asset > 0:
        if net_asset is not None:
            total_liab = total_asset - net_asset
        elif cur_liab is not None or lt_liab is not None:
            total_liab = (cur_liab or 0) + (lt_liab or 0)
        else:
            total_liab = None
        if total_liab is not None and total_liab >= 0:
            lev = total_liab / total_asset * 100
            lev_note = '（偏高）' if lev > 70 else ''
            lines.append(f'  资产负债率: {lev:.1f}%{lev_note}')

    if eps_bv is not None:
        lines.append(f'  每股净资产: {eps_bv:.3f} 元')

    # 市值估算（需要外部当前价，这里只给流通股本参考）
    if shares_circ is not None:
        lines.append(f'  流通股本: {shares_circ/1e8:.2f} 亿股')

    # akshare 补充指标（已在 fetch_finance 合并入 cache）
    gm = _safe_float(data.get('gross_margin'))
    rg = _safe_float(data.get('revenue_growth'))
    pg = _safe_float(data.get('profit_growth'))
    if gm is not None:
        lines.append(f'  毛利率: {gm:.1f}%')
    if rg is not None:
        lines.append(f'  营收同比: {rg:+.1f}%')
    if pg is not None:
        lines.append(f'  净利润同比: {pg:+.1f}%')

    # 数据时间
    upd = data.get('updated_date')
    if upd:
        lines.append(f'  数据截至: {str(int(upd))[:4]}-{str(int(upd))[4:6]}-{str(int(upd))[6:8]}')

    if len(lines) == 1:
        return _NO_DATA_TEXT

    return '\n'.join(lines)
