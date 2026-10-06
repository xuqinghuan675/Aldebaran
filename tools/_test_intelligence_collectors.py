import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _company_announcement_spec():
    from core.intelligence.models import SourceSpec

    return SourceSpec(
        source_id='requests:company_announcements',
        name='Company Announcements',
        layer='company',
        method='requests',
        url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
        parser='cninfo_his_announcement',
        trust_level='primary',
        ttl_hours=6,
    )


class IntelligenceCollectorTest(unittest.TestCase):
    def test_collector_interface_runs_and_isolates_single_source_failure(self):
        from core.intelligence.collector_base import Collector, run_collector, run_collectors
        from core.intelligence.models import NormalizedIntelItem, RawIntelItem, SourceSpec

        spec = SourceSpec(
            source_id='test:ok',
            name='OK Source',
            layer='demand',
            method='api',
            parser='test',
            ttl_hours=72,
        )
        failing_spec = SourceSpec(
            source_id='test:fail',
            name='Fail Source',
            layer='cost',
            method='api',
            parser='test',
            ttl_hours=3,
        )

        class OKCollector(Collector):
            source_id = 'test:ok'

            def fetch(self, spec, *, code='', name='', context=None):
                return [RawIntelItem(
                    source_id=spec.source_id,
                    source_name=spec.name,
                    source_url=spec.url,
                    fetched_at=datetime.now().isoformat(timespec='seconds'),
                    published_at=datetime.now().isoformat(timespec='seconds'),
                    title='Demand order text',
                    text='Public text says demand improved, without a metric.',
                    layer=spec.layer,
                    trust_level=spec.trust_level,
                )]

            def normalize(self, raw, *, code='', name='', context=None):
                return [NormalizedIntelItem(
                    id='test:item',
                    source_id=raw.source_id,
                    title=raw.title,
                    summary=raw.text,
                    layer=raw.layer,
                    direction='neutral',
                    related_codes=[code],
                    related_names=[name],
                    related_sectors=[],
                    evidence_type='news',
                    published_at=raw.published_at,
                    fetched_at=raw.fetched_at,
                    url=raw.url,
                    trust_level=raw.trust_level,
                    confidence=4,
                    time_windows=['short'],
                )]

        class FailCollector(Collector):
            source_id = 'test:fail'

            def fetch(self, spec, *, code='', name='', context=None):
                raise RuntimeError('source unavailable')

        ok_result = run_collector(OKCollector(), spec, code='600584', name='Stock A')
        self.assertTrue(ok_result.ok)
        self.assertEqual(len(ok_result.raw_items), 1)
        self.assertEqual(len(ok_result.normalized_items), 1)
        self.assertEqual(ok_result.normalized_items[0].freshness, 'fresh')

        results = run_collectors(
            [FailCollector(), OKCollector()],
            [failing_spec, spec],
            code='600584',
            name='Stock A',
        )
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0].ok)
        self.assertTrue(results[1].ok)
        self.assertIn('source unavailable', results[0].error)

    def test_builtin_collectors_use_context_and_skip_technical_terms(self):
        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        specs = [
            SourceSpec(
                source_id='existing:stock_news',
                name='Stock News',
                layer='news_event',
                method='api',
                parser='test',
                trust_level='media',
                ttl_hours=3,
            ),
            SourceSpec(
                source_id='existing:fundamentals',
                name='Fundamentals',
                layer='financial',
                method='api',
                parser='test',
                trust_level='secondary',
                ttl_hours=24,
            ),
            SourceSpec(
                source_id='existing:public_fund_evidence',
                name='Trading Evidence',
                layer='trading_behavior',
                method='api',
                parser='test',
                trust_level='secondary',
                ttl_hours=6,
            ),
            SourceSpec(
                source_id='existing:money_flow',
                name='Money Flow',
                layer='trading_behavior',
                method='api',
                parser='test',
                trust_level='secondary',
                ttl_hours=1,
            ),
        ]
        context = {
            'subject': {'code': '600584', 'name': 'Stock A'},
            'stock_news': [
                {
                    'title': 'Stock A signed a new export order',
                    'content': 'Public announcement text says export demand improved, without numeric value.',
                    'date': '2026-07-03',
                    'url': 'https://example.com/order',
                },
                {
                    'title': 'MACD buy signal',
                    'content': 'This technical-analysis item must not enter intelligence collectors.',
                    'date': '2026-07-03',
                    'url': 'https://example.com/technical',
                },
            ],
            'context': {
                'fundamental_summary': 'Financial summary text without structured numeric metric.',
                'public_fund_evidence': {
                    'available': True,
                    'date': '2026-07-03',
                    'dzjy': {
                        'hit': True,
                        'rows': 1,
                        'premium_pct_avg': -1.2,
                        'amount_yi': 0.8,
                    },
                },
                'flow_profile': {
                    'available': True,
                    'days': 5,
                    'main_5d': 23000000,
                    'main_streak': 3,
                },
            },
        }

        batch = collect_builtin_sources(
            '600584',
            'Stock A',
            context=context,
            specs=specs,
            use_cache=False,
        )

        self.assertGreaterEqual(len(batch['raw_items']), 4)
        self.assertGreaterEqual(len(batch['normalized_items']), 4)
        self.assertIn('existing:stock_news', batch['source_status'])
        self.assertIn('existing:money_flow', batch['source_status'])
        self.assertEqual(batch['source_status']['existing:stock_news']['status'], 'ok')

        rendered = json.dumps(
            [item.to_dict() for item in batch['normalized_items']],
            ensure_ascii=False,
        )
        for forbidden in ('K线', '均线', 'MACD', 'RSI', 'KDJ', '买点', '止损', '支撑', '压力'):
            self.assertNotIn(forbidden, rendered)
        text_only = next(item for item in batch['normalized_items'] if item.layer == 'order_contract')
        self.assertIsNone(text_only.current_value)
        self.assertEqual(text_only.expectation_gap, 'unknown')
        self.assertTrue(text_only.time_windows)

    def test_thin_layer_context_collector_extracts_seed_evidence(self):
        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        spec = SourceSpec(
            source_id='existing:thin_layer_context',
            name='Thin Layer Context',
            layer='company',
            method='api',
            parser='test',
            trust_level='secondary',
            ttl_hours=6,
        )
        context = {
            'subject': {'code': '600584', 'name': 'Stock A'},
            'variable_snapshots': {
                'cost': {
                    'summary': 'Raw material cost pressure eased versus last month.',
                    'direction': 'bullish',
                    'metric_name': 'raw_material_cost_index',
                    'current_value': 92.5,
                    'unit': 'index',
                    'expectation_gap': 'better_than_expected',
                    'time_windows': ['swing', 'mid'],
                },
                'demand': {
                    'summary': 'Downstream demand visibility improved after customer restocking.',
                    'direction': 'bullish',
                },
                'inventory': {
                    'summary': 'Channel inventory remains elevated.',
                    'direction': 'bearish',
                },
            },
            'context': {
                'export_summary': 'Export orders increased as overseas customer pull-in improved.',
                'capacity_summary': {
                    'summary': 'New packaging capacity ramp stayed on schedule.',
                    'direction': 'bullish',
                    'published_at': '2026-07-02',
                },
                'competition_summary': 'Competitors are cutting prices in mature products.',
                'supplier_summary': 'Key supplier delivery has normalized.',
                'order_contract_summary': {
                    'summary': 'The company signed a large customer order contract.',
                    'direction': 'bullish',
                    'url': 'https://example.com/order-contract',
                },
            },
        }

        batch = collect_builtin_sources(
            '600584',
            'Stock A',
            context=context,
            specs=[spec],
            use_cache=False,
        )

        by_layer = {item.layer: item for item in batch['normalized_items']}
        self.assertTrue({
            'cost',
            'demand',
            'export',
            'customer_supplier',
            'inventory',
            'capacity',
            'competition',
            'order_contract',
        } <= set(by_layer))
        self.assertEqual(by_layer['cost'].metric_name, 'raw_material_cost_index')
        self.assertEqual(by_layer['cost'].current_value, 92.5)
        self.assertEqual(by_layer['cost'].expectation_gap, 'better_than_expected')
        self.assertEqual(by_layer['order_contract'].url, 'https://example.com/order-contract')
        self.assertEqual(batch['source_status']['existing:thin_layer_context']['status'], 'ok')

    def test_requests_eastmoney_kuaixun_pilot_uses_existing_fetcher(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        spec = SourceSpec(
            source_id='requests:eastmoney_kuaixun',
            name='Eastmoney Flash',
            layer='news_event',
            method='requests',
            parser='test',
            trust_level='media',
            ttl_hours=1,
        )

        with patch('core.intel_fetcher._fetch_eastmoney_kuaixun') as fetch:
            fetch.return_value = [
                {
                    'title': 'Stock A wins a new advanced packaging order contract',
                    'source': 'Eastmoney',
                },
                {
                    'title': 'MACD buy point appears for unrelated stock',
                    'source': 'Eastmoney',
                },
            ]
            batch = collect_builtin_sources(
                '600584',
                'Stock A',
                context={'sectors': ['advanced packaging']},
                specs=[spec],
                use_cache=False,
            )

        self.assertEqual(len(batch['normalized_items']), 1)
        item = batch['normalized_items'][0]
        self.assertEqual(item.source_id, 'requests:eastmoney_kuaixun')
        self.assertEqual(item.layer, 'order_contract')
        self.assertEqual(item.evidence_type, 'news')
        self.assertEqual(batch['source_status']['requests:eastmoney_kuaixun']['status'], 'ok')
        fetch.assert_called_once()

    def test_requests_eastmoney_kuaixun_falls_back_to_static_html(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        class Response:
            text = (
                '<html><body>'
                '<a href="https://example.com/order">Stock A export order contract expands</a>'
                '<a href="https://example.com/tech">MACD buy point appears</a>'
                '</body></html>'
            )

            def raise_for_status(self):
                return None

        spec = SourceSpec(
            source_id='requests:eastmoney_kuaixun',
            name='Eastmoney Flash',
            layer='news_event',
            method='requests',
            url='https://kuaixun.eastmoney.com/',
            parser='test',
            trust_level='media',
            ttl_hours=1,
        )

        with patch('core.intel_fetcher._fetch_eastmoney_kuaixun', side_effect=RuntimeError('api gone')), \
                patch('core.http_client.get', return_value=Response()) as http_get:
            batch = collect_builtin_sources(
                '600584',
                'Stock A',
                context={'sectors': ['advanced packaging']},
                specs=[spec],
                use_cache=False,
            )

        self.assertEqual(len(batch['normalized_items']), 1)
        item = batch['normalized_items'][0]
        self.assertEqual(item.layer, 'order_contract')
        self.assertEqual(item.url, 'https://example.com/order')
        self.assertEqual(batch['source_status']['requests:eastmoney_kuaixun']['status'], 'ok')
        http_get.assert_called_once()

    def test_rss_collectors_parse_filter_and_preserve_relevance(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel>
              <item>
                <title>AI chip demand lifts advanced packaging suppliers</title>
                <description>Semiconductor demand and customer orders improved.</description>
                <link>https://example.com/ai-chip-demand</link>
                <pubDate>Fri, 03 Jul 2026 08:00:00 GMT</pubDate>
              </item>
              <item>
                <title>MACD buy point appears in tech shares</title>
                <description>Technical analysis should be filtered.</description>
                <link>https://example.com/technical</link>
                <pubDate>Fri, 03 Jul 2026 09:00:00 GMT</pubDate>
              </item>
              <item>
                <title>Retail sales update unrelated to the subject</title>
                <description>No matching code, name, sector or upstream keyword.</description>
                <link>https://example.com/retail</link>
                <pubDate>Fri, 03 Jul 2026 10:00:00 GMT</pubDate>
              </item>
            </channel></rss>
            """

            def raise_for_status(self):
                return None

        spec = SourceSpec(
            source_id='rss:reuters_business',
            name='Reuters Business',
            layer='macro',
            method='rss',
            url='https://example.com/rss.xml',
            parser='feedparser',
            trust_level='media',
            ttl_hours=3,
        )

        with patch('core.http_client.get', return_value=Response()) as http_get:
            batch = collect_builtin_sources(
                '600584',
                'Stock A',
                context={'sectors': ['advanced packaging', 'semiconductor']},
                specs=[spec],
                use_cache=False,
            )

        self.assertEqual(len(batch['normalized_items']), 1)
        item = batch['normalized_items'][0]
        self.assertEqual(item.source_id, 'rss:reuters_business')
        self.assertEqual(item.layer, 'demand')
        self.assertEqual(item.url, 'https://example.com/ai-chip-demand')
        self.assertEqual(item.published_at, 'Fri, 03 Jul 2026 08:00:00 GMT')
        self.assertIn('sector:', item.raw_ref)
        self.assertEqual(batch['source_status']['rss:reuters_business']['status'], 'ok')
        http_get.assert_called_once()

    def test_multiple_rss_items_keep_unique_graph_ids(self):
        from unittest.mock import patch

        from core.evidence_seed_builder import build_evidence_pack_from_seed
        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        class Response:
            text = """<?xml version="1.0" encoding="UTF-8"?>
            <rss><channel>
              <item>
                <title>AI image copyright policy supports visual content platforms</title>
                <description>AIGC copyright policy affects image licensing demand.</description>
                <link>https://example.com/aigc-copyright</link>
                <pubDate>Fri, 03 Jul 2026 08:00:00 GMT</pubDate>
              </item>
              <item>
                <title>Visual content customers increase AI generated image licensing</title>
                <description>Customer demand improved for copyright-cleared image datasets.</description>
                <link>https://example.com/image-licensing</link>
                <pubDate>Fri, 03 Jul 2026 09:00:00 GMT</pubDate>
              </item>
            </channel></rss>
            """

            def raise_for_status(self):
                return None

        spec = SourceSpec(
            source_id='rss:cnbc_finance',
            name='CNBC Finance',
            layer='macro',
            method='rss',
            url='https://example.com/rss.xml',
            parser='feedparser',
            trust_level='media',
            ttl_hours=3,
        )

        with patch('core.http_client.get', return_value=Response()):
            batch = collect_builtin_sources(
                '000681',
                'Visual China',
                context={'sectors': ['AIGC', 'visual content']},
                specs=[spec],
                use_cache=False,
            )

        self.assertEqual(len(batch['normalized_items']), 2)
        self.assertEqual(len({item.id for item in batch['normalized_items']}), 2)
        pack = build_evidence_pack_from_seed({
            'subject': {'code': '000681', 'name': 'Visual China'},
            'normalized_intel_items': [item.to_dict() for item in batch['normalized_items']],
            'intel_events': [],
            'stock_news': [],
            'context': {},
        })
        collector_nodes = [node for node in pack.nodes if str(node.id).startswith('intel:')]
        self.assertEqual(len(collector_nodes), 2)

    def test_public_requests_collectors_parse_structured_public_endpoints(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        class Response:
            text = ''

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_post(url, **kwargs):
            return Response({
                'announcements': [
                    {
                        'secCode': '600584',
                        'secName': 'Stock A',
                        'announcementTitle': 'Stock A signs advanced packaging customer order contract',
                        'announcementTime': 1781107200000,
                        'adjunctUrl': 'finalpage/2026-06-11/contract.PDF',
                    },
                    {
                        'secCode': '600584',
                        'secName': 'Stock A',
                        'announcementTitle': '\u80a1\u4e1c\u5927\u4f1a\u6cd5\u5f8b\u610f\u89c1\u4e66',
                        'announcementTime': 1781107200000,
                        'adjunctUrl': 'finalpage/2026-06-11/noise.PDF',
                    },
                ],
            })

        def fake_get(url, **kwargs):
            return Response({
                'data': {
                    'searchResult': {
                        'dataResults': [
                            {
                                'groupData': [
                                    {'data': {
                                        'title': '\u5de5\u4fe1\u90e8\u5370\u53d1\u96c6\u6210\u7535\u8def\u4ea7\u4e1a\u653f\u7b56\u901a\u77e5',
                                        'infocontent': 'Policy supports semiconductor supply chain export.',
                                        'deploytime': 1781107200000,
                                        'url': '/zwgk/zcwj/wjfb/art/2026/policy.html',
                                    }}
                                ]
                            }
                        ]
                    }
                }
            })

        specs = [
            SourceSpec(
                source_id='requests:company_announcements',
                name='Company Announcements',
                layer='company',
                method='requests',
                url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
                parser='cninfo_his_announcement',
                trust_level='primary',
                ttl_hours=6,
            ),
            SourceSpec(
                source_id='requests:policy_pages',
                name='Policy Pages',
                layer='policy',
                method='requests',
                url='https://www.miit.gov.cn/search-front-server/api/search/info',
                parser='miit_search_api',
                trust_level='official',
                ttl_hours=6,
            ),
        ]

        with patch('core.http_client.post', side_effect=fake_post), \
                patch('core.http_client.get', side_effect=fake_get):
            batch = collect_builtin_sources(
                '600584',
                'Stock A',
                context={'sectors': ['semiconductor', 'export']},
                specs=specs,
                use_cache=False,
            )

        layers_by_source = {}
        for item in batch['normalized_items']:
            layers_by_source.setdefault(item.source_id, set()).add(item.layer)
        self.assertEqual(set(layers_by_source), {'requests:company_announcements', 'requests:policy_pages'})
        self.assertIn('order_contract', layers_by_source['requests:company_announcements'])
        self.assertIn('customer_supplier', layers_by_source['requests:company_announcements'])
        self.assertEqual(layers_by_source['requests:policy_pages'], {'policy'})
        self.assertEqual(batch['source_status']['requests:company_announcements']['status'], 'ok')
        self.assertEqual(batch['source_status']['requests:policy_pages']['status'], 'ok')
        self.assertEqual(batch['coverage_summary']['real_node_count'], 3)
        self.assertEqual(batch['coverage_summary']['by_layer']['order_contract'], 1)
        self.assertEqual(batch['coverage_summary']['by_layer']['customer_supplier'], 1)

    def test_company_announcements_preserve_explicit_secondary_layers(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        customer = '\u5ba2\u6237'
        contract = '\u5408\u540c'
        overseas = '\u6d77\u5916'
        demand = '\u9700\u6c42'
        inventory = '\u5e93\u5b58'
        restock = '\u8865\u5e93'

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'announcements': [
                        {
                            'secCode': '300502',
                            'secName': '\u65b0\u6613\u76db',
                            'announcementTitle': (
                                f'\u5173\u4e8e\u4e0e{overseas}{customer}\u7b7e\u7f72{contract}\u5e76'
                                f'\u5e26\u52a8{demand}{restock}\u548c{inventory}\u6539\u5584\u7684\u516c\u544a'
                            ),
                            'announcementTime': 1781107200000,
                            'adjunctUrl': 'finalpage/2026-06-11/secondary.PDF',
                        },
                    ],
                }

        spec = SourceSpec(
            source_id='requests:company_announcements',
            name='Company Announcements',
            layer='company',
            method='requests',
            url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
            parser='cninfo_his_announcement',
            trust_level='primary',
            ttl_hours=6,
        )

        with patch('core.http_client.post', return_value=Response()):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'sectors': ['CPO']},
                specs=[spec],
                use_cache=False,
            )

        layers = {item.layer for item in batch['normalized_items']}
        self.assertTrue({'order_contract', 'demand', 'export', 'customer_supplier', 'inventory'} <= layers)
        self.assertEqual(len({item.id for item in batch['normalized_items']}), len(batch['normalized_items']))
        self.assertEqual(batch['coverage_summary']['by_layer']['order_contract'], 1)
        self.assertEqual(batch['coverage_summary']['by_layer']['demand'], 1)
        self.assertEqual(batch['coverage_summary']['by_layer']['export'], 1)
        self.assertEqual(batch['coverage_summary']['by_layer']['customer_supplier'], 1)
        self.assertEqual(batch['coverage_summary']['by_layer']['inventory'], 1)

    def test_company_announcements_default_does_not_download_disclosure_pdf(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'sectors': ['CPO']},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        self.assertIn('order_contract', {item.layer for item in batch['normalized_items']})
        self.assertNotIn(
            'announcement_pdf_body',
            {item.category for item in batch['normalized_items']},
        )

    def test_company_announcements_default_does_not_reuse_cached_pdf_body_status(self):
        from unittest.mock import patch

        from core.intelligence import cache_store
        from core.intelligence.collectors_builtin import collect_builtin_sources

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}), \
                patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            cache_store.save_source_status({
                'requests:company_announcements': {
                    'status': 'ok',
                    'pdf_body': {
                        'attempted': 1,
                        'parsed': 1,
                        'evidence_count': 3,
                        'statuses': {'ok': 1},
                    },
                },
            })

            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'sectors': ['CPO']},
                specs=[_company_announcement_spec()],
                use_cache=True,
            )

        download_pdf.assert_not_called()
        self.assertNotIn(
            'pdf_body',
            batch['source_status']['requests:company_announcements'],
        )

    def test_company_announcements_parse_pdf_only_for_strong_matching_titles(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [
            {
                'secCode': '300502',
                'secName': '\u65b0\u6613\u76db',
                'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
                'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
                'published_at': '2026-06-11',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
            },
            {
                'secCode': '300502',
                'secName': '\u65b0\u6613\u76db',
                'title': '\u5173\u4e8e\u884c\u4e1a\u653f\u7b56\u901a\u77e5\u7684\u516c\u544a',
                'summary': '\u5173\u4e8e\u884c\u4e1a\u653f\u7b56\u901a\u77e5\u7684\u516c\u544a',
                'published_at': '2026-06-11',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/policy.PDF',
            },
        ]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            text='\u516c\u53f8\u4e0e\u5ba2\u6237\u7b7e\u7f72\u9500\u552e\u5408\u540c\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={
                    'sectors': ['CPO'],
                    'parse_disclosure_pdf': True,
                    'max_pdf_per_source': 5,
                    'pdf_timeout_seconds': 3,
                },
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_called_once()
        self.assertEqual(download_pdf.call_args.args[0], entries[0]['url'])
        self.assertIn('announcements', Path(download_pdf.call_args.args[1]).parts)
        self.assertEqual(download_pdf.call_args.kwargs['timeout_seconds'], 3)

    def test_company_announcements_skips_fx_hedging_pdf_body_noise(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u5f00\u5c55\u5916\u6c47\u5957\u671f\u4fdd\u503c\u4e1a\u52a1\u7684\u53ef\u884c\u6027\u5206\u6790\u62a5\u544a',
            'summary': '\u5173\u4e8e\u5f00\u5c55\u5916\u6c47\u5957\u671f\u4fdd\u503c\u4e1a\u52a1\u7684\u53ef\u884c\u6027\u5206\u6790\u62a5\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/fx-hedging.PDF',
        }]

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={
                    'parse_disclosure_pdf': True,
                    'max_pdf_per_source': 1,
                },
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        self.assertNotIn(
            'announcement_pdf_body',
            {item.category for item in batch['normalized_items']},
        )
        self.assertEqual(
            batch['source_status']['requests:company_announcements']['pdf_body']['statuses']['skipped_title_mismatch'],
            1,
        )

    def test_company_announcements_respects_max_pdf_per_source(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = []
        for index in range(3):
            entries.append({
                'secCode': '300502',
                'secName': '\u65b0\u6613\u76db',
                'title': f'\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c{index}\u7684\u516c\u544a',
                'summary': f'\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c{index}\u7684\u516c\u544a',
                'published_at': '2026-06-11',
                'url': f'https://static.cninfo.com.cn/finalpage/2026-06-11/contract-{index}.PDF',
            })
        download_result = DisclosurePdfResult(
            url='',
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url='',
            cache_path=Path('contract.PDF'),
            status='ok',
            text='\u516c\u53f8\u4e0e\u5ba2\u6237\u7b7e\u7f72\u9500\u552e\u5408\u540c\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={
                    'sectors': ['CPO'],
                    'parse_disclosure_pdf': True,
                    'max_pdf_per_source': 2,
                },
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertEqual(download_pdf.call_count, 2)

    def test_company_announcements_pdf_download_error_keeps_title_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='download_error',
            error='network down',
            fetched_at='2026-07-04T10:00:00',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text') as extract_pdf_text:
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_disclosure_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        extract_pdf_text.assert_not_called()
        self.assertIn('order_contract', {item.layer for item in batch['normalized_items']})
        self.assertEqual(
            batch['source_status']['requests:company_announcements']['pdf_body']['statuses']['download_error'],
            1,
        )
        self.assertIn('network down', batch['raw_items'][0].raw['pdf_body_status'][0]['error'])

    def test_company_announcements_pdf_empty_text_generates_no_body_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='empty_text',
            error='no text',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_disclosure_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertNotIn(
            'announcement_pdf_body',
            {item.category for item in batch['normalized_items']},
        )
        self.assertEqual(
            batch['source_status']['requests:company_announcements']['pdf_body']['statuses']['empty_text'],
            1,
        )

    def test_company_announcements_pdf_body_generates_multi_layer_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]
        body = (
            '\u516c\u53f8\u4e0e\u6d77\u5916\u5927\u5ba2\u6237\u7b7e\u7f72\u9500\u552e\u5408\u540c\u3002'
            '\u672c\u6b21\u51fa\u53e3\u8ba2\u5355\u5c06\u652f\u6301\u56fd\u9645\u5ba2\u6237\u9700\u6c42\u3002'
            '\u4e0b\u6e38\u8865\u5e93\u5e26\u52a8\u5e93\u5b58\u7ed3\u6784\u6539\u5584\u3002'
        )
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            text=body,
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_disclosure_pdf': True, 'max_pdf_text_chars': 2000},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        body_items = [
            item for item in batch['normalized_items']
            if item.category == 'announcement_pdf_body'
        ]
        self.assertTrue(
            {'order_contract', 'export', 'customer_supplier', 'inventory'}
            <= {item.layer for item in body_items}
        )
        self.assertTrue(all(item.evidence_type == 'announcement_pdf' for item in body_items))
        self.assertTrue(all(item.source_id == 'requests:company_announcements' for item in body_items))
        pdf_body = batch['source_status']['requests:company_announcements']['pdf_body']
        self.assertEqual(pdf_body['attempted'], 1)
        self.assertEqual(pdf_body['parsed'], 1)
        self.assertGreaterEqual(pdf_body['evidence_count'], 1)
        self.assertEqual(pdf_body['download_error'], 0)
        self.assertEqual(pdf_body['extract_error'], 0)
        self.assertEqual(pdf_body['empty_text'], 0)
        self.assertEqual(pdf_body['skipped_by_limit'], 0)

    def test_company_announcements_title_and_pdf_body_evidence_coexist(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            text='\u516c\u53f8\u4e0e\u5ba2\u6237\u7b7e\u7f72\u9500\u552e\u5408\u540c\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_disclosure_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        order_items = [item for item in batch['normalized_items'] if item.layer == 'order_contract']
        self.assertIn('company_announcement', {item.category for item in order_items})
        self.assertIn('announcement_pdf_body', {item.category for item in order_items})

    def test_company_announcements_pdf_technical_text_does_not_create_body_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        technical = ''.join(['M', 'A', 'C', 'D'])
        entries = [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
            'published_at': '2026-06-11',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('contract.PDF'),
            status='ok',
            text=technical + '\u91d1\u53c9\u540e\u51fa\u73b0\u4e70\u70b9\u548c\u6b62\u635f\u4f4d\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_disclosure_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertNotIn(
            'announcement_pdf_body',
            {item.category for item in batch['normalized_items']},
        )

    def test_company_announcements_default_does_not_download_periodic_report_pdf(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        entries = [{
            'secCode': '600584',
            'secName': '\u957f\u7535\u79d1\u6280',
            'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'sectors': ['\u5148\u8fdb\u5c01\u88c5']},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        self.assertIn('financial', {item.layer for item in batch['normalized_items']})
        self.assertNotIn(
            'periodic_report_body',
            {item.category for item in batch['normalized_items']},
        )
        self.assertNotIn(
            'periodic_report',
            batch['source_status']['requests:company_announcements'],
        )

    def test_company_announcements_periodic_report_parses_only_when_explicitly_enabled(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '600584',
            'secName': '\u957f\u7535\u79d1\u6280',
            'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]
        body = (
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u516c\u53f8\u4e3b\u8425\u4ea7\u54c1\u5b9e\u73b0\u8425\u4e1a\u6536\u5165100\u4ebf\u5143\uff0c'
            '\u4ea7\u54c1\u9500\u552e\u91cf\u589e\u957f\uff0c\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002'
            '\u5206\u5730\u533a\u6536\u5165\n'
            '\u5883\u5916\u6536\u5165\u5360\u6bd4\u63d0\u5347\uff0c\u6d77\u5916\u4e1a\u52a1\u8d21\u732e\u589e\u52a0\u3002'
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u6bd4\u4e3a42%\uff0c'
            '\u524d\u4e94\u5927\u4f9b\u5e94\u5546\u91c7\u8d2d\u989d\u5360\u6bd4\u4e3a38%\u3002'
            '\u5b58\u8d27\n'
            '\u671f\u672b\u5b58\u8d27\u4f59\u989d\u4e0a\u5347\uff0c\u5e76\u8ba1\u63d0\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u3002'
            '\u6210\u672c\u6784\u6210\n'
            '\u8425\u4e1a\u6210\u672c\u4e2d\u539f\u6750\u6599\u6210\u672c\u5360\u6bd4\u8f83\u9ad8\u3002'
            '\u98ce\u9669\u56e0\u7d20\n'
            '\u516c\u53f8\u9762\u4e34\u7ecf\u8425\u98ce\u9669\u3001\u6c47\u7387\u98ce\u9669\u548c\u5ba2\u6237\u96c6\u4e2d\u98ce\u9669\u3002'
        )
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            text=body,
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={
                    'sectors': ['\u5148\u8fdb\u5c01\u88c5'],
                    'parse_periodic_report_pdf': True,
                    'max_periodic_text_chars': 20000,
                },
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_called_once()
        self.assertIn('periodic_reports', Path(download_pdf.call_args.args[1]).parts)
        periodic_items = [
            item for item in batch['normalized_items']
            if item.category == 'periodic_report_body'
        ]
        self.assertTrue(
            {'financial', 'demand', 'export', 'customer_supplier', 'inventory', 'cost', 'risk'}
            <= {item.layer for item in periodic_items}
        )
        self.assertTrue(all(item.evidence_type == 'periodic_report' for item in periodic_items))
        self.assertTrue(all(item.source_id == 'requests:company_announcements' for item in periodic_items))
        self.assertTrue(all(item.metric_name == '' and item.current_value is None for item in periodic_items))
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['attempted'], 1)
        self.assertEqual(status['parsed'], 1)
        self.assertGreaterEqual(status['evidence_count'], 6)

    def test_company_announcements_periodic_report_table_and_section_status(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '300308',
            'secName': '\u4e2d\u9645\u65ed\u521b',
            'title': '\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]
        body = (
            '\u7b2c\u4e09\u8282 \u7ba1\u7406\u5c42\u8ba8\u8bba\u4e0e\u5206\u6790\n'
            '\u516c\u53f8\u7ecf\u8425\u60c5\u51b5\u7a33\u5b9a\u3002\n'
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u516c\u53f8\u4e3b\u8425\u4ea7\u54c1\u5b9e\u73b0\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c'
            '\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002\n'
            '\u5206\u884c\u4e1a\u3001\u5206\u4ea7\u54c1\u3001\u5206\u5730\u533a\u60c5\u51b5\n'
            '\u5883\u5916\u6536\u5165\u589e\u957f\u3002\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u6bd4\u63d0\u5347\u3002\n'
            '\u5b58\u8d27\n'
            '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u589e\u52a0\u3002\n'
            '\u6210\u672c\u6784\u6210\n'
            '\u8425\u4e1a\u6210\u672c\u4e2d\u539f\u6750\u6599\u5360\u6bd4\u8f83\u9ad8\u3002'
        )
        tables = [
            {
                'page': 18,
                'bbox': [36, 120, 560, 220],
                'section_guess': 'customer_supplier',
                'header': ['\u5ba2\u6237\u540d\u79f0', '\u9500\u552e\u989d'],
                'rows': [['\u7b2c\u4e00\u540d\u5ba2\u6237', '12\u4ebf\u5143']],
                'confidence': 0.9,
                'extractor': 'pdfplumber',
            },
            {
                'page': 24,
                'bbox': [36, 130, 560, 230],
                'section_guess': 'inventory',
                'header': ['\u9879\u76ee', '\u671f\u672b\u4f59\u989d', '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907'],
                'rows': [['\u5b58\u8d27', '8\u4ebf\u5143', '0.4\u4ebf\u5143']],
                'confidence': 0.86,
                'extractor': 'pdfplumber',
            },
            {
                'page': 20,
                'bbox': [36, 120, 560, 230],
                'section_guess': 'business_segments',
                'header': ['\u5206\u5730\u533a', '\u8425\u4e1a\u6536\u5165', '\u6bdb\u5229\u7387'],
                'rows': [['\u5883\u5916', '31\u4ebf\u5143', '38%']],
                'confidence': 0.88,
                'extractor': 'pdfplumber',
            },
            {
                'page': 21,
                'bbox': [36, 140, 560, 240],
                'section_guess': 'cost',
                'header': ['\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u5360\u6bd4'],
                'rows': [['\u539f\u6750\u6599', '42\u4ebf\u5143', '66%']],
                'confidence': 0.84,
                'extractor': 'pdfplumber',
            },
        ]

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp}, clear=False):
            from types import SimpleNamespace

            pdf_path = Path(tmp) / 'annual.PDF'
            pdf_path.write_bytes(b'%PDF-1.4 annual report')
            download_result = DisclosurePdfResult(
                url=entries[0]['url'],
                cache_path=pdf_path,
                status='ok',
                fetched_at='2026-07-04T10:00:00',
            )
            extract_result = DisclosurePdfResult(
                url=entries[0]['url'],
                cache_path=pdf_path,
                status='ok',
                text=body,
                fetched_at='2026-07-04T10:00:01',
            )

            with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                    patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                    patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result), \
                    patch(
                        'core.intelligence.periodic_report_documents.extract_periodic_report_tables',
                        return_value=SimpleNamespace(
                            status='ok',
                            tables=tables,
                            path=Path('doc.tables.jsonl'),
                            error='',
                        ),
                    ):
                batch = collect_builtin_sources(
                    '300308',
                    '\u4e2d\u9645\u65ed\u521b',
                    context={'parse_periodic_report_pdf': True},
                    specs=[_company_announcement_spec()],
                    use_cache=False,
                )

        periodic_items = [
            item for item in batch['normalized_items']
            if item.category == 'periodic_report_body'
        ]
        summary_items = [
            item for item in batch['normalized_items']
            if item.category == 'periodic_field_summary'
        ]
        self.assertTrue({'financial', 'demand', 'export', 'customer_supplier', 'inventory', 'cost'} <= {item.layer for item in periodic_items})
        periodic_ids = [item.id for item in periodic_items]
        self.assertEqual(len(periodic_ids), len(set(periodic_ids)))
        self.assertTrue(summary_items)
        summary_by_field = {item.metadata['field']: item for item in summary_items}
        self.assertIn('business_segments', summary_by_field)
        self.assertIn('inventory', summary_by_field)
        self.assertEqual(summary_by_field['business_segments'].evidence_type, 'periodic_field_summary')
        self.assertGreaterEqual(summary_by_field['business_segments'].metadata['evidence_count'], 1)
        top_ids = set(summary_by_field['business_segments'].metadata['top_evidence_ids'])
        self.assertTrue(top_ids <= set(periodic_ids))
        self.assertIn('summary_text', summary_by_field['business_segments'].metadata)
        self.assertNotIn('technical-analysis', json.dumps([item.to_dict() for item in summary_items], ensure_ascii=False))
        inventory_raw = next(
            item for item in batch['raw_items']
            if item.raw.get('category') == 'periodic_report_body' and item.raw.get('field') == 'inventory'
        )
        self.assertEqual(inventory_raw.raw['page'], 24)
        self.assertEqual(
            inventory_raw.raw['table_header'],
            ['\u9879\u76ee', '\u671f\u672b\u4f59\u989d', '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907'],
        )
        self.assertGreaterEqual(inventory_raw.raw['table_quality_score'], 0.5)
        raw_fields = {
            item.raw.get('field')
            for item in batch['raw_items']
            if item.raw.get('category') == 'periodic_report_body'
        }
        self.assertIn('customer_concentration', raw_fields)
        self.assertIn('business_segments', raw_fields)
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['section_index_status'], 'ok')
        self.assertEqual(status['table_extract_status'], 'ok')
        self.assertEqual(status['table_count'], 4)
        self.assertGreaterEqual(status['matched_section_count'], 5)
        self.assertEqual(status['field_missing_reason']['competition'], 'no_matching_table_or_section')
        self.assertNotIn('customer_supplier', status['field_missing_reason'])
        self.assertGreaterEqual(status['table_evidence_count_by_field']['customer_concentration'], 1)
        self.assertGreaterEqual(status['table_evidence_count_by_field']['business_segments'], 1)
        self.assertIn('business_segments', status['table_evidence_top_fields'])
        self.assertEqual(status['customer_supplier_split_status']['customer_concentration'], 1)
        self.assertEqual(status['customer_supplier_split_status']['supplier_concentration'], 0)
        self.assertGreaterEqual(status['low_quality_table_skipped'], 0)
        self.assertGreaterEqual(status['field_summary_count'], 1)
        self.assertGreaterEqual(status['field_summary_by_field']['business_segments'], 1)
        self.assertIn('competition', status['missing_fields'])
        self.assertIsInstance(status['weak_fields'], list)
        self.assertIsInstance(status['mixed_fields'], list)

    def test_periodic_report_body_items_are_not_truncated_by_title_candidates(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        annual_url = 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF'
        regular_entries = [
            {
                'secCode': '300502',
                'secName': '\u65b0\u6613\u76db',
                'title': f'\u65b0\u6613\u76db\uff1a\u7b7e\u7f72\u7b2c{index}\u4efd\u91cd\u5927\u5408\u540c\u516c\u544a',
                'summary': '\u65b0\u6613\u76db\u7b7e\u7f72\u91cd\u5927\u5408\u540c\uff0c\u6d89\u53ca\u6d77\u5916\u5ba2\u6237\u8ba2\u5355\u3002',
                'published_at': f'2026-06-{index:02d}',
                'url': f'https://static.cninfo.com.cn/finalpage/2026-06-{index:02d}/contract-{index}.PDF',
            }
            for index in range(1, 27)
        ]
        entries = regular_entries + [{
            'secCode': '300502',
            'secName': '\u65b0\u6613\u76db',
            'title': '\u65b0\u6613\u76db\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u65b0\u6613\u76db\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': annual_url,
        }]
        body = (
            '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
            '\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e3b\u8425\u4ea7\u54c1\u9500\u552e\u91cf\u63d0\u5347\u3002\n'
            '\u5206\u5730\u533a\u6536\u5165\n'
            '\u5883\u5916\u6536\u5165\u589e\u957f\u3002\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u548c\u4f9b\u5e94\u5546\n'
            '\u524d\u4e94\u5927\u5ba2\u6237\u9500\u552e\u989d\u5360\u6bd4\u63d0\u5347\u3002\n'
            '\u5b58\u8d27\n'
            '\u5b58\u8d27\u8dcc\u4ef7\u51c6\u5907\u589e\u52a0\u3002\n'
            '\u6210\u672c\u6784\u6210\n'
            '\u8425\u4e1a\u6210\u672c\u4e2d\u539f\u6750\u6599\u5360\u6bd4\u8f83\u9ad8\u3002'
        )
        download_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            text=body,
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'parse_periodic_report_pdf': True, 'max_periodic_pdf_per_source': 1},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        periodic_layers = {
            item.layer for item in batch['normalized_items']
            if item.category == 'periodic_report_body'
        }
        self.assertTrue({'financial', 'demand', 'export', 'customer_supplier', 'inventory', 'cost'} <= periodic_layers)
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertGreaterEqual(status['evidence_count'], 6)

    def test_company_announcements_periodic_report_reports_zero_without_candidates(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=[]), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '300308',
                '\u4e2d\u9645\u65ed\u521b',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['attempted'], 0)
        self.assertEqual(status['parsed'], 0)
        self.assertEqual(status['evidence_count'], 0)
        self.assertEqual(status['statuses'], {})

    def test_periodic_report_disabled_does_not_run_dedicated_search(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        calls = []

        def fake_fetch(query, *, limit):
            calls.append(query)
            return []

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', side_effect=fake_fetch), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '300308',
                '\u4e2d\u9645\u65ed\u521b',
                context={'parse_periodic_report_pdf': False},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        self.assertEqual(calls, ['\u4e2d\u9645\u65ed\u521b'])
        self.assertNotIn(
            'periodic_report',
            batch['source_status']['requests:company_announcements'],
        )

    def test_periodic_report_enabled_appends_dedicated_code_and_name_search(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        annual_url = 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF'
        calls = []

        def fake_fetch(query, *, limit):
            calls.append(query)
            if query == '\u4e2d\u9645\u65ed\u521b':
                return [{
                    'secCode': '300308',
                    'secName': '\u4e2d\u9645\u65ed\u521b',
                    'title': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
                    'summary': '\u5173\u4e8e\u7b7e\u7f72\u91cd\u5927\u5408\u540c\u7684\u516c\u544a',
                    'published_at': '2026-06-11',
                    'url': 'https://static.cninfo.com.cn/finalpage/2026-06-11/contract.PDF',
                }]
            if query == '300308 \u5e74\u5ea6\u62a5\u544a':
                return [{
                    'secCode': '300308',
                    'secName': '\u4e2d\u9645\u65ed\u521b',
                    'title': '\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                    'summary': '\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                    'published_at': '2026-04-20',
                    'url': annual_url,
                }]
            return []

        download_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            text=(
                '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n'
                '\u516c\u53f8\u4e3b\u8425\u4ea7\u54c1\u5b9e\u73b0\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c'
                '\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002'
            ),
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', side_effect=fake_fetch), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '300308',
                '\u4e2d\u9645\u65ed\u521b',
                context={'parse_periodic_report_pdf': True, 'max_periodic_pdf_per_source': 1},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertIn('300308 \u5e74\u5ea6\u62a5\u544a', calls)
        self.assertIn('300308 \u534a\u5e74\u5ea6\u62a5\u544a', calls)
        self.assertIn('\u4e2d\u9645\u65ed\u521b \u5e74\u5ea6\u62a5\u544a', calls)
        download_pdf.assert_called_once()
        self.assertEqual(download_pdf.call_args.args[0], annual_url)

        periodic_items = [
            item for item in batch['normalized_items']
            if item.category == 'periodic_report_body'
        ]
        self.assertTrue(periodic_items)
        self.assertTrue(all('300308 \u5e74\u5ea6\u62a5\u544a' not in item.summary for item in periodic_items))
        self.assertTrue(all(item.layer in {'financial', 'demand'} for item in periodic_items))
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['periodic_candidates_found'], 1)
        self.assertEqual(status['periodic_selected_title'], '\u4e2d\u9645\u65ed\u521b\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a')
        self.assertEqual(status['periodic_selected_url'], annual_url)

    def test_periodic_report_filters_noise_with_specific_skip_reasons(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        annual_url = 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF'
        entries = [
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u6458\u8981',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u6458\u8981',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual-summary.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u66f4\u6b63\u516c\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u66f4\u6b63\u516c\u544a',
                'published_at': '2026-04-21',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-21/annual-correction.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\uff08\u82f1\u6587\u7248\uff09',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\uff08\u82f1\u6587\u7248\uff09',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual-en.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u72ec\u7acb\u8463\u4e8b\u5173\u4e8e\u5e74\u5ea6\u62a5\u544a\u76f8\u5173\u4e8b\u9879\u7684\u610f\u89c1',
                'summary': '\u72ec\u7acb\u8463\u4e8b\u610f\u89c1',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/independent-director.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5ea6\u5ba1\u8ba1\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5ea6\u5ba1\u8ba1\u62a5\u544a',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/audit.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'published_at': '2026-04-20',
                'url': annual_url,
            },
        ]
        download_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=annual_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            text='\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['skipped_summary'], 1)
        self.assertEqual(status['skipped_correction'], 1)
        self.assertEqual(status['skipped_english'], 1)
        self.assertGreaterEqual(status['skipped_not_periodic'], 2)
        self.assertEqual(status['periodic_candidates_found'], 1)
        self.assertEqual(status['attempted'], 1)

    def test_periodic_report_selects_latest_annual_candidate_only(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        older_url = 'https://static.cninfo.com.cn/finalpage/2025-04-20/annual-2024.PDF'
        latest_url = 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual-2025.PDF'
        entries = [
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2024\u5e74\u5e74\u5ea6\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2024\u5e74\u5e74\u5ea6\u62a5\u544a',
                'published_at': '2025-04-20',
                'url': older_url,
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'published_at': '2026-04-20',
                'url': latest_url,
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2026\u5e74\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2026\u5e74\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a',
                'published_at': '2026-04-25',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-25/q1.PDF',
            },
        ]
        download_result = DisclosurePdfResult(
            url=latest_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=latest_url,
            cache_path=Path('annual.PDF'),
            status='ok',
            text='\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'parse_periodic_report_pdf': True, 'max_periodic_pdf_per_source': 1},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_called_once()
        self.assertEqual(download_pdf.call_args.args[0], latest_url)
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['periodic_candidates_found'], 3)
        self.assertEqual(status['periodic_selected_url'], latest_url)
        self.assertEqual(status['skipped_by_limit'], 2)

    def test_periodic_report_zero_status_includes_no_candidate_reason(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=[]), \
                patch('core.intelligence.disclosure_pdf.download_pdf') as download_pdf:
            batch = collect_builtin_sources(
                '000681',
                '\u89c6\u89c9\u4e2d\u56fd',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_not_called()
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['attempted'], 0)
        self.assertEqual(status['periodic_candidates_found'], 0)
        self.assertEqual(status['missing_reason'], 'no_periodic_candidates')

    def test_company_announcements_periodic_report_title_filter_and_limit(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u6458\u8981',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\u6458\u8981',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual-summary.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
            },
            {
                'secCode': '600584',
                'secName': '\u957f\u7535\u79d1\u6280',
                'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\uff08\u82f1\u6587\u7248\uff09',
                'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a\uff08\u82f1\u6587\u7248\uff09',
                'published_at': '2026-04-20',
                'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual-en.PDF',
            },
        ]
        download_result = DisclosurePdfResult(
            url=entries[1]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[1]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            text='\u4e3b\u8425\u4e1a\u52a1\u6784\u6210\n\u8425\u4e1a\u6536\u5165\u589e\u957f\uff0c\u4e0b\u6e38\u9700\u6c42\u6539\u5584\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result) as download_pdf, \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={
                    'parse_periodic_report_pdf': True,
                    'max_periodic_pdf_per_source': 3,
                },
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        download_pdf.assert_called_once()
        self.assertEqual(download_pdf.call_args.args[0], entries[1]['url'])
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['attempted'], 1)
        self.assertEqual(status['skipped_by_limit'], 0)
        self.assertEqual(status['skipped_summary'], 1)
        self.assertEqual(status['skipped_english'], 1)

    def test_company_announcements_periodic_report_download_error_keeps_title_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '600584',
            'secName': '\u957f\u7535\u79d1\u6280',
            'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='download_error',
            error='network down',
            fetched_at='2026-07-04T10:00:00',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text') as extract_pdf_text:
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        extract_pdf_text.assert_not_called()
        self.assertIn('financial', {item.layer for item in batch['normalized_items']})
        self.assertIn('company_announcement', {item.category for item in batch['normalized_items']})
        self.assertNotIn(
            'periodic_report_body',
            {item.category for item in batch['normalized_items']},
        )
        status = batch['source_status']['requests:company_announcements']['periodic_report']
        self.assertEqual(status['download_error'], 1)
        self.assertIn('network down', status['details'][0]['error'])

    def test_company_announcements_periodic_report_empty_text_generates_no_body_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        entries = [{
            'secCode': '600584',
            'secName': '\u957f\u7535\u79d1\u6280',
            'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='empty_text',
            error='no text',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertNotIn(
            'periodic_report_body',
            {item.category for item in batch['normalized_items']},
        )
        self.assertEqual(
            batch['source_status']['requests:company_announcements']['periodic_report']['empty_text'],
            1,
        )

    def test_company_announcements_periodic_report_technical_text_does_not_create_body_evidence(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.disclosure_pdf import DisclosurePdfResult

        technical = ''.join(['M', 'A', 'C', 'D'])
        entries = [{
            'secCode': '600584',
            'secName': '\u957f\u7535\u79d1\u6280',
            'title': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'summary': '\u957f\u7535\u79d1\u6280\uff1a2025\u5e74\u5e74\u5ea6\u62a5\u544a',
            'published_at': '2026-04-20',
            'url': 'https://static.cninfo.com.cn/finalpage/2026-04-20/annual.PDF',
        }]
        download_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            fetched_at='2026-07-04T10:00:00',
        )
        extract_result = DisclosurePdfResult(
            url=entries[0]['url'],
            cache_path=Path('annual.PDF'),
            status='ok',
            text=technical + '\u91d1\u53c9\u540e\u51fa\u73b0\u4e70\u70b9\u548c\u6b62\u635f\u4f4d\uff0cRSI\u8fdb\u5165\u5f3a\u52bf\u533a\u3002',
            fetched_at='2026-07-04T10:00:01',
        )

        with patch('core.intelligence.collectors_builtin._fetch_cninfo_announcements', return_value=entries), \
                patch('core.intelligence.disclosure_pdf.download_pdf', return_value=download_result), \
                patch('core.intelligence.disclosure_pdf.extract_pdf_text', return_value=extract_result):
            batch = collect_builtin_sources(
                '600584',
                '\u957f\u7535\u79d1\u6280',
                context={'parse_periodic_report_pdf': True},
                specs=[_company_announcement_spec()],
                use_cache=False,
            )

        self.assertNotIn(
            'periodic_report_body',
            {item.category for item in batch['normalized_items']},
        )
        rendered = json.dumps([item.to_dict() for item in batch['normalized_items']], ensure_ascii=False)
        for forbidden in ('MACD', 'RSI', '\u4e70\u70b9', '\u6b62\u635f'):
            self.assertNotIn(forbidden, rendered)

    def test_external_chain_sources_use_query_hints_and_metadata(self):
        from urllib.parse import parse_qs, urlparse
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        optical = '\u5149\u6a21\u5757'
        customer = '\u5ba2\u6237'
        contract = '\u5408\u540c'
        ai = '\u4eba\u5de5\u667a\u80fd'
        computing = '\u7b97\u529b'
        price = '\u4ef7\u683c'
        overseas = '\u6d77\u5916'
        demand = '\u9700\u6c42'
        inventory = '\u5e93\u5b58'
        restock = '\u8865\u5e93'

        class Response:
            def __init__(self, *, data=None, text=''):
                self._data = data
                self.text = text
                self.status_code = 200
                self.headers = {'content-type': 'application/json'}

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_post(url, **kwargs):
            form = kwargs.get('data') or {}
            searchkey = form.get('searchkey') or ''
            if '\u65b0\u6613\u76db' in searchkey:
                return Response(data={
                    'announcements': [
                        {
                            'secCode': '300502',
                            'secName': '\u65b0\u6613\u76db',
                            'announcementTitle': f'\u5173\u4e8e\u4e0e\u6d77\u5916{customer}\u7b7e\u7f72\u9ad8\u901f{optical}\u91c7\u8d2d{contract}\u7684\u516c\u544a',
                            'announcementTime': 1781107200000,
                            'adjunctUrl': 'finalpage/2026-06-11/1225363988.PDF',
                        },
                        {
                            'secCode': '300502',
                            'secName': '\u65b0\u6613\u76db',
                            'announcementTitle': '\u5317\u4eac\u56fd\u67ab\u5f8b\u5e08\u4e8b\u52a1\u6240\u5173\u4e8e\u80a1\u4e1c\u5927\u4f1a\u7684\u6cd5\u5f8b\u610f\u89c1\u4e66',
                            'announcementTime': 1781107200000,
                            'adjunctUrl': 'finalpage/2026-06-11/noise.PDF',
                        },
                    ]
                })
            return Response(data={
                'announcements': [
                    {
                        'secCode': '300857',
                        'secName': '\u534f\u521b\u6570\u636e',
                        'announcementTitle': f'\u5173\u4e8e\u7b7e\u7f72\u300a\u5149\u82af\u7247\u3001{optical}\u7814\u53d1\u548c\u751f\u4ea7\u5efa\u8bbe\u9879\u76ee\u5408\u4f5c\u534f\u8bae\u4e66\u300b\u7684\u81ea\u613f\u6027\u62ab\u9732\u516c\u544a',
                        'announcementTime': 1765966349000,
                        'adjunctUrl': 'finalpage/2025-12-17/1224883577.PDF',
                    }
                ]
            })

        def fake_get(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            q = ''.join(query.get('q') or [''])
            return Response(data={
                'data': {
                    'searchResult': {
                        'total': 1,
                        'dataResults': [
                            {
                                'groupData': [
                                    {'data': {
                                        'title': f'\u5de5\u4fe1\u90e8\u5370\u53d1\u201c{ai}+\u4fe1\u606f\u901a\u4fe1\u201d\u521b\u65b0\u53d1\u5c55\u5b9e\u65bd\u610f\u89c1',
                                        'infocontent': f'\u63d0\u5347{computing}\u5e95\u5ea7\uff0c\u652f\u6301{optical}\u3001800G\u548c\u6570\u636e\u4e2d\u5fc3\u7f51\u7edc\u5347\u7ea7\u3002',
                                        'deploytime': 1781140310542,
                                        'url': '/zwgk/zcwj/wjfb/art/2026/art_demo.html',
                                    }}
                                ]
                            },
                            {
                                'groupData': [
                                    {'data': {
                                        'title': f'{optical}{price}\u8d8b\u52bf\u5f71\u54cd\u5149\u82af\u7247\u548c\u5149\u5668\u4ef6\u91c7\u8d2d\u6210\u672c',
                                        'infocontent': f'{optical}\u4ea7\u4e1a\u94fe\u5173\u6ce8{price}\u4e0e\u6210\u672c\u53d8\u5316\u3002',
                                        'deploytime': 1780310082829,
                                        'url': '/xwdt/xydt/art/2026/art_cost.html',
                                    }}
                                ]
                            },
                            {
                                'groupData': [
                                    {'data': {
                                        'title': f'{overseas}{customer}{optical}{demand}{restock}\u548c{inventory}\u53ef\u89c1\u5ea6\u6539\u5584',
                                        'infocontent': f'{overseas}{customer}\u62c9\u52a8{optical}{demand}\uff0c\u4ea7\u4e1a\u94fe\u5173\u6ce8{inventory}\u548c{restock}\u8282\u594f\u3002',
                                        'deploytime': 1780310082829,
                                        'url': '/xwdt/xydt/art/2026/art_inventory.html',
                                    }}
                                ]
                            },
                        ],
                    }
                }
            })

        specs = [
            SourceSpec(
                source_id='requests:company_announcements',
                name='Company Announcements',
                layer='company',
                method='requests',
                url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
                parser='cninfo_his_announcement',
                trust_level='primary',
                ttl_hours=6,
            ),
            SourceSpec(
                source_id='requests:policy_pages',
                name='Policy Pages',
                layer='policy',
                method='requests',
                url='https://www.miit.gov.cn/search-front-server/api/search/info',
                parser='miit_search_api',
                trust_level='official',
                ttl_hours=6,
            ),
            SourceSpec(
                source_id='requests:industry_chain_pages',
                name='Industry Chain Pages',
                layer='company',
                method='requests',
                url='https://www.cninfo.com.cn/new/hisAnnouncement/query',
                parser='public_chain_search',
                trust_level='secondary',
                ttl_hours=12,
            ),
        ]

        with patch('core.http_client.post', side_effect=fake_post), \
                patch('core.http_client.get', side_effect=fake_get):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'sectors': [optical, 'CPO', '800G']},
                specs=specs,
                use_cache=False,
            )

        external_items = [item for item in batch['normalized_items'] if not item.source_id.startswith('existing:')]
        self.assertGreaterEqual(len(external_items), 3)
        layers = {item.layer for item in external_items}
        self.assertTrue({'policy', 'order_contract', 'capacity'} <= layers)
        self.assertTrue({'cost', 'customer_supplier', 'export', 'inventory'} <= layers)
        for item in external_items:
            self.assertTrue(item.source_url)
            self.assertTrue(item.relevance_reason)
            self.assertTrue(item.matched_keywords)
            self.assertTrue(item.category)
            self.assertFalse(item.metric_name)
            self.assertIsNone(item.current_value)

        coverage = batch['coverage_summary']
        self.assertGreaterEqual(coverage['external_source_hits'], 3)
        self.assertEqual(coverage['local_source_hits'], 0)
        self.assertGreaterEqual(coverage['by_source']['requests:policy_pages'], 1)
        self.assertGreaterEqual(coverage['matched_keywords'][optical], 1)
        self.assertGreaterEqual(coverage['by_layer']['customer_supplier'], 1)
        self.assertGreaterEqual(coverage['by_layer']['export'], 1)
        self.assertGreaterEqual(coverage['by_layer']['inventory'], 1)

    def test_scrapling_static_demo_collector_can_be_run_explicitly(self):
        from unittest.mock import patch

        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        spec = SourceSpec(
            source_id='scrapling:policy_static_demo',
            name='Policy Static Demo',
            layer='policy',
            method='scrapling_static',
            url='https://example.com/policy',
            parser='PolicyPageCollector',
            trust_level='official',
            ttl_hours=24,
            enabled=False,
        )
        payload = {
            'ok': True,
            'url': spec.url,
            'mode': 'static',
            'title': 'Policy page',
            'text': 'Policy supports semiconductor export and customer demand.',
            'items': [
                {
                    'title': 'Policy supports semiconductor export demand',
                    'summary': 'Official policy text mentions semiconductor export demand.',
                    'url': 'https://example.com/policy/item',
                    'date': '2026-07-03',
                }
            ],
            'error': '',
        }

        with patch('core.intelligence.scrapling_runtime.run_scrapling_fetch', return_value=payload):
            batch = collect_builtin_sources(
                '600584',
                'Stock A',
                context={'sectors': ['semiconductor']},
                specs=[spec],
                use_cache=False,
            )

        self.assertEqual(len(batch['normalized_items']), 1)
        item = batch['normalized_items'][0]
        self.assertEqual(item.source_id, 'scrapling:policy_static_demo')
        self.assertEqual(item.layer, 'policy')
        self.assertEqual(item.trust_level, 'official')
        self.assertEqual(batch['source_status']['scrapling:policy_static_demo']['status'], 'ok')

    def test_unimplemented_enabled_source_is_recorded_without_blocking_batch(self):
        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.models import SourceSpec

        specs = [
            SourceSpec(
                source_id='requests:test_static_page',
                name='Static Page',
                layer='policy',
                method='requests',
                parser='test',
                trust_level='media',
                ttl_hours=6,
            ),
            SourceSpec(
                source_id='existing:fundamentals',
                name='Fundamentals',
                layer='financial',
                method='api',
                parser='test',
                trust_level='secondary',
                ttl_hours=24,
            ),
        ]
        batch = collect_builtin_sources(
            '600584',
            'Stock A',
            context={'context': {'fundamental_summary': 'Financial text.'}},
            specs=specs,
            use_cache=False,
        )

        self.assertEqual(batch['source_status']['requests:test_static_page']['status'], 'skipped_not_implemented')
        self.assertEqual(batch['source_status']['existing:fundamentals']['status'], 'ok')
        self.assertEqual(len(batch['normalized_items']), 1)


if __name__ == '__main__':
    unittest.main()
