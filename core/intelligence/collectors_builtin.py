"""Built-in collectors that reuse Aldebaran's existing local/API data."""
from __future__ import annotations

import hashlib
import html
import importlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

from core.intelligence import cache_store
from core.intelligence.collection_plan import CollectionPlan, build_collection_plan
from core.intelligence.collector_base import Collector, CollectorResult, run_collector
from core.intelligence.field_summary import build_periodic_field_summary_items
from core.intelligence.models import NormalizedIntelItem, RawIntelItem, SourceSpec
from core.intelligence.normalize import _layer_from_text, normalized_items_from_seed
from core.intelligence.route_profile import RouteProfile, build_route_profile
from core.intelligence.source_catalog import REQUIRED_FIELDS, credential_env_names, get_source_catalog
from core.intelligence.source_registry import get_enabled_sources


FORBIDDEN_INTELLIGENCE_TERMS = (
    'K线',
    '均线',
    'MACD',
    'RSI',
    'KDJ',
    '支撑位',
    '压力位',
    '支撑',
    '压力',
    '买点',
    '止损',
    '失效价',
    '触发价',
    'technical-analysis',
)

_THIN_LAYER_EVIDENCE_TYPES = {
    'cost': 'cost_factor',
    'demand': 'demand_factor',
    'export': 'export_factor',
    'customer_supplier': 'customer',
    'inventory': 'inventory_factor',
    'capacity': 'capacity_factor',
    'competition': 'competition_factor',
    'order_contract': 'order_contract',
}

_THIN_LAYER_KEYS = {
    'cost': ('cost_summary', 'cost_evidence', 'cost_profile'),
    'demand': ('demand_summary', 'demand_evidence', 'demand_profile'),
    'export': ('export_summary', 'export_evidence', 'export_profile'),
    'customer_supplier': (
        'customer_supplier_summary',
        'supply_chain_summary',
        'customer_summary',
        'supplier_summary',
        'customer_supplier',
        'customer_profile',
        'supplier_profile',
    ),
    'inventory': ('inventory_summary', 'inventory_evidence', 'inventory_profile'),
    'capacity': ('capacity_summary', 'capacity_evidence', 'capacity_profile'),
    'competition': ('competition_summary', 'competition_evidence', 'competition_profile'),
    'order_contract': (
        'order_contract_summary',
        'order_contract',
        'contract_summary',
        'order_summary',
    ),
}

_REQUIRED_COVERAGE_LAYERS = (
    'cost',
    'demand',
    'export',
    'customer_supplier',
    'inventory',
    'capacity',
    'competition',
    'order_contract',
    'policy',
    'risk',
    'trading_behavior',
)

_CODE_HINTS = {
    '600584': ('semiconductor', 'advanced packaging', 'chip packaging', 'outsourced assembly', 'OSAT'),
    '300308': ('optical module', 'optical transceiver', 'AI data center', 'datacenter', 'cpo'),
    '300502': ('optical module', 'optical transceiver', 'AI data center', 'datacenter', 'cpo'),
    '000681': ('visual content', 'copyright', 'AI generated content', 'AIGC', 'image licensing'),
}

_STOCK_QUERY_HINTS = {
    '300308': (
        'CPO', '\u5149\u6a21\u5757', '800G', '1.6T', 'AI\u7b97\u529b',
        '\u7b97\u529b', '\u6570\u636e\u4e2d\u5fc3', '\u6d77\u5916\u4e91\u5382\u5546',
        '\u5149\u901a\u4fe1', 'optical module', 'optical transceiver',
        'AI data center', 'cloud customer',
    ),
    '300502': (
        'CPO', '\u5149\u6a21\u5757', '800G', '1.6T', '\u7845\u5149',
        '\u6570\u636e\u4e2d\u5fc3', '\u6d77\u5916\u5ba2\u6237',
        '\u5149\u901a\u4fe1', 'silicon photonics', 'optical module',
        'AI data center', 'overseas customer',
    ),
    '600584': (
        '\u534a\u5bfc\u4f53\u5c01\u6d4b', '\u5148\u8fdb\u5c01\u88c5', 'Chiplet',
        '\u5b58\u50a8', '\u6d88\u8d39\u7535\u5b50', '\u6c7d\u8f66\u7535\u5b50',
        '\u96c6\u6210\u7535\u8def', '\u5c01\u88c5\u6750\u6599',
        'advanced packaging', 'semiconductor packaging', 'OSAT',
    ),
    '000681': (
        '\u7248\u6743', 'AI\u8bed\u6599', '\u56fe\u7247\u7248\u6743', 'AIGC',
        '\u5185\u5bb9\u6388\u6743', '\u8bc9\u8bbc\u98ce\u9669',
        '\u91cd\u70b9\u4f5c\u54c1\u7248\u6743\u4fdd\u62a4',
        'copyright', 'image licensing', 'content licensing',
    ),
}

_POLICY_QUERY_HINTS = {
    '300308': ('\u5149\u6a21\u5757', '800G', '\u4eba\u5de5\u667a\u80fd \u4fe1\u606f\u901a\u4fe1'),
    '300502': ('\u5149\u6a21\u5757', '\u7845\u5149', '\u4eba\u5de5\u667a\u80fd \u4fe1\u606f\u901a\u4fe1'),
    '600584': ('\u534a\u5bfc\u4f53', '\u5148\u8fdb\u5c01\u88c5', '\u96c6\u6210\u7535\u8def'),
    '000681': ('\u7248\u6743', 'AIGC', '\u4eba\u5de5\u667a\u80fd'),
}

_INDUSTRY_QUERY_HINTS = {
    '300308': ('\u5149\u6a21\u5757', '\u5149\u82af\u7247 \u4ef7\u683c', '\u6570\u636e\u4e2d\u5fc3 \u4ea4\u6362\u673a'),
    '300502': ('\u5149\u6a21\u5757', '\u7845\u5149', '\u5149\u5668\u4ef6 \u6210\u672c'),
    '600584': ('\u5148\u8fdb\u5c01\u88c5', '\u5c01\u88c5\u6750\u6599', '\u534a\u5bfc\u4f53 \u5c01\u88c5 \u6210\u672c'),
    '000681': ('\u7248\u6743', 'AIGC', '\u5185\u5bb9\u6388\u6743'),
}

_EXTERNAL_LAYER_CHECKS = (
    ('order_contract', (
        '\u5408\u540c', '\u8ba2\u5355', '\u4e2d\u6807', '\u7b7e\u7f72', '\u534f\u8bae',
        '\u91c7\u8d2d', 'contract', 'order', 'bid',
    )),
    ('risk', (
        '\u98ce\u9669\u63d0\u793a', '\u8bc9\u8bbc', '\u5904\u7f5a', '\u7acb\u6848',
        '\u51cf\u503c', '\u5916\u6c47\u5957\u671f\u4fdd\u503c', 'risk', 'lawsuit',
    )),
    ('financial', (
        '\u5e74\u5ea6\u62a5\u544a', '\u534a\u5e74\u5ea6\u62a5\u544a', '\u5b63\u5ea6\u62a5\u544a',
        '\u4e1a\u7ee9', '\u5ba1\u8ba1', 'H\u80a1', '\u53d1\u884c\u80a1\u4efd',
        'financial', 'audit',
    )),
    ('capacity', (
        '\u4ea7\u80fd', '\u6295\u4ea7', '\u6269\u4ea7', '\u5efa\u8bbe', '\u9879\u76ee',
        '\u5de5\u5382', '\u5bf9\u5916\u6295\u8d44', '\u751f\u4ea7\u57fa\u5730',
        'capacity', 'project',
    )),
    ('cost', (
        '\u6210\u672c', '\u4ef7\u683c', '\u6750\u6599', '\u539f\u6750\u6599',
        '\u5149\u82af\u7247', '\u5149\u5668\u4ef6', '\u8d44\u672c\u5f00\u652f',
        'cost', 'price', 'material', 'capex',
    )),
    ('competition', (
        '\u7ade\u4e89', '\u683c\u5c40', '\u4efd\u989d', '\u66ff\u4ee3', '400G',
        '800G', '1.6T', 'CPO', '\u7845\u5149', 'competition', 'market share',
    )),
    ('customer_supplier', (
        '\u5ba2\u6237', '\u4f9b\u5e94\u5546', '\u4f9b\u5e94\u94fe', '\u6d77\u5916\u5ba2\u6237',
        '\u4e91\u5382\u5546', 'customer', 'supplier', 'supply chain',
    )),
    ('policy', (
        '\u653f\u7b56', '\u901a\u77e5', '\u610f\u89c1', '\u884c\u52a8\u8ba1\u5212',
        '\u6807\u51c6', '\u6307\u5bfc', 'policy', 'regulation',
    )),
)

_EXTERNAL_SECONDARY_LAYER_CHECKS = (
    ('demand', (
        '\u9700\u6c42', '\u9500\u91cf', '\u51fa\u8d27', '\u7ec8\u7aef',
        '\u590d\u82cf', '\u62c9\u52a8', 'demand', 'shipment', 'sales',
    )),
    ('export', (
        '\u51fa\u53e3', '\u6d77\u5916', '\u5916\u8d38', '\u6d77\u5173',
        'export', 'overseas', 'global customer',
    )),
    ('customer_supplier', (
        '\u5ba2\u6237', '\u4f9b\u5e94\u5546', '\u4f9b\u5e94\u94fe',
        '\u4e91\u5382\u5546', 'customer', 'supplier', 'supply chain',
    )),
    ('inventory', (
        '\u5e93\u5b58', '\u53bb\u5e93', '\u8865\u5e93', '\u5907\u8d27',
        'inventory', 'restock', 'restocking',
    )),
)

_ANNOUNCEMENT_PDF_BODY_LAYERS = {
    'order_contract',
    'capacity',
    'risk',
    'financial',
    'customer_supplier',
    'inventory',
    'export',
}

_PERIODIC_REPORT_TITLE_TERMS = (
    '\u5e74\u5ea6\u62a5\u544a',
    '\u534a\u5e74\u5ea6\u62a5\u544a',
    '\u5b63\u5ea6\u62a5\u544a',
    '\u4e00\u5b63\u62a5',
    '\u4e09\u5b63\u62a5',
    '\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a',
    '\u7b2c\u4e09\u5b63\u5ea6\u62a5\u544a',
)

_PERIODIC_REPORT_EXCLUDE_TERMS = (
    '\u6458\u8981',
    '\u53d6\u6d88',
    '\u66f4\u6b63',
    '\u8865\u5145\u516c\u544a',
    '\u72ec\u7acb\u8463\u4e8b\u610f\u89c1',
    '\u72ec\u8463\u610f\u89c1',
    '\u5ba1\u8ba1\u62a5\u544a',
    '\u82f1\u6587\u7248',
    '\u5916\u6587\u7248',
    'English',
)

_PERIODIC_REPORT_SUMMARY_TERMS = ('\u6458\u8981',)
_PERIODIC_REPORT_CORRECTION_TERMS = (
    '\u66f4\u6b63',
    '\u8865\u5145',
    '\u53d6\u6d88',
)
_PERIODIC_REPORT_ENGLISH_TERMS = (
    '\u82f1\u6587\u7248',
    '\u5916\u6587\u7248',
    'English',
)
_PERIODIC_REPORT_NOT_PERIODIC_TERMS = (
    '\u72ec\u7acb\u8463\u4e8b\u610f\u89c1',
    '\u72ec\u8463\u610f\u89c1',
    '\u5ba1\u8ba1\u62a5\u544a',
    '\u6cd5\u5f8b\u610f\u89c1\u4e66',
)

_SOURCE_PRIORITY = {
    'official': 40,
    'primary': 35,
    'secondary': 25,
    'media': 15,
    'unknown': 5,
}


class ExistingIntelFeedCollector(Collector):
    source_id = 'existing:intel_feed'

    def fetch(self, spec, *, code='', name='', context=None):
        root = _root(context)
        events = list(root.get('intel_events') or [])
        if not events:
            from core.intel_matcher import filter_relevant_intel, load_intel_feed
            all_events = load_intel_feed()
            sectors = list(root.get('sectors') or [])
            try:
                events = filter_relevant_intel(all_events, code, name, sector_hints=sectors, max_n=20)
            except Exception:
                events = all_events[:20]
        return [
            _raw_from_payload(
                spec,
                title=str(_event_dict(event).get('title') or ''),
                text=str(_event_dict(event).get('summary') or _event_dict(event).get('interpretation') or ''),
                published_at=str(_event_dict(event).get('timestamp') or '') or None,
                layer=spec.layer,
                payload={'event': event, 'code': code, 'name': name, 'sectors': list(root.get('sectors') or [])},
            )
            for event in events
            if not _contains_forbidden(
                str(_event_dict(event).get('title') or ''),
                str(_event_dict(event).get('summary') or _event_dict(event).get('interpretation') or ''),
            )
        ]

    def normalize(self, raw, *, code='', name='', context=None):
        seed = _seed(code, name, context)
        seed['intel_events'] = [raw.raw.get('event')]
        return _normalize_seed(seed, self.source_id)


class StockNewsCollector(Collector):
    source_id = 'existing:stock_news'

    def fetch(self, spec, *, code='', name='', context=None):
        news = list(_value(context, 'stock_news') or [])
        if not news and code:
            from core.stock_news_provider import fetch_stock_news
            news = fetch_stock_news(code, limit=12, force=False)
        items = []
        for item in news:
            data = dict(item or {})
            title = str(data.get('title') or '').strip()
            text = str(data.get('content') or title)
            if not title or _contains_forbidden(title, text):
                continue
            items.append(_raw_from_payload(
                spec,
                title=title,
                text=text,
                published_at=str(data.get('date') or '') or None,
                url=str(data.get('url') or ''),
                layer=spec.layer,
                payload={'news': data, 'code': code, 'name': name},
            ))
        return items

    def normalize(self, raw, *, code='', name='', context=None):
        seed = _seed(code, name, context)
        seed['stock_news'] = [raw.raw.get('news') or {}]
        return _normalize_seed(seed, self.source_id)


