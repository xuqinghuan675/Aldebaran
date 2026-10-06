"""
本地结构化分类器（绕过 LLM）

处理龙虎榜（ak.stock_lhb_detail_em）和大宗交易（ak.stock_dzjy_hy）数据，
直接生成与 LLM 输出对齐的 intel structured dict。

设计目标：
  * 100% 本地规则，零外部依赖、零 LLM 调用
  * 输出 schema 与 classify_headlines_batch 完全一致，调用方可统一入库
  * 阈值可在 LHB_THRESHOLDS / DZJY_THRESHOLDS 调整

intel structured dict schema：
  title, category, level, direction, summary, interpretation,
  trading_tip, related_sectors, source
"""
from __future__ import annotations

from typing import Any

# ──────────────────────────────────────────────────────────────────
# 阈值配置
# ──────────────────────────────────────────────────────────────────

LHB_THRESHOLDS = {
    'critical_yi': 3.0,   # 净买/卖额 ≥ 3 亿 → critical
    'important_yi': 1.0,  # 净买/卖额 ≥ 1 亿 → important
}

DZJY_THRESHOLDS = {
    'important_amt_yi': 5.0,    # 成交额 ≥ 5 亿 → important
    'important_prem_pct': 8.0,  # 折溢价绝对值 ≥ 8% → important
    'mid_amt_yi': 1.0,          # 成交额 ≥ 1 亿 → important（中档放行到 important）
    'mid_prem_pct': 4.0,        # 折溢价绝对值 ≥ 4% → important
    'skip_amt_yi': 0.5,         # 成交额 < 5000 万 且 |折溢价| < 2% → 调用方应 skip
    'skip_prem_pct': 2.0,
    'bullish_prem_pct': 3.0,    # 折溢价 ≥ +3% → bullish
    'bearish_prem_pct': -3.0,   # 折溢价 ≤ -3% → bearish
}


# ──────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    """容错转 float（akshare 偶尔会给 '-' 或 None）。"""
    try:
        if v is None or v == '' or v == '-':
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def _resolve_sectors(code: str, sector_map: dict[str, list[str]]) -> list[str]:
    """从映射表取板块（最多 3 个）；缺失返回空列表。"""
    sectors = sector_map.get(code) or []
    return list(sectors[:3])


def _sector_str(sectors: list[str]) -> str:
    """格式化板块字符串：[半导体, AI算力] → '半导体/AI算力'；空 → '相关'。"""
    return '/'.join(sectors) if sectors else '相关'


def _trading_tip(direction: str, level: str) -> str:
    """根据 direction × level 返回操作提示。"""
    if level == 'info' or direction == 'neutral':
        return '信号偏弱，仅做观察'
    if direction == 'bullish':
        if level == 'critical':
            return '强信号关注，板块次日开盘可关注同板块跟风票'
        return '中性偏多信号，板块回调可观察买点'
    # bearish
    if level == 'critical':
        return '强离场信号，回避同板块短期追高'
    return '情绪转弱，持仓注意止盈，回避同板块新进场'


# ──────────────────────────────────────────────────────────────────
# 龙虎榜
# ──────────────────────────────────────────────────────────────────

def classify_lhb_row(
    row: dict[str, Any],
    sector_map: dict[str, list[str]],
) -> dict[str, Any]:
    """
    龙虎榜单行 → intel structured dict。

    ak.stock_lhb_detail_em 字段：
      代码, 名称, 涨跌幅(%), 龙虎榜净买额(元),
      龙虎榜成交额(元), 上榜原因
    """
    code = str(row.get('代码', '') or '').strip()
    name = str(row.get('名称', '') or '').strip()
    pct = _safe_float(row.get('涨跌幅'))
    net_buy = _safe_float(row.get('龙虎榜净买额'))
    reason = str(row.get('上榜原因', '') or '').strip()
    concentration = _safe_float(row.get('净买额占总成交比'))  # 龙虎榜净买额占市场总成交 %

    net_buy_yi = net_buy / 1e8
    abs_net_yi = abs(net_buy_yi)

    # level：先按金额定，再用集中度二次升级（集中度≥5% 代表游资主导，info→important）
    if abs_net_yi >= LHB_THRESHOLDS['critical_yi']:
        level = 'critical'
    elif abs_net_yi >= LHB_THRESHOLDS['important_yi']:
        level = 'important'
    elif abs(concentration) >= 5.0:
        level = 'important'
    else:
        level = 'info'

    # direction
    if net_buy_yi > 0.05:
        direction = 'bullish'
    elif net_buy_yi < -0.05:
        direction = 'bearish'
    else:
        direction = 'neutral'

    sectors = _resolve_sectors(code, sector_map)
    sec_s = _sector_str(sectors)

    # title (≤35 字)
    if direction == 'bullish':
        flow_label = f'净买入{abs_net_yi:.2f}亿'
    elif direction == 'bearish':
        flow_label = f'净卖出{abs_net_yi:.2f}亿'
    else:
        flow_label = '多空均衡'
    title = f'龙虎榜：{name}({code}) {flow_label} 涨跌{pct:+.2f}%'

    # summary (20-40 字)
    reason_short = reason[:12] if reason else '资金异动'
    summary = f'{name}因{reason_short}上榜，{flow_label}，涨跌{pct:+.2f}%。'

    # interpretation (50-100 字)
    if direction == 'bullish':
        interpretation = (
            f'{name}({code})当日机构/游资合计净买入{abs_net_yi:.2f}亿元，'
            f'涨幅{pct:+.2f}%。资金对{sec_s}板块短期情绪明确，'
            f'关注次日能否放量延续，警惕高开低走的获利兑现。'
        )
    elif direction == 'bearish':
        interpretation = (
            f'{name}({code})当日净卖出{abs_net_yi:.2f}亿元，'
            f'涨跌{pct:+.2f}%。主力资金获利了结或止损离场，'
            f'{sec_s}板块情绪短期承压，回避追高，等待企稳信号。'
        )
    else:
        interpretation = (
            f'{name}({code})当日多空博弈均衡，净买额接近 0，'
            f'涨跌{pct:+.2f}%。{sec_s}板块内部资金分歧明显，'
            f'等待下一交易日的方向选择信号，暂不宜重仓追入。'
        )

    if abs(concentration) >= 3.0:
        concentration_label = '高' if abs(concentration) >= 5.0 else '中等'
        interpretation += f'龙虎榜净买额占市场总成交 {abs(concentration):.1f}%，资金集中度{concentration_label}。'

    return {
        'title': title,
        'category': 'finance',
        'level': level,
        'direction': direction,
        'summary': summary,
        'interpretation': interpretation,
        'trading_tip': _trading_tip(direction, level),
        'related_sectors': sectors,
        'source': '龙虎榜',
    }


