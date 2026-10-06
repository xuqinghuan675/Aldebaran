"""intel_fetcher.py — 自动抓取财经快讯并通过 DeepSeek 结构化分类。

流程：
  1. 6 源全并行：财联社 + 华尔街见闻 + 新浪 + 东财 + Reuters + CNBC，合并去重
  2. 相似度去重（>70% 视为重复）
  3. 规则粗筛（黑名单 regex 先杀，白名单关键词留存，100条→约30条）
  4. Batch 调用 DeepSeek chat API 分类（每批 20 条，2-3 次调用）
  5. 本地质量门槛过滤（neutral+info+无板块 不入库）
  6. 追加到 data/intel_feed.json

配置文件：BASE_DIR/intel_config.json
  {"deepseek_api_key": "sk-..."}
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from core.cache import _FEED_LOCK  # 复用 intel_feed.json 写锁
from core.js_runtime import js_runtime_diagnostic
from core.intel_local_classify import (
    classify_lhb_row,
    classify_dzjy_row,
    should_skip_dzjy,
)

logger = logging.getLogger(__name__)


def _js_diag_suffix() -> str:
    diag = js_runtime_diagnostic()
    return f' ({diag})' if diag else ''

from core.paths import HOME, CACHE_DIR as _BASE_CACHE, INTEL_FEED_FILE as _FEED_FILE  # noqa: E402
_CONFIG_FILE = HOME / 'intel_config.json'

# 项目根目录下的 data/ 资产
_PROJECT_ROOT = Path(__file__).parent.parent
_LHB_SECTOR_MAP_FILE = _PROJECT_ROOT / 'data' / 'lhb_code_sector_map.json'
_WHITELIST_SUPP_FILE = _PROJECT_ROOT / 'data' / 'whitelist_keywords_supplement.json'
_BLACKLIST_SUPP_FILE = _PROJECT_ROOT / 'data' / 'blacklist_regex_supplement.json'
_INTEL_CONFIG_FILE = _PROJECT_ROOT / 'data' / 'intel_config.json'


def _load_lhb_sector_map() -> dict[str, list[str]]:
    """读取 lhb_code_sector_map.json；失败返回 {}。"""
    try:
        if _LHB_SECTOR_MAP_FILE.exists():
            return json.loads(_LHB_SECTOR_MAP_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning('lhb_code_sector_map.json 加载失败: %s', e)
    return {}


def _load_intel_config() -> dict:
    """读取 intel_config.json；失败返回默认 {'enable_overseas': True}。"""
    try:
        if _INTEL_CONFIG_FILE.exists():
            cfg = json.loads(_INTEL_CONFIG_FILE.read_text(encoding='utf-8'))
            if isinstance(cfg, dict):
                return cfg
    except Exception as e:
        logger.debug('intel_config.json 读取失败（用默认）: %s', e)
    return {'enable_overseas': True}


# 模块加载时缓存（运行期不会变；如需热更新重启 app）
_LHB_SECTOR_MAP: dict[str, list[str]] = _load_lhb_sector_map()
_INTEL_CONFIG: dict = _load_intel_config()
_ANALYSIS_CACHE_FILE = _BASE_CACHE / 'ai_sector_analysis.json'
_DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
_MODEL = 'deepseek-v4-flash'
_REASONER_MODEL = 'deepseek-flash'
_REQUEST_TIMEOUT = 60
_ANALYSIS_TIMEOUT = 120  # V4.1 Flash 深度分析通常 15-60 秒

_HEADERS_BROWSER = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
}

_SYSTEM_PROMPT = """\
你是一位服务于 A 股散户的专业市场分析师。你的任务是：对每条输入的财经新闻（中文或英文），\
判断其是否对 A 股有实质影响，并输出结构化 JSON。

═══════════════════════════════════════════════════════
一、核心判断原则
═══════════════════════════════════════════════════════
1. 你服务的对象是 A 股散户，所有分析必须落地到「对 A 股哪些板块有什么影响」。
2. 只有符合下方第四节「必须 skip」①-⑨ 条目的新闻才返回 skip；其余一律输出结构化 JSON。
3. 对 A 股传导路径不确定时，输出 level=info、direction=neutral，related_sectors 填最可能受影响的板块或留空。
4. 英文标题请正常分析，翻译成中文标题输出，重点判断对 A 股的传导逻辑。

═══════════════════════════════════════════════════════
二、字段枚举与规则
═══════════════════════════════════════════════════════
category（事件分类）:
  ai | semiconductor | new_energy | robotics | medical | consumer | macro | overseas | gold | real_estate | policy | military | finance
  - ai: AI大模型/应用/算力/数据中心
  - semiconductor: 芯片/EDA/封测/存储/晶圆
  - new_energy: 光伏/储能/锂电/风电/氢能/新能源汽车
  - robotics: 人形机器人/减速器 + eVTOL/低空经济/无人机
  - medical: 创新药/CXO/医疗器械/减肥药
  - consumer: 白酒/食品/家电/消费电子/果链
  - overseas: 仅用于对 A 股有明确传导的国际事件（如中美关税、美联储加息）
  - macro: 国内宏观政策、经济数据
  - finance: 券商/银行/保险/资本市场改革

level（影响等级）:
  critical — 可能引发板块 ≥3% 波动或全市场共振的事件
    例：美联储意外加息50bp、中美断供升级、央行降准降息
  important — 单一板块 1-3% 波动
    例：某龙头公司重大订单、行业政策出台
  info — 值得关注但短期影响有限
    例：行业数据公布、分析师观点

direction（方向判断）:
  bullish — 有明确利好逻辑（资金会流入相关板块）
  bearish — 有明确利空逻辑（资金会流出相关板块）
  neutral — 影响不确定或多空交织
  ★ 注意：direction 必须有逻辑支撑，不要因为不确定就全标 neutral

related_sectors（关联板块，严格从以下白名单选取，最多 4 个）:
  半导体 芯片 AI算力 算力 光模块 服务器 数据中心
  新能源 光伏 储能 锂电 新能源汽车
  银行 券商 金融 保险 地产 建材
  黄金 有色金属 军工 消费电子 医药 机器人 低空经济

═══════════════════════════════════════════════════════
三、板块映射规范（极重要）
═══════════════════════════════════════════════════════
子概念必须归并到白名单父类：
  EDA/光刻胶/封测/存储/HBM/DRAM/NAND → 半导体
  GPU/TPU/AI芯片/CUDA → AI算力
  CPO/硅光/光通信 → 光模块
  液冷/AI服务器/DPU → 服务器
  IDC/云计算 → 数据中心
  人形机器人/减速器/丝杠/RV减速机/伺服 → 机器人
  eVTOL/飞行汽车/无人机物流 → 低空经济
  创新药/CXO/中药/医疗器械 → 医药
  果链/TWS/VR/AR/MR/手机产业链 → 消费电子
  工业母机/数控机床 → 机器人（或留空）
  信创/国产替代/自主可控 → 半导体
  氢能/风电 → 新能源
  固态电池/钠电池 → 锂电

禁止放入 related_sectors 的词：
  宏观、成长股、价值股、资本市场、A股、大盘、指数、ETF、北向资金

═══════════════════════════════════════════════════════
四、必须 skip 的情况（严格执行）
═══════════════════════════════════════════════════════
返回 {"skip": true} 的场景：
  ① 娱乐/体育/社会/天气新闻
  ② 海外企业财报（除非该企业是 A 股产业链核心：如英伟达/台积电/ASML）
  ③ 小国央行常规操作（印尼/挪威/澳洲/韩国等常规利率决议）
  ④ 纯外汇非人民币（美元兑日元/欧元兑英镑等）
  ⑤ 海外股市日常涨跌幅 <2%（如"日经涨0.5%"、"ASX200跌0.3%"）
  ⑥ 海外企业并购/重组/配股（与 A 股无产业链关系）
  ⑦ A 股个股研报/评级（如"XX证券首予XX买入"）
  ⑧ A 股个股异动/涨跌停（如"ST百利涨停"、"XX股跌停"）
  ⑨ 笼统的鸡汤观点（如"分析师看好下半年行情"）
  ⑩ 完全无行业关联且非宏观/政策/金融类的纯模糊表述（例："专家称要理性投资"）
     注意：category=macro/policy/finance 的新闻即使 related_sectors 为空，也应正常输出

不应 skip 的反例（注意区分）：
  ✓ 英伟达财报超预期 → 不 skip（AI算力/半导体/服务器核心上游）
  ✓ 台积电扩产 → 不 skip（半导体核心代工）
  ✓ 美联储加息/降息 → 不 skip（macro，影响全市场）
  ✓ 中美芯片出口管制 → 不 skip（半导体/AI算力）
  ✓ 美股科技股暴跌 >3% → 不 skip（overseas，情绪传导）

