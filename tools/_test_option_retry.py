import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.option_quote_provider as option_quote_provider
from core.option_quote_provider import fetch_option_quotes


def _df(rows):
    return pd.DataFrame(rows, columns=['字段', '值'])


_HEALTHY = _df([
    ('期权合约简称', '50ETF购6月2650'),
    ('最新价', '0.3415'),
    ('昨收价', '0.3693'),
    ('行权价', '2.6500'),
    ('涨幅', '-8.86'),
    ('成交量', '27'),
    ('标的股票', '510050'),
    ('行情时间', '2026-06-16 15:00:00'),
])
_GARBLED = _df([('期权合约简称', '�' * 4), ('最新价', '')])
_REMNANT = _df([
    ('期权合约简称', '50ETF购6月2650'),
    ('最新价', '0.3415'),
    ('行权价(元)', '2.6500'),
])
_FINANCE_BOARD = pd.DataFrame([{
    '合约交易代码': '10011251',
    '当前价': '0.3527',
    '涨跌幅': '19.16',
    '前结价': '0.296',
    '行权价': '2.6500',
}])
_EMPTY_FINANCE_BOARD = pd.DataFrame(columns=['合约交易代码', '当前价', '涨跌幅', '前结价', '行权价'])
_KC_FINANCE_BOARD = pd.DataFrame([{
    '合约交易代码': '588000C2607M01800',
    '当前价': '0.3354',
    '涨跌幅': '-11.27',
    '前结价': '0.378',
    '行权价': '1.8000',
}])
_MINUTE = pd.DataFrame([[
    '2026-06-25',
    '09:35',
    '0.3527',
]])


