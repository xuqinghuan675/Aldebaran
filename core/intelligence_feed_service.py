"""Reusable data service for the 3D intelligence network page."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.intelligence.collectors_builtin import collect_builtin_sources
from core.intelligence.normalize import normalized_items_from_seed
from core.intelligence.source_registry import source_status_for_specs


def load_current_intel_events() -> list:
    from core.intel_matcher import load_intel_feed
    return load_intel_feed()


def refresh_intel_feed(force: bool = False) -> dict[str, Any]:
    """Return current feed metadata.

    Collection is intentionally not started here in v1; the network page first
    consumes existing data and explicit missing-evidence nodes.
    """
    events = load_current_intel_events()
    return {
        'force_refresh': bool(force),
        'event_count': len(events),
        'refreshed_at': datetime.now().isoformat(timespec='seconds'),
        'events': events,
    }


def get_relevant_events_for_stock(
    code: str,
    name: str,
    sectors: list[str],
    max_n: int = 20,
) -> list:
    from core.intel_matcher import filter_relevant_intel
    return filter_relevant_intel(
        load_current_intel_events(),
        code,
        name,
        sector_hints=sectors,
        max_n=max_n,
    )


def build_today_market_graph_seed() -> dict[str, Any]:
    return {
        'subject': {
            'code': '',
            'name': '今日市场',
            'type': 'market',
        },
        'intel_events': load_current_intel_events(),
        'stock_news': [],
        'context': {},
        'source_status': {
            'intel_feed': 'loaded',
            'created_at': datetime.now().isoformat(timespec='seconds'),
        },
    }


def build_stock_graph_seed(
    code: str,
    name: str,
    *,
    force_refresh: bool = False,
    sector_context: dict | None = None,
) -> dict[str, Any]:
    code = _clean_code(code)
    name = str(name or code).strip()

    context = _load_stock_context(code, name, force_refresh=force_refresh)
    sectors = _infer_sectors(code, name, sector_context)
    events = get_relevant_events_for_stock(code, name, sectors, max_n=20)
    news = _fetch_stock_news(code, force=force_refresh)
    holdings = _load_holdings()
    watchlist = _load_watchlist()
    realtime = context.get('realtime') if isinstance(context.get('realtime'), dict) else {}

    public_evidence = context.get('public_fund_evidence') if isinstance(context.get('public_fund_evidence'), dict) else {}

    seed = {
        'subject': {
            'code': code,
            'name': name,
            'type': 'stock',
            'price': realtime.get('current_price') or context.get('_kline_last_close'),
        },
        'sectors': sectors,
        'intel_events': events,
        'stock_news': news,
        'flow_profile': context.get('flow_profile'),
        'trading_behavior': {
            'fund_flow': context.get('flow_profile') or {},
            'hot_money': public_evidence.get('lhb') or {},
            'institutional': public_evidence.get('hsgt') or {},
            'margin_financing': context.get('margin_financing') or {},
            'block_trade': public_evidence.get('dzjy') or {},
        },
        'fundamental_summary': context.get('fundamental_summary') or '',
        'variable_snapshots': {},
        'time_windows': ['immediate', 'short', 'swing', 'mid', 'long'],
        'market_context': {
            'emotion': context.get('emotion'),
            'global': context.get('global'),
            'market_phase': context.get('market_phase'),
        },
        'restricted_release': context.get('restricted_release'),
        'hot_rank': context.get('hot_rank'),
        'context': context,
        'holdings': holdings,
        'watchlist': watchlist,
        'source_status': {
            'registered_sources': source_status_for_specs(),
            'intel_feed': 'loaded',
            'stock_news': 'loaded' if news else 'missing_or_empty',
            'stock_context': 'loaded' if context else 'missing_or_empty',
            'public_fund_evidence': 'loaded' if public_evidence else 'missing_or_empty',
            'block_trade': 'loaded' if _payload_hit(public_evidence.get('dzjy')) else 'missing_or_empty',
            'margin_financing': 'loaded' if context.get('margin_financing') else 'missing_or_empty',
            'created_at': datetime.now().isoformat(timespec='seconds'),
        },
    }
    _attach_collector_output(seed, code, name)
    return seed


def _attach_collector_output(seed: dict[str, Any], code: str, name: str) -> None:
    try:
        batch = collect_builtin_sources(code, name, context=seed, use_cache=True)
        normalized_items = list(batch.get('normalized_items') or [])
        seed['normalized_intel_items'] = [
            item.to_dict() if hasattr(item, 'to_dict') else dict(item)
            for item in normalized_items
        ]
        seed['source_status']['collector_sources'] = dict(batch.get('source_status') or {})
        seed['source_status']['normalized_item_count'] = len(normalized_items)
        seed['source_status']['coverage_summary'] = dict(batch.get('coverage_summary') or {})
    except Exception as exc:
        fallback_items = normalized_items_from_seed(seed)
        seed['normalized_intel_items'] = [item.to_dict() for item in fallback_items]
        seed['source_status']['collector_sources'] = {
            'collector_batch': {
                'status': 'error',
                'last_error': str(exc),
                'normalized_count': len(fallback_items),
            }
        }
        seed['source_status']['normalized_item_count'] = len(fallback_items)
        seed['source_status']['coverage_summary'] = {
            'real_node_count': len(fallback_items),
            'by_layer': {},
            'missing_layers': [],
            'freshness': {},
            'sources': {'error': 1},
        }


def _load_stock_context(code: str, name: str, *, force_refresh: bool) -> dict[str, Any]:
    try:
        from core.data_orchestrator import gather_stock_context
        ctx = gather_stock_context(code, name, force_refresh=force_refresh)
        return ctx if isinstance(ctx, dict) else {}
    except Exception:
        return {}


def _infer_sectors(code: str, name: str, sector_context: dict | None) -> list[str]:
    try:
        from core.intel_matcher import infer_sectors_for_stock
        return infer_sectors_for_stock(code, name, sector_context=sector_context)
    except Exception:
        return []


def _fetch_stock_news(code: str, *, force: bool) -> list[dict]:
    try:
        from core.stock_news_provider import fetch_stock_news
        return fetch_stock_news(code, limit=12, force=force)
    except Exception:
        return []


def _load_watchlist() -> list[dict]:
    try:
        from core.cache import load_watchlist
        return load_watchlist()
    except Exception:
        return []


def _load_holdings() -> list[dict]:
    try:
        from core.portfolio_data import load_holdings
        return load_holdings()
    except Exception:
        return []


def _clean_code(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _payload_hit(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return value.get('hit', True) is not False
