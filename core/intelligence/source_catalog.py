"""Stable source catalog for intelligence-route collection.

access_mode intentionally stays coarse in this pass:
- free: no external credential is needed.
- optional_api: a credential may improve coverage, but missing credentials skip the source.

The form field carries the finer source shape, such as local_context, rss,
public_http_json, exchange_http_json, official_http_json, or vendor_api.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from typing import Any

from core.intelligence.models import SourceSpec


REQUIRED_FIELDS = (
    'macro',
    'policy',
    'company_disclosure',
    'financial',
    'cost',
    'demand',
    'export',
    'customer_supplier',
    'inventory',
    'capacity',
    'competition',
    'order_contract',
    'trading_behavior',
    'risk',
    'news_sentiment',
)

_CNINFO_ANNOUNCEMENT_URL = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
_CNINFO_ANNOUNCEMENT_FORM = 'exchange_http_json+pdf_body'
_CNINFO_ANNOUNCEMENT_PARSER = 'cninfo_his_announcement+cninfo_pdf_body'


@dataclass(frozen=True)
class CatalogSource:
    field: str
    source_id: str
    name: str
    form: str
    trust_level: str
    access_mode: str
    ttl_hours: int
    requires_api_key: bool
    method: str
    layer: str
    parser: str
    url: str = ''
    credential_env: str = ''
    credential_aliases: tuple[str, ...] = ()
    enabled_by_default: bool = True
    supports_pdf_body: bool = False
    evidence_origin: str = 'news'
    source_role: str = 'secondary'
    stable_output: bool = False
    recommended_extractor: str = 'manual'
    implementation_status: str = 'implemented'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_source_spec(self, fields: tuple[str, ...] | None = None) -> SourceSpec:
        return SourceSpec(
            source_id=self.source_id,
            name=self.name,
            layer=self.layer,
            method=self.method,
            url=self.url,
            ttl_hours=self.ttl_hours,
            rate_limit_seconds=1 if self.access_mode == 'free' else 2,
            enabled=self.enabled_by_default,
            parser=self.parser,
            trust_level=self.trust_level,
            form=self.form,
            access_mode=self.access_mode,
            requires_api_key=self.requires_api_key,
            credential_env=self.credential_env,
            credential_aliases=self.credential_aliases,
            fields=tuple(fields or (self.field,)),
            supports_pdf_body=self.supports_pdf_body,
        )


def get_source_catalog() -> dict[str, tuple[CatalogSource, ...]]:
    catalog: dict[str, list[CatalogSource]] = {field: [] for field in REQUIRED_FIELDS}
    for source in _CATALOG:
        catalog[source.field].append(source)
    return {field: tuple(catalog[field]) for field in REQUIRED_FIELDS}


def get_enabled_catalog_sources(
    catalog: dict[str, tuple[CatalogSource, ...]] | None = None,
) -> tuple[CatalogSource, ...]:
    enabled: list[CatalogSource] = []
    for sources in (catalog or get_source_catalog()).values():
        for source in sources:
            if not source.enabled_by_default:
                continue
            if source.access_mode == 'free' or has_source_credentials(source):
                enabled.append(source)
    return tuple(_dedupe_sources(enabled))


def get_catalog_status(
    catalog: dict[str, tuple[CatalogSource, ...]] | None = None,
) -> dict[str, dict[str, Any]]:
    status: dict[str, dict[str, Any]] = {}
    for source in _dedupe_sources(source for sources in (catalog or get_source_catalog()).values() for source in sources):
        if not source.enabled_by_default:
            state = 'disabled'
            missing_reason = 'firecrawl_disabled' if source.source_id.startswith('firecrawl:') else ''
        elif source.access_mode == 'optional_api' and not has_source_credentials(source):
            state = 'skipped_missing_credentials'
            missing_reason = 'missing_credentials'
        else:
            state = 'registered' if source.enabled_by_default else 'disabled'
            missing_reason = ''
        status[source.source_id] = {
            'field': source.field,
            'fields': _source_fields(source.source_id, catalog or get_source_catalog()),
            'form': source.form,
            'trust_level': source.trust_level,
            'access_mode': source.access_mode,
            'ttl_hours': source.ttl_hours,
            'requires_api_key': source.requires_api_key,
            'credential_env': source.credential_env,
            'credential_envs': list(credential_env_names(source)),
            'supports_pdf_body': source.supports_pdf_body,
            'evidence_origin': source.evidence_origin,
            'source_role': source.source_role,
            'stable_output': source.stable_output,
            'recommended_extractor': source.recommended_extractor,
            'implementation_status': source.implementation_status,
            'status': state,
            'missing_reason': missing_reason,
            'enabled': source.enabled_by_default,
        }
    return status


def has_source_credentials(source: CatalogSource) -> bool:
    if not source.requires_api_key:
        return True
    return any(str(os.environ.get(env_name) or '').strip() for env_name in credential_env_names(source))


def credential_env_names(source: CatalogSource) -> tuple[str, ...]:
    values: list[str] = []
    for value in (source.credential_env, *source.credential_aliases):
        text = str(value or '').strip()
        if text and text not in values:
            values.append(text)
    return tuple(values)


def merge_source_fields(sources: tuple[CatalogSource, ...] | list[CatalogSource]) -> dict[str, tuple[str, ...]]:
    by_source: dict[str, list[str]] = {}
    for source in sources:
        fields = by_source.setdefault(source.source_id, [])
        if source.field not in fields:
            fields.append(source.field)
    return {source_id: tuple(fields) for source_id, fields in by_source.items()}


def specs_from_catalog_sources(sources: tuple[CatalogSource, ...] | list[CatalogSource]) -> tuple[SourceSpec, ...]:
    fields_by_source = merge_source_fields(sources)
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for source in sources:
        if source.source_id in seen:
            continue
        seen.add(source.source_id)
        specs.append(source.to_source_spec(fields=fields_by_source.get(source.source_id, (source.field,))))
    return tuple(specs)


def with_fields(source: CatalogSource, fields: tuple[str, ...]) -> CatalogSource:
    return replace(source, field=fields[0] if fields else source.field)


def _source_fields(
    source_id: str,
    catalog: dict[str, tuple[CatalogSource, ...]],
) -> tuple[str, ...]:
    fields: list[str] = []
    for field, sources in catalog.items():
        if any(source.source_id == source_id for source in sources):
            fields.append(field)
    return tuple(fields)


def _dedupe_sources(sources) -> list[CatalogSource]:
    out: list[CatalogSource] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (source.source_id, source.field)
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return out


def _free(
    field: str,
    source_id: str,
    name: str,
    form: str,
    trust_level: str,
    ttl_hours: int,
    method: str,
    layer: str,
    parser: str,
    url: str = '',
    supports_pdf_body: bool = False,
    evidence_origin: str = 'news',
    source_role: str = 'secondary',
    stable_output: bool = False,
    recommended_extractor: str = 'manual',
    implementation_status: str = 'implemented',
) -> CatalogSource:
    return CatalogSource(
        field=field,
        source_id=source_id,
        name=name,
        form=form,
        trust_level=trust_level,
        access_mode='free',
        ttl_hours=ttl_hours,
        requires_api_key=False,
        method=method,
        layer=layer,
        parser=parser,
        url=url,
        supports_pdf_body=bool(supports_pdf_body),
        evidence_origin=evidence_origin,
        source_role=source_role,
        stable_output=bool(stable_output),
        recommended_extractor=recommended_extractor,
        implementation_status=implementation_status,
    )


def _api(
    field: str,
    source_id: str,
    name: str,
    form: str,
    trust_level: str,
    ttl_hours: int,
    credential_env: str,
    layer: str,
    credential_aliases: tuple[str, ...] = (),
) -> CatalogSource:
    return CatalogSource(
        field=field,
        source_id=source_id,
        name=name,
        form=form,
        trust_level=trust_level,
        access_mode='optional_api',
        ttl_hours=ttl_hours,
        requires_api_key=True,
        method='api',
        layer=layer,
        parser=source_id.replace(':', '.'),
        credential_env=credential_env,
        credential_aliases=tuple(credential_aliases),
        evidence_origin='optional_api',
        source_role='primary' if 'financial' in source_id or 'wind' in source_id or 'choice' in source_id else 'secondary',
        stable_output=True,
        recommended_extractor='json_api',
        implementation_status='optional_api',
    )


def _firecrawl_context(
    field: str,
    source_id: str,
    name: str,
    layer: str,
    source_role: str,
) -> CatalogSource:
    return CatalogSource(
        field=field,
        source_id=source_id,
        name=name,
        form='web_context_markdown',
        trust_level='secondary',
        access_mode='optional_api',
        ttl_hours=12,
        requires_api_key=True,
        method='requests',
        layer=layer,
        parser='firecrawl_scrape',
        credential_env='FIRECRAWL_API_KEY',
        enabled_by_default=False,
        evidence_origin='web_context',
        source_role=source_role,
        stable_output=False,
        recommended_extractor='firecrawl_scrape',
        implementation_status='implemented',
    )


def _company_announcements(
    field: str,
    *,
    evidence_origin: str = 'disclosure_pdf',
    source_role: str = 'primary',
    recommended_extractor: str = 'pdf_text',
    implementation_status: str = 'implemented',
) -> CatalogSource:
    return _free(
        field,
        'requests:company_announcements',
        'Public company announcement pages',
        _CNINFO_ANNOUNCEMENT_FORM,
        'primary',
        6,
        'requests',
        'company',
        _CNINFO_ANNOUNCEMENT_PARSER,
        _CNINFO_ANNOUNCEMENT_URL,
        supports_pdf_body=True,
        evidence_origin=evidence_origin,
        source_role=source_role,
        stable_output=True,
        recommended_extractor=recommended_extractor,
        implementation_status=implementation_status,
    )


def _periodic_reports(field: str) -> CatalogSource:
    return _company_announcements(
        field,
        evidence_origin='periodic_report',
        source_role='primary',
        recommended_extractor='pdf_text',
        implementation_status='implemented',
    )


_CATALOG = (
    _free('macro', 'existing:market_context', 'Market emotion and global context', 'local_context', 'secondary', 1, 'api', 'macro', 'core.market_context_provider', evidence_origin='market_behavior', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _free('macro', 'rss:reuters_business', 'Reuters business RSS', 'rss', 'media', 3, 'rss', 'macro', 'feedparser', 'https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best', evidence_origin='news', source_role='secondary', recommended_extractor='rss'),
    _api('macro', 'api:gdelt_macro', 'GDELT macro news API', 'json_api', 'media', 3, 'ALDEBARAN_GDELT_KEY', 'macro', ('GDELT_API_KEY', 'GDELT_KEY')),

    _free('policy', 'requests:policy_pages', 'Official policy and regulator public pages', 'official_http_json', 'official', 6, 'requests', 'policy', 'miit_search_api', 'https://www.miit.gov.cn/search-front-server/api/search/info', evidence_origin='official_stats', source_role='primary', stable_output=True, recommended_extractor='json_api'),
    _firecrawl_context('policy', 'firecrawl:policy_web_context', 'Firecrawl policy web context', 'policy', 'context'),
    _api('policy', 'api:wind_policy', 'Wind policy and regulator feed', 'vendor_api', 'primary', 12, 'ALDEBARAN_WIND_KEY', 'policy', ('WIND_API_KEY', 'WIND_TOKEN')),

    _company_announcements('company_disclosure'),
    _api('company_disclosure', 'api:cninfo_disclosure', 'CNINFO disclosure API', 'vendor_api', 'primary', 6, 'ALDEBARAN_CNINFO_KEY', 'company', ('CNINFO_API_KEY', 'CNINFO_TOKEN')),

    _free('financial', 'existing:fundamentals', 'Local fundamentals provider', 'local_context', 'secondary', 24, 'api', 'financial', 'core.fundamentals_provider', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _periodic_reports('financial'),
    _api('financial', 'api:tushare_pro', 'Tushare Pro financial statements', 'vendor_api', 'primary', 24, 'ALDEBARAN_TUSHARE_KEY', 'financial', ('TUSHARE_TOKEN', 'TUSHARE_PRO_TOKEN', 'TUSHARE_KEY')),
    _api('financial', 'api:wind_financial', 'Wind financial statements', 'vendor_api', 'primary', 24, 'ALDEBARAN_WIND_KEY', 'financial', ('WIND_API_KEY', 'WIND_TOKEN')),
    _api('financial', 'api:choice_financial', 'Choice financial statements', 'vendor_api', 'primary', 24, 'ALDEBARAN_CHOICE_KEY', 'financial', ('CHOICE_API_KEY', 'CHOICE_TOKEN')),

    _free('cost', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _periodic_reports('cost'),
    _free('cost', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='official_stats', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _api('cost', 'api:rqdata_industry_cost', 'RQData industry cost factors', 'vendor_api', 'secondary', 12, 'ALDEBARAN_RQDATA_KEY', 'cost', ('RQDATA_KEY', 'RQDATA_TOKEN')),

    _free('demand', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _periodic_reports('demand'),
    _free('demand', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='official_stats', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _api('demand', 'api:choice_industry_demand', 'Choice industry demand indicators', 'vendor_api', 'secondary', 12, 'ALDEBARAN_CHOICE_KEY', 'demand', ('CHOICE_API_KEY', 'CHOICE_TOKEN')),

    _free('export', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _free('export', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='official_stats', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _periodic_reports('export'),
    _firecrawl_context('export', 'firecrawl:export_web_context', 'Firecrawl export web context', 'export', 'secondary'),
    _api('export', 'api:tushare_customs', 'Tushare customs and export data', 'vendor_api', 'secondary', 24, 'ALDEBARAN_TUSHARE_KEY', 'export', ('TUSHARE_TOKEN', 'TUSHARE_PRO_TOKEN', 'TUSHARE_KEY')),

    _free('customer_supplier', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _periodic_reports('customer_supplier'),
    _free('customer_supplier', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),

    _free('inventory', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _free('inventory', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _periodic_reports('inventory'),
    _api('inventory', 'api:rqdata_inventory', 'RQData inventory indicators', 'vendor_api', 'secondary', 12, 'ALDEBARAN_RQDATA_KEY', 'inventory', ('RQDATA_KEY', 'RQDATA_TOKEN')),

    _free('capacity', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _company_announcements('capacity'),
    _free('capacity', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),

    _free('competition', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _free('competition', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _firecrawl_context('competition', 'firecrawl:industry_web_context', 'Firecrawl industry web context', 'industry', 'context'),
    _api('competition', 'api:wind_competition', 'Wind peer and competition data', 'vendor_api', 'secondary', 24, 'ALDEBARAN_WIND_KEY', 'competition', ('WIND_API_KEY', 'WIND_TOKEN')),

    _free('order_contract', 'existing:thin_layer_context', 'Existing thin-layer context evidence', 'local_context', 'secondary', 6, 'api', 'company', 'core.intelligence.collectors_builtin.ThinLayerContextCollector', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _company_announcements('order_contract'),
    _free('order_contract', 'requests:industry_chain_pages', 'Public industry-chain evidence pages', 'public_http_json', 'secondary', 12, 'requests', 'company', 'public_chain_search', 'https://www.cninfo.com.cn/new/hisAnnouncement/query', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),

    _free('trading_behavior', 'existing:public_fund_evidence', 'LHB, northbound and block-trade public evidence', 'local_context', 'secondary', 6, 'api', 'trading_behavior', 'core.public_fund_evidence.get_public_evidence', evidence_origin='exchange_data', source_role='secondary', stable_output=True, recommended_extractor='json_api'),
    _free('trading_behavior', 'existing:money_flow', 'Existing stock money-flow profile', 'local_context', 'secondary', 1, 'api', 'trading_behavior', 'core.data_orchestrator.flow_profile', evidence_origin='market_behavior', source_role='secondary', stable_output=True, recommended_extractor='json_api'),
    _api('trading_behavior', 'api:tushare_dragon_tiger', 'Tushare trading behavior data', 'vendor_api', 'primary', 6, 'ALDEBARAN_TUSHARE_KEY', 'trading_behavior', ('TUSHARE_TOKEN', 'TUSHARE_PRO_TOKEN', 'TUSHARE_KEY')),

    _free('risk', 'existing:event_calendar', 'Event calendar from existing disclosure signals', 'local_context', 'secondary', 24, 'api', 'risk', 'core.restricted_release_provider.fetch_restricted_release', evidence_origin='ir_activity', source_role='fallback', stable_output=True, recommended_extractor='manual'),
    _company_announcements('risk'),
    _periodic_reports('risk'),
    _firecrawl_context('risk', 'firecrawl:risk_web_context', 'Firecrawl risk web context', 'risk', 'secondary'),
    _api('risk', 'api:wind_risk', 'Wind risk events', 'vendor_api', 'primary', 6, 'ALDEBARAN_WIND_KEY', 'risk', ('WIND_API_KEY', 'WIND_TOKEN')),

    _free('news_sentiment', 'existing:stock_news', 'Eastmoney stock news via existing provider', 'local_context', 'media', 3, 'api', 'news_event', 'core.stock_news_provider.fetch_stock_news', evidence_origin='news', source_role='fallback', stable_output=False, recommended_extractor='manual'),
    _free('news_sentiment', 'requests:eastmoney_kuaixun', 'Eastmoney flash news HTTP endpoint', 'public_http_html', 'media', 1, 'requests', 'news_event', 'core.intel_fetcher._fetch_eastmoney_kuaixun', 'https://kuaixun.eastmoney.com/', evidence_origin='news', source_role='secondary', stable_output=False, recommended_extractor='html_table'),
    _free('news_sentiment', 'rss:cnbc_finance', 'CNBC finance RSS', 'rss', 'media', 3, 'rss', 'macro', 'feedparser', 'https://www.cnbc.com/id/10000664/device/rss/rss.html', evidence_origin='news', source_role='secondary', recommended_extractor='rss'),
    _api('news_sentiment', 'api:gdelt_news', 'GDELT news sentiment API', 'json_api', 'media', 3, 'ALDEBARAN_GDELT_KEY', 'news_event', ('GDELT_API_KEY', 'GDELT_KEY')),
)