# ──────────────────────────────────────────────────────────────────
# 大宗交易
# ──────────────────────────────────────────────────────────────────

def should_skip_dzjy(row: dict[str, Any]) -> bool:
    """大宗交易 row 噪音过滤：成交额 < 5000 万 且 |折溢价| < 2% → skip。

    抓取层在喂给 classify_dzjy_row 前应先调用此函数，符合的直接丢弃。
    """
    amount = _safe_float(row.get('成交额'))
    premium = _safe_float(row.get('折溢价率'))
    amount_yi = amount / 1e8
    return (
        amount_yi < DZJY_THRESHOLDS['skip_amt_yi']
        and abs(premium) < DZJY_THRESHOLDS['skip_prem_pct']
    )


def classify_dzjy_row(
    row: dict[str, Any],
    sector_map: dict[str, list[str]],
) -> dict[str, Any]:
    """
    大宗交易单行 → intel structured dict。

    ak.stock_dzjy_hy 字段：
      证券代码, 证券简称, 成交价格, 折溢价率(%),
      成交额(元), 成交量(股)

    调用方应先用 should_skip_dzjy() 过滤；本函数对所有进入的行都生成结果。
    """
    code = str(row.get('证券代码', '') or '').strip()
    name = str(row.get('证券简称', '') or '').strip()
    price = _safe_float(row.get('成交价格'))
    premium = _safe_float(row.get('折溢价率'))
    amount = _safe_float(row.get('成交额'))

    amount_yi = amount / 1e8
    abs_prem = abs(premium)

    # level（三档：≥5亿 或 ≥8%折溢价 → important; ≥1亿 或 ≥4% → 也算 important; 其他 info）
    if (amount_yi >= DZJY_THRESHOLDS['important_amt_yi']
            or abs_prem >= DZJY_THRESHOLDS['important_prem_pct']):
        level = 'important'
    elif (amount_yi >= DZJY_THRESHOLDS['mid_amt_yi']
            or abs_prem >= DZJY_THRESHOLDS['mid_prem_pct']):
        level = 'important'
    else:
        level = 'info'

    # direction
    if premium >= DZJY_THRESHOLDS['bullish_prem_pct']:
        direction = 'bullish'
    elif premium <= DZJY_THRESHOLDS['bearish_prem_pct']:
        direction = 'bearish'
    else:
        direction = 'neutral'

    sectors = _resolve_sectors(code, sector_map)
    sec_s = _sector_str(sectors)

    # 价格语态
    if premium > 0:
        prem_label = f'溢价{abs_prem:.1f}%'
    elif premium < 0:
        prem_label = f'折价{abs_prem:.1f}%'
    else:
        prem_label = '平价'

    # title (≤35 字)
    title = f'大宗交易：{name}({code}) {prem_label} 成交{amount_yi:.2f}亿'

    # summary (20-40 字)
    summary = f'{name}({code}) 大宗{prem_label}，成交{amount_yi:.2f}亿元。'

    # interpretation (50-100 字)
    if direction == 'bullish':
        interpretation = (
            f'{name}({code})大宗交易溢价{abs_prem:.1f}%成交{amount_yi:.2f}亿元，'
            f'成交价{price:.3f}元。溢价成交显示买方接盘意愿强，'
            f'资金对{sec_s}板块预期向好，可留意跟进机会。'
        )
    elif direction == 'bearish':
        interpretation = (
            f'{name}({code})大宗交易折价{abs_prem:.1f}%成交{amount_yi:.2f}亿元，'
            f'成交价{price:.3f}元。高折价抛售显示卖方急于变现，'
            f'{sec_s}板块面临抛压传导，短期建议规避。'
        )
    else:
        interpretation = (
            f'{name}({code})平价附近大宗交易，成交{amount_yi:.2f}亿元，'
            f'折溢价{premium:+.1f}%。买卖双方预期平稳，'
            f'对{sec_s}板块短期影响中性偏弱。'
        )

    return {
        'title': title,
        'category': 'finance',
        'level': level,
        'direction': direction,
        'summary': summary,
        'interpretation': interpretation,
        'trading_tip': _trading_tip(direction, level),
        'related_sectors': sectors,
        'source': '大宗交易',
    }
