import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceSourceRegistryTest(unittest.TestCase):
    def test_registry_has_unique_v1_sources_with_valid_contract(self):
        from core.intelligence.models import VALID_METHODS, VALID_TRUST_LEVELS
        from core.intelligence.source_registry import SOURCE_SPECS, get_enabled_sources

        required = {
            'existing:intel_feed',
            'existing:stock_news',
            'existing:fundamentals',
            'existing:public_fund_evidence',
            'existing:money_flow',
            'existing:restricted_release',
            'existing:event_calendar',
            'existing:market_context',
            'existing:thin_layer_context',
            'rss:reuters_business',
            'requests:eastmoney_kuaixun',
            'requests:company_announcements',
            'requests:policy_pages',
            'requests:industry_chain_pages',
            'scrapling:policy_static_demo',
        }
        source_ids = [spec.source_id for spec in SOURCE_SPECS]

        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertTrue(required <= set(source_ids))
        self.assertTrue(all(spec.method in VALID_METHODS for spec in SOURCE_SPECS))
        self.assertTrue(all(spec.trust_level in VALID_TRUST_LEVELS for spec in SOURCE_SPECS))
        self.assertTrue(all(spec.ttl_hours > 0 for spec in SOURCE_SPECS))
        self.assertTrue(all(spec.parser for spec in SOURCE_SPECS))
        self.assertTrue(all('technical' not in spec.layer for spec in SOURCE_SPECS))
        self.assertTrue(get_enabled_sources())
        scrapling_demos = [spec for spec in SOURCE_SPECS if spec.source_id.startswith('scrapling:')]
        self.assertTrue(scrapling_demos)
        self.assertTrue(all(not spec.enabled for spec in scrapling_demos))
        self.assertTrue(all(spec.url.startswith('https://') for spec in scrapling_demos))

    def test_raw_and_normalized_items_preserve_metrics_without_invention(self):
        from core.intelligence.models import NormalizedIntelItem, RawIntelItem, SourceSpec

        spec = SourceSpec(
            source_id='test:source',
            name='Test Source',
            layer='demand',
            method='api',
            ttl_hours=6,
            trust_level='secondary',
            parser='TestParser',
            enabled=True,
        )
        raw = RawIntelItem(
            source_id=spec.source_id,
            source_name=spec.name,
            source_url=spec.url,
            fetched_at='2026-07-03T10:00:00',
            published_at=None,
            title='Demand text only',
            text='Demand improved according to public text, but no numeric metric was provided.',
            layer=spec.layer,
            trust_level=spec.trust_level,
        )
        normalized = NormalizedIntelItem(
            id='test:normalized',
            source_id=raw.source_id,
            title=raw.title,
            summary=raw.text,
            layer=raw.layer,
            direction='neutral',
            related_codes=['600584'],
            related_names=['Stock A'],
            related_sectors=[],
            evidence_type='news',
            published_at=raw.published_at,
            fetched_at=raw.fetched_at,
            url=raw.url,
            trust_level=raw.trust_level,
            confidence=4,
            time_windows=['short'],
            source_status='ok',
        )

        self.assertIsNone(normalized.current_value)
        self.assertIsNone(normalized.change_7d)
        self.assertEqual(normalized.expectation_gap, 'unknown')
        self.assertEqual(normalized.source_status, 'ok')
        self.assertEqual(normalized.missing_evidence, [])

    def test_normalize_seed_items_outputs_text_evidence_without_fake_metrics(self):
        from core.intelligence.normalize import normalized_items_from_seed

        items = normalized_items_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A'},
            'intel_events': [],
            'stock_news': [
                {
                    'title': 'Demand recovery report',
                    'content': 'Public text says demand recovered, but no numeric value is provided.',
                    'date': '2026-07-03',
                    'source': 'news',
                    'url': 'https://example.com/news',
                },
            ],
            'context': {
                'public_fund_evidence': {
                    'available': True,
                    'dzjy': {'hit': True, 'rows': 1, 'premium_pct_avg': -2.5, 'amount_yi': 0.8},
                }
            },
        })

        self.assertGreaterEqual(len(items), 2)
        demand = next(item for item in items if item.layer == 'demand')
        block_trade = next(item for item in items if item.evidence_type == 'block_trade')
        self.assertIsNone(demand.current_value)
        self.assertEqual(demand.expectation_gap, 'unknown')
        self.assertEqual(block_trade.current_value, 0.8)
        self.assertEqual(block_trade.unit, 'yi')

    def test_normalize_seed_maps_chinese_thin_layer_keywords(self):
        from core.intelligence.normalize import normalized_items_from_seed

        items = normalized_items_from_seed({
            'subject': {'code': '600584', 'name': 'Stock A'},
            'intel_events': [],
            'stock_news': [
                {
                    'title': '\u62df\u6295\u8d4478\u4ebf\u5143\u5efa\u8bbe\u9ad8\u7aef\u5148\u8fdb\u5c01\u6d4b\u5de5\u5382',
                    'content': '\u6295\u4ea7\u540e\u589e\u52a0\u5148\u8fdb\u5c01\u6d4b\u4ea7\u80fd\u3002',
                    'date': '2026-07-03',
                    'url': 'https://example.com/capacity',
                },
                {
                    'title': '9\u53ea\u4e2a\u80a1\u5927\u5b97\u4ea4\u6613\u8d855000\u4e07\u5143',
                    'content': '\u5927\u5b97\u4ea4\u6613\u4f53\u73b0\u8d44\u91d1\u884c\u4e3a\u3002',
                    'date': '2026-07-03',
                    'url': 'https://example.com/block-trade',
                },
                {
                    'title': '\u4e2d\u6807\u6d77\u5916\u5ba2\u6237\u8ba2\u5355',
                    'content': '\u516c\u544a\u62ab\u9732\u7b7e\u7ea6\u65b0\u5408\u540c\u3002',
                    'date': '2026-07-03',
                    'url': 'https://example.com/order',
                },
            ],
            'context': {},
        })

        by_url = {item.url: item for item in items}
        self.assertEqual(by_url['https://example.com/capacity'].layer, 'capacity')
        self.assertEqual(by_url['https://example.com/block-trade'].layer, 'trading_behavior')
        self.assertEqual(by_url['https://example.com/order'].layer, 'order_contract')


if __name__ == '__main__':
    unittest.main()
