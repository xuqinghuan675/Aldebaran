import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceCollectionPlanTest(unittest.TestCase):
    def test_collection_plan_only_allows_efficiency_and_performance_modes(self):
        from core.intelligence.collection_plan import build_collection_plan
        from core.intelligence.route_profile import build_route_profile
        from core.intelligence.source_catalog import get_source_catalog

        profile = build_route_profile('300502', '\u65b0\u6613\u76db')
        catalog = get_source_catalog()

        efficiency = build_collection_plan(profile, catalog, mode='efficiency')
        performance = build_collection_plan(profile, catalog, mode='performance')

        self.assertEqual(efficiency.mode, 'efficiency')
        self.assertEqual(performance.mode, 'performance')
        self.assertLessEqual(len(efficiency.sources), len(performance.sources))
        self.assertTrue(efficiency.queries_by_source)
        self.assertIn('requests:company_announcements', efficiency.source_ids)

        with self.assertRaises(ValueError):
            build_collection_plan(profile, catalog, mode='balanced')

    def test_collection_plan_controls_disclosure_pdf_parsing_explicitly(self):
        from core.intelligence.collection_plan import build_collection_plan
        from core.intelligence.route_profile import build_route_profile
        from core.intelligence.source_catalog import get_source_catalog

        profile = build_route_profile('300502', '\u65b0\u6613\u76db')
        catalog = get_source_catalog()

        default_plan = build_collection_plan(profile, catalog, mode='efficiency')
        enabled_plan = build_collection_plan(
            profile,
            catalog,
            mode='performance',
            parse_disclosure_pdf=True,
            max_pdf_per_source=4,
            pdf_timeout_seconds=5,
            max_pdf_text_chars=8000,
            only_parse_when_title_matches=True,
        )

        self.assertFalse(default_plan.parse_disclosure_pdf)
        self.assertEqual(default_plan.max_pdf_per_source, 0)
        self.assertTrue(enabled_plan.parse_disclosure_pdf)
        self.assertEqual(enabled_plan.max_pdf_per_source, 4)
        self.assertEqual(enabled_plan.pdf_timeout_seconds, 5)
        self.assertEqual(enabled_plan.max_pdf_text_chars, 8000)
        self.assertTrue(enabled_plan.only_parse_when_title_matches)
        self.assertTrue(enabled_plan.to_dict()['parse_disclosure_pdf'])
        pdf_control = enabled_plan.to_dict()['pdf_control']
        self.assertTrue(pdf_control['parse_disclosure_pdf'])
        self.assertEqual(pdf_control['max_pdf_per_source'], 4)
        self.assertEqual(pdf_control['pdf_timeout_seconds'], 5)
        self.assertEqual(pdf_control['max_pdf_text_chars'], 8000)
        self.assertTrue(pdf_control['only_parse_when_title_matches'])

    def test_collection_plan_include_pdf_body_explicitly_enables_pdf_controls(self):
        from core.intelligence.collection_plan import build_collection_plan
        from core.intelligence.route_profile import build_route_profile
        from core.intelligence.source_catalog import get_source_catalog

        profile = build_route_profile('300502', '\u65b0\u6613\u76db')
        catalog = get_source_catalog()

        default_performance = build_collection_plan(profile, catalog, mode='performance')
        explicit_plan = build_collection_plan(
            profile,
            catalog,
            mode='performance',
            include_pdf_body=True,
        )

        self.assertFalse(default_performance.parse_disclosure_pdf)
        self.assertEqual(default_performance.max_pdf_per_source, 0)
        self.assertTrue(explicit_plan.parse_disclosure_pdf)
        self.assertEqual(explicit_plan.max_pdf_per_source, 3)
        self.assertIn('requests:company_announcements', explicit_plan.source_ids)
        source = next(
            source
            for source in explicit_plan.sources
            if source.source_id == 'requests:company_announcements'
        )
        self.assertTrue(source.supports_pdf_body)

    def test_collection_plan_controls_periodic_report_parsing_explicitly(self):
        from core.intelligence.collection_plan import build_collection_plan
        from core.intelligence.route_profile import build_route_profile
        from core.intelligence.source_catalog import get_source_catalog

        profile = build_route_profile('600584', '\u957f\u7535\u79d1\u6280')
        catalog = get_source_catalog()

        default_efficiency = build_collection_plan(profile, catalog, mode='efficiency')
        default_performance = build_collection_plan(profile, catalog, mode='performance')
        enabled_plan = build_collection_plan(
            profile,
            catalog,
            mode='performance',
            parse_periodic_report_pdf=True,
            max_periodic_pdf_per_source=4,
            periodic_pdf_timeout_seconds=6,
            max_periodic_text_chars=12000,
        )
        included_plan = build_collection_plan(
            profile,
            catalog,
            mode='performance',
            include_periodic_report=True,
        )

        self.assertFalse(default_efficiency.parse_periodic_report_pdf)
        self.assertEqual(default_efficiency.max_periodic_pdf_per_source, 0)
        self.assertFalse(default_performance.parse_periodic_report_pdf)
        self.assertEqual(default_performance.max_periodic_pdf_per_source, 0)
        self.assertTrue(enabled_plan.parse_periodic_report_pdf)
        self.assertEqual(enabled_plan.max_periodic_pdf_per_source, 1)
        self.assertEqual(enabled_plan.periodic_pdf_timeout_seconds, 6)
        self.assertEqual(enabled_plan.max_periodic_text_chars, 12000)
        self.assertTrue(included_plan.parse_periodic_report_pdf)
        self.assertEqual(included_plan.max_periodic_pdf_per_source, 1)

        control = enabled_plan.to_dict()['periodic_report_control']
        self.assertTrue(control['parse_periodic_report_pdf'])
        self.assertEqual(control['max_periodic_pdf_per_source'], 1)
        self.assertEqual(control['periodic_pdf_timeout_seconds'], 6)
        self.assertEqual(control['max_periodic_text_chars'], 12000)

    def test_collector_executes_plan_and_reports_field_access_coverage(self):
        from core.intelligence.collection_plan import build_collection_plan
        from core.intelligence.collectors_builtin import collect_builtin_sources
        from core.intelligence.route_profile import build_route_profile
        from core.intelligence.source_catalog import get_source_catalog

        overseas = '\u6d77\u5916'
        customer = '\u5ba2\u6237'
        contract = '\u5408\u540c'
        demand = '\u9700\u6c42'
        inventory = '\u5e93\u5b58'

        class Response:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        def fake_post(url, **kwargs):
            form = kwargs.get('data') or {}
            searchkey = form.get('searchkey') or ''
            self.assertNotIn('inventory', searchkey.lower())
            return Response({
                'announcements': [
                    {
                        'secCode': '300502',
                        'secName': '\u65b0\u6613\u76db',
                        'announcementTitle': f'\u4e0e{overseas}{customer}\u7b7e\u7f72{contract}\u5e76\u5e26\u52a8{demand}\u548c{inventory}\u6539\u5584',
                        'announcementTime': 1781107200000,
                        'adjunctUrl': 'finalpage/2026-06-11/plan.PDF',
                    }
                ]
            })

        profile = build_route_profile('300502', '\u65b0\u6613\u76db')
        catalog = get_source_catalog()
        plan = build_collection_plan(
            profile,
            catalog,
            mode='efficiency',
            include_fields=('company_disclosure', 'export', 'demand', 'inventory'),
        )

        with patch('core.http_client.post', side_effect=fake_post):
            batch = collect_builtin_sources(
                '300502',
                '\u65b0\u6613\u76db',
                context={'sectors': ['CPO']},
                collection_plan=plan,
                use_cache=False,
            )

        layers = {item.layer for item in batch['normalized_items']}
        self.assertTrue({'order_contract', 'export', 'demand', 'inventory'} <= layers)

        coverage = batch['coverage_summary']
        self.assertGreaterEqual(coverage['by_field']['company_disclosure'], 1)
        self.assertGreaterEqual(coverage['by_field']['export'], 1)
        self.assertGreaterEqual(coverage['by_field']['demand'], 1)
        self.assertGreaterEqual(coverage['by_field']['inventory'], 1)
        self.assertGreaterEqual(coverage['by_access_mode']['free'], 1)
        self.assertIn('skipped_missing_credentials', coverage['sources'])
        self.assertGreaterEqual(coverage['skipped_missing_credentials'], 1)
        self.assertEqual(coverage['optional_api_hit_count'], 0)
        self.assertTrue(coverage['missing_reason'])

        source_status = batch['source_status']
        self.assertEqual(
            source_status['api:tushare_customs']['status'],
            'skipped_missing_credentials',
        )
        self.assertIn('route_profile', batch)
        self.assertIn('collection_plan', batch)
        self.assertEqual(batch['collection_plan']['mode'], 'efficiency')


if __name__ == '__main__':
    unittest.main()
