import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import requests

from core import kline_provider


class KlineCacheFreshnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._tmp.name)
        self._patches = [
            patch.object(kline_provider, '_CACHE_DIR', self._cache_dir),
            patch.object(kline_provider, '_latest_available_trade_date', return_value='2026-06-22'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _write_cache(self, last_date):
        payload = {
            'date': '2026-06-22',
            'columns': ['datetime', 'open', 'close'],
            'rows': [
                {'datetime': f'{last_date} 15:00', 'open': 1.0, 'close': 1.1},
            ],
        }
        (self._cache_dir / '600000.json').write_text(
            json.dumps(payload), encoding='utf-8'
        )

    def test_cache_with_old_last_bar_is_not_fresh(self):
        self._write_cache('2026-06-18')
        self.assertFalse(kline_provider._cache_is_fresh('600000'))

    def test_cache_with_expected_last_bar_is_fresh(self):
        self._write_cache('2026-06-22')
        self.assertTrue(kline_provider._cache_is_fresh('600000'))

    def test_cache_with_bad_latest_tail_bar_is_not_fresh(self):
        payload = {
            'date': '2026-06-22',
            'columns': ['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount'],
            'rows': [
                {'datetime': '2026-06-19 15:00', 'open': 10.0, 'high': 10.8, 'low': 9.8, 'close': 10.5, 'volume': 1000, 'amount': 10000},
                {'datetime': '2026-06-22 15:00', 'open': 10.88, 'high': 10.88, 'low': 10.88, 'close': 10.88, 'volume': 5.877472e-39, 'amount': 5.877472e-39},
            ],
        }
        (self._cache_dir / '600000.json').write_text(json.dumps(payload), encoding='utf-8')
        self.assertFalse(kline_provider._cache_is_fresh('600000'))

    def test_fetch_drops_bad_provider_tail_before_return_and_cache(self):
        idx = pd.DatetimeIndex(['2026-06-19 15:00', '2026-06-22 15:00'])
        bad_tail = pd.DataFrame(
            {
                'open': [10.0, 10.88],
                'high': [10.8, 10.88],
                'low': [9.8, 10.88],
                'close': [10.5, 10.88],
                'volume': [1000, 5.877472e-39],
                'amount': [10000, 5.877472e-39],
            },
            index=idx,
        )
        with patch.object(kline_provider, '_fetch_via_mootdx', return_value=bad_tail), \
             patch.object(kline_provider, '_fetch_via_eastmoney_http', return_value=None), \
             patch.object(kline_provider, '_fetch_via_akshare', return_value=None), \
             patch.object(kline_provider, '_price_sanity_ok', return_value=True):
            df = kline_provider.fetch_daily_kline('600000', days=60)

        self.assertIsNotNone(df)
        self.assertEqual(str(df.index[-1].date()), '2026-06-19')
        cached = json.loads((self._cache_dir / '600000.json').read_text(encoding='utf-8'))
        self.assertEqual(cached['rows'][-1]['datetime'][:10], '2026-06-19')

    def test_paid_tickflow_short_circuits_free_kline_sources(self):
        idx = pd.DatetimeIndex(['2026-06-22 15:00'])
        paid = pd.DataFrame(
            {'open': [10.0], 'high': [10.5], 'low': [9.9], 'close': [10.3],
             'volume': [1000.0], 'amount': [10000.0]},
            index=idx,
        )
        with patch.object(kline_provider, '_has_paid_tickflow_token', return_value=True), \
             patch.object(kline_provider, 'fetch_tickflow_kline', return_value=paid) as paid_fetch, \
             patch.object(kline_provider, '_fetch_via_mootdx') as mootdx, \
             patch.object(kline_provider, '_fetch_via_eastmoney_http') as eastmoney, \
             patch.object(kline_provider, '_fetch_via_akshare') as akshare, \
             patch.object(kline_provider, '_price_sanity_ok', return_value=True):
            df = kline_provider.fetch_daily_kline('600000', days=60)

        self.assertIsNotNone(df)
        paid_fetch.assert_called_once()
        mootdx.assert_not_called()
        eastmoney.assert_not_called()
        akshare.assert_not_called()

    def test_paid_tickflow_failure_falls_back_sequentially(self):
        idx = pd.DatetimeIndex(['2026-06-22 15:00'])
        free = pd.DataFrame(
            {'open': [10.0], 'high': [10.5], 'low': [9.9], 'close': [10.3],
             'volume': [1000.0], 'amount': [10000.0]},
            index=idx,
        )
        with patch.object(kline_provider, '_has_paid_tickflow_token', return_value=True), \
             patch.object(kline_provider, 'fetch_tickflow_kline', return_value=None), \
             patch.object(kline_provider, '_fetch_via_mootdx', return_value=free) as mootdx, \
             patch.object(kline_provider, '_fetch_via_eastmoney_http') as eastmoney, \
             patch.object(kline_provider, '_fetch_via_akshare') as akshare, \
             patch.object(kline_provider, '_price_sanity_ok', return_value=True):
            df = kline_provider.fetch_daily_kline('600000', days=60)

        self.assertIsNotNone(df)
        mootdx.assert_called_once()
        eastmoney.assert_not_called()
        akshare.assert_not_called()

    def test_tickflow_daily_timestamp_uses_china_trade_date(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    'data': {
                        'timestamp': [1782921600000],  # 2026-07-01 16:00 UTC = 2026-07-02 China
                        'open': [9.1],
                        'high': [9.5],
                        'low': [9.0],
                        'close': [9.4],
                        'volume': [1000],
                        'amount': [9000],
                    }
                }

        with patch.object(kline_provider, '_tickflow_kline_token', return_value='token'), \
             patch.object(requests, 'get', return_value=FakeResponse()):
            df = kline_provider.fetch_tickflow_kline('000725.SZ', count=60)

        self.assertIsNotNone(df)
        self.assertEqual(str(df.index[-1]), '2026-07-02 00:00:00')


if __name__ == '__main__':
    unittest.main()
