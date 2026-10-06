"""Registry of intelligence sources available to the stock EvidenceGraph."""
from __future__ import annotations

from core.intelligence.models import SourceSpec


SOURCE_SPECS = (
    SourceSpec(
        source_id='existing:intel_feed',
        name='Local structured intelligence feed',
        layer='news_event',
        method='api',
        ttl_hours=1,
        rate_limit_seconds=0,
        enabled=True,
        parser='core.intel_matcher.load_intel_feed',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:stock_news',
        name='Eastmoney stock news via existing provider',
        layer='news_event',
        method='api',
        ttl_hours=3,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.stock_news_provider.fetch_stock_news',
        trust_level='media',
    ),
    SourceSpec(
        source_id='existing:fundamentals',
        name='Local fundamentals provider',
        layer='financial',
        method='api',
        ttl_hours=24,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.fundamentals_provider',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:public_fund_evidence',
        name='LHB, northbound and block-trade public evidence',
        layer='trading_behavior',
        method='api',
        ttl_hours=6,
        rate_limit_seconds=2,
        enabled=True,
        parser='core.public_fund_evidence.get_public_evidence',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:money_flow',
        name='Existing stock money-flow profile',
        layer='trading_behavior',
        method='api',
        ttl_hours=1,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.data_orchestrator.flow_profile',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:restricted_release',
        name='Restricted share release calendar',
        layer='shareholder',
        method='api',
        ttl_hours=24,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.restricted_release_provider.fetch_restricted_release',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:event_calendar',
        name='Event calendar from existing disclosure signals',
        layer='risk',
        method='api',
        ttl_hours=24,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.restricted_release_provider.fetch_restricted_release',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:market_context',
        name='Market emotion and global context',
        layer='macro',
        method='api',
        ttl_hours=1,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.market_context_provider',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='existing:thin_layer_context',
        name='Existing thin-layer context evidence',
        layer='company',
        method='api',
        ttl_hours=6,
        rate_limit_seconds=1,
        enabled=True,
        parser='core.intelligence.collectors_builtin.ThinLayerContextCollector',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='rss:reuters_business',
        name='Reuters business RSS',
        layer='macro',
        method='rss',
        url='https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best',
        ttl_hours=3,
        rate_limit_seconds=5,
        enabled=True,
        parser='feedparser',
        trust_level='media',
    ),
    SourceSpec(
        source_id='rss:cnbc_finance',
        name='CNBC finance RSS',
        layer='macro',
        method='rss',
        url='https://www.cnbc.com/id/10000664/device/rss/rss.html',
        ttl_hours=3,
        rate_limit_seconds=5,
        enabled=True,
        parser='feedparser',
        trust_level='media',
    ),
    SourceSpec(
        source_id='requests:eastmoney_kuaixun',
        name='Eastmoney flash news HTTP endpoint',
        layer='news_event',
        method='requests',
        url='https://kuaixun.eastmoney.com/',
        ttl_hours=1,
        rate_limit_seconds=3,
        enabled=True,
        parser='core.intel_fetcher._fetch_eastmoney_kuaixun',
        trust_level='media',
    ),
    SourceSpec(
        source_id='requests:company_announcements',
        name='Public company announcement pages',
        layer='company',
        method='requests',
        url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
        ttl_hours=6,
        rate_limit_seconds=3,
        enabled=True,
        parser='cninfo_his_announcement',
        trust_level='primary',
    ),
    SourceSpec(
        source_id='requests:policy_pages',
        name='Official policy and regulator public pages',
        layer='policy',
        method='requests',
        url='https://www.miit.gov.cn/search-front-server/api/search/info',
        ttl_hours=6,
        rate_limit_seconds=3,
        enabled=True,
        parser='miit_search_api',
        trust_level='official',
    ),
    SourceSpec(
        source_id='requests:industry_chain_pages',
        name='Public industry-chain evidence pages',
        layer='company',
        method='requests',
        url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
        ttl_hours=12,
        rate_limit_seconds=3,
        enabled=True,
        parser='public_chain_search',
        trust_level='secondary',
    ),
    SourceSpec(
        source_id='scrapling:policy_static_demo',
        name='Official policy page static pilot',
        layer='policy',
        method='scrapling_static',
        url='https://www.gov.cn/zhengce/zuixin/',
        ttl_hours=24,
        rate_limit_seconds=10,
        enabled=False,
        parser='PolicyPageCollector',
        trust_level='official',
    ),
    SourceSpec(
        source_id='scrapling:announcement_static_demo',
        name='Company announcement page static pilot',
        layer='company',
        method='scrapling_static',
        url='https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search',
        ttl_hours=24,
        rate_limit_seconds=10,
        enabled=False,
        parser='AnnouncementPageCollector',
        trust_level='primary',
    ),
    SourceSpec(
        source_id='scrapling:industry_price_static_demo',
        name='Industry price page static pilot',
        layer='cost',
        method='scrapling_static',
        url='https://www.mysteel.com/',
        ttl_hours=12,
        rate_limit_seconds=10,
        enabled=False,
        parser='IndustryPriceCollector',
        trust_level='secondary',
    ),
)


def get_source_spec(source_id: str) -> SourceSpec | None:
    for spec in SOURCE_SPECS:
        if spec.source_id == source_id:
            return spec
    return None


def get_enabled_sources(layer: str | None = None, method: str | None = None) -> list[SourceSpec]:
    sources = [spec for spec in SOURCE_SPECS if spec.enabled]
    if layer:
        sources = [spec for spec in sources if spec.layer == layer]
    if method:
        sources = [spec for spec in sources if spec.method == method]
    return sources


def source_status_for_specs(specs: tuple[SourceSpec, ...] = SOURCE_SPECS) -> dict[str, dict]:
    return {
        spec.source_id: {
            'layer': spec.layer,
            'method': spec.method,
            'trust_level': spec.trust_level,
            'parser': spec.parser,
            'enabled': spec.enabled,
            'ttl_hours': spec.ttl_hours,
            'status': 'registered' if spec.enabled else 'disabled',
        }
        for spec in specs
    }