class FundamentalsCollector(Collector):
    source_id = 'existing:fundamentals'

    def fetch(self, spec, *, code='', name='', context=None):
        summary = str(_value(context, 'fundamental_summary') or '').strip()
        if not summary and code:
            from core.fundamentals_provider import summarize
            summary = str(summarize(code) or '').strip()
        if not summary or _contains_forbidden(summary):
            return []
        return [_raw_from_payload(
            spec,
            title='fundamental_summary',
            text=summary,
            published_at=None,
            layer=spec.layer,
            payload={'fundamental_summary': summary, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        seed = _seed(code, name, context)
        seed['context'] = dict(seed.get('context') or {})
        seed['context']['fundamental_summary'] = raw.raw.get('fundamental_summary') or raw.text
        return _normalize_seed(seed, self.source_id)


class MarketContextCollector(Collector):
    source_id = 'existing:market_context'

    def fetch(self, spec, *, code='', name='', context=None):
        emotion = _value(context, 'emotion')
        global_snap = _value(context, 'global')
        phase = _value(context, 'market_phase')
        if not any((emotion, global_snap, phase)):
            from core.market_context_provider import get_emotion_snapshot, get_global_snapshot, get_market_phase
            emotion = get_emotion_snapshot(force=False)
            global_snap = get_global_snapshot(force=False)
            phase = get_market_phase(emotion)
        summary = _market_summary(emotion, global_snap, phase)
        if not summary or _contains_forbidden(summary):
            return []
        return [_raw_from_payload(
            spec,
            title='market_context',
            text=summary,
            published_at=_today(),
            layer=spec.layer,
            payload={'emotion': emotion, 'global': global_snap, 'market_phase': phase, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        return [_normalized(
            raw,
            code,
            name,
            evidence_type='macro',
            direction='neutral',
            confidence=4,
            time_windows=['immediate', 'short', 'swing'],
        )]


class TradingBehaviorCollector(Collector):
    source_id = 'existing:public_fund_evidence'

    def fetch(self, spec, *, code='', name='', context=None):
        evidence = _value(context, 'public_fund_evidence')
        if not isinstance(evidence, dict) or not evidence:
            from core.public_fund_evidence import get_public_evidence
            evidence = get_public_evidence(code, name)
        if not isinstance(evidence, dict) or not evidence.get('available', True):
            return []
        hits = [
            payload for key in ('lhb', 'hsgt', 'dzjy')
            if isinstance((payload := evidence.get(key)), dict) and payload and payload.get('hit', True) is not False
        ]
        if not hits:
            return []
        summary = '；'.join(str(item.get('summary') or item)[:120] for item in hits)
        if _contains_forbidden(summary):
            return []
        return [_raw_from_payload(
            spec,
            title='public_fund_evidence',
            text=summary,
            published_at=str(evidence.get('date') or '') or None,
            layer=spec.layer,
            payload={'public_fund_evidence': evidence, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        seed = _seed(code, name, context)
        seed['context'] = dict(seed.get('context') or {})
        seed['context']['public_fund_evidence'] = raw.raw.get('public_fund_evidence') or {}
        return _normalize_seed(seed, self.source_id)


class MoneyFlowCollector(Collector):
    source_id = 'existing:money_flow'

    def fetch(self, spec, *, code='', name='', context=None):
        flow = _value(context, 'flow_profile')
        summary = str(_value(context, 'money_flow_summary') or '').strip()
        if not isinstance(flow, dict):
            flow = {}
        if not flow and not summary:
            return []
        text = summary or _flow_summary(flow)
        if not text or _contains_forbidden(text):
            return []
        return [_raw_from_payload(
            spec,
            title='money_flow_profile',
            text=text,
            published_at=_today(),
            layer=spec.layer,
            payload={'flow_profile': flow, 'money_flow_summary': summary, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        flow = raw.raw.get('flow_profile') if isinstance(raw.raw.get('flow_profile'), dict) else {}
        main_5d = _num(flow.get('main_5d'))
        direction = 'bullish' if main_5d and main_5d > 0 else 'bearish' if main_5d and main_5d < 0 else 'neutral'
        item = _normalized(
            raw,
            code,
            name,
            evidence_type='fund_flow',
            direction=direction,
            confidence=6 if flow.get('available', True) and int(flow.get('days') or 0) >= 3 else 4,
            time_windows=['immediate', 'short'],
        )
        if main_5d is not None:
            item.metric_name = 'main_5d'
            item.current_value = main_5d
            item.unit = 'yuan'
        return [item]


class ThinLayerContextCollector(Collector):
    source_id = 'existing:thin_layer_context'

    def fetch(self, spec, *, code='', name='', context=None):
        items: list[RawIntelItem] = []
        for layer, key, payload in _thin_layer_payloads(context):
            summary, data = _payload_summary(payload)
            if not summary or _contains_forbidden(str(key), summary):
                continue
            items.append(_raw_from_payload(
                spec,
                title=str(data.get('title') or key),
                text=summary,
                published_at=str(data.get('published_at') or data.get('date') or '') or None,
                url=str(data.get('url') or ''),
                layer=layer,
                payload={
                    'payload': data,
                    'layer_key': key,
                    'code': code,
                    'name': name,
                },
            ))
        return items

    def normalize(self, raw, *, code='', name='', context=None):
        payload = raw.raw.get('payload') if isinstance(raw.raw.get('payload'), dict) else {}
        item = _normalized(
            raw,
            code,
            name,
            evidence_type=str(payload.get('evidence_type') or _THIN_LAYER_EVIDENCE_TYPES.get(raw.layer, 'news')),
            direction=_direction_value(payload.get('direction')),
            confidence=_int_between(payload.get('confidence'), 5, 0, 10),
            time_windows=_time_windows(payload, ['short', 'swing', 'mid']),
        )
        item.id = (
            f'{item.evidence_type}:{_safe_id(raw.source_id)}:'
            f'{_safe_id(raw.layer)}:{_safe_id(raw.title)}'
        )
        item.metric_name = str(payload.get('metric_name') or '')
        item.current_value = _num(payload.get('current_value'))
        item.unit = str(payload.get('unit') or '')
        item.change_1d = _num(payload.get('change_1d'))
        item.change_3d = _num(payload.get('change_3d'))
        item.change_7d = _num(payload.get('change_7d'))
        item.change_30d = _num(payload.get('change_30d'))
        item.change_qoq = _num(payload.get('change_qoq'))
        item.change_yoy = _num(payload.get('change_yoy'))
        item.acceleration = str(payload.get('acceleration') or 'unknown')
        item.expectation_gap = str(payload.get('expectation_gap') or 'unknown')
        item.related_sectors = [
            str(value).strip()
            for value in (payload.get('related_sectors') or [])
            if str(value).strip()
        ]
        return [item]


class RestrictedReleaseCollector(Collector):
    source_id = 'existing:restricted_release'

    def fetch(self, spec, *, code='', name='', context=None):
        restricted = _value(context, 'restricted_release')
        if not isinstance(restricted, dict) or not restricted:
            from core.restricted_release_provider import fetch_restricted_release
            restricted = fetch_restricted_release(code) or {}
        summary = str((restricted or {}).get('summary') or '').strip()
        if not summary or _contains_forbidden(summary):
            return []
        return [_raw_from_payload(
            spec,
            title='restricted_release',
            text=summary,
            published_at=str(restricted.get('release_date') or '') or None,
            layer=spec.layer,
            payload={'restricted_release': restricted, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        seed = _seed(code, name, context)
        seed['context'] = dict(seed.get('context') or {})
        seed['context']['restricted_release'] = raw.raw.get('restricted_release') or {}
        return _normalize_seed(seed, self.source_id)


class EventCalendarCollector(Collector):
    source_id = 'existing:event_calendar'

    def fetch(self, spec, *, code='', name='', context=None):
        restricted = _value(context, 'restricted_release')
        if not isinstance(restricted, dict) or not restricted:
            from core.restricted_release_provider import fetch_restricted_release
            restricted = fetch_restricted_release(code) or {}
        summary = str((restricted or {}).get('summary') or '').strip()
        if not summary or not restricted.get('release_date') or _contains_forbidden(summary):
            return []
        return [_raw_from_payload(
            spec,
            title='restricted_release_event',
            text=summary,
            published_at=str(restricted.get('release_date') or '') or None,
            layer=spec.layer,
            payload={'restricted_release': restricted, 'code': code, 'name': name},
        )]

    def normalize(self, raw, *, code='', name='', context=None):
        restricted = raw.raw.get('restricted_release') if isinstance(raw.raw.get('restricted_release'), dict) else {}
        ratio = _num(restricted.get('ratio'))
        item = _normalized(
            raw,
            code,
            name,
            evidence_type='event_calendar',
            direction='bearish' if ratio is not None and ratio >= 5 else 'neutral',
            confidence=4,
            time_windows=['short', 'swing'],
        )
        item.relation_targets = ['shareholder']
        return [item]


class RssFeedCollector(Collector):
    source_id = ''

    def fetch(self, spec, *, code='', name='', context=None):
        sectors = _subject_sectors(context)
        entries = _fetch_rss_entries(spec.url, limit=30)
        raw_items: list[RawIntelItem] = []
        for data in entries:
            title = str(data.get('title') or '').strip()
            text = str(data.get('summary') or title).strip()
            if not title or _contains_forbidden(title, text):
                continue
            reason = _subject_match_reason(title + ' ' + text, code, name, sectors)
            if not reason:
                continue
            raw_items.append(_raw_from_payload(
                spec,
                title=title,
                text=text,
                published_at=str(data.get('published_at') or '') or None,
                url=str(data.get('url') or ''),
                layer=_layer_from_text(title) or _layer_from_text(title, text) or spec.layer,
                payload={'entry': data, 'match_reason': reason, 'code': code, 'name': name, 'sectors': sectors},
            ))
        return raw_items

    def normalize(self, raw, *, code='', name='', context=None):
        item = _normalized(
            raw,
            code,
            name,
            evidence_type='news',
            direction='neutral',
            confidence=4,
            time_windows=['immediate', 'short'] if raw.layer == 'news_event' else ['short', 'swing', 'mid'],
        )
        item.id = _normalized_item_id(item.evidence_type, raw)
        item.raw_ref = str(raw.raw.get('match_reason') or '')
        return [item]


class EastmoneyKuaixunCollector(Collector):
    source_id = 'requests:eastmoney_kuaixun'

    def fetch(self, spec, *, code='', name='', context=None):
        items = _value(context, 'eastmoney_kuaixun') or _value(context, 'requests_eastmoney_kuaixun')
        if items is None:
            api_error: Exception | None = None
            try:
                from core.intel_fetcher import _fetch_eastmoney_kuaixun
                items = _fetch_eastmoney_kuaixun(20)
            except Exception as exc:
                api_error = exc
                items = _fetch_static_eastmoney_titles(spec.url or 'https://kuaixun.eastmoney.com/', limit=20)
            if not items and api_error is not None:
                raise api_error
        sectors = [str(value).strip() for value in (_root(context).get('sectors') or []) if str(value).strip()]
        raw_items: list[RawIntelItem] = []
        for item in list(items or []):
            data = dict(item or {}) if isinstance(item, dict) else {'title': str(item)}
            title = str(data.get('title') or data.get('content') or '').strip()
            text = str(data.get('content') or title).strip()
            if not title or _contains_forbidden(title, text):
                continue
            if not _is_relevant_to_subject(title + ' ' + text, code, name, sectors):
                continue
            raw_items.append(_raw_from_payload(
                spec,
                title=title,
                text=text,
                published_at=str(data.get('date') or data.get('published_at') or '') or None,
                url=str(data.get('url') or ''),
                layer=_layer_from_text(title, text) or spec.layer,
                payload={'news': data, 'code': code, 'name': name, 'sectors': sectors},
            ))
        return raw_items

    def normalize(self, raw, *, code='', name='', context=None):
        item = _normalized(
            raw,
            code,
            name,
            evidence_type='news',
            direction='neutral',
            confidence=4,
            time_windows=['immediate', 'short'] if raw.layer == 'news_event' else ['short', 'swing', 'mid'],
        )
        item.id = _normalized_item_id(item.evidence_type, raw)
        return [item]


class ReutersBusinessRssCollector(RssFeedCollector):
    source_id = 'rss:reuters_business'


class CnbcFinanceRssCollector(RssFeedCollector):
    source_id = 'rss:cnbc_finance'


class StaticPublicPageCollector(Collector):
    source_id = ''

    def fetch(self, spec, *, code='', name='', context=None):
        sectors = _subject_sectors(context)
        entries = _fetch_static_page_titles(spec.url, limit=30)
        raw_items: list[RawIntelItem] = []
        for data in entries:
            title = str(data.get('title') or '').strip()
            text = str(data.get('summary') or title).strip()
            if not title or _contains_forbidden(title, text):
                continue
            reason = _subject_match_reason(title + ' ' + text, code, name, sectors)
            if not reason:
                continue
            layer = spec.layer if spec.layer == 'policy' else (_layer_from_text(title, text) or spec.layer)
            raw_items.append(_raw_from_payload(
                spec,
                title=title,
                text=text,
                published_at=str(data.get('published_at') or '') or None,
                url=str(data.get('url') or ''),
                layer=layer,
                payload={'entry': data, 'match_reason': reason, 'code': code, 'name': name, 'sectors': sectors},
            ))
        return raw_items

    def normalize(self, raw, *, code='', name='', context=None):
        item = _normalized(
            raw,
            code,
            name,
            evidence_type='news',
            direction='neutral',
            confidence=5 if raw.trust_level in {'official', 'primary'} else 4,
            time_windows=['short', 'swing', 'mid'],
        )
        item.id = _normalized_item_id(item.evidence_type, raw)
        item.raw_ref = str(raw.raw.get('match_reason') or '')
        return [item]


class ScraplingStaticPageCollector(StaticPublicPageCollector):
    source_id = ''

    def fetch(self, spec, *, code='', name='', context=None):
        if not spec.url:
            raise RuntimeError('scrapling source url is required')
        from core.intelligence import scrapling_runtime

        payload = scrapling_runtime.run_scrapling_fetch(
            spec.url,
            mode='static',
            timeout_seconds=25,
        )
        if not payload.get('ok'):
            raise RuntimeError(str(payload.get('error') or 'scrapling fetch failed'))

        sectors = _subject_sectors(context)
        raw_items: list[RawIntelItem] = []
        entries = payload.get('items') if isinstance(payload.get('items'), list) else []
        for entry in entries:
            data = dict(entry or {}) if isinstance(entry, dict) else {'title': str(entry)}
            title = str(data.get('title') or '').strip()
            text = str(data.get('summary') or title).strip()
            if not title or _contains_forbidden(title, text):
                continue
            reason = _subject_match_reason(title + ' ' + text, code, name, sectors)
            if not reason:
                continue
            layer = spec.layer if spec.layer == 'policy' else (_layer_from_text(title, text) or spec.layer)
            raw_items.append(_raw_from_payload(
                spec,
                title=title,
                text=text,
                published_at=str(data.get('date') or data.get('published_at') or '') or None,
                url=str(data.get('url') or ''),
                layer=layer,
                payload={'entry': data, 'match_reason': reason, 'code': code, 'name': name, 'sectors': sectors},
            ))

        if raw_items:
            return raw_items

        page_title = str(payload.get('title') or '').strip()
        page_text = str(payload.get('text') or '').strip()
        if page_title and page_text and not _contains_forbidden(page_title, page_text):
            reason = _subject_match_reason(page_title + ' ' + page_text, code, name, sectors)
            if reason:
                raw_items.append(_raw_from_payload(
                    spec,
                    title=page_title,
                    text=page_text[:1000],
                    published_at=None,
                    url=str(payload.get('url') or spec.url),
                    layer=spec.layer,
                    payload={'entry': payload, 'match_reason': reason, 'code': code, 'name': name, 'sectors': sectors},
                ))
        return raw_items


class CompanyAnnouncementsCollector(StaticPublicPageCollector):
    source_id = 'requests:company_announcements'

    def fetch(self, spec, *, code='', name='', context=None):
        raw_items: list[RawIntelItem] = []
        pdf_options = _disclosure_pdf_options(context)
        periodic_options = _periodic_report_pdf_options(context)
        pdf_attempts = 0
        parsed_pdf_urls: set[str] = set()
        periodic_candidates: list[tuple[RawIntelItem, dict[str, Any]]] = []
        periodic_status_item = _periodic_report_status_item(spec, code, name) if periodic_options['enabled'] else None
        seen_entries: set[str] = set()
        sectors = _subject_sectors(context)
        queries = _queries_for_source(self.source_id, code, name, sectors, context, fallback=(name or code,))

        def scan_entries(query: str) -> None:
            nonlocal pdf_attempts
            for data in _fetch_cninfo_announcements(query, limit=18):
                if _clean_code(data.get('secCode')) and _clean_code(data.get('secCode')) != _clean_code(code):
                    continue
                entry_key = _periodic_entry_key(data)
                if entry_key in seen_entries:
                    continue
                seen_entries.add(entry_key)
                title = str(data.get('title') or '').strip()
                text = str(data.get('summary') or title).strip()
                if not title or _contains_forbidden(title, text):
                    continue

                if periodic_options['enabled']:
                    skip_reason = _periodic_report_candidate_skip_reason(data)
                    if skip_reason:
                        _record_periodic_report_status(
                            periodic_status_item,
                            skip_reason,
                            url=str(data.get('url') or ''),
                            title=title,
                        )
                    else:
                        periodic_candidates.append((
                            _raw_periodic_candidate_item(spec, data, query, code, name),
                            data,
                        ))

                if _is_noise_announcement(title):
                    continue

                layer = _external_layer(title, text)
                raw_item: RawIntelItem | None = None
                if layer in {'order_contract', 'capacity', 'risk', 'financial', 'customer_supplier', 'policy'}:
                    reason, matched = _subject_relevance(title + ' ' + text + ' ' + query, code, name, sectors)
                    if not reason:
                        reason = f'name:{name or code}'
                        matched = [value for value in (name, code) if value]
                    raw_item = _raw_from_payload(
                        spec,
                        title=title,
                        text=text,
                        published_at=str(data.get('published_at') or '') or None,
                        url=str(data.get('url') or ''),
                        layer=layer,
                        payload={
                            'entry': data,
                            'match_reason': reason,
                            'matched_keywords': matched,
                            'category': 'company_announcement',
                            'query': query,
                            'code': code,
                            'name': name,
                        },
                    )

                if raw_item is None:
                    continue

                pdf_body_items: list[RawIntelItem] = []
                if pdf_options['enabled']:
                    if pdf_attempts >= pdf_options['max_pdf_per_source']:
                        _record_pdf_body_status(raw_item, 'skipped_limit', url=str(data.get('url') or ''))
                    elif str(data.get('url') or '') in parsed_pdf_urls:
                        _record_pdf_body_status(raw_item, 'skipped_duplicate', url=str(data.get('url') or ''))
                    elif not _is_cninfo_pdf_url(data.get('url')):
                        _record_pdf_body_status(raw_item, 'skipped_non_cninfo_pdf', url=str(data.get('url') or ''))
                    elif (
                        pdf_options['only_parse_when_title_matches']
                        and not _announcement_pdf_title_matches(title, text)
                    ):
                        _record_pdf_body_status(raw_item, 'skipped_title_mismatch', url=str(data.get('url') or ''))
                    else:
                        pdf_attempts += 1
                        parsed_pdf_urls.add(str(data.get('url') or ''))
                        pdf_body_items = _announcement_pdf_body_items(
                            spec,
                            raw_item,
                            data,
                            pdf_options,
                            code=code,
                            name=name,
                        )
                raw_items.extend(_expand_external_public_item(raw_item))
                raw_items.extend(pdf_body_items)

        for query in queries:
            scan_entries(query)

        best_periodic_priority = min(
            (_periodic_report_priority(str(data.get('title') or '')) for _item, data in periodic_candidates),
            default=99,
        )
        if periodic_options['enabled'] and best_periodic_priority > 0:
            for query in _periodic_report_queries(code, name):
                scan_entries(query)

        if periodic_options['enabled']:
            selected = _select_periodic_report_candidates(
                periodic_candidates,
                limit=periodic_options['max_periodic_pdf_per_source'],
            )
            periodic_selected_items: list[RawIntelItem] = []
            selected_keys = {_periodic_entry_key(data) for _item, data in selected}
            for candidate_item, data in periodic_candidates:
                if _periodic_entry_key(data) not in selected_keys:
                    _record_periodic_report_status(
                        periodic_status_item,
                        'skipped_limit',
                        url=str(data.get('url') or ''),
                        title=str(data.get('title') or ''),
                        candidate=True,
                    )
                    continue
                body_items = _periodic_report_body_items(
                    spec,
                    candidate_item,
                    data,
                    periodic_options,
                    code=code,
                    name=name,
                )
                periodic_selected_items.extend(body_items)
                periodic_selected_items.extend(_expand_external_public_item(candidate_item))
            if periodic_selected_items:
                raw_items = periodic_selected_items + raw_items

        if periodic_status_item is not None and periodic_status_item.raw.get('periodic_report_status'):
            raw_items.insert(0, periodic_status_item)
        return _dedupe_raw_items(raw_items, limit=_company_announcements_raw_limit(periodic_options))

    def normalize(self, raw, *, code='', name='', context=None):
        if raw.raw.get('category') == 'periodic_report_status':
            return []
        if raw.raw.get('category') == 'announcement_pdf_body':
            item = _normalized(
                raw,
                code,
                name,
                evidence_type='announcement_pdf',
                direction='neutral',
                confidence=_int_between(raw.raw.get('confidence'), 7, 1, 10),
                time_windows=['short', 'swing', 'mid'],
            )
            item.id = _normalized_item_id(item.evidence_type, raw)
            item.raw_ref = str(raw.raw.get('match_reason') or '')
            item.source_url = str(raw.raw.get('pdf_url') or raw.source_url or raw.url)
            return [item]
        if raw.raw.get('category') == 'periodic_report_body':
            item = _normalized(
                raw,
                code,
                name,
                evidence_type='periodic_report',
                direction='neutral',
                confidence=_int_between(raw.raw.get('confidence'), 7, 1, 10),
                time_windows=['swing', 'mid', 'long'],
            )
            item.id = _normalized_item_id(item.evidence_type, raw)
            item.raw_ref = str(raw.raw.get('match_reason') or '')
            item.source_url = str(raw.raw.get('pdf_url') or raw.source_url or raw.url)
            item.metadata = _periodic_report_body_metadata(raw)
            return [item]
        return super().normalize(raw, code=code, name=name, context=context)


class PolicyPagesCollector(StaticPublicPageCollector):
    source_id = 'requests:policy_pages'

    def fetch(self, spec, *, code='', name='', context=None):
        sectors = _subject_sectors(context)
        raw_items: list[RawIntelItem] = []
        for query in _queries_for_source(self.source_id, code, name, sectors, context, fallback=_policy_queries(code, name, sectors)):
            try:
                entries = _fetch_miit_search(query, limit=5)
            except Exception:
                entries = []
            for data in entries:
                title = str(data.get('title') or '').strip()
                text = str(data.get('summary') or title).strip()
                if not title or _contains_forbidden(title, text):
                    continue
                source_layer = _external_layer(title, text)
                if source_layer == 'cost':
                    continue
                if source_layer not in {'policy', 'news_event'} and not _is_policy_text(title, text):
                    continue
                reason, matched = _subject_relevance(title + ' ' + text + ' ' + query, code, name, sectors)
                if not reason:
                    continue
                raw_items.append(_raw_from_payload(
                    spec,
                    title=title,
                    text=text,
                    published_at=str(data.get('published_at') or '') or None,
                    url=str(data.get('url') or ''),
                    layer='policy',
                    payload={
                        'entry': data,
                        'match_reason': reason,
                        'matched_keywords': matched,
                        'category': 'policy',
                        'query': query,
                        'code': code,
                        'name': name,
                    },
                ))
        return _dedupe_raw_items(raw_items, limit=8)


class IndustryChainPagesCollector(StaticPublicPageCollector):
    source_id = 'requests:industry_chain_pages'

    def fetch(self, spec, *, code='', name='', context=None):
        sectors = _subject_sectors(context)
        raw_items: list[RawIntelItem] = []
        for query in _queries_for_source(self.source_id, code, name, sectors, context, fallback=_industry_queries(code, name, sectors)):
            try:
                cninfo_entries = _fetch_cninfo_announcements(query, limit=5)
            except Exception:
                cninfo_entries = []
            for data in cninfo_entries:
                item = _raw_external_chain_item(spec, data, query, code, name, sectors, source='cninfo_industry')
                if item is not None:
                    raw_items.extend(_expand_external_public_item(item))
            try:
                miit_entries = _fetch_miit_search(query, limit=3)
            except Exception:
                miit_entries = []
            for data in miit_entries:
                item = _raw_external_chain_item(spec, data, query, code, name, sectors, source='miit_industry')
                if item is not None:
                    raw_items.extend(_expand_external_public_item(item))
        return _dedupe_raw_items(raw_items, limit=10)


class ScraplingPolicyStaticDemoCollector(ScraplingStaticPageCollector):
    source_id = 'scrapling:policy_static_demo'


class ScraplingAnnouncementStaticDemoCollector(ScraplingStaticPageCollector):
    source_id = 'scrapling:announcement_static_demo'


class ScraplingIndustryPriceStaticDemoCollector(ScraplingStaticPageCollector):
    source_id = 'scrapling:industry_price_static_demo'


_BUILTIN_FACTORIES: dict[str, Callable[[], Collector]] = {
    ExistingIntelFeedCollector.source_id: ExistingIntelFeedCollector,
    StockNewsCollector.source_id: StockNewsCollector,
    FundamentalsCollector.source_id: FundamentalsCollector,
    TradingBehaviorCollector.source_id: TradingBehaviorCollector,
    MoneyFlowCollector.source_id: MoneyFlowCollector,
    ThinLayerContextCollector.source_id: ThinLayerContextCollector,
    RestrictedReleaseCollector.source_id: RestrictedReleaseCollector,
    MarketContextCollector.source_id: MarketContextCollector,
    EventCalendarCollector.source_id: EventCalendarCollector,
    EastmoneyKuaixunCollector.source_id: EastmoneyKuaixunCollector,
    ReutersBusinessRssCollector.source_id: ReutersBusinessRssCollector,
    CnbcFinanceRssCollector.source_id: CnbcFinanceRssCollector,
    CompanyAnnouncementsCollector.source_id: CompanyAnnouncementsCollector,
    PolicyPagesCollector.source_id: PolicyPagesCollector,
    IndustryChainPagesCollector.source_id: IndustryChainPagesCollector,
    ScraplingPolicyStaticDemoCollector.source_id: ScraplingPolicyStaticDemoCollector,
    ScraplingAnnouncementStaticDemoCollector.source_id: ScraplingAnnouncementStaticDemoCollector,
    ScraplingIndustryPriceStaticDemoCollector.source_id: ScraplingIndustryPriceStaticDemoCollector,
}


def get_builtin_collectors(specs: list[SourceSpec] | tuple[SourceSpec, ...] | None = None) -> list[Collector]:
    selected = specs or get_enabled_sources()
    return [
        _BUILTIN_FACTORIES[spec.source_id]()
        for spec in selected
        if spec.source_id in _BUILTIN_FACTORIES
    ]


def _collection_context(
    context: dict[str, Any] | None,
    route_profile: RouteProfile | None,
    collection_plan: CollectionPlan | None,
) -> dict[str, Any] | None:
    if route_profile is None and collection_plan is None:
        return context
    root = dict(context or {})
    if route_profile is not None:
        root['_route_profile'] = route_profile
        if not root.get('sectors'):
            root['sectors'] = list(route_profile.sectors)
    if collection_plan is not None:
        root['_collection_plan'] = collection_plan
    return root


def _skipped_plan_status(collection_plan: CollectionPlan | None) -> dict[str, dict[str, Any]]:
    if collection_plan is None:
        return {}
    status: dict[str, dict[str, Any]] = {}
    for source in collection_plan.skipped_sources:
        status[source.source_id] = {
            'field': source.field,
            'fields': list(collection_plan.fields_by_source.get(source.source_id, (source.field,))),
            'layer': source.layer,
            'method': source.method,
            'form': source.form,
            'trust_level': source.trust_level,
            'parser': source.parser,
            'enabled': source.enabled_by_default,
            'ttl_hours': source.ttl_hours,
            'access_mode': source.access_mode,
            'requires_api_key': source.requires_api_key,
            'credential_env': source.credential_env,
            'credential_aliases': list(source.credential_aliases),
            'credential_envs': list(credential_env_names(source)),
            'supports_pdf_body': source.supports_pdf_body,
            'status': 'skipped_missing_credentials',
            'raw_count': 0,
            'normalized_count': 0,
            'last_error': 'missing_credentials',
            'missing_reason': 'missing_credentials',
            'last_attempt_at': datetime.now().isoformat(timespec='seconds'),
        }
    return status


def collect_builtin_sources(
    code: str,
    name: str,
    *,
    context: dict[str, Any] | None = None,
    specs: list[SourceSpec] | tuple[SourceSpec, ...] | None = None,
    route_profile: RouteProfile | None = None,
    collection_plan: CollectionPlan | None = None,
    plan_mode: str = 'efficiency',
    use_cache: bool = True,
) -> dict[str, Any]:
    if collection_plan is None and specs is None:
        route_profile = route_profile or build_route_profile(code, name, context=context)
        collection_plan = build_collection_plan(route_profile, get_source_catalog(), mode=plan_mode)
    elif collection_plan is not None:
        route_profile = collection_plan.route_profile

    selected = list(collection_plan.source_specs if collection_plan is not None else (specs or get_enabled_sources()))
    effective_context = _collection_context(context, route_profile, collection_plan)
    source_status = cache_store.load_source_status() if use_cache else {}
    source_status.update(_skipped_plan_status(collection_plan))
    results: list[CollectorResult] = []
    raw_items: list[RawIntelItem] = []
    normalized_items: list[NormalizedIntelItem] = []

    for spec in selected:
        factory = _BUILTIN_FACTORIES.get(spec.source_id)
        if factory is None:
            source_status[spec.source_id] = _status_entry(
                spec,
                status='skipped_not_implemented',
                raw_count=0,
                normalized_count=0,
                error='collector_not_implemented',
            )
            continue
        result = run_collector(factory(), spec, code=code, name=name, context=effective_context)
        results.append(result)
        raw_items.extend(result.raw_items)
        normalized_items.extend(result.normalized_items)
        status = 'ok' if result.ok else 'error'
        status_payload = _status_entry(
            spec,
            status=status,
            raw_count=len(result.raw_items),
            normalized_count=len(result.normalized_items),
            error=result.error,
        )
        pdf_body_status = _pdf_body_status_summary(result.raw_items)
        periodic_report_status = _periodic_report_status_summary(result.raw_items)
        if (
            not periodic_report_status
            and spec.source_id == CompanyAnnouncementsCollector.source_id
            and _periodic_report_pdf_options(effective_context)['enabled']
        ):
            periodic_report_status = _empty_periodic_report_status()
        if pdf_body_status:
            status_payload['pdf_body'] = pdf_body_status
        if periodic_report_status:
            status_payload['periodic_report'] = periodic_report_status
        source_status[spec.source_id] = status_payload
        if use_cache:
            if result.ok:
                cache_store.save_normalized(spec.source_id, result.normalized_items)
            persisted = cache_store.update_source_status(
                spec.source_id,
                result.ok,
                len(result.normalized_items),
                error=result.error,
            )
            source_status[spec.source_id].update(persisted)
            source_status[spec.source_id]['raw_count'] = len(result.raw_items)
            source_status[spec.source_id]['normalized_count'] = len(result.normalized_items)
            if pdf_body_status:
                source_status[spec.source_id]['pdf_body'] = pdf_body_status
            else:
                source_status[spec.source_id].pop('pdf_body', None)
            if periodic_report_status:
                source_status[spec.source_id]['periodic_report'] = periodic_report_status
            else:
                source_status[spec.source_id].pop('periodic_report', None)

    normalized_items = _dedupe_items(normalized_items)
    field_summary_items, field_summary_status = build_periodic_field_summary_items(
        normalized_items,
        missing_reasons=_periodic_report_missing_reasons(source_status),
    )
    normalized_items = _dedupe_items(normalized_items + field_summary_items)
    _apply_periodic_field_summary_status(source_status, field_summary_status)
    coverage_summary = _coverage_summary(normalized_items, source_status)
    if use_cache:
        merged = cache_store.load_source_status()
        merged.update(source_status)
        cache_store.save_source_status(merged)
        source_status = merged
    return {
        'results': results,
        'raw_items': raw_items,
        'normalized_items': normalized_items,
        'source_status': source_status,
        'coverage_summary': coverage_summary,
        'route_profile': route_profile.to_dict() if route_profile is not None else {},
        'collection_plan': collection_plan.to_dict() if collection_plan is not None else {},
    }


def _normalize_seed(seed: dict[str, Any], source_id: str) -> list[NormalizedIntelItem]:
    return [item for item in normalized_items_from_seed(seed) if item.source_id == source_id]


def _normalized(
    raw: RawIntelItem,
    code: str,
    name: str,
    *,
    evidence_type: str,
    direction: str,
    confidence: int,
    time_windows: list[str],
) -> NormalizedIntelItem:
    matched = raw.raw.get('matched_keywords') if isinstance(raw.raw.get('matched_keywords'), list) else []
    relevance_reason = str(raw.raw.get('match_reason') or '')
    category = str(raw.raw.get('category') or raw.layer or evidence_type)
    return NormalizedIntelItem(
        id=f'{evidence_type}:{_safe_id(raw.source_id)}:{_safe_id(code or name or raw.title)}',
        source_id=raw.source_id,
        title=raw.title,
        summary=raw.text[:260],
        layer=raw.layer,
        direction=direction,
        related_codes=[_clean_code(code)] if _clean_code(code) else [],
        related_names=[name] if name else [],
        related_sectors=[],
        evidence_type=evidence_type,
        published_at=raw.published_at,
        fetched_at=raw.fetched_at,
        url=raw.url,
        source_url=raw.source_url,
        trust_level=raw.trust_level,
        confidence=confidence,
        category=category,
        relevance_reason=relevance_reason,
        matched_keywords=[str(value).strip() for value in matched if str(value).strip()],
        time_windows=list(time_windows),
        raw_ref=relevance_reason,
    )


def _normalized_item_id(evidence_type: str, raw: RawIntelItem) -> str:
    if isinstance(raw.raw, dict) and raw.raw.get('category') == 'periodic_report_body':
        field = str(raw.raw.get('field') or raw.layer or '')
        identity = str(raw.raw.get('match_reason') or raw.url or raw.title or raw.fetched_at)
        return f'{evidence_type}:{_safe_id(raw.source_id)}:{_safe_id(field)}:{_safe_id(identity)}'
    identity = raw.url or raw.title or raw.published_at or raw.fetched_at
    return f'{evidence_type}:{_safe_id(raw.source_id)}:{_safe_id(raw.layer)}:{_safe_id(identity)}'


def _periodic_report_body_metadata(raw: RawIntelItem) -> dict[str, Any]:
    payload = raw.raw if isinstance(raw.raw, dict) else {}
    return {
        'field': str(payload.get('field') or ''),
        'source_title': str(payload.get('source_title') or raw.title or ''),
        'source_period': str(raw.published_at or ''),
        'report_type': str(payload.get('report_type') or ''),
        'section': str(payload.get('section') or ''),
        'page': int(payload.get('page') or 0),
        'table_header': list(payload.get('table_header') or []),
        'table_quality_score': float(payload.get('table_quality_score') or 0.0),
        'table_values': dict(payload.get('table_values') or {}),
        'table_row_index': int(payload.get('table_row_index') or 0),
        'pdf_url': str(payload.get('pdf_url') or raw.source_url or raw.url or ''),
    }


def _raw_from_payload(
    spec: SourceSpec,
    *,
    title: str,
    text: str,
    published_at: str | None,
    layer: str,
    payload: dict[str, Any],
    url: str = '',
) -> RawIntelItem:
    return RawIntelItem(
        source_id=spec.source_id,
        source_name=spec.name,
        source_url=spec.url,
        fetched_at=datetime.now().isoformat(timespec='seconds'),
        published_at=published_at,
        title=str(title or '').strip(),
        text=str(text or title or '').strip(),
        url=url,
        layer=layer,
        trust_level=spec.trust_level,
        raw=payload,
    )


def _disclosure_pdf_options(context: dict[str, Any] | None) -> dict[str, Any]:
    root = _root(context)
    plan = root.get('_collection_plan')
    mode = str(getattr(plan, 'mode', 'efficiency') or 'efficiency')
    plan_parse = bool(getattr(plan, 'parse_disclosure_pdf', False))
    enabled = _bool_option(_context_option(context, 'parse_disclosure_pdf', plan_parse), False)

    explicit_max = _context_option(context, 'max_pdf_per_source', None)
    if explicit_max is None:
        if plan_parse:
            explicit_max = getattr(plan, 'max_pdf_per_source', None)
        elif enabled:
            explicit_max = 3 if mode == 'performance' else 1
        else:
            explicit_max = 0

    return {
        'enabled': enabled,
        'max_pdf_per_source': _int_between(explicit_max, 0, 0, 10),
        'timeout_seconds': _int_between(
            _context_option(context, 'pdf_timeout_seconds', getattr(plan, 'pdf_timeout_seconds', 8)),
            8,
            1,
            30,
        ),
        'max_text_chars': _int_between(
            _context_option(context, 'max_pdf_text_chars', getattr(plan, 'max_pdf_text_chars', 6000)),
            6000,
            1000,
            100000,
        ),
        'only_parse_when_title_matches': _bool_option(
            _context_option(
                context,
                'only_parse_when_title_matches',
                getattr(plan, 'only_parse_when_title_matches', True),
            ),
            True,
        ),
    }


def _periodic_report_pdf_options(context: dict[str, Any] | None) -> dict[str, Any]:
    root = _root(context)
    plan = root.get('_collection_plan')
    plan_parse = bool(getattr(plan, 'parse_periodic_report_pdf', False))
    enabled = _bool_option(_context_option(context, 'parse_periodic_report_pdf', plan_parse), False)

    explicit_max = _context_option(context, 'max_periodic_pdf_per_source', None)
    if explicit_max is None:
        explicit_max = getattr(plan, 'max_periodic_pdf_per_source', None) if plan_parse else (1 if enabled else 0)

    return {
        'enabled': enabled,
        'max_periodic_pdf_per_source': _int_between(explicit_max, 0, 0, 1),
        'timeout_seconds': _int_between(
            _context_option(context, 'periodic_pdf_timeout_seconds', getattr(plan, 'periodic_pdf_timeout_seconds', 8)),
            8,
            1,
            30,
        ),
        'max_text_chars': _int_between(
            _context_option(context, 'max_periodic_text_chars', getattr(plan, 'max_periodic_text_chars', 30000)),
            30000,
            1000,
            200000,
        ),
    }


def _company_announcements_raw_limit(periodic_options: dict[str, Any]) -> int:
    if not periodic_options.get('enabled'):
        return 24
    periodic_limit = _int_between(periodic_options.get('max_periodic_pdf_per_source'), 1, 1, 3)
    return 24 + (32 * periodic_limit)


def _context_option(context: dict[str, Any] | None, key: str, default: Any) -> Any:
    root = _root(context)
    if key in root:
        return root.get(key)
    inner = root.get('context') if isinstance(root.get('context'), dict) else {}
    if key in inner:
        return inner.get(key)
    return default


def _bool_option(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {'1', 'true', 'yes', 'on'}:
            return True
        if text in {'0', 'false', 'no', 'off'}:
            return False
        return default
    return bool(value)


def _is_cninfo_pdf_url(value: Any) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    parsed = urlparse(text)
    return (
        parsed.scheme in {'http', 'https'}
        and parsed.netloc.lower().endswith('cninfo.com.cn')
        and parsed.path.lower().endswith('.pdf')
    )


def _announcement_pdf_title_matches(title: str, text: str) -> bool:
    if _is_pdf_body_noise_title(title):
        return False
    layers = {_external_layer(title, text)}
    layers.update(_secondary_external_layers(title, text))
    return bool(layers & _ANNOUNCEMENT_PDF_BODY_LAYERS)


def _is_pdf_body_noise_title(title: str) -> bool:
    text = str(title or '')
    noise_terms = (
        '\u5957\u671f\u4fdd\u503c',
        '\u5916\u6c47\u5957\u671f',
        '\u884d\u751f\u54c1',
        '\u5916\u6c47\u98ce\u9669\u7ba1\u7406',
        '\u91d1\u878d\u884d\u751f',
    )
    return any(term in text for term in noise_terms)


def _periodic_report_queries(code: str, name: str) -> list[str]:
    clean_code = _clean_code(code)
    clean_name = str(name or '').strip()
    values = [
        f'{clean_code} \u5e74\u5ea6\u62a5\u544a',
        f'{clean_code} \u534a\u5e74\u5ea6\u62a5\u544a',
        f'{clean_code} \u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a',
        f'{clean_code} \u7b2c\u4e09\u5b63\u5ea6\u62a5\u544a',
    ]
    if clean_name:
        values.extend([
            f'{clean_name} \u5e74\u5ea6\u62a5\u544a',
            f'{clean_name} \u534a\u5e74\u5ea6\u62a5\u544a',
        ])
    return _unique_texts(values, limit=6)


def _periodic_report_status_item(spec: SourceSpec, code: str, name: str) -> RawIntelItem:
    return RawIntelItem(
        source_id=spec.source_id,
        source_name=spec.name,
        source_url=spec.url,
        fetched_at=datetime.now().isoformat(timespec='seconds'),
        published_at=None,
        title='periodic report candidate discovery',
        text='periodic report candidate discovery status',
        url=f'status:periodic-report:{_clean_code(code) or _safe_id(name)}',
        layer='company',
        trust_level=spec.trust_level,
        raw={
            'category': 'periodic_report_status',
            'code': code,
            'name': name,
            'periodic_report_status': [],
        },
    )


def _raw_periodic_candidate_item(
    spec: SourceSpec,
    data: dict[str, Any],
    query: str,
    code: str,
    name: str,
) -> RawIntelItem:
    title = str(data.get('title') or '').strip()
    return _raw_from_payload(
        spec,
        title=title,
        text=str(data.get('summary') or title).strip(),
        published_at=str(data.get('published_at') or '') or None,
        url=str(data.get('url') or ''),
        layer='financial',
        payload={
            'entry': data,
            'match_reason': f'periodic_report_candidate:{_clean_code(code) or name}',
            'matched_keywords': [value for value in (_clean_code(code), name) if value],
            'category': 'company_announcement',
            'query': query,
            'code': code,
            'name': name,
        },
    )


def _periodic_report_candidate_skip_reason(data: dict[str, Any]) -> str:
    title = str(data.get('title') or data.get('summary') or '').strip()
    if not _is_cninfo_pdf_url(data.get('url')):
        return 'skipped_non_cninfo_pdf'
    if any(term in title for term in _PERIODIC_REPORT_SUMMARY_TERMS):
        return 'skipped_summary'
    if any(term in title for term in _PERIODIC_REPORT_CORRECTION_TERMS):
        return 'skipped_correction'
    if any(term in title for term in _PERIODIC_REPORT_ENGLISH_TERMS):
        return 'skipped_english'
    if '\u72ec\u7acb\u8463\u4e8b' in title and '\u610f\u89c1' in title:
        return 'skipped_not_periodic'
    if any(term in title for term in _PERIODIC_REPORT_NOT_PERIODIC_TERMS):
        return 'skipped_not_periodic'
    if not _periodic_report_title_matches(title):
        return 'skipped_not_periodic'
    return ''


def _select_periodic_report_candidates(
    candidates: list[tuple[RawIntelItem, dict[str, Any]]],
    *,
    limit: int,
) -> list[tuple[RawIntelItem, dict[str, Any]]]:
    if int(limit or 0) <= 0:
        return []
    ordered = sorted(candidates, key=lambda item: _periodic_candidate_sort_key(item[1]))
    return ordered[:int(limit)]


def _periodic_candidate_sort_key(data: dict[str, Any]) -> tuple[int, int, str]:
    title = str(data.get('title') or '')
    return (
        _periodic_report_priority(title),
        -_date_sort_value(data.get('published_at')),
        title,
    )


def _periodic_report_priority(title: str) -> int:
    text = str(title or '')
    if '\u5e74\u5ea6\u62a5\u544a' in text or '\u5e74\u62a5' in text:
        if '\u534a\u5e74' not in text:
            return 0
    if '\u534a\u5e74\u5ea6\u62a5\u544a' in text or '\u534a\u5e74\u62a5' in text or '\u4e2d\u62a5' in text:
        return 1
    return 2


def _date_sort_value(value: Any) -> int:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return int(digits[:8]) if len(digits) >= 8 else 0


def _periodic_entry_key(data: dict[str, Any]) -> str:
    url = str(data.get('url') or '').strip().lower()
    if url:
        return url
    return '|'.join(str(data.get(key) or '').strip() for key in ('secCode', 'title', 'published_at'))


def _announcement_pdf_body_items(
    spec: SourceSpec,
    title_item: RawIntelItem,
    data: dict[str, Any],
    options: dict[str, Any],
    *,
    code: str,
    name: str,
) -> list[RawIntelItem]:
    from core.intelligence import disclosure_pdf
    from core.intelligence.disclosure_parser import parse_announcement_pdf_text

    pdf_url = str(data.get('url') or title_item.url or '').strip()
    cache_path = disclosure_pdf.build_pdf_cache_path(
        pdf_url,
        code=_clean_code(code),
        published_at=title_item.published_at,
    )
    try:
        downloaded = disclosure_pdf.download_pdf(
            pdf_url,
            cache_path,
            timeout_seconds=int(options.get('timeout_seconds') or 8),
        )
    except Exception as exc:
        _record_pdf_body_status(title_item, 'download_error', url=pdf_url, error=str(exc), attempted=True)
        return []

    if downloaded.status not in {'ok', 'cached'}:
        _record_pdf_body_status(
            title_item,
            downloaded.status or 'download_error',
            url=pdf_url,
            error=downloaded.error,
            attempted=True,
        )
        return []

    try:
        extracted = disclosure_pdf.extract_pdf_text(downloaded.cache_path)
    except Exception as exc:
        _record_pdf_body_status(title_item, 'extract_error', url=pdf_url, error=str(exc), attempted=True)
        return []

    if extracted.status != 'ok':
        _record_pdf_body_status(
            title_item,
            extracted.status or 'extract_error',
            url=pdf_url,
            error=extracted.error,
            attempted=True,
        )
        return []

    text = str(extracted.text or '')[:int(options.get('max_text_chars') or 6000)]
    evidence = parse_announcement_pdf_text(
        text,
        title='',
        source_url=pdf_url,
        published_at=title_item.published_at,
        code=code,
        name=name,
    )
    if not evidence:
        _record_pdf_body_status(title_item, 'no_evidence', url=pdf_url, attempted=True)
        return []

    items = [
        _raw_from_disclosure_evidence(spec, title_item, item, data, pdf_url, code=code, name=name)
        for item in evidence
    ]
    _record_pdf_body_status(
        title_item,
        'ok',
        url=pdf_url,
        attempted=True,
        generated_count=len(items),
        page_count=extracted.page_count,
    )
    return items


def _periodic_report_title_matches(title: str) -> bool:
    text = str(title or '').strip()
    if not text:
        return False
    if any(term in text for term in _PERIODIC_REPORT_EXCLUDE_TERMS):
        return False
    return any(term in text for term in _PERIODIC_REPORT_TITLE_TERMS)


def _periodic_report_body_items(
    spec: SourceSpec,
    title_item: RawIntelItem,
    data: dict[str, Any],
    options: dict[str, Any],
    *,
    code: str,
    name: str,
) -> list[RawIntelItem]:
    from core.intelligence import disclosure_pdf, periodic_report_documents
    from core.intelligence.periodic_report_parser import (
        detect_report_type,
        parse_periodic_report_text,
        periodic_report_field_missing_reasons,
        periodic_report_table_status,
    )

    pdf_url = str(data.get('url') or title_item.url or '').strip()
    cache_path = disclosure_pdf.build_pdf_cache_path(
        pdf_url,
        code=_clean_code(code),
        published_at=title_item.published_at,
        document_kind='periodic_report',
    )
    report_type = detect_report_type(title_item.title, '')
    doc_id = periodic_report_documents.build_periodic_report_doc_id(
        code=code,
        title=title_item.title,
        url=pdf_url,
        local_path=cache_path,
    )

    def record_index(
        status: str,
        *,
        error: str = '',
        downloaded_at: str = '',
        local_path: Any = None,
    ) -> None:
        periodic_report_documents.record_periodic_report_pdf_index(
            code=code,
            name=name,
            report_type=report_type,
            title=title_item.title,
            url=pdf_url,
            local_path=local_path or cache_path,
            downloaded_at=downloaded_at,
            parse_status=status,
            error=error,
            doc_id=doc_id,
        )

    try:
        downloaded = disclosure_pdf.download_pdf(
            pdf_url,
            cache_path,
            timeout_seconds=int(options.get('timeout_seconds') or 8),
        )
    except Exception as exc:
        record_index('download_error', error=str(exc))
        _record_periodic_report_status(
            title_item,
            'download_error',
            url=pdf_url,
            error=str(exc),
            attempted=True,
            title=title_item.title,
            candidate=True,
            selected=True,
        )
        return []

    if downloaded.status not in {'ok', 'cached'}:
        record_index(
            downloaded.status or 'download_error',
            error=downloaded.error,
            downloaded_at=downloaded.fetched_at,
            local_path=downloaded.cache_path,
        )
        _record_periodic_report_status(
            title_item,
            downloaded.status or 'download_error',
            url=pdf_url,
            error=downloaded.error,
            attempted=True,
            title=title_item.title,
            candidate=True,
            selected=True,
        )
        return []

    try:
        extracted = disclosure_pdf.extract_pdf_text(downloaded.cache_path)
    except Exception as exc:
        record_index('extract_error', error=str(exc), downloaded_at=downloaded.fetched_at, local_path=downloaded.cache_path)
        _record_periodic_report_status(
            title_item,
            'extract_error',
            url=pdf_url,
            error=str(exc),
            attempted=True,
            title=title_item.title,
            candidate=True,
            selected=True,
        )
        return []

    if extracted.status != 'ok':
        record_index(
            extracted.status or 'extract_error',
            error=extracted.error,
            downloaded_at=downloaded.fetched_at,
            local_path=downloaded.cache_path,
        )
        _record_periodic_report_status(
            title_item,
            extracted.status or 'extract_error',
            url=pdf_url,
            error=extracted.error,
            attempted=True,
            title=title_item.title,
            candidate=True,
            selected=True,
        )
        return []

    text = str(extracted.text or '')
    section_result = periodic_report_documents.write_periodic_report_sections(doc_id, text)
    table_result = periodic_report_documents.extract_periodic_report_tables(doc_id, downloaded.cache_path)
    sections = section_result.sections if section_result.status == 'ok' else []
    tables = table_result.tables if table_result.status == 'ok' else []
    evidence = parse_periodic_report_text(
        text,
        title=title_item.title,
        source_url=pdf_url,
        published_at=title_item.published_at,
        code=code,
        name=name,
        sections=sections,
        table_blocks=tables,
    )
    field_missing_reason = periodic_report_field_missing_reasons(
        evidence,
        sections=sections,
        table_blocks=tables,
    )
    table_status = periodic_report_table_status(evidence, table_blocks=tables)
    if not evidence:
        record_index('no_evidence', downloaded_at=downloaded.fetched_at, local_path=downloaded.cache_path)
        _record_periodic_report_status(
            title_item,
            'no_evidence',
            url=pdf_url,
            attempted=True,
            title=title_item.title,
            candidate=True,
            selected=True,
            section_index_status=section_result.status,
            table_extract_status=table_result.status,
            table_count=len(tables),
            matched_section_count=len(sections),
            field_missing_reason=field_missing_reason,
            table_evidence_count_by_field=table_status.get('table_evidence_count_by_field'),
            table_evidence_top_fields=table_status.get('table_evidence_top_fields'),
            low_quality_table_skipped=int(table_status.get('low_quality_table_skipped') or 0),
            customer_supplier_split_status=table_status.get('customer_supplier_split_status'),
        )
        return []

    items = [
        _raw_from_periodic_report_evidence(spec, title_item, item, data, pdf_url, code=code, name=name)
        for item in evidence
    ]
    record_index('ok', downloaded_at=downloaded.fetched_at, local_path=downloaded.cache_path)
    _record_periodic_report_status(
        title_item,
        'ok',
        url=pdf_url,
        attempted=True,
        generated_count=len(items),
        page_count=extracted.page_count,
        title=title_item.title,
        candidate=True,
        selected=True,
        section_index_status=section_result.status,
        table_extract_status=table_result.status,
        table_count=len(tables),
        matched_section_count=len(sections),
        field_missing_reason=field_missing_reason,
        table_evidence_count_by_field=table_status.get('table_evidence_count_by_field'),
        table_evidence_top_fields=table_status.get('table_evidence_top_fields'),
        low_quality_table_skipped=int(table_status.get('low_quality_table_skipped') or 0),
        customer_supplier_split_status=table_status.get('customer_supplier_split_status'),
    )
    return items


def _raw_from_disclosure_evidence(
    spec: SourceSpec,
    title_item: RawIntelItem,
    evidence: Any,
    data: dict[str, Any],
    pdf_url: str,
    *,
    code: str,
    name: str,
) -> RawIntelItem:
    field = str(evidence.field or evidence.layer or 'body')
    raw_url = f'{pdf_url}#pdf-body:{_safe_id(field)}'
    return RawIntelItem(
        source_id=spec.source_id,
        source_name=spec.name,
        source_url=pdf_url,
        fetched_at=datetime.now().isoformat(timespec='seconds'),
        published_at=evidence.published_at or title_item.published_at,
        title=f'{title_item.title} PDF body: {field}',
        text=str(evidence.evidence_text or evidence.summary or title_item.text),
        url=raw_url,
        layer=str(evidence.layer or field),
        trust_level=spec.trust_level,
        raw={
            'entry': data,
            'match_reason': evidence.raw_ref,
            'matched_keywords': list(evidence.matched_keywords or []),
            'category': 'announcement_pdf_body',
            'parser_category': evidence.category,
            'field': field,
            'evidence_type': 'announcement_pdf',
            'pdf_url': pdf_url,
            'source_title': title_item.title,
            'confidence': evidence.confidence,
            'code': code,
            'name': name,
        },
    )


def _raw_from_periodic_report_evidence(
    spec: SourceSpec,
    title_item: RawIntelItem,
    evidence: Any,
    data: dict[str, Any],
    pdf_url: str,
    *,
    code: str,
    name: str,
) -> RawIntelItem:
    field = str(evidence.field or evidence.layer or 'body')
    ref_digest = hashlib.sha256(str(evidence.raw_ref or field).encode('utf-8')).hexdigest()[:10]
    raw_url = f'{pdf_url}#periodic-report:{_safe_id(field)}:{ref_digest}'
    return RawIntelItem(
        source_id=spec.source_id,
        source_name=spec.name,
        source_url=pdf_url,
        fetched_at=datetime.now().isoformat(timespec='seconds'),
        published_at=evidence.published_at or title_item.published_at,
        title=f'{title_item.title} periodic report: {field}',
        text=str(evidence.evidence_text or evidence.summary or title_item.text),
        url=raw_url,
        layer=str(evidence.layer or field),
        trust_level=spec.trust_level,
        raw={
            'entry': data,
            'match_reason': evidence.raw_ref,
            'matched_keywords': list(evidence.matched_keywords or []),
            'category': 'periodic_report_body',
            'parser_category': evidence.category,
            'field': field,
            'evidence_type': 'periodic_report',
            'pdf_url': pdf_url,
            'source_title': title_item.title,
            'report_type': evidence.report_type,
            'section': evidence.section,
            'page': int(getattr(evidence, 'page', 0) or 0),
            'table_header': list(getattr(evidence, 'table_header', []) or []),
            'table_quality_score': float(getattr(evidence, 'table_quality_score', 0.0) or 0.0),
            'table_values': dict(getattr(evidence, 'table_values', {}) or {}),
            'table_row_index': int(getattr(evidence, 'table_row_index', 0) or 0),
            'confidence': evidence.confidence,
            'code': code,
            'name': name,
        },
    )


def _record_pdf_body_status(
    raw_item: RawIntelItem,
    status: str,
    *,
    url: str,
    error: str = '',
    attempted: bool = False,
    generated_count: int = 0,
    page_count: int = 0,
) -> None:
    values = raw_item.raw.setdefault('pdf_body_status', [])
    if not isinstance(values, list):
        values = []
        raw_item.raw['pdf_body_status'] = values
    values.append({
        'status': str(status or 'unknown'),
        'url': str(url or ''),
        'error': str(error or ''),
        'attempted': bool(attempted),
        'generated_count': int(generated_count or 0),
        'page_count': int(page_count or 0),
    })


def _record_periodic_report_status(
    raw_item: RawIntelItem,
    status: str,
    *,
    url: str,
    error: str = '',
    attempted: bool = False,
    generated_count: int = 0,
    page_count: int = 0,
    title: str = '',
    candidate: bool = False,
    selected: bool = False,
    section_index_status: str = '',
    table_extract_status: str = '',
    table_count: int = 0,
    matched_section_count: int = 0,
    field_missing_reason: dict[str, str] | None = None,
    table_evidence_count_by_field: dict[str, int] | None = None,
    table_evidence_top_fields: list[str] | None = None,
    low_quality_table_skipped: int = 0,
    customer_supplier_split_status: dict[str, int] | None = None,
) -> None:
    values = raw_item.raw.setdefault('periodic_report_status', [])
    if not isinstance(values, list):
        values = []
        raw_item.raw['periodic_report_status'] = values
    values.append({
        'status': str(status or 'unknown'),
        'url': str(url or ''),
        'error': str(error or ''),
        'attempted': bool(attempted),
        'generated_count': int(generated_count or 0),
        'page_count': int(page_count or 0),
        'title': str(title or raw_item.title or ''),
        'candidate': bool(candidate),
        'selected': bool(selected),
        'section_index_status': str(section_index_status or ''),
        'table_extract_status': str(table_extract_status or ''),
        'table_count': int(table_count or 0),
        'matched_section_count': int(matched_section_count or 0),
        'field_missing_reason': dict(field_missing_reason or {}),
        'table_evidence_count_by_field': dict(table_evidence_count_by_field or {}),
        'table_evidence_top_fields': list(table_evidence_top_fields or []),
        'low_quality_table_skipped': int(low_quality_table_skipped or 0),
        'customer_supplier_split_status': dict(customer_supplier_split_status or {}),
    })


def _seed(code: str, name: str, context: dict[str, Any] | None) -> dict[str, Any]:
    root = _root(context)
    inner = _inner_context(context)
    seed = {
        'subject': dict(root.get('subject') or {'code': _clean_code(code), 'name': name}),
        'sectors': list(root.get('sectors') or []),
        'intel_events': [],
        'stock_news': [],
        'context': dict(inner),
    }
    if not seed['subject'].get('code'):
        seed['subject']['code'] = _clean_code(code)
    if not seed['subject'].get('name'):
        seed['subject']['name'] = name or seed['subject'].get('code', '')
    return seed


def _root(context: dict[str, Any] | None) -> dict[str, Any]:
    return context if isinstance(context, dict) else {}


def _inner_context(context: dict[str, Any] | None) -> dict[str, Any]:
    root = _root(context)
    value = root.get('context')
    return value if isinstance(value, dict) else root


def _value(context: dict[str, Any] | None, key: str) -> Any:
    root = _root(context)
    if key in root:
        return root.get(key)
    inner = root.get('context') if isinstance(root.get('context'), dict) else {}
    if key in inner:
        return inner.get(key)
    market = root.get('market_context') if isinstance(root.get('market_context'), dict) else {}
    if key in market:
        return market.get(key)
    return None


def _thin_layer_payloads(context: dict[str, Any] | None) -> list[tuple[str, str, Any]]:
    found: list[tuple[str, str, Any]] = []
    root = _root(context)
    inner = _inner_context(context)
    for holder in (root.get('variable_snapshots'), inner.get('variable_snapshots')):
        snapshots = holder if isinstance(holder, dict) else {}
        for layer in ('cost', 'demand', 'export', 'inventory', 'capacity', 'competition'):
            if layer in snapshots:
                found.append((layer, f'{layer}_snapshot', snapshots.get(layer)))
    for layer, keys in _THIN_LAYER_KEYS.items():
        for key in keys:
            value = _value(context, key)
            if value not in (None, '', [], {}):
                found.append((layer, key, value))
    return found


def _payload_summary(payload: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(payload, dict):
        data = dict(payload)
        summary = str(
            data.get('summary')
            or data.get('text')
            or data.get('content')
            or data.get('description')
            or ''
        ).strip()
        return summary, data
    if isinstance(payload, (list, tuple)):
        parts = []
        for item in payload[:3]:
            summary, _data = _payload_summary(item)
            if summary:
                parts.append(summary)
        return '; '.join(parts), {'items': list(payload)}
    text = str(payload or '').strip()
    return text, {'summary': text}


def _time_windows(payload: dict[str, Any], default: list[str]) -> list[str]:
    value = payload.get('time_windows')
    if value is None:
        value = payload.get('window')
    if isinstance(value, str):
        return [value] if value else list(default)
    if isinstance(value, (list, tuple)):
        windows = [str(item).strip() for item in value if str(item).strip()]
        return windows or list(default)
    return list(default)


def _direction_value(value: Any) -> str:
    text = str(value or 'neutral')
    return text if text in {'bullish', 'bearish', 'neutral', 'mixed', 'unknown'} else 'neutral'


def _int_between(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _is_relevant_to_subject(text: str, code: str, name: str, sectors: list[str]) -> bool:
    return bool(_subject_match_reason(text, code, name, sectors))


def _subject_sectors(context: dict[str, Any] | None) -> list[str]:
    root = _root(context)
    sectors = root.get('sectors')
    if not isinstance(sectors, (list, tuple)):
        sectors = []
    profile = root.get('_route_profile')
    profile_sectors = getattr(profile, 'sectors', ())
    if not profile_sectors and isinstance(profile, dict):
        profile_sectors = profile.get('sectors') or ()
    return list(dict.fromkeys(
        str(value).strip()
        for value in [*list(sectors), *list(profile_sectors or ())]
        if str(value).strip()
    ))


def _queries_for_source(
    source_id: str,
    code: str,
    name: str,
    sectors: list[str],
    context: dict[str, Any] | None,
    *,
    fallback,
) -> list[str]:
    root = _root(context)
    plan = root.get('_collection_plan')
    queries: tuple[str, ...] | list[str] = ()
    if hasattr(plan, 'queries_for_source'):
        queries = plan.queries_for_source(source_id)
    elif isinstance(plan, dict):
        by_source = plan.get('queries_by_source') if isinstance(plan.get('queries_by_source'), dict) else {}
        queries = by_source.get(source_id) or ()
    if queries:
        return _unique_texts(queries, limit=10)
    return _unique_texts(fallback or _subject_query_hints(code, name, sectors), limit=10)


def _unique_texts(values, *, limit: int) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _subject_match_reason(text: str, code: str, name: str, sectors: list[str]) -> str:
    reason, _matched = _subject_relevance(text, code, name, sectors)
    return reason


def _subject_relevance(text: str, code: str, name: str, sectors: list[str]) -> tuple[str, list[str]]:
    merged = str(text or '').lower()
    matched: list[str] = []
    reasons: list[str] = []
    for label, hints in (
        ('code', [_clean_code(code)]),
        ('name', [name]),
        ('sector', sectors),
        ('concept', _concept_hints(code, name, sectors)),
        ('query', _subject_query_hints(code, name, sectors)),
    ):
        for hint in hints:
            cleaned = str(hint or '').strip()
            if len(cleaned) >= 2 and cleaned.lower() in merged:
                if cleaned not in matched:
                    matched.append(cleaned)
                if not any(reason.endswith(f':{cleaned}') for reason in reasons):
                    reasons.append(f'{label}:{cleaned}')
    if not reasons:
        return '', []
    return ';'.join(reasons[:4]), matched[:8]


def _subject_query_hints(code: str, name: str, sectors: list[str]) -> list[str]:
    clean_code = _clean_code(code)
    hints: list[str] = []
    hints.extend(_STOCK_QUERY_HINTS.get(clean_code, ()))
    hints.extend(_CODE_HINTS.get(clean_code, ()))
    hints.extend(str(value).strip() for value in sectors if str(value).strip())
    if name:
        hints.append(str(name).strip())
    return list(dict.fromkeys(value for value in hints if value))


def _policy_queries(code: str, name: str, sectors: list[str]) -> list[str]:
    clean_code = _clean_code(code)
    queries = list(_POLICY_QUERY_HINTS.get(clean_code, ()))
    if not queries:
        queries = _subject_query_hints(code, name, sectors)[:3]
    return list(dict.fromkeys(str(query).strip() for query in queries if str(query).strip()))[:3]


def _industry_queries(code: str, name: str, sectors: list[str]) -> list[str]:
    clean_code = _clean_code(code)
    queries = list(_INDUSTRY_QUERY_HINTS.get(clean_code, ()))
    if not queries:
        queries = _subject_query_hints(code, name, sectors)[:3]
    return list(dict.fromkeys(str(query).strip() for query in queries if str(query).strip()))[:3]


def _external_layer(*texts: str) -> str:
    merged = ' '.join(str(text or '') for text in texts).lower()
    if any(marker in merged for marker in (
        '\u4ef7\u683c', '\u6210\u672c', '\u539f\u6750\u6599', '\u91c7\u8d2d\u6210\u672c',
        'cost', 'price', 'material',
    )):
        return 'cost'
    if any(marker in merged for marker in (
        '\u751f\u4ea7\u5efa\u8bbe', '\u5efa\u8bbe\u9879\u76ee', '\u6295\u8d44\u5efa\u8bbe',
        '\u4ea7\u80fd\u5efa\u8bbe', '\u751f\u4ea7\u57fa\u5730', 'capacity project',
    )):
        return 'capacity'
    for layer, keywords in _EXTERNAL_LAYER_CHECKS:
        if any(str(keyword).lower() in merged for keyword in keywords):
            return layer
    return _layer_from_text(*texts) or 'news_event'


def _is_policy_text(*texts: str) -> bool:
    merged = ' '.join(str(text or '') for text in texts)
    return any(marker in merged for marker in (
        '\u5de5\u4fe1\u90e8', '\u8bc1\u76d1\u4f1a', '\u4ea4\u6613\u6240',
        '\u56fd\u52a1\u9662', '\u5370\u53d1', '\u5b9e\u65bd\u610f\u89c1',
        '\u884c\u52a8\u8ba1\u5212', '\u6307\u5bfc\u610f\u89c1',
        '\u901a\u77e5', '\u653f\u7b56', '\u6807\u51c6',
        '\u7248\u6743\u4fdd\u62a4', '\u51fa\u53e3\u7ba1\u5236',
    ))


def _raw_external_chain_item(
    spec: SourceSpec,
    data: dict[str, Any],
    query: str,
    code: str,
    name: str,
    sectors: list[str],
    *,
    source: str,
) -> RawIntelItem | None:
    title = str(data.get('title') or '').strip()
    text = str(data.get('summary') or title).strip()
    if not title or _contains_forbidden(title, text):
        return None
    reason, matched = _subject_relevance(title + ' ' + text + ' ' + query, code, name, sectors)
    if not reason:
        return None
    layer = _external_layer(title, text)
    if layer not in {
        'cost',
        'capacity',
        'competition',
        'customer_supplier',
        'order_contract',
        'policy',
        'risk',
        'demand',
        'export',
        'financial',
    }:
        return None
    return _raw_from_payload(
        spec,
        title=title,
        text=text,
        published_at=str(data.get('published_at') or '') or None,
        url=str(data.get('url') or ''),
        layer=layer,
        payload={
            'entry': data,
            'match_reason': reason,
            'matched_keywords': matched,
            'category': f'industry_chain:{source}',
            'query': query,
            'code': code,
            'name': name,
        },
    )


def _expand_external_public_item(item: RawIntelItem) -> list[RawIntelItem]:
    layers = [item.layer]
    for layer in _secondary_external_layers(item.title, item.text):
        if layer != item.layer and layer not in layers:
            layers.append(layer)
    if len(layers) == 1:
        return [item]
    return [item, *[_raw_item_with_layer(item, layer) for layer in layers[1:]]]


def _secondary_external_layers(*texts: str) -> list[str]:
    merged = ' '.join(str(text or '') for text in texts).lower()
    layers: list[str] = []
    for layer, keywords in _EXTERNAL_SECONDARY_LAYER_CHECKS:
        if any(str(keyword).lower() in merged for keyword in keywords):
            layers.append(layer)
    return layers


def _raw_item_with_layer(item: RawIntelItem, layer: str) -> RawIntelItem:
    payload = dict(item.raw or {})
    payload['primary_layer'] = item.layer
    payload['secondary_layer'] = layer
    return RawIntelItem(
        source_id=item.source_id,
        source_name=item.source_name,
        source_url=item.source_url,
        fetched_at=item.fetched_at,
        published_at=item.published_at,
        title=item.title,
        text=item.text,
        html=item.html,
        url=item.url,
        layer=layer,
        trust_level=item.trust_level,
        raw=payload,
    )


def _concept_hints(code: str, name: str, sectors: list[str]) -> list[str]:
    hints: list[str] = []
    clean_code = _clean_code(code)
    hints.extend(_CODE_HINTS.get(clean_code, ()))
    lowered = ' '.join([str(name or ''), *[str(value or '') for value in sectors]]).lower()
    if any(token in lowered for token in ('半导体', '封装', '芯片', 'semiconductor', 'chip')):
        hints.extend(('semiconductor', 'chip', 'advanced packaging', 'OSAT'))
    if any(token in lowered for token in ('光模块', '光通信', 'cpo', 'optical')):
        hints.extend(('optical module', 'optical transceiver', 'AI data center', 'datacenter', 'CPO'))
    if any(token in lowered for token in ('aigc', '视觉', '版权', 'image')):
        hints.extend(('AIGC', 'visual content', 'copyright', 'image licensing'))
    return list(dict.fromkeys(str(value).strip() for value in hints if str(value).strip()))


def _fetch_cninfo_announcements(searchkey: str, *, limit: int) -> list[dict[str, Any]]:
    from core.http_client import post as http_post

    key = str(searchkey or '').strip()
    if not key:
        return []
    response = http_post(
        'https://www.cninfo.com.cn/new/hisAnnouncement/query',
        data={
            'pageNum': '1',
            'pageSize': str(max(5, min(30, limit))),
            'column': 'szse',
            'tabName': 'fulltext',
            'plate': '',
            'stock': '',
            'searchkey': key,
            'secid': '',
            'category': '',
            'trade': '',
            'seDate': '',
        },
        headers={
            'Referer': 'https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search',
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    items: list[dict[str, Any]] = []
    for entry in list(payload.get('announcements') or [])[:limit]:
        data = dict(entry or {})
        title = _clean_html(data.get('announcementTitle'))
        if not title:
            continue
        adjunct = str(data.get('adjunctUrl') or '').strip()
        items.append({
            'title': title,
            'summary': title,
            'published_at': _date_from_ms(data.get('announcementTime')),
            'url': urljoin('https://static.cninfo.com.cn/', adjunct),
            'secCode': data.get('secCode'),
            'secName': data.get('secName'),
            'source': 'CNINFO',
        })
    return items


def _fetch_miit_search(query: str, *, limit: int) -> list[dict[str, Any]]:
    from core.http_client import get as http_get

    q = str(query or '').strip()
    if not q:
        return []
    params = {
        'websiteid': '110000000000000',
        'scope': 'basic',
        'q': q,
        'pg': str(max(3, min(10, limit))),
        'cateid': '',
        'pos': 'title_text,infocontent',
        'pq': '',
        'oq': '',
        'eq': '',
        'begin': '',
        'end': '',
        'dateField': 'deploytime',
        'selectFields': 'title,content,deploytime,_index,url,cdate,columnname,publishtime,infocontent',
        'group': 'distinct',
        'highlightFields': 'title_text,infocontent,webid',
        'level': '6',
        'sortFields': '[{"name":"deploytime","type":"desc"}]',
        'p': '1',
    }
    response = http_get(
        'https://www.miit.gov.cn/search-front-server/api/search/info?' + urlencode(params),
        timeout=12,
        headers={'Referer': 'https://www.miit.gov.cn/search/index.html'},
    )
    response.raise_for_status()
    payload = response.json()
    result = (payload.get('data') or {}).get('searchResult') or {}
    items: list[dict[str, Any]] = []
    for row in list(result.get('dataResults') or [])[:limit]:
        data = _miit_result_data(row)
        title = _clean_html(data.get('title'))
        summary = _clean_html(data.get('infocontent') or data.get('content') or title)
        if not title:
            continue
        url = str(data.get('url') or '').strip()
        items.append({
            'title': title,
            'summary': summary or title,
            'published_at': _date_from_ms(data.get('deploytime') or data.get('publishtime') or data.get('cdate')),
            'url': urljoin('https://www.miit.gov.cn/', url),
            'source': 'MIIT',
            'column': data.get('columnname'),
        })
    return items


def _miit_result_data(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    group = row.get('groupData') if isinstance(row.get('groupData'), list) else []
    if group and isinstance(group[0], dict):
        data = group[0].get('data')
        if isinstance(data, dict):
            return data
    data = row.get('data')
    return data if isinstance(data, dict) else {}


def _date_from_ms(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or '').strip()
    if number > 100000000000:
        number = number // 1000
    try:
        return datetime.fromtimestamp(number).strftime('%Y-%m-%d')
    except (OSError, OverflowError, ValueError):
        return ''


def _clean_html(value: Any) -> str:
    text = html.unescape(str(value or ''))
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;?', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _is_noise_announcement(title: str) -> bool:
    text = str(title or '')
    noise_terms = (
        '\u80a1\u4e1c\u5927\u4f1a', '\u8463\u4e8b\u4f1a', '\u76d1\u4e8b\u4f1a',
        '\u6cd5\u5f8b\u610f\u89c1\u4e66', '\u516c\u53f8\u7ae0\u7a0b',
        '\u8bae\u4e8b\u89c4\u5219', '\u85aa\u916c\u7ba1\u7406\u5236\u5ea6',
        '\u804c\u5de5\u4ee3\u8868\u8463\u4e8b', '\u6362\u5c4a\u9009\u4e3e',
    )
    if any(term in text for term in noise_terms):
        return True
    strong = _external_layer(text)
    return strong not in {'order_contract', 'capacity', 'risk', 'financial', 'customer_supplier', 'policy'}


def _dedupe_raw_items(items: list[RawIntelItem], *, limit: int) -> list[RawIntelItem]:
    out: list[RawIntelItem] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (item.source_id, item.layer, item.url or item.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _fetch_static_eastmoney_titles(url: str, *, limit: int) -> list[dict[str, str]]:
    return _fetch_static_page_titles(url, limit=limit, referer='https://www.eastmoney.com/')


def _fetch_static_page_titles(
    url: str,
    *,
    limit: int,
    referer: str = '',
) -> list[dict[str, str]]:
    from core.http_client import get as http_get

    response = http_get(
        url,
        timeout=8,
        headers={'Referer': referer or url},
    )
    response.raise_for_status()
    text = response.text or ''
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    pattern = re.compile(r'<a\b[^>]*href=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>', re.I | re.S)
    for match in pattern.finditer(text):
        title = html.unescape(re.sub(r'<[^>]+>', '', match.group('title'))).strip()
        title = re.sub(r'\s+', ' ', title)
        if len(title) < 8 or title in seen:
            continue
        seen.add(title)
        results.append({
            'title': title[:120],
            'url': html.unescape(match.group('url')).strip(),
            'source': 'Eastmoney static',
        })
        if len(results) >= limit:
            break
    return results


def _fetch_rss_entries(url: str, *, limit: int) -> list[dict[str, str]]:
    from core.http_client import get as http_get

    response = http_get(url, timeout=8, headers={'Accept': 'application/rss+xml, application/xml, text/xml'})
    response.raise_for_status()
    text = response.text or ''
    entries = _parse_feedparser_entries(text, limit=limit)
    if not entries:
        entries = _parse_xml_feed_entries(text, limit=limit)
    return entries


def _parse_feedparser_entries(text: str, *, limit: int) -> list[dict[str, str]]:
    try:
        feedparser = importlib.import_module('feedparser')
    except Exception:
        return []
    try:
        parsed = feedparser.parse(text)
    except Exception:
        return []
    entries: list[dict[str, str]] = []
    for entry in list(getattr(parsed, 'entries', []) or [])[:limit]:
        data = dict(entry)
        title = str(data.get('title') or '').strip()
        summary = str(data.get('summary') or data.get('description') or title).strip()
        url = str(data.get('link') or '').strip()
        published_at = str(data.get('published') or data.get('updated') or '').strip()
        if title:
            entries.append({'title': title, 'summary': summary, 'url': url, 'published_at': published_at})
    return entries


def _parse_xml_feed_entries(text: str, *, limit: int) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text.encode('utf-8') if isinstance(text, str) else text)
    except Exception:
        return []
    entries: list[dict[str, str]] = []
    for item in list(root.iter()):
        tag = _xml_tag(item.tag)
        if tag not in {'item', 'entry'}:
            continue
        title = _xml_child_text(item, 'title')
        summary = _xml_child_text(item, 'description') or _xml_child_text(item, 'summary') or title
        url = _xml_child_text(item, 'link')
        if not url:
            for child in list(item):
                if _xml_tag(child.tag) == 'link':
                    url = str(child.attrib.get('href') or '').strip()
                    break
        published_at = (
            _xml_child_text(item, 'pubDate')
            or _xml_child_text(item, 'published')
            or _xml_child_text(item, 'updated')
        )
        if title:
            entries.append({
                'title': title,
                'summary': summary,
                'url': url,
                'published_at': published_at,
            })
        if len(entries) >= limit:
            break
    return entries


def _xml_child_text(item: ET.Element, child_name: str) -> str:
    for child in list(item):
        if _xml_tag(child.tag) == child_name:
            return html.unescape(''.join(child.itertext())).strip()
    return ''


def _xml_tag(tag: str) -> str:
    return str(tag).rsplit('}', 1)[-1]


def _coverage_summary(
    items: list[NormalizedIntelItem],
    source_status: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_layer = Counter(item.layer for item in items)
    by_source = Counter(item.source_id for item in items)
    by_field: Counter[str] = Counter()
    by_access_mode: Counter[str] = Counter()
    optional_api_hit_count = 0
    for item in items:
        entry = source_status.get(item.source_id, {})
        access_mode = _source_access_mode(item.source_id, entry)
        by_access_mode[access_mode] += 1
        if access_mode == 'optional_api':
            optional_api_hit_count += 1
        for field in _fields_for_item(item, entry):
            by_field[field] += 1
    freshness = Counter(item.freshness or 'unknown' for item in items)
    matched_keywords = Counter(
        keyword
        for item in items
        for keyword in (item.matched_keywords or [])
        if keyword
    )
    sources = Counter(str(entry.get('status') or 'unknown') for entry in source_status.values())
    missing_layers = [layer for layer in _REQUIRED_COVERAGE_LAYERS if by_layer.get(layer, 0) <= 0]
    missing_reason = _missing_reason_by_field(by_field, source_status)
    external_hits = sum(1 for item in items if not str(item.source_id).startswith('existing:'))
    local_hits = len(items) - external_hits
    return {
        'real_node_count': len(items),
        'by_layer': dict(sorted(by_layer.items())),
        'by_field': dict(sorted(by_field.items())),
        'by_source': dict(sorted(by_source.items())),
        'by_access_mode': dict(sorted(by_access_mode.items())),
        'missing_layers': missing_layers,
        'missing_layer_count': len(missing_layers),
        'missing_reason': missing_reason,
        'optional_api_hit_count': optional_api_hit_count,
        'skipped_missing_credentials': int(sources.get('skipped_missing_credentials', 0)),
        'freshness': dict(sorted(freshness.items())),
        'matched_keywords': dict(sorted(matched_keywords.items())),
        'external_source_hits': external_hits,
        'local_source_hits': local_hits,
        'sources': dict(sorted(sources.items())),
        'failed_sources': [
            source_id for source_id, entry in sorted(source_status.items())
            if entry.get('status') == 'error'
        ],
        'skipped_sources': [
            source_id for source_id, entry in sorted(source_status.items())
            if str(entry.get('status') or '').startswith('skipped')
        ],
    }


def _source_access_mode(source_id: str, entry: dict[str, Any]) -> str:
    access_mode = str(entry.get('access_mode') or '')
    if access_mode:
        return access_mode
    return 'optional_api' if str(source_id).startswith('api:') else 'free'


def _fields_for_item(item: NormalizedIntelItem, entry: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    layer = str(item.layer or '')
    if layer in REQUIRED_FIELDS:
        fields.append(layer)
    elif layer == 'news_event':
        fields.append('news_sentiment')

    source_id = str(item.source_id or '')
    category = str(item.category or '')
    source_fields = entry.get('fields')
    if not isinstance(source_fields, (list, tuple)):
        source_fields = [entry.get('field')] if entry.get('field') else []
    for field in source_fields:
        text = str(field or '')
        if not text:
            continue
        if text == layer:
            fields.append(text)
        elif text == 'company_disclosure' and (source_id == 'requests:company_announcements' or category == 'company_announcement'):
            fields.append(text)
        elif text == 'news_sentiment' and source_id in {'existing:stock_news', 'requests:eastmoney_kuaixun', 'rss:cnbc_finance'}:
            fields.append(text)
        elif text == 'policy' and layer == 'policy':
            fields.append(text)
        elif text == 'macro' and layer == 'macro':
            fields.append(text)
    return list(dict.fromkeys(field for field in fields if field in REQUIRED_FIELDS))


def _missing_reason_by_field(
    by_field: Counter[str],
    source_status: dict[str, dict[str, Any]],
) -> dict[str, str]:
    missing: dict[str, str] = {}
    for field in REQUIRED_FIELDS:
        if by_field.get(field, 0) > 0:
            continue
        entries = [
            entry for entry in source_status.values()
            if field in _entry_fields(entry)
        ]
        statuses = {str(entry.get('status') or '') for entry in entries}
        if 'ok' in statuses:
            reason = 'no_relevant_items'
        elif 'error' in statuses:
            reason = 'source_error'
        elif 'skipped_missing_credentials' in statuses:
            reason = 'missing_credentials'
        elif any(status.startswith('skipped') for status in statuses):
            reason = 'skipped_source'
        else:
            reason = 'no_planned_source'
        missing[field] = reason
    return missing


def _entry_fields(entry: dict[str, Any]) -> list[str]:
    fields = entry.get('fields')
    if isinstance(fields, (list, tuple)):
        return [str(field) for field in fields if str(field)]
    field = str(entry.get('field') or '')
    return [field] if field else []


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, 'to_dict'):
        return event.to_dict()
    return dict(event or {}) if isinstance(event, dict) else {}


def _market_summary(emotion: Any, global_snap: Any, phase: Any) -> str:
    parts = []
    if isinstance(emotion, dict):
        if emotion.get('zt') is not None:
            parts.append(f"涨停 {emotion.get('zt')}")
        if emotion.get('dt') is not None:
            parts.append(f"跌停 {emotion.get('dt')}")
        if emotion.get('up') is not None and emotion.get('down') is not None:
            parts.append(f"上涨 {emotion.get('up')} / 下跌 {emotion.get('down')}")
    if isinstance(global_snap, dict):
        parts.append(str(global_snap.get('summary') or global_snap)[:120])
    if phase:
        parts.append(f'市场阶段 {phase}')
    return '；'.join(part for part in parts if part)


def _flow_summary(flow: dict[str, Any]) -> str:
    parts = []
    if flow.get('days'):
        parts.append(f"{flow.get('days')} 日资金窗口")
    if flow.get('main_5d') is not None:
        parts.append(f"5日主力净额 {flow.get('main_5d')}")
    if flow.get('main_streak'):
        parts.append(f"连续方向 {flow.get('main_streak')}")
    return '；'.join(parts) or str(flow)


def _status_entry(
    spec: SourceSpec,
    *,
    status: str,
    raw_count: int,
    normalized_count: int,
    error: str,
) -> dict[str, Any]:
    return {
        'field': spec.fields[0] if spec.fields else '',
        'fields': list(spec.fields or ()),
        'layer': spec.layer,
        'method': spec.method,
        'form': spec.form,
        'trust_level': spec.trust_level,
        'parser': spec.parser,
        'enabled': spec.enabled,
        'ttl_hours': spec.ttl_hours,
        'access_mode': spec.access_mode,
        'requires_api_key': spec.requires_api_key,
        'credential_env': spec.credential_env,
        'credential_aliases': list(spec.credential_aliases or ()),
        'credential_envs': [
            value for value in (spec.credential_env, *list(spec.credential_aliases or ()))
            if value
        ],
        'supports_pdf_body': spec.supports_pdf_body,
        'status': status,
        'raw_count': raw_count,
        'normalized_count': normalized_count,
        'last_error': error,
        'missing_reason': '',
        'last_attempt_at': datetime.now().isoformat(timespec='seconds'),
    }


def _pdf_body_status_summary(raw_items: list[RawIntelItem]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in raw_items:
        values = item.raw.get('pdf_body_status') if isinstance(item.raw, dict) else None
        if isinstance(values, list):
            entries.extend(value for value in values if isinstance(value, dict))
    if not entries:
        return {}

    statuses = Counter(str(entry.get('status') or 'unknown') for entry in entries)
    attempted = sum(1 for entry in entries if entry.get('attempted'))
    generated = sum(_int_between(entry.get('generated_count'), 0, 0, 1000) for entry in entries)
    return {
        'attempted': attempted,
        'parsed': int(statuses.get('ok', 0)),
        'download_error': int(statuses.get('download_error', 0)),
        'extract_error': int(statuses.get('extract_error', 0)),
        'empty_text': int(statuses.get('empty_text', 0)),
        'evidence_count': generated,
        'skipped_by_limit': int(statuses.get('skipped_limit', 0)),
        'generated_items': generated,
        'statuses': dict(sorted(statuses.items())),
        'details': entries[:5],
    }


def _periodic_report_status_summary(raw_items: list[RawIntelItem]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in raw_items:
        values = item.raw.get('periodic_report_status') if isinstance(item.raw, dict) else None
        if isinstance(values, list):
            entries.extend(value for value in values if isinstance(value, dict))
    if not entries:
        return {}

    statuses = Counter(str(entry.get('status') or 'unknown') for entry in entries)
    attempted = sum(1 for entry in entries if entry.get('attempted'))
    generated = sum(_int_between(entry.get('generated_count'), 0, 0, 1000) for entry in entries)
    candidates_found = sum(1 for entry in entries if entry.get('candidate'))
    selected = next((entry for entry in entries if entry.get('selected')), {})
    field_missing = selected.get('field_missing_reason') if isinstance(selected.get('field_missing_reason'), dict) else {}
    table_count = sum(_int_between(entry.get('table_count'), 0, 0, 10000) for entry in entries)
    matched_section_count = sum(_int_between(entry.get('matched_section_count'), 0, 0, 10000) for entry in entries)
    table_field_counts: Counter[str] = Counter()
    customer_supplier_split: Counter[str] = Counter()
    low_quality_table_skipped = 0
    for entry in entries:
        by_field = entry.get('table_evidence_count_by_field')
        if isinstance(by_field, dict):
            for field, count in by_field.items():
                table_field_counts[str(field)] += _int_between(count, 0, 0, 10000)
        split_status = entry.get('customer_supplier_split_status')
        if isinstance(split_status, dict):
            for field, count in split_status.items():
                customer_supplier_split[str(field)] += _int_between(count, 0, 0, 10000)
        low_quality_table_skipped += _int_between(entry.get('low_quality_table_skipped'), 0, 0, 10000)
    missing_reason = ''
    if not candidates_found:
        missing_reason = 'no_periodic_candidates'
    elif attempted and generated == 0:
        if statuses.get('no_evidence'):
            missing_reason = 'no_periodic_evidence'
        elif statuses.get('download_error'):
            missing_reason = 'download_error'
        elif statuses.get('extract_error') or statuses.get('unsupported'):
            missing_reason = 'extract_error'
    return {
        'periodic_candidates_found': candidates_found,
        'periodic_selected_title': str(selected.get('title') or ''),
        'periodic_selected_url': str(selected.get('url') or ''),
        'section_index_status': str(selected.get('section_index_status') or ''),
        'table_extract_status': str(selected.get('table_extract_status') or ''),
        'table_count': table_count,
        'matched_section_count': matched_section_count,
        'field_missing_reason': field_missing,
        'table_evidence_count_by_field': dict(sorted(table_field_counts.items())),
        'table_evidence_top_fields': [field for field, _count in table_field_counts.most_common(6)],
        'low_quality_table_skipped': int(low_quality_table_skipped),
        'customer_supplier_split_status': {
            'customer_concentration': int(customer_supplier_split.get('customer_concentration', 0)),
            'supplier_concentration': int(customer_supplier_split.get('supplier_concentration', 0)),
            'legacy_customer_supplier': int(customer_supplier_split.get('legacy_customer_supplier', 0)),
        },
        'skipped_summary': int(statuses.get('skipped_summary', 0)),
        'skipped_correction': int(statuses.get('skipped_correction', 0)),
        'skipped_english': int(statuses.get('skipped_english', 0)),
        'skipped_not_periodic': int(statuses.get('skipped_not_periodic', 0)),
        'skipped_non_cninfo_pdf': int(statuses.get('skipped_non_cninfo_pdf', 0)),
        'attempted': attempted,
        'parsed': int(statuses.get('ok', 0)),
        'download_error': int(statuses.get('download_error', 0)),
        'extract_error': int(statuses.get('extract_error', 0)),
        'empty_text': int(statuses.get('empty_text', 0)),
        'evidence_count': generated,
        'skipped_by_limit': int(statuses.get('skipped_limit', 0)),
        'generated_items': generated,
        'missing_reason': missing_reason,
        'statuses': dict(sorted(statuses.items())),
        'details': entries[:5],
    }


def _periodic_report_missing_reasons(source_status: dict[str, dict[str, Any]]) -> dict[str, str]:
    missing: dict[str, str] = {}
    for entry in source_status.values():
        periodic = entry.get('periodic_report') if isinstance(entry, dict) else None
        if not isinstance(periodic, dict):
            continue
        field_missing = periodic.get('field_missing_reason')
        if isinstance(field_missing, dict):
            for field, reason in field_missing.items():
                text = str(reason or '')
                if str(field) and text:
                    missing[str(field)] = text
    return missing


def _apply_periodic_field_summary_status(
    source_status: dict[str, dict[str, Any]],
    field_summary_status: dict[str, Any],
) -> None:
    if not isinstance(field_summary_status, dict):
        return
    for entry in source_status.values():
        periodic = entry.get('periodic_report') if isinstance(entry, dict) else None
        if not isinstance(periodic, dict):
            continue
        periodic.update({
            'field_summary_count': int(field_summary_status.get('field_summary_count') or 0),
            'field_summary_by_field': dict(field_summary_status.get('field_summary_by_field') or {}),
            'mixed_fields': list(field_summary_status.get('mixed_fields') or []),
            'weak_fields': list(field_summary_status.get('weak_fields') or []),
            'missing_fields': dict(field_summary_status.get('missing_fields') or {}),
        })


def _empty_periodic_report_status() -> dict[str, Any]:
    return {
        'periodic_candidates_found': 0,
        'periodic_selected_title': '',
        'periodic_selected_url': '',
        'section_index_status': '',
        'table_extract_status': '',
        'table_count': 0,
        'matched_section_count': 0,
        'field_missing_reason': {},
        'table_evidence_count_by_field': {},
        'table_evidence_top_fields': [],
        'low_quality_table_skipped': 0,
        'customer_supplier_split_status': {
            'customer_concentration': 0,
            'supplier_concentration': 0,
            'legacy_customer_supplier': 0,
        },
        'skipped_summary': 0,
        'skipped_correction': 0,
        'skipped_english': 0,
        'skipped_not_periodic': 0,
        'skipped_non_cninfo_pdf': 0,
        'attempted': 0,
        'parsed': 0,
        'download_error': 0,
        'extract_error': 0,
        'empty_text': 0,
        'evidence_count': 0,
        'skipped_by_limit': 0,
        'generated_items': 0,
        'missing_reason': 'no_periodic_candidates',
        'field_summary_count': 0,
        'field_summary_by_field': {},
        'mixed_fields': [],
        'weak_fields': [],
        'missing_fields': {},
        'statuses': {},
        'details': [],
    }


def _dedupe_items(items: list[NormalizedIntelItem]) -> list[NormalizedIntelItem]:
    out: list[NormalizedIntelItem] = []
    seen: set[str] = set()
    ordered = sorted(
        items,
        key=lambda item: (
            -_source_priority(item),
            str(item.published_at or ''),
            str(item.source_id or ''),
        ),
    )
    for item in ordered:
        title_key = re.sub(r'\s+', '', str(item.title or '').lower())[:80]
        url_key = str(item.url or '').strip().lower()
        layer_key = str(item.layer or '').strip().lower()
        category_key = str(item.category or '').strip().lower()
        if category_key in {'periodic_report_body', 'periodic_field_summary'}:
            metadata = item.metadata if isinstance(getattr(item, 'metadata', None), dict) else {}
            field_key = str(metadata.get('field') or item.layer or '').strip().lower()
            identity = str(item.raw_ref or item.id or title_key).strip().lower()
            key = f'{str(item.source_id)}:{category_key}:{field_key}:{identity}'
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            continue
        key = f'{url_key}:{layer_key}' if url_key else (
            f'{str(item.source_id)}:{title_key}:{item.published_at or ""}:{layer_key}'
        )
        title_seen_key = f'title:{title_key}:{layer_key}'
        if key in seen or (title_key and title_seen_key in seen):
            continue
        seen.add(key)
        if title_key:
            seen.add(title_seen_key)
        out.append(item)
    return out


def _source_priority(item: NormalizedIntelItem) -> int:
    source_id = str(item.source_id or '')
    if source_id.startswith('requests:company_announcements'):
        return 45
    if item.trust_level in {'official', 'primary'}:
        return _SOURCE_PRIORITY.get(item.trust_level, 5)
    if source_id.startswith('requests:'):
        return 28
    if source_id.startswith('rss:'):
        return 18
    return _SOURCE_PRIORITY.get(str(item.trust_level or 'unknown'), 5)


def _contains_forbidden(*texts: str) -> bool:
    merged = ' '.join(str(text or '') for text in texts)
    upper = merged.upper()
    return any(term in merged or term.upper() in upper for term in FORBIDDEN_INTELLIGENCE_TERMS)


def _clean_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _safe_id(value: Any) -> str:
    text = str(value or '').strip()
    return ''.join(ch if ch.isalnum() or ch in '_.:-' else '_' for ch in text)[:80] or 'item'


def _num(value: Any) -> float | None:
    try:
        if value in (None, ''):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')
