import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntelligenceSourceCatalogTest(unittest.TestCase):
    def test_catalog_covers_required_fields_with_source_metadata(self):
        from core.intelligence.source_catalog import REQUIRED_FIELDS, get_source_catalog

        catalog = get_source_catalog()
        self.assertEqual(set(REQUIRED_FIELDS), set(catalog))

        for field, sources in catalog.items():
            self.assertTrue(sources, field)
            for source in sources:
                self.assertEqual(source.field, field)
                self.assertTrue(source.source_id)
                self.assertTrue(source.form)
                self.assertIn(source.trust_level, {'official', 'primary', 'secondary', 'media'})
                self.assertIn(source.access_mode, {'free', 'optional_api'})
                self.assertGreater(source.ttl_hours, 0)
                self.assertIsInstance(source.requires_api_key, bool)
                self.assertIn(source.evidence_origin, {
                    'disclosure_pdf',
                    'periodic_report',
                    'official_stats',
                    'exchange_data',
                    'market_behavior',
                    'news',
                    'ir_activity',
                    'optional_api',
                    'web_context',
                })
                self.assertIn(source.source_role, {'primary', 'secondary', 'fallback', 'context'})
                self.assertIsInstance(source.stable_output, bool)
                self.assertIn(source.recommended_extractor, {
                    'pdf_text',
                    'pdf_table',
                    'xbrl',
                    'html_table',
                    'json_api',
                    'rss',
                    'manual',
                    'firecrawl_scrape',
                })
                self.assertIn(source.implementation_status, {
                    'implemented',
                    'planned',
                    'optional_api',
                    'not_started',
                })

    def test_free_sources_are_enabled_and_optional_apis_skip_without_credentials(self):
        from core.intelligence.source_catalog import (
            get_catalog_status,
            get_enabled_catalog_sources,
            get_source_catalog,
        )

        old_env = dict(os.environ)
        try:
            catalog = get_source_catalog()
            for key in _credential_envs(catalog):
                os.environ.pop(key, None)
            enabled = get_enabled_catalog_sources(catalog)
            free_sources = [
                source
                for sources in catalog.values()
                for source in sources
                if source.access_mode == 'free'
            ]
            optional_sources = [
                source
                for sources in catalog.values()
                for source in sources
                if source.access_mode == 'optional_api' and source.enabled_by_default
            ]

            self.assertTrue(free_sources)
            self.assertTrue(optional_sources)
            self.assertTrue({source.source_id for source in free_sources} <= {source.source_id for source in enabled})

            status = get_catalog_status(catalog)
            for source in optional_sources:
                entry = status[source.source_id]
                self.assertEqual(entry['status'], 'skipped_missing_credentials')
                self.assertEqual(entry['missing_reason'], 'missing_credentials')
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_optional_api_sources_accept_common_credential_aliases(self):
        from core.intelligence.source_catalog import (
            get_catalog_status,
            get_enabled_catalog_sources,
            get_source_catalog,
        )

        old_env = dict(os.environ)
        try:
            catalog = get_source_catalog()
            for key in _credential_envs(catalog):
                os.environ.pop(key, None)
            os.environ['TUSHARE_TOKEN'] = 'demo-token'

            enabled_ids = {source.source_id for source in get_enabled_catalog_sources(catalog)}
            self.assertIn('api:tushare_pro', enabled_ids)
            self.assertIn('api:tushare_customs', enabled_ids)

            status = get_catalog_status(catalog)
            self.assertEqual(status['api:tushare_pro']['status'], 'registered')
            self.assertIn('TUSHARE_TOKEN', status['api:tushare_pro']['credential_envs'])
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_company_announcements_expresses_pdf_body_capability(self):
        from core.intelligence.source_catalog import get_source_catalog, specs_from_catalog_sources

        catalog = get_source_catalog()
        pdf_fields = {
            'company_disclosure',
            'order_contract',
            'capacity',
            'risk',
            'financial',
            'customer_supplier',
            'inventory',
            'export',
        }

        selected = []
        for field in pdf_fields:
            source = next(
                source
                for source in catalog[field]
                if source.source_id == 'requests:company_announcements'
            )
            selected.append(source)
            self.assertTrue(source.supports_pdf_body, field)
            self.assertIn('cninfo_pdf_body', source.parser)
            self.assertIn('pdf_body', source.form)

        specs = specs_from_catalog_sources(selected)
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.source_id, 'requests:company_announcements')
        self.assertTrue(spec.supports_pdf_body)
        self.assertTrue(pdf_fields <= set(spec.fields))
        self.assertIn('cninfo_pdf_body', spec.parser)
        self.assertIn('pdf_body', spec.form)

    def test_periodic_report_is_primary_for_report_derived_fields(self):
        from core.intelligence.source_catalog import get_source_catalog

        catalog = get_source_catalog()
        periodic_primary_fields = {
            'financial',
            'cost',
            'inventory',
            'customer_supplier',
            'export',
            'demand',
        }

        for field in periodic_primary_fields:
            matches = [
                source for source in catalog[field]
                if source.evidence_origin == 'periodic_report' and source.source_role == 'primary'
            ]
            self.assertTrue(matches, field)

    def test_news_sources_are_not_primary_for_report_derived_fields(self):
        from core.intelligence.source_catalog import get_source_catalog

        catalog = get_source_catalog()
        report_fields = {'financial', 'cost', 'inventory', 'customer_supplier', 'export', 'demand'}

        for field in report_fields:
            for source in catalog[field]:
                if source.evidence_origin == 'news' or source.form == 'rss':
                    self.assertNotEqual(source.source_role, 'primary', f'{field}:{source.source_id}')

    def test_firecrawl_sources_are_planned_disabled_context_capabilities(self):
        from core.intelligence.source_catalog import (
            get_catalog_status,
            get_enabled_catalog_sources,
            get_source_catalog,
        )

        catalog = get_source_catalog()
        firecrawl_sources = [
            source
            for sources in catalog.values()
            for source in sources
            if source.source_id.startswith('firecrawl:')
        ]
        expected_ids = {
            'firecrawl:policy_web_context',
            'firecrawl:industry_web_context',
            'firecrawl:export_web_context',
            'firecrawl:risk_web_context',
        }

        self.assertEqual(expected_ids, {source.source_id for source in firecrawl_sources})
        for source in firecrawl_sources:
            self.assertFalse(source.enabled_by_default)
            self.assertEqual(source.access_mode, 'optional_api')
            self.assertTrue(source.requires_api_key)
            self.assertEqual(source.credential_env, 'FIRECRAWL_API_KEY')
            self.assertEqual(source.evidence_origin, 'web_context')
            self.assertEqual(source.implementation_status, 'implemented')
            self.assertEqual(source.recommended_extractor, 'firecrawl_scrape')

        old_env = dict(os.environ)
        try:
            os.environ['FIRECRAWL_API_KEY'] = 'fc-demo'
            enabled_ids = {source.source_id for source in get_enabled_catalog_sources(catalog)}
            self.assertFalse(expected_ids & enabled_ids)

            status = get_catalog_status(catalog)
            for source_id in expected_ids:
                self.assertEqual(status[source_id]['status'], 'disabled')
                self.assertEqual(status[source_id]['missing_reason'], 'firecrawl_disabled')
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def _credential_envs(catalog):
    names = set()
    for sources in catalog.values():
        for source in sources:
            for name in (source.credential_env, *source.credential_aliases):
                if name:
                    names.add(name)
    return names


if __name__ == '__main__':
    unittest.main()
