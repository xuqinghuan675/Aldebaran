"""情报匹配器 — 持仓股 → 相关情报筛选 + 股票 → 板块推断。

职责：
1. load_intel_feed()         — 从 data/intel_feed.json 加载当期有效情报
2. match_event_to_sector()   — 判断 event 与 sector_name 是否相关（从 intel_panel 迁移）
3. filter_relevant_intel()   — 三维 OR 匹配（名称关键词 + 代码 + 板块）
4. infer_sectors_for_stock() — 股票 → 板块推断（名称启发式 + 成分缓存扫描）
5. warm_sectors_async()      — 后台预热 sector 推断缓存

缓存: ~/.aldebaran/cache/market/stock_sectors.json  (TTL 30 天)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

from core.intel_models import IntelEvent

from core.paths import INTEL_FEED_FILE as _FEED_PATH, CACHE_DIR as _BASE_CACHE  # noqa: E402
_SECTORS_CACHE = _BASE_CACHE / 'market' / 'stock_sectors.json'
_SECTORS_TTL = 30 * 86400  # 30 天

_DISPLAY_DAYS = {'critical': 5, 'important': 4, 'info': 3}

# ── sector_panel 别名表（软依赖，失败时静默降级） ──────────────────────
try:
    from ui.sector_panel import _FOCUS_SECTOR_ALIASES as _SP_ALIASES
except Exception:
    _SP_ALIASES = {}
_ALIAS_FORWARD: dict = dict(_SP_ALIASES)
_ALIAS_REVERSE: dict = {v: k for k, v in _SP_ALIASES.items()}

_INTEL_TO_SECTOR_EXPAND: dict = {
    '有色金属': ['有色金属', '贵金属', '工业金属', '小金属'],
    '军工':     ['国防军工', '航天', '航空装备', '通用航空'],
    '机器人':   ['机器人概念', '减速器', '工业母机'],
    'AI算力':   ['算力', 'CPO概念', '光通信模块'],
    '数据中心': ['IDC概念', '算力', '云计算'],
}

# ── 名称关键词 → 可能所属板块列表 ─────────────────────────────────────
_NAME_HEURISTICS: dict[str, list[str]] = {
    '银行':   ['银行'],
    '保险':   ['保险'],
    '证券':   ['券商', '证券'],
    '券商':   ['券商'],
    '医药':   ['医药', '生物医药'],
    '生物':   ['生物医药', '医药'],
    '白酒':   ['白酒'],
    '新能源':  ['新能源', '新能源汽车'],
    '锂电':   ['锂电池', '新能源'],
    '光伏':   ['光伏'],
    '半导体':  ['半导体'],
    '通信':   ['通信', '光模块', '光通信模块'],
    '芯片':   ['半导体', '芯片'],
    '军工':   ['国防军工', '军工'],
    '地产':   ['地产'],
    '房地产':  ['地产'],
    '钢铁':   ['钢铁'],
    '有色':   ['有色金属'],
    '煤炭':   ['煤炭'],
    '电力':   ['电力'],
    '汽车':   ['汽车'],
    '机器人':  ['机器人'],
    '算力':   ['算力', 'AI算力'],
    '人工智能': ['AI算力', '算力'],
    '芯片':   ['半导体', '芯片'],
    '云计算':  ['云计算'],
    '量子':   ['量子计算'],
    '黄金':   ['黄金', '贵金属'],
    '稀土':   ['稀土', '有色金属'],
    '储能':   ['储能', '新能源'],
    '氢能':   ['氢能源'],
    '互联网':  ['互联网'],
    '游戏':   ['游戏'],
    '传媒':   ['传媒'],
    '航空':   ['航空'],
    '航运':   ['航运'],
    '物流':   ['物流'],
    '食品':   ['食品饮料'],
    '饮料':   ['食品饮料'],
    '农业':   ['农业'],
    '化工':   ['化工'],
    '建筑':   ['建筑'],
    '基建':   ['基建'],
    '水泥':   ['水泥', '建材'],
}

# ETF 主题词剥离规则（名称 → 核心词）
_ETF_SUFFIX_RE = re.compile(
    r'(ETF|LOF|基金|[A-Z])(嘉实|华夏|博时|易方达|广发|汇添富|南方|富国|工银|建信|招商|天弘|鹏华|国泰|大成|万家|银华)*$',
    re.IGNORECASE,
)
_STOCK_SUFFIXES = (
    '股份', '集团', '控股', '科技', '医药', '证券', '银行', '保险',
    '能源', '电力', '资源', '材料', '制造', '传媒', '投资', '实业',
    'A', 'B', 'H',
)


# ─────────────────────────── feed loader ──────────────────────────

def load_intel_feed() -> list[IntelEvent]:
    """读取 data/intel_feed.json，返回当期有效情报列表。"""
    if not _FEED_PATH.exists():
        return []
    try:
        raw = json.loads(_FEED_PATH.read_text(encoding='utf-8'))
    except Exception:
        return []

    today = date.today()
    events: list[IntelEvent] = []
    for d in raw:
        try:
            ev = IntelEvent.from_dict(d)
            if not ev.date:
                continue
            max_days = _DISPLAY_DAYS.get(ev.level, 3)
            if (today - ev.date).days <= max_days:
                events.append(ev)
        except Exception:
            continue
    events.sort(
        key=lambda e: (0 if e.date == today else 1, -(e.date.toordinal() if e.date else 0))
    )
    return events


# ─────────────────────────── sector matcher ───────────────────────

def match_event_to_sector(event: IntelEvent, sector_name: str) -> bool:
    """判断 event 是否与 sector_name 相关（四级匹配）。

    迁移自 ui/intel_panel._match_event_to_sector，供 core 层使用。
    """
    related = event.related_sectors or []
    if not related:
        return False
    if sector_name in related:
        return True
    for r in related:
        candidates = _INTEL_TO_SECTOR_EXPAND.get(r)
        if candidates and sector_name in candidates:
            return True
    for r in related:
        if _ALIAS_FORWARD.get(r) == sector_name:
            return True
        if _ALIAS_REVERSE.get(r) == sector_name:
            return True
        if _ALIAS_FORWARD.get(sector_name) == r:
            return True
        if _ALIAS_REVERSE.get(sector_name) == r:
            return True
    sn = str(sector_name)
    if sn.startswith('非') or len(sn) < 2:
        return False
    for r in related:
        rs = str(r)
        if rs.startswith('非') or len(rs) < 2:
            continue
        if rs in sn or sn in rs:
            return True
    return False


# ─────────────────────────── name keyword extraction ──────────────

def _stock_core_keywords(name: str) -> list[str]:
    """从股票/ETF 名称提取核心词列表（≥ 2 字）。

    e.g. '通信ETF嘉实' → ['通信']
         '招商银行'    → ['招商银行', '招商']
         '中国平安'    → ['中国平安', '平安']
    """
    n = str(name).strip()
    keywords: list[str] = []

    # ETF / LOF：剥离 ETF+管理人 后缀，取前缀主题词
    etf_match = _ETF_SUFFIX_RE.search(n)
    if etf_match:
        core = n[:etf_match.start()].strip()
        if len(core) >= 2:
            keywords.append(core)
        return keywords  # ETF 只返回主题词

    # 普通股票：保留完整名称 + 去掉末尾单字/常见后缀后的缩略版
    keywords.append(n)
    stripped = n
    for suf in _STOCK_SUFFIXES:
        if stripped.endswith(suf) and len(stripped) - len(suf) >= 2:
            stripped = stripped[:-len(suf)]
            break
    if stripped != n and len(stripped) >= 2:
        keywords.append(stripped)

    return list(dict.fromkeys(keywords))  # 去重保序


# ─────────────────────────── intel filter ─────────────────────────

def filter_relevant_intel(
    events: list[IntelEvent],
    code: str,
    name: str,
    sector_hints: list[str] | None = None,
    max_n: int = 5,
    today: date | None = None,
) -> list[IntelEvent]:
    """三维 OR 匹配筛选与持仓股相关的情报。

    1. 名称关键词命中 — core_keywords 出现在 title/summary/interpretation
    2. 代码命中 — code 或 sh/sz+code 出现在文本
    3. 板块命中 — sector_hints 中任意板块与 event 相关（match_event_to_sector）

    按相关性、新鲜度、level 排序，取前 max_n 条。
    """
    if not events:
        return []

    sectors = sector_hints or []

    matched: list[IntelEvent] = []
    for ev in events:
        if score_intel_event(ev, code, name, sectors, today=today) > 0:
            matched.append(ev)

    matched.sort(
        key=lambda e: score_intel_event(e, code, name, sectors, today=today),
        reverse=True,
    )
    return matched[:max_n]


def score_intel_event(
    event: IntelEvent,
    code: str,
    name: str,
    sector_hints: list[str] | None = None,
    *,
    today: date | None = None,
) -> float:
    """Score an event for one stock: relevance first, then freshness and level."""
    if not event:
        return 0.0
    today = today or date.today()
    sectors = sector_hints or []
    core_kws = [kw.lower() for kw in _stock_core_keywords(name) if len(kw) >= 2]
    code = str(code or '').lower()
    code_variants = {code, f'sh{code}', f'sz{code}'} if code else set()
    text_blob = (event.title + event.summary + event.interpretation).lower()

    relevance = 0.0
    if any(cv and cv in text_blob for cv in code_variants):
        relevance = max(relevance, 100.0)
    if any(kw in text_blob for kw in core_kws):
        relevance = max(relevance, 90.0)
    if any(match_event_to_sector(event, s) for s in sectors):
        relevance = max(relevance, 55.0)
    if relevance <= 0:
        return 0.0

    days_old = 30
    if event.date:
        days_old = max(0, (today - event.date).days)
    recency = max(0.0, 30.0 - min(days_old, 30) * 5.0)
    level_score = {'critical': 18.0, 'important': 10.0, 'info': 3.0}.get(event.level, 3.0)
    direction_score = 2.0 if event.direction in ('bullish', 'bearish') else 0.0
    return relevance + recency + level_score + direction_score


def sort_intel_events_for_stock(
    events: list[IntelEvent],
    code: str,
    name: str,
    sector_hints: list[str] | None = None,
    max_n: int = 5,
    *,
    today: date | None = None,
) -> list[IntelEvent]:
    """Return already-related events in stock-specific priority order."""
    if not events:
        return []
    sectors = sector_hints or []
    return sorted(
        events,
        key=lambda e: score_intel_event(e, code, name, sectors, today=today),
        reverse=True,
    )[:max_n]


# ─────────────────────────── sector inference ─────────────────────

_sectors_cache_mem: dict | None = None
_sectors_cache_mtime: float = 0.0


def _load_sectors_cache() -> dict:
    """读取股票板块缓存。进程内 dict 缓存 + mtime 校验，避免持仓刷新时重复 IO。"""
    global _sectors_cache_mem, _sectors_cache_mtime
    try:
        mtime = _SECTORS_CACHE.stat().st_mtime
    except OSError:
        return {} if _sectors_cache_mem is None else _sectors_cache_mem
    if _sectors_cache_mem is not None and mtime == _sectors_cache_mtime:
        return _sectors_cache_mem
    try:
        _sectors_cache_mem = json.loads(_SECTORS_CACHE.read_text(encoding='utf-8'))
        _sectors_cache_mtime = mtime
    except Exception:
        _sectors_cache_mem = {}
    return _sectors_cache_mem


def _save_sectors_cache(data: dict):
    global _sectors_cache_mem, _sectors_cache_mtime
    try:
        _SECTORS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _SECTORS_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        _sectors_cache_mem = data
        try:
            _sectors_cache_mtime = _SECTORS_CACHE.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass


def infer_sectors_for_stock(
    code: str,
    name: str,
    sector_context: dict | None = None,
) -> list[str]:
    """推断股票所属板块列表（用于情报筛选）。

    策略（依次尝试，OR 合并）：
    1. 持久缓存命中（30 天有效）→ 直接返回
    2. sector_context 匹配（name 关键词命中某板块名称）
    3. 名称启发式（_NAME_HEURISTICS 映射）
    4. 成分股缓存扫描（~/.aldebaran/cache/ 中 sector_constituents_*.json）
    结果去重后写缓存。
    """
    # 1. 持久缓存
    cache = _load_sectors_cache()
    entry = cache.get(code)
    if entry and isinstance(entry, dict):
        if time.time() - entry.get('ts', 0) < _SECTORS_TTL:
            cached = entry.get('sectors', [])
            # 过滤坏缓存：板块名 = 股票名，属于误写入，重新推断
            if cached and not all(s == name for s in cached):
                return cached

    sectors: list[str] = []
    core_kws = _stock_core_keywords(name)

    # 2. sector_context 匹配
    if sector_context:
        for kind in ('concept', 'industry'):
            for sector_name in sector_context.get(kind, {}):
                if sector_name == name:   # 跳过与股票同名的概念板块
                    continue
                for kw in core_kws:
                    if len(kw) >= 2 and (kw in sector_name or sector_name in kw):
                        sectors.append(sector_name)

    # 3. 名称启发式
    name_lower = name.lower()
    for kw, slist in _NAME_HEURISTICS.items():
        if kw in name:
            sectors.extend(slist)

    # 4. 成分股缓存扫描（opportunistic）
    try:
        cache_dir = _BASE_CACHE
        for f in cache_dir.iterdir():
            if not f.is_file() or not f.name.startswith('sector_constituents_'):
                continue
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                clist = data.get('constituents') or []
                if any(c.get('code') == code for c in clist):
                    # 板块名取文件级 sector 标识，禁止用成分记录里的 name（那是个股名）当板块，
                    # 否则会把股票自身名写成所属板块（坏缓存），污染情报/主线契合匹配
                    sector_name_hint = (
                        data.get('sector_name') or data.get('sector') or data.get('name')
                        or next((c.get('sector_name', '') for c in clist if c.get('code') == code), None)
                    )
                    if sector_name_hint and len(sector_name_hint) >= 2 and sector_name_hint != name:
                        sectors.append(sector_name_hint)
            except Exception:
                continue
    except Exception:
        pass

    # 去重
    seen: set[str] = set()
    unique: list[str] = []
    for s in sectors:
        if s and s not in seen:
            seen.add(s)
            unique.append(s)

    # 写缓存（只有推断出结果时才写）
    if unique:
        cache[code] = {'sectors': unique, 'ts': time.time()}
        _save_sectors_cache(cache)

    return unique


def warm_sectors_async(code: str, name: str, sector_context: dict | None = None):
    """在后台线程中预热 sector 推断缓存，不阻塞调用方。"""
    def _run():
        try:
            infer_sectors_for_stock(code, name, sector_context)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def sector_strength_text(sectors: list[str], sector_context: dict | None) -> str:
    """根据 sector_context 生成所属板块资金强度文本（用于 fund_context 注入）。

    格式: 所属板块资金 | 银行: 净额+8.5亿 涨幅+1.2% | 金融: 净额+3.2亿 涨幅+0.8%
    """
    if not sectors or not sector_context:
        return ''
    parts: list[str] = []
    for kind in ('concept', 'industry'):
        ctx = sector_context.get(kind, {})
        for s in sectors:
            info = ctx.get(s)
            if info:
                net = info.get('net', 0)
                pct = info.get('pct', 0)
                sign = '+' if net >= 0 else ''
                parts.append(f'{s}: 净额{sign}{net:.1f}亿 涨幅{pct:+.2f}%')
    if not parts:
        return ''
    return '所属板块资金 | ' + ' | '.join(parts[:3])  # 最多 3 个板块