═══════════════════════════════════════════════════════
五、interpretation 写作要求
═══════════════════════════════════════════════════════
必须包含以下三要素（50-100字）：
  1. 传导逻辑：这个事件通过什么路径影响 A 股？
  2. 受益/受损标的方向：哪些板块会被资金关注？
  3. 时间维度：短期情绪冲击还是中长期基本面变化？

好的示例：
  "美联储暂停加息利好全球风险资产，A股成长板块（AI算力、半导体）估值修复空间打开，\
短期北向资金有望回流，中期看科技股盈利预期改善。"

差的示例（禁止）：
  "对市场有一定影响，需关注后续发展。" ← 太空泛，无操作价值

═══════════════════════════════════════════════════════
六、输出格式
═══════════════════════════════════════════════════════
只返回纯 JSON，不要 markdown、不要解释、不要前缀后缀：
{
  "title": "中文简化标题（≤35字，英文新闻翻译为中文）",
  "category": "枚举值",
  "level": "枚举值",
  "direction": "枚举值",
  "summary": "一句话事件摘要，20-40字",
  "interpretation": "传导逻辑+板块方向+时间维度，50-100字",
  "trading_tip": "对散户的一句话操作提示，20-60字，明确告知关注/观望/介入/回避，禁止空泛",
  "related_sectors": ["板块1", "板块2"],
  "source": "来源机构简称"
}

或者：{"skip": true}\
"""

_TRIAGE_PROMPT = """\
你是 A 股情报初筛员。从编号新闻标题列表中，挑选出对 A 股散户可能有参考价值的条目。

排除规则（以下情况才排除，否则一律保留）：
① 娱乐/体育/社会/明星/天气新闻
② A 股个股研报/评级/涨停/跌停（如"XX证券首予买入"、"ST百利涨停"）
③ 非人民币纯外汇（美元兑日元/欧元兑英镑等）
④ 小国央行常规利率操作（印尼/挪威/澳大利亚/韩国等）
⑤ 海外股市日常小幅波动（涨跌幅 <2% 且无特别原因）
⑥ 笼统空洞观点（"专家呼吁理性投资"、"分析师建议关注"）

以下情况必须保留：
✓ 宏观数据（CPI/PPI/PMI/GDP/社融/M2）
✓ 货币政策/财政政策/央行操作
✓ 产业政策/行业监管动态
✓ 地缘政治/中美关系/贸易
✓ 海外科技巨头重要动态（英伟达/台积电/苹果/Meta等）
✓ 大宗商品（黄金/原油/铜/锂）异动

宁可多选，不要漏掉有价值的信息。

