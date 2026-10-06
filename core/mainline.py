"""大主线 / 小主线识别。

口径：大主线 = 当前市场总龙头方向（目前为 AI 算力链），小主线 = 次级轮动题材。
识别用「代码清单 + 关键词 + 数据驱动强势板块」三路 OR，因个股名/板块推断常抓不住
真龙头（如工业富联名字无 AI），代码清单是必需兜底。主线随行情轮动，直接改本文件。
"""
from __future__ import annotations

# 大主线：AI 算力链（光模块/CPO、算力服务器、AI 芯片、先进封装、PCB 载板、液冷/铜连接、通信设备）
AI_MAJOR_CODES = {
    # 光模块 / CPO / 光器件
    '300308', '300502', '300394', '002281', '603083', '300570', '300548', '688498', '688191',
    # 算力 / 服务器 / 算力租赁
    '601138', '000977', '603019', '000938', '000034', '000628', '300383', '688256',
    # AI 芯片 / GPU / 存储 / 接口
    '688041', '688047', '300474', '688008', '688981', '688521', '603986', '688200',
    # PCB / 载板（AI 算力硬件）
    '300476', '002463', '600183', '002916', '002384',
    # 铜连接 / 液冷 / AI 连接
    '002130', '002837', '002475', '300913', '002179',
    # 通信设备 / 光通信整机
    '000063', '600498',
}

AI_MAJOR_KEYWORDS = (
    '算力', '光模块', '光通信', '光器件', 'CPO', '半导体', '芯片', '人工智能',
    '数据中心', '云计算', '服务器', '先进封装', '液冷', '存储芯片', 'GPU',
    'AI算力', 'AI应用', 'AI芯片', 'PCB', '铜连接', '通信设备',
)

# 小主线：硬编码次级轮动题材（关键词），随轮动调整
MINOR_THEME_KEYWORDS = (
    '机器人', '人形机器人', '减速器', '创新药', '减肥药', '军工', '商业航天',
    '固态电池', '可控核聚变', '稀土', '永磁', '低空经济', '智能驾驶', '华为',
    '数字货币', '稳定币', '电力设备', '储能',
)


def _text_of(name: str, sectors: list[str] | None) -> str:
    return f"{name or ''} " + ' '.join(str(s) for s in (sectors or []))


def classify_mainline(
    code: str,
    name: str,
    sectors: list[str] | None = None,
    strong_sectors: list[str] | None = None,
) -> str:
    """返回 'major'（大主线/AI）| 'minor'（小主线）| 'none'。"""
    from core.board_rules import is_etf
    if is_etf(code):
        return 'none'
    c = str(code or '').strip()[-6:]
    if c in AI_MAJOR_CODES:
        return 'major'
    text = _text_of(name, sectors)
    if any(kw in text for kw in AI_MAJOR_KEYWORDS):
        return 'major'
    if any(kw in text for kw in MINOR_THEME_KEYWORDS):
        return 'minor'
    # 数据驱动：个股所属板块命中当日强势板块 → 小主线
    if strong_sectors:
        sset = set(str(s) for s in (sectors or []))
        if sset & set(str(s) for s in strong_sectors):
            return 'minor'
    return 'none'


def rs_leads_market(stock_ret: float | None, index_ret: float | None) -> bool:
    """个股区间收益是否跑赢大盘（RS 领先）。"""
    if stock_ret is None or index_ret is None:
        return False
    return float(stock_ret) > float(index_ret)
