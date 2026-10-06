import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data_source import get_stock_quotes, quotes_routed


class PriceRoutingTest(unittest.TestCase):
    def test_quotes_routed_uses_tickflow_for_non_option_stock_codes(self):
        tickflow_quote = {
            '600000': {'price': 10.0, 'pre_close': 9.5, 'source': 'tickflow'},
            '000001': {'price': 20.0, 'pre_close': 19.0, 'source': 'tickflow'},
        }
        free_quote = {
            '600000': {'price': 9.0, 'pre_close': 8.8, 'source': 'tencent'},
            '000001': {'price': 18.0, 'pre_close': 17.5, 'source': 'tencent'},
        }
        with patch('core.credentials.has_tickflow_token', return_value=True), \
             patch('core.etf_quote_provider.fetch_etf_quotes', return_value=tickflow_quote), \
             patch('core.data_source.tencent_quotes', return_value=free_quote):
            quotes = quotes_routed(['600000', '000001'])

        self.assertEqual(quotes['600000']['source'], 'tickflow')
        self.assertEqual(quotes['000001']['source'], 'tickflow')

    def test_bulk_codes_skip_tickflow_and_use_free_source(self):
        bulk_codes = [f'{600000 + i:06d}' for i in range(60)]
        free_quote = {c: {'price': 1.0, 'source': 'tencent'} for c in bulk_codes}
        with patch('core.credentials.has_tickflow_token', return_value=True), \
             patch('core.etf_quote_provider.fetch_etf_quotes') as mock_tf, \
             patch('core.data_source.tencent_quotes', return_value=free_quote):
            quotes = quotes_routed(bulk_codes)

        mock_tf.assert_not_called()
        self.assertEqual(len(quotes), 60)
        self.assertTrue(all(q['source'] == 'tencent' for q in quotes.values()))

    def test_bulk_tickflow_flag_forces_tickflow_for_large_lists(self):
        bulk_codes = [f'{510000 + i:06d}' for i in range(60)]
        tf_quote = {c: {'price': 2.0, 'source': 'tickflow'} for c in bulk_codes}
        with patch('core.credentials.has_tickflow_token', return_value=True), \
             patch('core.etf_quote_provider.fetch_etf_quotes', return_value=tf_quote) as mock_tf, \
             patch('core.data_source.tencent_quotes', return_value={}):
            quotes = quotes_routed(bulk_codes, bulk_tickflow=True)

        mock_tf.assert_called_once()
        self.assertEqual(mock_tf.call_args.kwargs.get('max_batches'), None)
        self.assertEqual(len(quotes), 60)
        self.assertTrue(all(q['source'] == 'tickflow' for q in quotes.values()))

    def test_legacy_get_stock_quotes_uses_routed_tickflow_path(self):
        tickflow_quote = {
            '600000': {'price': 10.0, 'pre_close': 9.5, 'source': 'tickflow'},
        }
        free_quote = {
            '600000': {'price': 9.0, 'pre_close': 8.8, 'source': 'tencent'},
        }
        with patch('core.credentials.has_tickflow_token', return_value=True), \
             patch('core.etf_quote_provider.fetch_etf_quotes', return_value=tickflow_quote), \
             patch('core.data_source.tencent_quotes', return_value=free_quote):
            quotes = get_stock_quotes(['600000'])

        self.assertEqual(quotes['600000']['source'], 'tickflow')


if __name__ == '__main__':
    unittest.main()
