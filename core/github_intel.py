"""GitHub 开源生态情报拉取器。

流程：
  1. 根据股票板块，从 sector_repo_map 获取相关 GitHub 仓库列表
  2. 调用 GitHub REST API 拉取最近 Releases 和重大 Issues
  3. 将活跃度/发版信息转换为 IntelEvent 格式，注入情报面板

配置：
  GitHub Token（可选，提升速率限制）存储在 core.credentials：
    ~/.aldebaran/intel_config.json → {"github_token": "ghp_..."}

速率限制：
  - 无 Token：60 次/h
  - 有 Token：5000 次/h
  建议仅在持仓面板初始化或手动刷新时调用，不要轮询。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import http_client
from core.sector_repo_map import get_repos_for_sectors

logger = logging.getLogger(__name__)

_GH_API = 'https://api.github.com'
_TIMEOUT = 8
from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'github'
_CACHE_TTL = 3600  # 1h


def _github_token() -> str:
    """读取 GitHub Personal Access Token（可选）。"""
    try:
        from core.credentials import load_github_token
        return load_github_token()
    except Exception:
        return ''


def _gh_headers() -> dict:
    token = _github_token()
    h = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _cache_path(repo: str) -> Path:
    safe = repo.replace('/', '__')
    return _CACHE_DIR / f'{safe}.json'


def _cache_is_fresh(repo: str) -> bool:
    p = _cache_path(repo)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        ts = data.get('fetched_at', 0)
        return (time.time() - ts) < _CACHE_TTL
    except Exception:
        return False


def _load_cache(repo: str) -> list[dict]:
    try:
        data = json.loads(_cache_path(repo).read_text(encoding='utf-8'))
        return data.get('events', [])
    except Exception:
        return []


def _save_cache(repo: str, events: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(repo).write_text(
            json.dumps({'fetched_at': time.time(), 'events': events},
                       ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def _fetch_releases(repo: str, days: int = 30) -> list[dict]:
    """获取最近 N 天的 Release 事件。"""
    try:
        r = http_client.get(
            f'{_GH_API}/repos/{repo}/releases',
            headers=_gh_headers(),
            params={'per_page': 5},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        events = []
        for rel in r.json():
            pub = rel.get('published_at') or rel.get('created_at', '')
            try:
                pub_dt = datetime.fromisoformat(pub.replace('Z', '+00:00'))
            except Exception:
                continue
            if pub_dt < cutoff:
                continue
            events.append({
                'type': 'release',
                'tag': rel.get('tag_name', ''),
                'name': rel.get('name', '') or rel.get('tag_name', ''),
                'body': (rel.get('body') or '')[:200],
                'url': rel.get('html_url', ''),
                'date': pub_dt.strftime('%Y-%m-%d'),
                'stars': None,  # releases don't carry star count
            })
        return events
    except Exception as e:
        logger.debug('github releases fetch failed for %s: %s', repo, e)
        return []


def _fetch_repo_meta(repo: str) -> dict:
    """获取仓库基本信息（star 数、描述）。"""
    try:
        r = http_client.get(
            f'{_GH_API}/repos/{repo}',
            headers=_gh_headers(),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        return {
            'stars': d.get('stargazers_count', 0),
            'description': (d.get('description') or '')[:80],
            'pushed_at': d.get('pushed_at', ''),
        }
    except Exception:
        return {}


def fetch_repo_signals(repo_entry: dict, days: int = 14) -> list[dict]:
    """拉取单个仓库的信号事件（带缓存）。

    Args:
        repo_entry: {"repo": "owner/name", "label": "...", "kind": "..."}
        days: 只保留最近 N 天的事件

    Returns:
        list of raw signal dicts
    """
    repo = repo_entry['repo']
    if _cache_is_fresh(repo):
        return _load_cache(repo)

    meta = _fetch_repo_meta(repo)
    releases = _fetch_releases(repo, days=days)

    signals: list[dict] = []
    for rel in releases:
        signals.append({
            **rel,
            'repo': repo,
            'label': repo_entry.get('label', repo.split('/')[-1]),
            'kind': repo_entry.get('kind', 'tool'),
            'stars': meta.get('stars'),
            'description': meta.get('description', ''),
        })

    # 若无 Release 但最近有 push，生成一条活跃度信号
    if not releases and meta.get('pushed_at'):
        try:
            pushed = datetime.fromisoformat(
                meta['pushed_at'].replace('Z', '+00:00')
            )
            if datetime.now(timezone.utc) - pushed < timedelta(days=days):
                signals.append({
                    'type': 'push',
                    'tag': '',
                    'name': f'{repo_entry.get("label", repo)} 近期活跃',
                    'body': meta.get('description', ''),
                    'url': f'https://github.com/{repo}',
                    'date': pushed.strftime('%Y-%m-%d'),
                    'repo': repo,
                    'label': repo_entry.get('label', repo.split('/')[-1]),
                    'kind': repo_entry.get('kind', 'tool'),
                    'stars': meta.get('stars'),
                    'description': meta.get('description', ''),
                })
        except Exception:
            pass

    _save_cache(repo, signals)
    return signals


def build_intel_events_from_signals(
    signals: list[dict],
    sector: str = '',
) -> list[dict]:
    """将 GitHub 信号转换为 IntelEvent 格式的 dict 列表（未实例化）。

    返回的每个 dict 可直接传给 IntelEvent.from_dict() 或注入 context。
    """
    events = []
    for sig in signals:
        label = sig.get('label', sig.get('repo', ''))
        stars = sig.get('stars')
        stars_str = f'（⭐{stars:,}）' if stars else ''
        kind_map = {'data': '数据', 'tool': '工具', 'research': '研究'}
        kind_str = kind_map.get(sig.get('kind', ''), '开源')

        if sig['type'] == 'release':
            title = f'[GitHub {kind_str}] {label}{stars_str} 发布 {sig["tag"]}'
            tip = sig.get('body', '')[:80] or f'查看详情：{sig["url"]}'
            direction = 'bullish'  # 开源工具更新通常是利多
        else:
            title = f'[GitHub {kind_str}] {label}{stars_str} 近期活跃更新'
            tip = sig.get('description', '')[:80] or f'仓库活跃：{sig["url"]}'
            direction = 'neutral'

        events.append({
            'id': f'gh_{sig["repo"].replace("/", "_")}_{sig["date"]}',
            'title': title[:80],
            'category': 'ai',
            'direction': direction,
            'level': 'info',
            'summary': f'{label} 开源仓库动态：{sig.get("name", "")}',
            'interpretation': f'开源生态活跃度信号，相关板块：{sector}',
            'related_sectors': [sector] if sector else [],
            'timestamp': sig.get('date', ''),
            'source': f'GitHub/{sig["repo"]}',
            'trading_tip': tip,
        })
    return events


def fetch_github_intel_for_sectors(
    sectors: list[str],
    days: int = 14,
) -> list[dict]:
    """根据板块列表拉取所有相关 GitHub 信号并转换为 intel event dicts。

    Args:
        sectors: 板块名称列表（来自 intel_matcher.infer_sectors_for_stock）
        days: 时间窗口天数

    Returns:
        list of intel event dicts，可用 IntelEvent.from_dict() 实例化
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    repo_entries = get_repos_for_sectors(sectors)
    if not repo_entries:
        return []

    all_signals: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(fetch_repo_signals, entry, days): entry
                for entry in repo_entries
            }
            for fut in as_completed(futures, timeout=15):
                entry = futures[fut]
                try:
                    sigs = fut.result()
                    sector_for_entry = next(
                        (s for s in sectors
                         if entry['repo'] in [e['repo']
                                              for e in get_repos_for_sectors([s])]),
                        sectors[0] if sectors else '',
                    )
                    intel = build_intel_events_from_signals(sigs, sector=sector_for_entry)
                    all_signals.extend(intel)
                except Exception as e:
                    logger.debug('github intel fetch error for %s: %s', entry['repo'], e)
    except Exception as e:
        logger.debug('github intel ThreadPool error: %s', e)

    return all_signals