只返回纯 JSON（不要 markdown、不要解释）：
{"selected": [编号整数列表]}
"""


# ======================================================================
# 配置管理
# ======================================================================

def load_api_key() -> str:
    """读取 DeepSeek API Key（代理到 core.credentials）。"""
    from core.credentials import load_api_key as _cred_load
    return _cred_load()


def save_api_key(key: str) -> None:
    """保存 API Key（代理到 core.credentials）。"""
    from core.credentials import save_api_key as _cred_save
    _cred_save(key)


# ======================================================================
# 新闻抓取
# ======================================================================

def _cls_sign(params_str: str) -> str:
    """财联社 API 签名：SHA1(params) → MD5(sha1_hex)。"""
    sha1 = hashlib.sha1(params_str.encode('utf-8')).hexdigest()
    return hashlib.md5(sha1.encode('utf-8')).hexdigest()


def _fetch_cls_telegraph(limit: int) -> list[dict]:
    """财联社电报 — A 股盘中信息密度最高的快讯源。"""
    last_time = int(time.time())
    app, os_val, sv = 'CailianpressWeb', 'web', '7.7.5'
    rn = min(limit, 100)
    params_str = f'app={app}&last_time={last_time}&os={os_val}&rn={rn}&sv={sv}'
    sign = _cls_sign(params_str)
    url = f'https://www.cls.cn/nodeapi/telegraphList?{params_str}&sign={sign}'
    r = requests.get(
        url,
        headers={
            **_HEADERS_BROWSER,
            'Referer': 'https://www.cls.cn/telegraph',
            'Host': 'www.cls.cn',
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json().get('data', {}).get('roll_data', [])
    results = []
    for item in data:
        content = item.get('content', '').strip()
        # 去掉 HTML 标签
        content = re.sub(r'<[^>]+>', '', content).strip()
        if len(content) < 10:
            continue
        results.append({'title': content[:120], 'source': '财联社'})
    return results


def _fetch_wallstreetcn(limit: int) -> list[dict]:
    """华尔街见闻 7×24 快讯 — A 股频道。"""
    r = requests.get(
        'https://api-prod.wallstreetcn.com/apiv1/content/lives',
        params={
            'channel': 'a-stock-channel',
            'limit': str(min(limit, 50)),
        },
        headers={**_HEADERS_BROWSER, 'Referer': 'https://wallstreetcn.com/'},
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get('data', {}).get('items', [])
    results = []
    for item in items:
        title = item.get('title', '') or item.get('content_text', '')
        title = re.sub(r'<[^>]+>', '', title).strip()
        if len(title) < 10:
            continue
        results.append({'title': title[:120], 'source': '华尔街见闻'})
    return results


def _fetch_eastmoney_kuaixun(limit: int) -> list[dict]:
    """东方财富 A 股快讯（备用）。"""
    url = 'https://newsapi.eastmoney.com/kuaixun/v1/getlist_v3.ashx'
    r = requests.get(
        url,
        params={'PageIndex': '1', 'PageSize': str(limit), 'Category': 'A股'},
        headers={**_HEADERS_BROWSER, 'Referer': 'https://finance.eastmoney.com/'},
        timeout=8,
    )
    r.raise_for_status()
    items = r.json().get('LiveList') or []
    results = []
    for item in items:
        raw = item.get('Content', '')
        # 去掉 【板块名】 前缀，保留核心文字
        clean = re.sub(r'^【[^】]{1,20}】\s*', '', raw).strip()
        if len(clean) < 10:
            continue
        results.append({
            'title': clean[:120],
            'source': item.get('MediaName', '东方财富'),
        })
    return results


def _fetch_sina_zhibo(limit: int) -> list[dict]:
    """新浪财经直播快讯（兜底）。"""
    r = requests.get(
        'https://zhibo.sina.com.cn/api/zhibo/feed',
        params={'zhibo_id': '152', 'count': str(limit), 'type': '1'},
        headers={**_HEADERS_BROWSER, 'Referer': 'https://finance.sina.com.cn/'},
        timeout=8,
    )
    r.raise_for_status()
    raw_list = (
        r.json()
        .get('result', {})
        .get('data', {})
        .get('feed', {})
        .get('list', [])
    )
    results = []
    for item in raw_list:
        text = re.sub(r'<[^>]+>', '', item.get('rich_text', '')).strip()
        if len(text) < 10:
            continue
        results.append({'title': text[:120], 'source': '新浪财经'})
    return results


def _overseas_enabled() -> bool:
    """海外源是否启用：环境变量 INTEL_ENABLE_OVERSEAS=0 → 禁用；
    配置文件 enable_overseas=false → 禁用；
    都未设但 HTTPS_PROXY 也未配置 → 禁用（避免 10s 空等）。"""
    env = os.environ.get('INTEL_ENABLE_OVERSEAS', '').strip()
    if env in ('0', 'false', 'False'):
        return False
    if _INTEL_CONFIG.get('enable_overseas') is False:
        return False
    if env in ('1', 'true', 'True'):
        return True
    # 默认行为：仅当检测到代理时启用
    return bool(
        os.environ.get('HTTPS_PROXY')
        or os.environ.get('https_proxy')
        or _INTEL_CONFIG.get('https_proxy')
    )


def _fetch_reuters_rss(limit: int) -> list[dict]:
    """Reuters 商业新闻 RSS — 全球宏观源头（需代理）。"""
    if not _overseas_enabled():
        logger.debug('Reuters 跳过：未检测到代理且未显式启用')
        return []
    import feedparser
    feeds = [
        'https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best',
        'https://www.reutersagency.com/feed/?best-topics=economy&post_type=best',
    ]
    results = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit // 2]:
                title = entry.get('title', '').strip()
                if len(title) < 10:
                    continue
                results.append({'title': title[:120], 'source': 'Reuters'})
        except Exception:
            continue
    return results[:limit]


def _fetch_cnbc_rss(limit: int) -> list[dict]:
    """CNBC 商业/市场新闻 RSS（需代理）。"""
    if not _overseas_enabled():
        logger.debug('CNBC 跳过：未检测到代理且未显式启用')
        return []
    import feedparser
    feeds = [
        'https://www.cnbc.com/id/10001147/device/rss/rss.html',  # Business
        'https://www.cnbc.com/id/20910258/device/rss/rss.html',  # Markets
    ]
    results = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit // 2]:
                title = entry.get('title', '').strip()
                if len(title) < 10:
                    continue
                results.append({'title': title[:120], 'source': 'CNBC'})
        except Exception:
            continue
    return results[:limit]


def _rsshub_enabled() -> bool:
    env = os.environ.get('INTEL_ENABLE_RSSHUB', '').strip()
    if env in ('0', 'false', 'False'):
        return False
    if env in ('1', 'true', 'True'):
        return True
    return _INTEL_CONFIG.get('enable_rsshub', True) is not False


def _rsshub_base_url() -> str:
    return (
        os.environ.get('RSSHUB_BASE_URL', '').strip()
        or str(_INTEL_CONFIG.get('rsshub_base_url') or '').strip()
        or 'https://rsshub.app'
    ).rstrip('/')


def _fetch_rsshub_finance(limit: int = 60) -> list[dict]:
    if not _rsshub_enabled():
        logger.debug('RSSHub 跳过：已禁用')
        return []
    import feedparser
    base = _rsshub_base_url()
    routes = [
        ('/cls/telegraph', 'RSSHub/财联社'),
        ('/wallstreetcn/live/global/2', 'RSSHub/华尔街见闻'),
        ('/wallstreetcn/live/a-stock-channel/2', 'RSSHub/华尔街见闻'),
        ('/gelonghui/live', 'RSSHub/格隆汇'),
    ]

    def _fetch_route(route_info):
        route, source = route_info
        try:
            r = requests.get(
                base + route,
                headers=_HEADERS_BROWSER,
                timeout=6,
            )
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            items = []
            per_route = max(1, limit // len(routes))
            for entry in feed.entries[:per_route]:
                title = entry.get('title', '').strip()
                if len(title) < 10:
                    continue
                items.append({'title': title[:120], 'source': source})
            return items
        except Exception as e:
            logger.debug('RSSHub 路由失败 [%s]: %s', route, e)
            return []

    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for items in executor.map(_fetch_route, routes):
            results.extend(items)
    return results[:limit]


def _fetch_lhb_intel(limit: int = 30) -> list[dict]:
    """用 akshare 获取当日龙虎榜数据，**调用本地分类器直接生成 structured intel**。

    返回 list[dict]，每项含 structured 字段（title/category/level/direction/
    summary/interpretation/trading_tip/related_sectors/source）以及 _pre_classified
    标记，让 run_fetch 跳过 LLM 直接入库。

    akshare 失败时 fallback 到 HTTP 直连兜底。
    """
    from datetime import date as _date
    today = _date.today().strftime('%Y%m%d')

    rows_iter = None
    # 主源：akshare
    try:
        import akshare as ak
        df = ak.stock_lhb_detail_em(date=today)
        if df is not None and not df.empty:
            rows_iter = (dict(row) for _, row in df.iterrows())
        else:
            logger.debug('龙虎榜 akshare 返回空%s', _js_diag_suffix())
    except Exception as e:
        logger.debug('龙虎榜 akshare 失败: %s%s', e, _js_diag_suffix())

    # 兜底：HTTP 直连
    if rows_iter is None:
        try:
            from core.astock_http_provider import fetch_lhb_detail
            fallback = fetch_lhb_detail(today)
            if fallback:
                rows_iter = iter(fallback)
                logger.debug(f'龙虎榜 fallback HTTP: {len(fallback)} 条')
        except Exception as e:
            logger.debug(f'龙虎榜 HTTP 兜底失败: {e}')

    if rows_iter is None:
        return []

    results = []
    for row_d in rows_iter:
        code = str(row_d.get('代码', '') or '').strip()
        name = str(row_d.get('名称', '') or '').strip()
        if not code or not name:
            continue
        try:
            structured = classify_lhb_row(row_d, _LHB_SECTOR_MAP)
        except Exception as ce:
            logger.debug('classify_lhb_row 失败 [%s]: %s', code, ce)
            continue
        structured['_pre_classified'] = True
        results.append(structured)
        if len(results) >= limit:
            break
    return results


def _fetch_dzjy_intel(limit: int = 20) -> list[dict]:
    """用 akshare 获取当日大宗交易数据，**调用本地分类器直接生成 structured intel**。

    小额 + 低折溢条目由 should_skip_dzjy 过滤；其余经 classify_dzjy_row 入库。
    akshare 失败时 fallback 到 HTTP 直连兜底。
    """
    from datetime import date as _date
    today = _date.today().strftime('%Y%m%d')

    rows_iter = None
    # 主源：akshare
    try:
        import akshare as ak
        df = ak.stock_dzjy_hy(date=today)
        if df is not None and not df.empty:
            rows_iter = (dict(row) for _, row in df.iterrows())
        else:
            logger.debug('大宗交易 akshare 返回空%s', _js_diag_suffix())
    except Exception as e:
        logger.debug('大宗交易 akshare 失败: %s%s', e, _js_diag_suffix())

    # 兜底：HTTP 直连
    if rows_iter is None:
        try:
            from core.astock_http_provider import fetch_dzjy_detail
            fallback = fetch_dzjy_detail(today)
            if fallback:
                rows_iter = iter(fallback)
                logger.debug(f'大宗交易 fallback HTTP: {len(fallback)} 条')
        except Exception as e:
            logger.debug(f'大宗交易 HTTP 兜底失败: {e}')

    if rows_iter is None:
        return []

    results = []
    for row_d in rows_iter:
        code = str(row_d.get('证券代码', '') or '').strip()
        name = str(row_d.get('证券简称', '') or '').strip()
        if not code or not name:
            continue
        if should_skip_dzjy(row_d):
            continue
        try:
            structured = classify_dzjy_row(row_d, _LHB_SECTOR_MAP)
        except Exception as ce:
            logger.debug('classify_dzjy_row 失败 [%s]: %s', code, ce)
            continue
        structured['_pre_classified'] = True
        results.append(structured)
        if len(results) >= limit:
            break
    return results


# 信任源：不经过白名单过滤，直接进入 LLM 分类
_TRUSTED_SIGNAL_SOURCES = {'龙虎榜', '大宗交易'}

# 高质量新闻源：只走黑名单过滤，不要求白名单关键词
_TRUSTED_NEWS_SOURCES = {'财联社', '华尔街见闻', '东方财富', '新浪财经'}


def fetch_headlines(limit: int = 50, stats: dict | None = None) -> list[dict]:
    """全源并行聚合抓取，返回去重后的 [{'title': str, 'source': str}]。

    8 个源全部并行拉取，各自失败不影响其他源；结果合并去重后返回。
    """
    all_sources = [
        (_fetch_cls_telegraph, '财联社电报', limit),
        (_fetch_wallstreetcn, '华尔街见闻', limit),
        (_fetch_sina_zhibo, '新浪财经', limit),
        (_fetch_eastmoney_kuaixun, '东财快讯', limit),
        (_fetch_reuters_rss, 'Reuters RSS', 30),
        (_fetch_cnbc_rss, 'CNBC RSS', 30),
        (_fetch_rsshub_finance, 'RSSHub 财经', 60),
        (_fetch_lhb_intel, '龙虎榜', 30),
        (_fetch_dzjy_intel, '大宗交易', 20),
    ]

    def _call_source(args):
        fetcher, name, n = args
        try:
            items = fetcher(n)
            if items:
                logger.info(f'新闻源 [{name}] 获取 {len(items)} 条')
            return items or []
        except Exception as e:
            logger.debug(f'新闻源 [{name}] 失败: {e}')
            return []

    # 并行拉取所有源（超时各自独立）
    all_items: list[list[dict]] = []
    with ThreadPoolExecutor(max_workers=9) as executor:
        all_items = list(executor.map(_call_source, all_sources))
    if stats is not None:
        stats['raw_total'] = sum(len(items) for items in all_items)

    # 合并去重（按标题前 20 字）
    merged: list[dict] = []
    seen: set = set()
    for items in all_items:
        for item in items:
            key = item['title'][:20]
            if key not in seen:
                seen.add(key)
                merged.append(item)
    if stats is not None:
        stats['merged_total'] = len(merged)

    return merged


# ======================================================================
# 去重工具
# ======================================================================

def _is_duplicate(new_title: str, existing_titles: list[str], threshold: float = 0.7) -> bool:
    """检测新标题是否与已有标题重复（相似度 > threshold 视为重复）。"""
    new_short = new_title[:30]
    for t in existing_titles:
        if difflib.SequenceMatcher(None, new_short, t[:30]).ratio() > threshold:
            return True
    return False


_SOURCE_SCORE = {
    '财联社': 30,
    '华尔街见闻': 28,
    'RSSHub/财联社': 26,
    'RSSHub/华尔街见闻': 24,
    '东方财富': 18,
    '东财快讯': 18,
    'RSSHub/格隆汇': 16,
    '新浪财经': 12,
    'Reuters': 10,
    'CNBC': 10,
}

_HIGH_VALUE_KEYWORDS = {
    '央行': 18, '降准': 18, '降息': 18, '财政部': 16, '证监会': 16, '发改委': 16,
    '半导体': 14, '芯片': 14, 'AI算力': 14, '算力': 12, '光模块': 12,
    '服务器': 10, '军工': 12, '机器人': 12, '低空经济': 10,
    '黄金': 12, '有色': 10, '原油': 10, '人民币': 10, '汇率': 10,
    '关税': 14, '制裁': 14, '英伟达': 14, '台积电': 14, 'ASML': 14,
}

_LOW_VALUE_REGEX = re.compile(
    r'(目标价|金股|概念股.*(一览|盘点)|分析师.*看好|教你|抄底|首予|评级)'
)


def _score_headline(item: dict) -> int:
    title = str(item.get('title', ''))
    source = str(item.get('source', ''))
    score = _SOURCE_SCORE.get(source, 10)
    for kw, value in _HIGH_VALUE_KEYWORDS.items():
        if kw in title:
            score += value
    if _LOW_VALUE_REGEX.search(title):
        score -= 40
    if '涨停' in title or '跌停' in title:
        score -= 20
    return score


def _normalize_title_for_cluster(title: str) -> str:
    t = re.sub(r'<[^>]+>', '', str(title))
    t = re.sub(r'^【[^】]{1,20}】\s*', '', t)
    t = re.sub(r'\s+', '', t)
    return re.sub(r'[，。！？、；：“”"\'（）()\[\]【】—\-_:：]', '', t)


def _dedupe_candidates_by_cluster(items: list[dict], threshold: float = 0.86) -> tuple[list[dict], int]:
    kept: list[dict] = []
    dropped = 0
    for item in sorted(items, key=_score_headline, reverse=True):
        title = _normalize_title_for_cluster(item.get('title', ''))[:40]
        duplicate = False
        for old in kept:
            old_title = _normalize_title_for_cluster(old.get('title', ''))[:40]
            if difflib.SequenceMatcher(None, title, old_title).ratio() >= threshold:
                duplicate = True
                break
        if duplicate:
            dropped += 1
        else:
            kept.append(item)
    return kept, dropped


# ======================================================================
# 规则粗筛
# ======================================================================

_WHITELIST_KEYWORDS = [
    # 宏观政策与央行
    '美联储', '央行', '降息', '降准', 'MLF', 'LPR', '财政部', '发改委',
    '证监会', '国常会', 'PMI', 'CPI', 'PPI', '国债', '货币政策', '财政政策',
    '专项债', '地方债', '再贷款', '逆回购', '公开市场', '存款准备金',
    'GDP', '增速', '工业增加值', '固定资产投资', '社会消费品零售',
    # 海外核心企业与事件
    '英伟达', '台积电', 'ASML', '三星', 'SK海力士', '特斯拉', 'Meta',
    '谷歌', '微软', '苹果', '亚马逊', '中美', '贸易战', '关税', '制裁',
    '博通', '高通', '英特尔', 'AMD', 'ARM', 'OpenAI', '埃隆·马斯克',
    # 半导体 / AI / 算力
    '半导体', '芯片', '光刻', 'EDA', 'HBM', 'AI算力', 'CPO', '光模块',
    '算力', '服务器', '数据中心', 'IDC', '液冷', 'GPU', 'AI',
    '存储', 'DRAM', 'NAND', '封测', '先进封装', '晶圆', '产能',
    # 新能源全链
    '光伏', '储能', '锂电', '固态电池', '钠电池', '新能源汽车', '充电桩',
    '风电', '核电', '氢能', '碳中和', '双碳',
    # 机器人 / 低空 / 军工
    '机器人', 'eVTOL', '低空经济', '无人机', '飞行汽车', '减速器',
    '军工', '国防', '航天', '卫星', '雷达',
    # 医药 / 生物
    '创新药', 'CXO', '医疗器械', '生物科技', 'mRNA', '新冠', '减肥药',
    # 大宗商品与有色
    '黄金', '有色', '铜', '铝', '锂', '钴', '稀土', '原油', '天然气',
    # 金融与资本市场
    '银行', '券商', '保险', '基金', 'ETF', 'IPO', '科创板', '北交所',
    '并购', '重组', '定增', '回购', '分红', '解禁',
    # 市场情绪与资金
    '美股', '港股', '纳指', '道指', '标普', '暴跌', '暴涨', '熔断',
    '北向资金', '融资融券', '主力净流入', '资金', '外资',
    # 宏观数据与汇率
    '人民币', '汇率', '出口', '进口', '通胀', '通缩', '就业',
    # 产业链关键词
    '供应链', '国产替代', '自主可控', '信创', '消费电子', '果链',
    '财报', '季报', '营收', '净利润', '毛利率',
]

_BLACKLIST_REGEX = re.compile(
    r'(\bST\b|^\*ST|涨停|跌停|首予|评级|目标价|金股|龙虎榜|'
    r'分析师看好|看好.*下半年|招商策略|国君观点|某券商)'
)


def _load_whitelist_supplement() -> list[str]:
    """加载 data/whitelist_keywords_supplement.json，平铺去重返回 keyword list。"""
    try:
        if not _WHITELIST_SUPP_FILE.exists():
            return []
        data = json.loads(_WHITELIST_SUPP_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return []
        flat = []
        for v in data.values():
            if isinstance(v, list):
                flat.extend(str(w).strip() for w in v if w)
        return flat
    except Exception as e:
        logger.warning('whitelist_keywords_supplement 加载失败: %s', e)
        return []


def _load_blacklist_supplement() -> list[re.Pattern]:
    """加载 data/blacklist_regex_supplement.json，逐条 re.compile 返回 list。"""
    try:
        if not _BLACKLIST_SUPP_FILE.exists():
            return []
        data = json.loads(_BLACKLIST_SUPP_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return []
        compiled = []
        for p in data.get('patterns', []):
            try:
                compiled.append(re.compile(p['regex']))
            except Exception as ce:
                logger.warning('blacklist regex 编译失败 [%s]: %s', p.get('regex'), ce)
        return compiled
    except Exception as e:
        logger.warning('blacklist_regex_supplement 加载失败: %s', e)
        return []


# 模块加载时合并白/黑名单补充（去重）
_supp_kw = _load_whitelist_supplement()
if _supp_kw:
    _existing = set(_WHITELIST_KEYWORDS)
    _WHITELIST_KEYWORDS.extend(w for w in _supp_kw if w not in _existing)
    logger.info('白名单关键词补充加载 %d 个新词', len(_WHITELIST_KEYWORDS) - len(_existing))

_BLACKLIST_REGEX_SUPP: list[re.Pattern] = _load_blacklist_supplement()
if _BLACKLIST_REGEX_SUPP:
    logger.info('黑名单 regex 补充加载 %d 条', len(_BLACKLIST_REGEX_SUPP))


def _hit_blacklist(title: str) -> bool:
    """命中原 _BLACKLIST_REGEX 或任一补充 pattern。"""
    if _BLACKLIST_REGEX.search(title):
        return True
    for p in _BLACKLIST_REGEX_SUPP:
        if p.search(title):
            return True
    return False


def _prefilter_candidates(headlines: list) -> list:
    """规则粗筛：信任源直通；高质量新闻源只走黑名单；其余黑名单+白名单双重筛。"""
    result = []
    for h in headlines:
        source = h.get('source', '')
        # 龙虎榜/大宗交易：结构化数据，直接放行
        if source in _TRUSTED_SIGNAL_SOURCES:
            result.append(h)
            continue
        title = h.get('title', '')
        if _hit_blacklist(title):
            continue
        # 高质量新闻源：黑名单通过即入选，不要求白名单关键词
        if source in _TRUSTED_NEWS_SOURCES:
            result.append(h)
            continue
        if any(kw in title for kw in _WHITELIST_KEYWORDS):
            result.append(h)
    return result


# ======================================================================
# DeepSeek 分类
# ======================================================================

def classify_headline(title: str, source: str, api_key: str) -> dict | None:
    # DEPRECATED: 改用 classify_headlines_batch
    """调用 DeepSeek chat API，返回结构化 dict 或 None（跳过/失败）。"""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': _MODEL,
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': f'新闻标题：{title}\n来源：{source}'},
        ],
        'temperature': 0.1,
        'max_tokens': 500,
    }
    try:
        r = requests.post(_DEEPSEEK_URL, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        content = r.json()['choices'][0]['message']['content'].strip()
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if not m:
            return None
        result = json.loads(m.group())
        if result.get('skip'):
            return None
        required = ('title', 'category', 'level', 'direction')
        if not all(k in result for k in required):
            return None
        result.setdefault('source', source)
        # ---- 枚举校验：防止 DeepSeek 返回白名单外的值 ----
        _VALID_CAT = {'ai', 'semiconductor', 'new_energy', 'robotics', 'medical',
                      'consumer', 'macro', 'overseas', 'gold',
                      'real_estate', 'policy', 'military', 'finance'}
        _VALID_LVL = {'critical', 'important', 'info'}
        _VALID_DIR = {'bullish', 'bearish', 'neutral'}
        if result.get('category') not in _VALID_CAT:
            result['category'] = 'macro'
        if result.get('level') not in _VALID_LVL:
            result['level'] = 'info'
        if result.get('direction') not in _VALID_DIR:
            result['direction'] = 'neutral'
        return result
    except Exception as e:
        logger.warning(f'DeepSeek 分类失败 [{title[:30]}…]: {e}')
        return None


def _triage_headlines(candidates: list[dict], api_key: str) -> list[dict]:
    """Phase-1 初筛：单次 LLM 调用，将候选标题一次性全部送入 _TRIAGE_PROMPT，
    宽松筛选后返回值得 Phase-2 深度分析的子集。
    失败（网络 / 解析错误）时 fallback 返回全量候选，不中断主流程。"""
    if not candidates:
        return []
    lines = [
        f'{i}. {h.get("title", "")}（{h.get("source", "?")}）'
        for i, h in enumerate(candidates)
    ]
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {
        'model': _MODEL,
        'messages': [
            {'role': 'system', 'content': _TRIAGE_PROMPT},
            {'role': 'user', 'content': '\n'.join(lines)},
        ],
        'temperature': 0.1,
        'max_tokens': 300,
    }
    try:
        r = requests.post(_DEEPSEEK_URL, headers=headers, json=payload,
                          timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        raw = r.json()['choices'][0]['message']['content'].strip()
        raw = re.sub(r'^```[^\n]*\n?|```$', '', raw, flags=re.MULTILINE).strip()
        indices = [
            int(x) for x in json.loads(raw).get('selected', [])
            if isinstance(x, (int, float)) and 0 <= int(x) < len(candidates)
        ]
        if not indices:
            logger.warning('_triage_headlines 返回空选集，fallback 全量')
            return candidates
        logger.info('_triage_headlines: %d → %d 条', len(candidates), len(indices))
        return [candidates[i] for i in indices]
    except Exception as e:
        logger.warning('_triage_headlines 失败，fallback 全量: %s', e)
        return candidates


def classify_headlines_batch(
    items: list[dict],
    api_key: str,
    batch_size: int = 20,
    progress_cb=None,
    start_progress: int = 0,
    total: int = 0,
    no_skip: bool = False,
) -> list[dict | None]:
    """Phase-2 深析：对 items 分 batch 调用 DeepSeek，返回与 items 等长的
    list，每项为 structured dict 或 None（解析失败时）。
    no_skip=True（Phase-2 默认模式）：每条必须输出结构化 JSON，禁止 skip；
    max_tokens 自动提升到 6000 防止批次输出被截断。"""
    _VALID_CAT = {'ai', 'semiconductor', 'new_energy', 'robotics', 'medical',
                  'consumer', 'macro', 'overseas', 'gold',
                  'real_estate', 'policy', 'military', 'finance'}
    _VALID_LVL = {'critical', 'important', 'info'}
    _VALID_DIR = {'bullish', 'bearish', 'neutral'}

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    # no_skip 模式：在 system prompt 前加强制指令，覆盖 skip 倾向
    sys_prompt = (
        '【强制指令】这些标题已经过初筛，每条均需输出完整结构化 JSON，禁止输出 skip。\n\n'
        + _SYSTEM_PROMPT
    ) if no_skip else _SYSTEM_PROMPT

    out: list[dict | None] = [None] * len(items)
    done = 0

    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        batch_input = [
            {'idx': i, 'title': item['title'], 'source': item.get('source', '')}
            for i, item in enumerate(batch)
        ]
        if no_skip:
            item_fmt = '{"idx": <整数>, ...单条字段...}（每条必须输出，禁止 skip）'
        else:
            item_fmt = '{"idx": <整数>, ...单条字段...} 或 {"idx": <整数>, "skip": true}'
        user_msg = (
            f'请对以下 {len(batch)} 条 headline 按 system 中的规则逐条分类，'
            '返回 JSON object，key 为 "results"，value 为数组，'
            '数组顺序必须与输入对齐，每项格式：\n'
            f'{item_fmt}\n\n'
            f'输入：\n{json.dumps(batch_input, ensure_ascii=False)}'
        )
        payload = {
            'model': _MODEL,
            'messages': [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': user_msg},
            ],
            'temperature': 0.1,
            'max_tokens': 6000 if no_skip else 4000,
        }
        try:
            r = requests.post(_DEEPSEEK_URL, headers=headers, json=payload,
                              timeout=_REQUEST_TIMEOUT)
            r.raise_for_status()
            content = r.json()['choices'][0]['message']['content']
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                m = re.search(r'\{.*\}', content, re.DOTALL)
                if not m:
                    raise ValueError('no JSON in response')
                data = json.loads(m.group())
            batch_results = data.get('results', [])
            for item_result in batch_results:
                local_idx = item_result.get('idx')
                if local_idx is None or not (0 <= local_idx < len(batch)):
                    continue
                global_idx = batch_start + local_idx
                if item_result.get('skip'):
                    out[global_idx] = None
                    continue
                required = ('title', 'category', 'level', 'direction')
                if not all(k in item_result for k in required):
                    continue
                item_result.setdefault('source', batch[local_idx].get('source', ''))
                if item_result.get('category') not in _VALID_CAT:
                    item_result['category'] = 'macro'
                if item_result.get('level') not in _VALID_LVL:
                    item_result['level'] = 'info'
                if item_result.get('direction') not in _VALID_DIR:
                    item_result['direction'] = 'neutral'
                out[global_idx] = item_result
        except Exception as e:
            logger.warning(f'DeepSeek batch 分类失败 [batch {batch_start}…]: {e}')

        done += len(batch)
        if progress_cb:
            progress_cb(start_progress + done, total,
                        f'已分析 {start_progress + done}/{total} 条…')

    return out


# ======================================================================
# 主流程
# ======================================================================


def run_fetch(
    api_key: str,
    limit: int = 100,
    llm_limit: int = 80,
    progress_cb=None,
) -> tuple[int, str, str]:
    """
    完整抓取 → 三级粗筛 → 两阶段 LLM 处理 → 质量门槛 → 去重入库。

    流程概述：
      1（锁内）snapshot 读，取历史标题快照
      2（锁外）多源并行抓取 → 历史去重 → 三级粗筛 → 聚类去重
      3（锁外）分流：结构化直通 ‖ Phase-1 triage → Phase-2 classify(no_skip=True)
      4（锁内）重读+最终去重+入库+写盘

    Parameters
    ----------
    api_key     : DeepSeek API Key
    limit       : 多源并行抓取上限（默认 100）
    llm_limit   : Phase-1 triage 输入上限，内部按本地评分排序后截取（默认 80）
    progress_cb : 可选回调 progress_cb(current, total, msg)，供 UI 进度更新

    Returns
    -------
    (added: int, error: str, summary: str)
      error   为空表示成功；非空为错误信息
      summary 格式："抓N→粗筛N→初筛N→返回N→质控N→去重N→新增N"
    """
    if not api_key:
        return 0, 'API Key 未配置，请先在设置中输入 DeepSeek API Key', ''
    stats: dict[str, int] = {}

    # ── 阶段 1（锁内）：snapshot 读 ─────────────────────────────────
    with _FEED_LOCK:
        snapshot: list[dict] = []
        if _FEED_FILE.exists():
            try:
                snapshot = json.loads(_FEED_FILE.read_text(encoding='utf-8'))
            except Exception as e:
                return 0, f'读取 intel_feed.json 失败: {e}', ''
    existing_titles_snap = [e.get('title', '') for e in snapshot]

    # ── 网络阶段（锁外）：抓取 → 粗筛 → 分流 → LLM ──────────────────
    if progress_cb:
        progress_cb(0, 0, '正在获取财经快讯（多源聚合）…')
    headlines = fetch_headlines(limit, stats=stats)
    if not headlines:
        return 0, '无法访问新闻源，请检查网络', ''

    # 与历史 feed 去重（snapshot 即可，最终写入前还会重检）
    candidates = []
    for h in headlines:
        if _is_duplicate(h['title'], existing_titles_snap):
            continue
        candidates.append(h)
    stats['history_deduped_total'] = len(candidates)
    if not candidates:
        return 0, '所有抓取到的新闻均已存在于情报库，无新增内容', ''

    # I1：粗筛后再 limit（粗筛前截断会把白名单命中的有用条目丢掉）
    filtered = _prefilter_candidates(candidates)
    stats['prefilter_kept'] = len(filtered)
    if not filtered:
        return 0, '粗筛后无符合条件的候选新闻', ''
    filtered, cluster_dropped = _dedupe_candidates_by_cluster(filtered)
    stats['cluster_dropped'] = cluster_dropped

    # I3：分流 — 结构化源（龙虎榜/大宗交易）已带 structured，不走 LLM
    pre_classified = [h for h in filtered if h.get('_pre_classified')]
    need_llm_all = [h for h in filtered if not h.get('_pre_classified')]
    stats['pre_classified'] = len(pre_classified)

    # Phase-1：初筛（单次 LLM，全量送入，返回值得深度分析的子集）
    triage_input = sorted(need_llm_all, key=_score_headline, reverse=True)[:llm_limit]
    if progress_cb:
        progress_cb(0, 0, f'情报初筛中（{len(triage_input)} 条候选）…')
    need_llm = _triage_headlines(triage_input, api_key)
    stats['triage_input'] = len(triage_input)
    stats['llm_selected'] = len(need_llm)

    total = len(need_llm)
    if progress_cb:
        progress_cb(
            0, total,
            f'粗筛{stats["prefilter_kept"]}→初筛{len(triage_input)}→选中{total}，深度分析中…'
        )

    # Phase-2：深度结构化分析（只处理初筛选出的子集，禁止 skip）
    structured_list: list[dict | None] = []
    if need_llm:
        structured_list = classify_headlines_batch(
            need_llm, api_key, batch_size=20,
            progress_cb=progress_cb, start_progress=0, total=total,
            no_skip=True,
        )

    # 合并 pre_classified + LLM 结果为统一 (structured, title) 列表
    final_items: list[tuple[dict, str]] = []
    for h in pre_classified:
        final_items.append((dict(h), h.get('title', '')))
    llm_returned = 0
    quality_dropped = 0
    for idx, structured in enumerate(structured_list):
        if not structured:
            continue
        llm_returned += 1
        # 本地质量门槛：neutral + info + 无板块 → 不入库
        sectors = structured.get('related_sectors') or []
        if (not sectors
                and structured.get('direction') == 'neutral'
                and structured.get('level') == 'info'):
            quality_dropped += 1
            continue
        new_title = structured.get('title') or need_llm[idx].get('title', '')
        structured['source'] = structured.get('source') or need_llm[idx].get('source', '')
        final_items.append((structured, new_title))
    stats['llm_valid'] = len(final_items) - len(pre_classified)
    stats['llm_returned'] = llm_returned
    stats['quality_dropped'] = quality_dropped

    if not final_items:
        _sm = f'抓{stats.get("raw_total",0)}→粗筛{stats.get("prefilter_kept",0)}→LLM{stats.get("llm_selected",0)}→返回{llm_returned}→质控{quality_dropped}→新增0'
        if progress_cb:
            progress_cb(total, total, _sm)
        return 0, '', _sm

    # ── 阶段 2（锁内）：重读 → 去重 → 入库 → 写 ─────────────────────
    with _FEED_LOCK:
        existing: list[dict] = []
        if _FEED_FILE.exists():
            try:
                existing = json.loads(_FEED_FILE.read_text(encoding='utf-8'))
            except Exception as e:
                return 0, f'重读 intel_feed.json 失败: {e}', ''

        existing_titles = [e.get('title', '') for e in existing]
        today = date.today().strftime('%Y-%m-%d')
        today_key = today.replace('-', '')
        today_seq_nums = [
            int(e['id'].split('_')[1])
            for e in existing
            if e.get('id', '').startswith(today_key + '_')
        ]
        next_seq = (max(today_seq_nums) + 1) if today_seq_nums else 1

        added = 0
        final_duplicate = 0
        for structured, new_title in final_items:
            if _is_duplicate(new_title, existing_titles, threshold=0.7):
                final_duplicate += 1
                continue
            entry = {
                'id': f'{today_key}_{next_seq:03d}',
                'title': new_title,
                'category': structured.get('category', 'macro'),
                'level': structured.get('level', 'info'),
                'direction': structured.get('direction', 'neutral'),
                'summary': structured.get('summary', ''),
                'interpretation': structured.get('interpretation', ''),
                'trading_tip': structured.get('trading_tip', ''),
                'related_sectors': list(structured.get('related_sectors') or []),
                'timestamp': today,
                'source': structured.get('source', ''),
            }
            existing.append(entry)
            existing_titles.append(new_title)
            next_seq += 1
            added += 1

        if added:
            try:
                cutoff = (date.today() - timedelta(days=30)).strftime('%Y-%m-%d')
                existing = [e for e in existing if e.get('timestamp', '9999') >= cutoff]
                _FEED_FILE.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2),
                    encoding='utf-8',
                )
            except Exception as e:
                return added, f'写入文件失败: {e}', ''

    summary = (
        f'抓{stats.get("raw_total",0)}→粗筛{stats.get("prefilter_kept",0)}→'
        f'初筛{stats.get("llm_selected",0)}→返回{stats.get("llm_returned",0)}→'
        f'质控{stats.get("quality_dropped",0)}→去重{final_duplicate}→新增{added}'
    )
    if progress_cb:
        progress_cb(total, total, summary)
    return added, '', summary


# ======================================================================
# AI 板块深度分析
# ======================================================================

def _load_analysis_cache() -> dict:
    """读取 ai_sector_analysis.json，返回 dict 或 {}。"""
    try:
        if _ANALYSIS_CACHE_FILE.exists():
            return json.loads(_ANALYSIS_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_analysis_cache(data: dict) -> None:
    try:
        _ANALYSIS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ANALYSIS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception:
        pass


_ANALYSIS_SYSTEM_PROMPT = """\
你是一位服务于成熟个人投资者的顶级卖方策略分析师，曾任职于头部券商研究所产业组，深耕A股市场10年。
你精通资金行为分析、政策传导逻辑、板块轮动判断和量价结构解读。
你的分析以【逻辑严密】【视角独到】【操作性强】著称，语言克制精准，不说废话。
你的读者是具备一定经验的A股投资者，期待"比普通分析师深一层"的视角。