class OptionRetryTest(unittest.TestCase):
    def setUp(self):
        option_quote_provider._SSE_FINANCE_BOARD_CACHE.clear()
        option_quote_provider._SSE_FINANCE_BOARD_CACHE_DATE = None

    def test_retry_recovers_from_garbled(self):
        seq = [_GARBLED, _GARBLED, _HEALTHY]
        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value={}), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', side_effect=seq):
            q = fetch_option_quotes(['10011251'])['10011251']
        self.assertTrue(q['ok'])
        self.assertEqual(q['strike_price'], 2.65)
        self.assertEqual(q['underlying_code'], '510050')

    def test_retry_skips_remnant_version(self):
        seq = [_REMNANT, _REMNANT, _HEALTHY]
        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value={}), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', side_effect=seq):
            q = fetch_option_quotes(['10011251'])['10011251']
        self.assertTrue(q['ok'])
        self.assertEqual(q['strike_price'], 2.65)

    def test_all_garbled_returns_not_ok(self):
        seq = [_GARBLED] * 5
        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value={}), \
             patch('core.option_quote_provider._fetch_sse_one_via_kline', return_value=None), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', side_effect=seq):
            q = fetch_option_quotes(['10011251'])['10011251']
        self.assertFalse(q['ok'])

    def test_healthy_first_call_no_retry(self):
        calls = []

        def _side(symbol):
            calls.append(symbol)
            return _HEALTHY

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value={}), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', side_effect=_side):
            q = fetch_option_quotes(['10011251'])['10011251']
        self.assertTrue(q['ok'])
        self.assertEqual(len(calls), 1)

    def test_sse_pre_close_uses_finance_board_settlement(self):
        metadata = {
            '10011251': {
                'name': '50ETF call Jul',
                'underlying_code': '510050',
                'option_type': 'call',
                'strike_price': 2.65,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
            },
        }

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value=metadata), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_HEALTHY), \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=_FINANCE_BOARD):
            q = fetch_option_quotes(['10011251'])['10011251']

        self.assertTrue(q['ok'])
        self.assertEqual(q['pre_close'], 0.296)
        self.assertEqual(q['pre_close_source'], 'akshare_option_finance_board')
        self.assertAlmostEqual(q['pct'], round((0.3415 - 0.296) / 0.296 * 100, 4))

    def test_sse_pre_close_matches_finance_board_trade_code(self):
        metadata = {
            '10011737': {
                'name': 'KC50 call Jul',
                'finance_trade_code': '588000C2607M01800',
                'underlying_code': '588000',
                'option_type': 'call',
                'strike_price': 1.8,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
            },
        }

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value=metadata), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_HEALTHY), \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=_KC_FINANCE_BOARD):
            q = fetch_option_quotes(['10011737'])['10011737']

        self.assertTrue(q['ok'])
        self.assertEqual(q['pre_close'], 0.378)
        self.assertEqual(q['pre_close_source'], 'akshare_option_finance_board')
        self.assertAlmostEqual(q['pct'], round((0.3415 - 0.378) / 0.378 * 100, 4))

    def test_sse_pre_close_does_not_use_snapshot_close_without_settlement(self):
        metadata = {
            '10011251': {
                'name': '50ETF call Jul',
                'underlying_code': '510050',
                'option_type': 'call',
                'strike_price': 2.65,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
            },
        }

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value=metadata), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_HEALTHY), \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=_EMPTY_FINANCE_BOARD):
            q = fetch_option_quotes(['10011251'])['10011251']

        self.assertTrue(q['ok'])
        self.assertEqual(q['pre_close'], 0.0)
        self.assertEqual(q['pre_close_source'], '')
        self.assertEqual(q['pct'], 0.0)

    def test_kline_fallback_uses_finance_board_settlement(self):
        metadata = {
            '10011251': {
                'name': '50ETF call Jul',
                'underlying_code': '510050',
                'option_type': 'call',
                'strike_price': 2.65,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
            },
        }
        daily = pd.DataFrame([['2026-06-24', 0, 0, 0, 0.1234]])

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value=metadata), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_GARBLED), \
             patch('core.option_quote_provider.ak.option_sse_minute_sina', return_value=_MINUTE), \
             patch('core.option_quote_provider.ak.option_sse_daily_sina', return_value=daily), \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=_FINANCE_BOARD):
            q = fetch_option_quotes(['10011251'])['10011251']

        self.assertTrue(q['ok'])
        self.assertEqual(q['current_price'], 0.3527)
        self.assertEqual(q['pre_close'], 0.296)
        self.assertEqual(q['pre_close_source'], 'akshare_option_finance_board')

    def test_kline_fallback_does_not_use_daily_close_without_settlement(self):
        metadata = {
            '10011251': {
                'name': '50ETF call Jul',
                'underlying_code': '510050',
                'option_type': 'call',
                'strike_price': 2.65,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
            },
        }

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider._fetch_exchange_contract_metadata', return_value=metadata), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_GARBLED), \
             patch('core.option_quote_provider.ak.option_sse_minute_sina', return_value=_MINUTE), \
             patch('core.option_quote_provider.ak.option_sse_daily_sina') as daily_sina, \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=_EMPTY_FINANCE_BOARD):
            q = fetch_option_quotes(['10011251'])['10011251']

        daily_sina.assert_not_called()
        self.assertTrue(q['ok'])
        self.assertEqual(q['current_price'], 0.3527)
        self.assertEqual(q['pre_close'], 0.0)
        self.assertEqual(q['pre_close_source'], '')
        self.assertEqual(q['pct'], 0.0)

    def test_exchange_contract_metadata_fills_expiry_date(self):
        sse_contracts = pd.DataFrame([[
            '10010295',
            '510050C2606A02850',
            '50ETF call Jun',
            '50ETF(510050)',
            'call',
            '2.776',
            '10265',
            '20260624',
            '20260625',
            '20260624',
            '20251023',
        ]])

        with patch('core.option_quote_provider.time.sleep'), \
             patch('core.option_quote_provider.ak.option_sse_spot_price_sina', return_value=_HEALTHY), \
             patch('core.option_quote_provider.ak.option_current_day_sse', return_value=sse_contracts), \
             patch('core.option_quote_provider.ak.option_finance_board', return_value=pd.DataFrame()):
            q = fetch_option_quotes(['10010295'])['10010295']

        self.assertTrue(q['ok'])
        self.assertEqual(q['expiry_date'], '2026-06-24')
        self.assertEqual(q['contract_unit'], 10265)


def _demo_real():
    print('=== 真实接口实测（带重试，需联网）===')
    res = fetch_option_quotes(['10011251', '10011737'])
    for c in ['10011251', '10011737']:
        r = res[c]
        print('%s %s 当前价=%s 行权价=%s 标的=%s ok=%s' % (
            c, r['name'], r['current_price'], r['strike_price'],
            r['underlying_code'], r['ok']))
    print()


if __name__ == '__main__':
    if '--demo' in sys.argv:
        _demo_real()
        sys.argv.remove('--demo')
    unittest.main()
