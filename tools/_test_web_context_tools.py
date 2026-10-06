import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class WebContextToolsTest(unittest.TestCase):
    def test_default_disabled_does_not_request_network(self):
        from core.intelligence.web_context_tools import scrape_url_to_markdown

        calls = []

        def request_post(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('network should not be requested')

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp},
            clear=False,
        ):
            os.environ.pop('FIRECRAWL_MODE', None)
            result = scrape_url_to_markdown('https://example.com/policy', layer='policy', request_post=request_post)

        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(calls, [])

    def test_missing_key_and_base_url_statuses_are_explicit(self):
        from core.intelligence.web_context_tools import build_firecrawl_status

        with patch.dict(os.environ, {'FIRECRAWL_MODE': 'remote', 'FIRECRAWL_BASE_URL': 'https://firecrawl.local'}, clear=True):
            self.assertEqual(build_firecrawl_status()['status'], 'missing_credentials')

        with patch.dict(os.environ, {'FIRECRAWL_MODE': 'remote', 'FIRECRAWL_API_KEY': 'fc-demo'}, clear=True):
            self.assertEqual(build_firecrawl_status()['status'], 'missing_base_url')

        with patch.dict(os.environ, {'FIRECRAWL_MODE': 'local_lite'}, clear=True):
            status = build_firecrawl_status()
            self.assertEqual(status['status'], 'missing_base_url')
            self.assertEqual(status['access_mode'], 'optional_self_hosted')
            self.assertEqual(status['max_concurrent'], 1)
            self.assertEqual(status['timeout_seconds'], 25)

    def test_mock_scrape_markdown_can_convert_to_raw_intel(self):
        from core.intelligence.web_context_tools import firecrawl_result_to_raw_intel, scrape_url_to_markdown

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'success': True,
                    'data': {
                        'markdown': '# Policy update\nDemand guidance improved.',
                        'metadata': {'title': 'Policy update'},
                    },
                }

        calls = []

        def request_post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp,
                'FIRECRAWL_MODE': 'remote',
                'FIRECRAWL_API_KEY': 'fc-demo',
                'FIRECRAWL_BASE_URL': 'https://api.firecrawl.dev',
            },
            clear=True,
        ):
            result = scrape_url_to_markdown('https://example.com/policy', layer='policy', request_post=request_post)
            item = firecrawl_result_to_raw_intel(
                result,
                url='https://example.com/policy',
                layer='policy',
                query='MACD should remain only query metadata',
            )

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(calls), 1)
        payload = calls[0][1]['json']
        self.assertEqual(payload['formats'], ['markdown'])
        self.assertTrue(payload['onlyMainContent'])
        self.assertTrue(payload['removeBase64Images'])
        self.assertFalse(payload['skipTlsVerification'])
        self.assertNotIn('actions', payload)
        self.assertIsNotNone(item)
        self.assertEqual(item.layer, 'policy')
        self.assertEqual(item.source_id, 'firecrawl:policy_web_context')
        self.assertEqual(item.raw['source_role'], 'secondary')
        self.assertEqual(item.raw['evidence_origin'], 'web_context')
        self.assertEqual(item.raw['access_mode'], 'optional_api')
        self.assertEqual(item.raw['extractor'], 'firecrawl_scrape')
        self.assertFalse(item.raw['cached'])
        self.assertEqual(item.raw['original_url'], 'https://example.com/policy')
        self.assertEqual(item.raw['markdown_length'], len(result['markdown']))
        self.assertEqual(item.raw['query'], 'MACD should remain only query metadata')
        self.assertNotIn('MACD', item.text)

    def test_cache_hit_does_not_request_network(self):
        from core.intelligence.web_context_tools import scrape_url_to_markdown

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'success': True,
                    'data': {
                        'markdown': '# Cached policy\nPolicy text',
                        'metadata': {'title': 'Cached policy'},
                    },
                }

        calls = []

        def request_post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp,
                'FIRECRAWL_MODE': 'remote',
                'FIRECRAWL_API_KEY': 'fc-demo',
                'FIRECRAWL_BASE_URL': 'https://api.firecrawl.dev',
            },
            clear=True,
        ):
            first = scrape_url_to_markdown('https://example.com/policy', layer='policy', request_post=request_post)
            second = scrape_url_to_markdown('https://example.com/policy', layer='policy', request_post=request_post)
            markdown_cache_exists = (Path(second['cache_path']) / 'markdown.json').exists()
            metadata_cache_exists = (Path(second['cache_path']) / 'metadata.json').exists()

        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['status'], 'cached')
        self.assertTrue(second['cached'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(markdown_cache_exists)
        self.assertTrue(metadata_cache_exists)

    def test_skipped_recently_does_not_request_network_when_cache_is_bypassed(self):
        from core.intelligence.web_context_tools import scrape_url_to_markdown

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {'success': True, 'data': {'markdown': '# First\nIndustry text'}}

        calls = []

        def request_post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp,
                'FIRECRAWL_MODE': 'local_lite',
                'FIRECRAWL_BASE_URL': 'http://localhost:3002',
                'FIRECRAWL_MIN_INTERVAL_HOURS': '12',
            },
            clear=True,
        ):
            first = scrape_url_to_markdown('https://example.com/industry', layer='industry', request_post=request_post)
            second = scrape_url_to_markdown(
                'https://example.com/industry',
                layer='industry',
                request_post=request_post,
                use_cache=False,
            )

        self.assertEqual(first['status'], 'ok')
        self.assertEqual(second['status'], 'skipped_recently')
        self.assertEqual(len(calls), 1)

    def test_scrape_timeout_reports_timeout_without_raising(self):
        from core.intelligence.web_context_tools import scrape_url_to_markdown

        def request_post(*args, **kwargs):
            raise TimeoutError('slow scrape')

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                'ALDEBARAN_INTELLIGENCE_CACHE_DIR': tmp,
                'FIRECRAWL_MODE': 'local_lite',
                'FIRECRAWL_BASE_URL': 'http://localhost:3002',
            },
            clear=True,
        ):
            result = scrape_url_to_markdown('https://example.com/risk', layer='risk', request_post=request_post)

        self.assertEqual(result['status'], 'timeout')
        self.assertIn('slow scrape', result['error'])

    def test_invalid_layer_and_technical_text_are_filtered(self):
        from core.intelligence.web_context_tools import firecrawl_result_to_raw_intel

        result = {
            'status': 'ok',
            'markdown': 'K-line and MACD breakout signal.',
            'metadata': {'title': 'Technical note'},
        }

        self.assertIsNone(
            firecrawl_result_to_raw_intel(result, url='https://example.com/a', layer='policy')
        )
        with self.assertRaises(ValueError):
            firecrawl_result_to_raw_intel({'status': 'ok', 'markdown': 'x'}, url='https://example.com/a', layer='support')

    def test_firecrawl_only_allows_secondary_web_context_layers(self):
        from core.intelligence.web_context_tools import FIRECRAWL_ALLOWED_LAYERS, firecrawl_result_to_raw_intel

        self.assertEqual(
            FIRECRAWL_ALLOWED_LAYERS,
            {'policy', 'industry', 'export', 'demand', 'competition', 'risk'},
        )
        result = {
            'status': 'ok',
            'markdown': '# Export note\nExport controls changed.',
            'metadata': {'title': 'Export note'},
            'cached': True,
            'access_mode': 'optional_self_hosted',
        }
        item = firecrawl_result_to_raw_intel(result, url='https://example.com/export', layer='export', query='inventory')
        self.assertIsNotNone(item)
        self.assertEqual(item.raw['query'], 'inventory')
        self.assertEqual(item.layer, 'export')
        for blocked in ('financial', 'inventory', 'customer_supplier', 'customer_concentration', 'supplier_concentration', 'cost'):
            with self.assertRaises(ValueError):
                firecrawl_result_to_raw_intel(result, url='https://example.com/a', layer=blocked)


if __name__ == '__main__':
    unittest.main()