输出必须按照以下六个模块结构化，每个模块标题用【】标记：

【一、资金行为深度解读】
• 净流入规模评估：主动买入还是被动承接？指标股修复还是调仓应对？
• 资金进入时间结构：早盘抢筹/尾盘建仓/全天均衡 — 各自对市场的含义差异
• 与大盘情绪相关性：是 alpha 主动配置还是 beta 跟随潮水？

【二、消息面与资金面博弈评估】
• 所有相关情报的实质影响力评分（政策落地/订单确认/技术突破 vs 传闻/预期/粉饰）
• 共振信号：情报利好 + 资金净流入 = 强共振；资金流入但情报偏弱 = 警惕
• 已发酵的预期 vs 尚未定价的预期：市场当前处于哪个阶段？

【三、产业链与景气度判断】
• 上游材料/中游制造/下游应用的资金轮动方向
• 当前产业链处于上行周期/谷底/震荡哪个阶段？
• 催化剂持续性：政策落地/订单放量/技术突破能带来多久的确定性？

【四、短线操作策略】
• 明确建议：加仓/维持/减仓/空仓，并给出以下条件的触发信号：
  — 进场时机：什么情况下介入最佳？
  — 持仓期间：拿仓期间的核心价格驱动因素
  — 退出信号：止损条件或背离预期发生时如何应对
