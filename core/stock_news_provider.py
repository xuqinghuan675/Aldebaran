"""个股专属新闻 — Aldebaran v3.0 Agent3 公司面分析的核心输入。

数据源：akshare.stock_news_em（东方财富个股新闻）

缓存策略：CACHE_DIR/stock_news/{code}_{YYYYMMDD}.json，TTL = 当日有效
触发时机：仅在 AI 分析按钮被点击时拉取（懒加载），不在面板启动时预拉。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'stock_news'
_DEFAULT_LIMIT = 10


def _cache_file(code: str) -> Path:
    today = date.today().strftime('%Y%m%d')
    return _CACHE_DIR / f'{code}_{today}.json'


def _load_cache(code: str) -> list[dict] | None:
    fp = _cache_file(code)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_cache(code: str, news: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(code).write_text(
            json.dumps(news, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def fetch_stock_news(code: str, limit: int = _DEFAULT_LIMIT, force: bool = False) -> list[dict]:
    """拉取个股新闻；当日缓存命中直接返回。

    Args:
        code: 6 位股票代码
        limit: 返回条数上限
        force: 强制重新拉取（跳过缓存）

    Returns:
        [{'title', 'date', 'content', 'source', 'url'}, ...]
        失败或无数据返回空列表。
    """
    code = (code or '').strip()
    if not code:
        return []

    if not force:
        cached = _load_cache(code)
        if cached is not None:
            return cached[:limit]

    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code)
        if df is None or len(df) == 0:
            _save_cache(code, [])
            return []
    except Exception as e:
        logger.warning(f'stock_news_em({code}) 失败: {e}')
        return []

    news: list[dict] = []
    for _, row in df.iterrows():
        try:
            item = {
                'title': str(row.get('新闻标题', '')).strip(),
                'date': str(row.get('发布时间', '')).strip(),
                'content': str(row.get('新闻内容', '')).strip()[:500],
                'source': str(row.get('文章来源', '')).strip(),
                'url': str(row.get('新闻链接', '')).strip(),
            }
            if item['title']:
                news.append(item)
        except Exception:
            continue

    # 按发布时间倒序（akshare 返回顺序不保证）
    def _parse_date(s: str) -> datetime:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return datetime.min
    news.sort(key=lambda x: _parse_date(x.get('date', '')), reverse=True)

    _save_cache(code, news)
    return news[:limit]


def summarize_for_prompt(news_list: list[dict], top_n: int = 6) -> str:
    """生成 prompt-ready 文本，前 N 条，含日期前缀。"""
    if not news_list:
        return '▶ 个股新闻：暂无'

    parts = ['▶ 个股新闻（最近）：']
    for item in news_list[:top_n]:
        title = (item.get('title') or '').strip()[:80]
        d = (item.get('date') or '').strip()[:16]
        if title:
            if d:
                parts.append(f'  · [{d}] {title}')
            else:
                parts.append(f'  · {title}')
    return '\n'.join(parts)


def clear_cache(older_than_days: int = 7) -> int:
    """清理过期缓存。返回删除数。"""
    if not _CACHE_DIR.exists():
        return 0
    today = date.today()
    removed = 0
    for fp in _CACHE_DIR.glob('*.json'):
        try:
            # 文件名格式：{code}_YYYYMMDD.json
            stem = fp.stem
            if '_' in stem:
                ds = stem.rsplit('_', 1)[-1]
                d = datetime.strptime(ds, '%Y%m%d').date()
                if (today - d).days >= older_than_days:
                    fp.unlink()
                    removed += 1
        except Exception:
            continue
    return removed


__all__ = ['fetch_stock_news', 'summarize_for_prompt', 'clear_cache']