• 时间周期：短线（1-3日）还是中线（1-3周）的做多机会？

【五、核心风险提示】
• 最大尾部风险（政策反转/外部冲击/板块高位/流动性收紧）
• 如果出现风险，应当关注哪些观察指标？

【六、综合评级】
• 短期热度：🔥🔥🔥🔥🔥（1-5颗）
• 资金方向：↑筑底反弹 / ↑趋势向上 / →震荡整理 / ↓产业预期消化 / ↓流动性承压
• 置信度：高/中/低 + 核心不确定因素一句话说明

总字数目标：700-900字。没有数据的字段用「暂无数据」代替，不要虚构。

**附加输出要求**

在自然语言分析末尾，追加一段严格合法的 JSON（用 ```json ``` 包裹），字段齐全、单一代码块：
```json
{
  "direction": "bullish",
  "target_pct": 5.0,
  "stop_pct": 2.5,
  "horizon_days": 5,
  "confidence": 7,
  "thesis": "≤30字核心论点"
}
```
direction 只能是 "bullish" / "bearish" / "neutral" 其中之一；target_pct 为正数（预期幅度%）；stop_pct 为正数（止损幅度%）；horizon_days 为整数交易日数；confidence 为 0-10 整数。\
"""


_ANALYSIS_INDEX_SYSTEM_PROMPT = """\
你是一位顶级宏观策略师，精通全球宏观研判、A股指数层面分析、板块轮动追踪和资金共振信号识别。
你的分析具备全局视野，不局限于单一板块，能从宏观、资金和情绪三个维度给出可操作建议。

输出必须按以下六模块构建分析报告：

【一、今日市场格局判断】
• 市场整体强弱：上涨家数 vs 下跌家数、涨停数 vs 跌停数、封板率
• 主要指数表现：是否出现强弱分化？大盘权重 vs 中小盘差异研判
• 情绪温度：偏冷/温和/小幅过热/高度亢奋

【二、资金共振与板块轮动】
• 今日资金主线：哪个板块成为增量资金的核心目的地？
• 轮动方向：防御 vs 成长、大盘 vs 中小盘、科技 vs 消费
• 北向资金：净流入还是净流出？对板块选择的指向性意义

【三、宏观传导逻辑】
• 对应今日情报中最重要的 1-3 条事件：其对A股的传导路径和时间周期
• 全球风险偏好：美股情绪、汇率变动、大宗商品价格对A股的当前影响向量
• 预期差异：市场现在定价的是「共识已充分入市」还是「预期差尚未定价」？

【四、局部热点 vs 全局资金流向】
• 单个板块资金热度超过全市平均时意味着什么？领涨逻辑是否可持续？
• 指数类 ETF 净申赎动向：平仓压力还是主动增配？

【五、未来 1-3 交易日展望】
• 板块轮动预判：当前主线资金还能延续吗？备选起动板块有哪些？
• 关键风险节点：让当前判断失效的重要事件和时间点
• 主指数后市：维稳震荡/小幅承压/技术反弹/趋势上攻

【六、市场综合评级】
• 今日市场热度：🔥🔥🔥🔥🔥（1-5）
• 主线方向：进攻/均衡/防守
• 操作建议：轻仓观望/顶仓配置主线/满仓追高，附触发条件

总字数目标：700-1000字。没有数据的字段用「暂无数据」代替，不要虚构。\
"""


def _build_analysis_prompt(
    sector_name: str,
    kind: str,
    intel_events: list,
    context: dict,
) -> str:
    """\u7ec4\u88c5\u677f\u5757/\u6307\u6570\u5206\u6790 user \u6d88\u606f\uff08\u7eaf\u6570\u636e\uff0c\u89d2\u8272\u5df2\u7531 system \u6d88\u606f\u5b9a\u4e49\uff09\u3002"""
    if kind in ('index', 'market'):
        return _build_index_analysis_prompt(sector_name, kind, intel_events, context)

    kind_label = '\u6982\u5ff5\u677f\u5757' if kind == 'concept' else '\u884c\u4e1a\u677f\u5757'
    today_str = date.today().strftime('%Y\u5e74%m\u6708%d\u65e5')
    _level_order = {'critical': 0, 'important': 1, 'info': 2}
    _dir_label = {'bullish': '\u5229\u591a\u2197', 'bearish': '\u5229\u7a7a\u2198', 'neutral': '\u4e2d\u6027\u2192'}
    _lvl_label = {'critical': '\u91cd\u5927', 'important': '\u91cd\u8981', 'info': '\u4e00\u822c'}

    parts = [f'\u5206\u6790\u5bf9\u8c61\uff1a{kind_label}\u300c{sector_name}\u300d\u00a0\u00a0\u5206\u6790\u65e5\u671f\uff1a{today_str}', '']

    # ── 宏观背景段（可选，macro_state 为空时跳过）──
    try:
        from core.macro_state import get_macro_context_text
        _macro_text = get_macro_context_text()
        if _macro_text:
            parts.append(_macro_text)
            parts.append('')
    except Exception:
        pass

    # \u2500\u2500 \u677f\u5757\u8d44\u91d1 \u2500\u2500
    ff = context.get('fund_flow')
    if ff:
        net = ff.get('net')
        pct = ff.get('pct')
        rank = ff.get('rank')
        net_str = f'{net:+.2f}\u4ebf' if net is not None else 'N/A'
        pct_str = f'{pct:+.2f}%' if pct is not None else 'N/A'
        rank_str = f'\u5168\u5e02\u8d44\u91d1\u6392\u540d\u7b2c{rank}\u540d' if rank is not None else 'N/A'
        parts.append(f'\u25b6 \u677f\u5757\u8d44\u91d1\uff1a\u51c0\u6d41\u5165 {net_str}\uff0c\u6da8\u8dcc\u5e45 {pct_str}\uff0c{rank_str}')
        # \u4e3b\u529b\u5927\u5355\u7ed3\u6784\uff08\u8d85\u5927/\u5927/\u4e2d/\u5c0f\uff09
        struct_parts = []
        if ff.get('super_large') is not None:
            struct_parts.append(f'\u8d85\u5927\u5355 {ff["super_large"]:+.1f}\u4ebf')
        if ff.get('large') is not None:
            struct_parts.append(f'\u5927\u5355 {ff["large"]:+.1f}\u4ebf')
        if ff.get('medium') is not None:
            struct_parts.append(f'\u4e2d\u5355 {ff["medium"]:+.1f}\u4ebf')
        if ff.get('small') is not None:
            struct_parts.append(f'\u5c0f\u5355 {ff["small"]:+.1f}\u4ebf')
        if struct_parts:
            extra = '  \u4e3b\u529b\u7ed3\u6784\uff1a' + ' / '.join(struct_parts)
            if ff.get('main_pct') is not None:
                extra += f'\uff08\u4e3b\u529b\u5360\u6bd4 {ff["main_pct"]:+.2f}%\uff09'
            parts.append(extra)
    else:
        parts.append('\u25b6 \u677f\u5757\u8d44\u91d1\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 \u76d8\u4e2d\u8d44\u91d1\u8d70\u52bf \u2500\u2500
    timeline = context.get('timeline', [])
    if timeline:
        tl_str = ' \u2192 '.join(f'{t}[{v:+.1f}\u4ebf]' for t, v in timeline)
        parts.append(f'\u25b6 \u76d8\u4e2d\u8d44\u91d1\u8d70\u52bf\uff1a{tl_str}')
    else:
        parts.append('\u25b6 \u76d8\u4e2d\u8d44\u91d1\u8d70\u52bf\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 \u5206\u949f\u6da8\u8dcc\uff08\u53ef\u9009\uff09 \u2500\u2500
    minute = context.get('minute')
    if minute:
        min_str = '  '.join(f'{t}[{v:+.1f}%]' for t, v in minute)
        parts.append(f'\u25b6 \u5206\u949f\u6da8\u8dcc\uff1a{min_str}')

    # \u2500\u2500 \u5927\u76d8\u60c5\u7eea \u2500\u2500
    emotion = context.get('emotion')
    if emotion:
        zt = emotion.get('zt', 'N/A')
        dt = emotion.get('dt', 'N/A')
        zdt = emotion.get('zdt') or emotion.get('\u5f00\u677f', 'N/A')
        amt = emotion.get('amount')
        amt_str = f"{amt / 1e12:.2f}\u4e07\u4ebf" if amt else 'N/A'
        zdt_part = f' / \u5f00\u677f {zdt}\u5bb6' if zdt != 'N/A' else ''
        parts.append(
            f'\u25b6 \u5927\u76d8\u60c5\u7eea\uff1a\u6da8\u505c {zt}\u5bb6 / \u8dcc\u505c {dt}\u5bb6'
            f'{zdt_part}'
            f'\uff0c\u4e24\u5e02\u6210\u4ea4\u989d {amt_str}'
        )
    else:
        parts.append('\u25b6 \u5927\u76d8\u60c5\u7eea\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 \u6210\u5206\u80a1 TOP5 \u2500\u2500
    constituents = context.get('constituents')
    if constituents:
        con_str = '\uff0c'.join(
            f"{c.get('name', '?')}({c.get('pct', 0):+.2f}%)"
            for c in constituents[:5]
        )
        parts.append(f'\u25b6 \u6838\u5fc3\u6210\u5206\u80a1 TOP5\uff1a{con_str}')
    else:
        parts.append('\u25b6 \u6838\u5fc3\u6210\u5206\u80a1\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 \u76f8\u5173\u60c5\u62a5\uff08\u6700\u591a12\u6761\uff0c\u6309 level \u964d\u5e8f\uff09 \u2500\u2500
    sorted_events = sorted(
        intel_events,
        key=lambda e: {'critical': 0, 'important': 1, 'info': 2}.get(e.level, 2)
    )[:12]
    parts.append('')
    if sorted_events:
        parts.append(f'\u76f8\u5173\u60c5\u62a5\uff08{len(sorted_events)}\u6761\uff09\uff1a')
        for ev in sorted_events:
            parts.append(
                f"  [{_lvl_label.get(ev.level, '?')}][{_dir_label.get(ev.direction, '?')}] {ev.title}"
            )
            if ev.interpretation:
                parts.append(f"    \u89e3\u8bfb\uff1a{ev.interpretation[:80]}")
    else:
        parts.append('\u76f8\u5173\u60c5\u62a5\uff1a\u6682\u65e0')

    parts.append('')
    parts.append('\u8bf7\u6309\u4e0a\u8ff0\u516d\u4e2a\u6a21\u5757\u8fdb\u884c\u6df1\u5ea6\u5206\u6790\uff1a')
    return '\n'.join(parts)


def _build_index_analysis_prompt(
    index_name: str,
    kind: str,
    intel_events: list,
    context: dict,
) -> str:
    """\u6307\u6570/\u4e3b\u7ebf\u5206\u6790\u7684 user \u6d88\u606f\u7ec4\u88c5\u51fd\u6570\u3002"""
    today_str = date.today().strftime('%Y\u5e74%m\u6708%d\u65e5')
    kind_label = '\u4e3b\u7ebf\u5206\u6790' if kind == 'market' else f'\u6307\u6570\u5206\u6790'
    _level_order = {'critical': 0, 'important': 1, 'info': 2}
    _dir_label = {'bullish': '\u5229\u591a\u2197', 'bearish': '\u5229\u7a7a\u2198', 'neutral': '\u4e2d\u6027\u2192'}
    _lvl_label = {'critical': '\u91cd\u5927', 'important': '\u91cd\u8981', 'info': '\u4e00\u822c'}

    parts = [f'\u5206\u6790\u5bf9\u8c61\uff1a{kind_label}\u300c{index_name}\u300d\u00a0\u00a0\u5206\u6790\u65e5\u671f\uff1a{today_str}', '']

    # ── 宏观背景段（可选，macro_state 为空时跳过）──
    try:
        from core.macro_state import get_macro_context_text
        _macro_text = get_macro_context_text()
        if _macro_text:
            parts.append(_macro_text)
            parts.append('')
    except Exception:
        pass

    # \u2500\u2500 \u5927\u76d8\u60c5\u7eea \u2500\u2500
    emotion = context.get('emotion')
    if emotion:
        zt = emotion.get('zt', 'N/A')
        dt = emotion.get('dt', 'N/A')
        zdt = emotion.get('zdt') or emotion.get('\u5f00\u677f', 'N/A')
        amt = emotion.get('amount')
        amt_str = f"{amt / 1e12:.2f}\u4e07\u4ebf" if amt else 'N/A'
        zdt_part = f' / \u5f00\u677f {zdt}\u5bb6' if zdt != 'N/A' else ''
        parts.append(
            f'\u25b6 \u5e02\u573612\u6708\u60c5\u7eea\uff1a\u6da8\u505c {zt}\u5bb6 / \u8dcc\u505c {dt}\u5bb6'
            f'{zdt_part}'
            f'\uff0c\u4e24\u5e02\u6210\u4ea4\u989d {amt_str}'
        )
    else:
        parts.append('\u25b6 \u5e02\u573612\u6708\u60c5\u7eea\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 TOP\u677f\u5757\u8d44\u91d1\u6982\u51b5 \u2500\u2500
    top_sectors = context.get('top_sectors', [])
    if top_sectors:
        parts.append('\u25b6 \u8d44\u91d1\u4e3b\u7ebf\u677f\u5757 TOP10\uff1a')
        for s in top_sectors[:10]:
            kind_tag = '\u6982' if s.get('kind') == 'concept' else '\u4e1a'
            net = s.get('net', 0)
            pct = s.get('pct', 0)
            parts.append(f"  [{kind_tag}] {s['name']}  {net:+.1f}\u4ebf  {pct:+.2f}%")
    else:
        parts.append('\u25b6 \u677f\u5757\u8d44\u91d1\uff1a\u6682\u65e0\u6570\u636e')

    # \u2500\u2500 \u5168\u90e8\u60c5\u62a5\uff08\u6309 level \u964d\u5e8f\uff0c\u6700\u591a15\u6761\uff09 \u2500\u2500
    sorted_events = sorted(
        intel_events,
        key=lambda e: {'critical': 0, 'important': 1, 'info': 2}.get(e.level, 2)
    )[:15]
    parts.append('')
    if sorted_events:
        parts.append(f'\u4eca\u65e5\u5168\u90e8\u60c5\u62a5\uff08{len(sorted_events)}\u6761\uff09\uff1a')
        cat_labels = {
            'macro': '\u5b8f\u89c2', 'overseas': '\u6d77\u5916', 'policy': '\u653f\u7b56',
            'ai': 'AI', 'semiconductor': '\u82af\u7247', 'gold': '\u9ec4\u91d1',
            'real_estate': '\u5730\u4ea7', 'military': '\u519b\u5de5', 'finance': '\u91d1\u878d',
        }
        for ev in sorted_events:
            cat = cat_labels.get(ev.category, ev.category)
            parts.append(
                f"  [{_lvl_label.get(ev.level, '?')}][{_dir_label.get(ev.direction, '?')}][{cat}] {ev.title}"
            )
            if ev.interpretation:
                parts.append(f"    \u89e3\u8bfb\uff1a{ev.interpretation[:80]}")
    else:
        parts.append('\u4eca\u65e5\u60c5\u62a5\uff1a\u6682\u65e0')

    parts.append('')
    parts.append('\u8bf7\u6309\u4e0a\u8ff0\u516d\u4e2a\u6a21\u5757\u8fdb\u884c\u6df1\u5ea6\u5206\u6790\uff1a')
    return '\n'.join(parts)


def analyze_sector(
    sector_name: str,
    kind: str,
    api_key: str,
    intel_events: list,
    context: dict,
) -> tuple[str, str, str, str, str]:
    """调用 deepseek-flash (V4.1 Flash) 进行深度分析。

    每次调用都会触发 API（不做内部缓存命中检查），结果以带秒级时间戳的
    cache_key 写入 ai_sector_analysis.json，同日同板块可多次分析并存。

    Args:
        sector_name: 板块/指数/主线名称
        kind: 'concept' | 'industry' | 'index' | 'market'
        api_key: DeepSeek API key
        intel_events: 相关 IntelEvent 对象列表
        context: {'fund_flow', 'timeline', 'emotion', 'minute', 'constituents', 'top_sectors'}
            fund_flow 可选扩展字段：main_pct / super_large / large / medium / small
            （主力大单结构，亿元单位；缺失时 prompt 跳过该行）

    Returns:
        (reasoning_text, result_text, error_msg, cache_key, now_time)
        error_msg 非空时表示失败，reasoning/result 为空字符串。
        cache_key 格式：'{sector_name}_{kind}_{HHMMSS}'
    """
    today_str = date.today().strftime('%Y-%m-%d')
    now_time = datetime.now().strftime('%H:%M:%S')
    cache_key = f'{sector_name}_{kind}_{now_time.replace(":", "")}'

    # 组装 prompt（system 角色按 kind 切换）
    sys_prompt = (
        _ANALYSIS_INDEX_SYSTEM_PROMPT
        if kind in ('index', 'market')
        else _ANALYSIS_SYSTEM_PROMPT
    )
    user_prompt = _build_analysis_prompt(sector_name, kind, intel_events, context)

    # 与个股 Agent 共用统一客户端：空 content / finish_reason=length 会自动重试，
    # 截断时会扩大一次 max_tokens，避免静默得到不完整分析。
    from core.agents.base import call_llm
    result, reasoning, usage = call_llm(
        sys_prompt,
        user_prompt,
        api_key,
        model=_REASONER_MODEL,
        max_tokens=8192,
        retries=1,
        timeout=_ANALYSIS_TIMEOUT,
    )
    if not result:
        finish_reason = (usage or {}).get('finish_reason') or 'unknown'
        reasoning_tokens = (
            ((usage or {}).get('completion_tokens_details') or {}).get('reasoning_tokens')
        )
        return (
            '', '',
            f'API 返回内容为空或被截断'
            f'（finish_reason={finish_reason}, reasoning_tokens={reasoning_tokens}）',
            '', '',
        )

    # 写缓存
    cache = _load_analysis_cache()
    cache[cache_key] = {
        'date': today_str,
        'sector_name': sector_name,
        'kind': kind,
        'reasoning': reasoning,
        'result': result,
        'time': now_time,
    }
    _save_analysis_cache(cache)

    # 归档到 analysis_memory（key = sector:{sector_name}，支持中文）
    try:
        from core.analysis_memory import archive_prediction
        _spec = None
        _m = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', result)
        if _m:
            try:
                _spec = json.loads(_m.group(1))
            except Exception:
                _spec = None
        _direction = 'neutral'
        if isinstance(_spec, dict):
            _direction = str(_spec.get('direction', 'neutral')).lower()
            if _direction not in ('bullish', 'bearish', 'neutral'):
                _direction = 'neutral'
        archive_prediction({
            'code': f'sector:{sector_name}',
            'name': sector_name,
            'direction': _direction,
            'confidence': (float(_spec.get('confidence', 5)) / 10.0) if isinstance(_spec, dict) else 0.5,
            'target_pct': _spec.get('target_pct') if isinstance(_spec, dict) else None,
            'stop_pct': _spec.get('stop_pct') if isinstance(_spec, dict) else None,
            'horizon_days': _spec.get('horizon_days') if isinstance(_spec, dict) else None,
            'reasoning': result[:500],
            'prediction_id': cache_key,
            'created_at': f'{date.today().strftime("%Y-%m-%d")} {now_time}',
        })
    except Exception:
        pass

    return reasoning, result, '', cache_key, now_time
