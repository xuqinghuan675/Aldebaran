import unittest
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.holdings_pnl_calendar import (
    build_holding_day_change,
    build_holdings_pnl_snapshot,
    format_holdings_pnl_tooltip,
    frozen_snapshot_matches_sources,
    holdings_pnl_source_key,
    pnl_calendar_update_phase,
)


class HoldingsPnlCalendarTest(unittest.TestCase):
    def test_holding_day_change_uses_current_price_minus_pre_close(self):
        metrics = build_holding_day_change(
            {'code': '600000', 'shares': 100, 'cost_price': 8.0},
            {'price': 10.0, 'pre_close': 9.5},
        )

        self.assertEqual(metrics['current_price'], 10.0)
        self.assertEqual(metrics['pre_close'], 9.5)
        self.assertEqual(metrics['day_change'], 0.5)
        self.assertEqual(metrics['day_pnl'], 50.0)

    def test_holding_day_change_requires_pre_close_for_daily_fields(self):
        metrics = build_holding_day_change(
            {'code': '600000', 'shares': 100, 'cost_price': 8.0},
            {'price': 10.0},
        )

        self.assertEqual(metrics['current_price'], 10.0)
        self.assertIsNone(metrics['pre_close'])
        self.assertIsNone(metrics['day_change'])
        self.assertIsNone(metrics['day_pnl'])

    def test_stock_pnl_uses_pre_close_even_when_added_today(self):
        holdings = [{
            'code': '600000',
            'shares': 100,
            'cost_price': 8.0,
            'buy_date': '2026-06-06',
        }]
        prices = {
            '600000': {'price': 10.0, 'pre_close': 9.0},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=[],
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 100.0)
        self.assertEqual(snap['stock_count'], 1)
        self.assertEqual(snap['missing_count'], 0)

    def test_missing_pre_close_does_not_fall_back_to_cost_price(self):
        holdings = [{
            'code': '600000',
            'shares': 100,
            'cost_price': 8.0,
            'buy_date': '2026-06-06',
        }]
        prices = {
            '600000': {'price': 10.0},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=[],
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 0.0)
        self.assertEqual(snap['stock_count'], 0)
        self.assertEqual(snap['missing_count'], 1)

    def test_stock_with_zero_cost_still_counts_daily_price_change(self):
        holdings = [{
            'code': '600000',
            'shares': 100,
            'cost_price': 0.0,
        }]
        prices = {
            '600000': {'price': 10.0, 'pre_close': 9.0},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=[],
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 100.0)
        self.assertEqual(snap['summary']['total_cost'], 0.0)
        self.assertEqual(snap['stock_count'], 1)
        self.assertEqual(snap['missing_count'], 0)

    def test_long_option_day_change_uses_current_price_field(self):
        options = [{
            'code': '10001234',
            'status': 'holding',
            'position_side': 'long_right',
            'option_type': 'put',
            'contracts': 2,
            'contract_unit': 10000,
            'current_price': 0.10,
        }]
        prices = {
            '10001234': {'current_price': 0.12, 'pre_close': 0.10},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=[],
            option_records=options,
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 400.0)
        self.assertEqual(snap['stock_count'], 1)
        self.assertEqual(snap['missing_count'], 0)

    def test_short_obligation_option_uses_reverse_sign(self):
        options = [{
            'code': '10009999',
            'status': 'holding',
            'position_side': 'short_obligation',
            'option_type': 'call',
            'contracts': 2,
            'contract_unit': 10000,
        }]
        prices = {
            '10009999': {'current_price': 0.12, 'pre_close': 0.10},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=[],
            option_records=options,
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], -400.0)

    def test_long_call_and_put_both_count_positive_when_price_rises(self):
        holdings = [
            {'code': '600000', 'shares': 100, 'cost_price': 8.0},
            {'code': '000001', 'shares': 200, 'cost_price': 12.0},
        ]
        options = [
            {
                'code': '10001234',
                'status': 'holding',
                'position_side': 'long_right',
                'option_type': 'put',
                'contracts': 1,
                'contract_unit': 10000,
            },
            {
                'code': '10005678',
                'status': 'holding',
                'position_side': 'long_right',
                'option_type': 'call',
                'contracts': 1,
                'contract_unit': 10000,
            },
        ]
        prices = {
            '600000': {'price': 10.0, 'pre_close': 9.5},
            '000001': {'price': 20.0, 'pre_close': 19.0},
            '10001234': {'current_price': 0.12, 'pre_close': 0.10},
            '10005678': {'current_price': 0.12, 'pre_close': 0.10},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=options,
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 650.0)
        self.assertEqual(snap['stock_count'], 4)
        self.assertEqual(snap['missing_count'], 0)

    def test_snapshot_breakdown_separates_stock_etf_and_option_pnl(self):
        holdings = [
            {'code': '600000', 'shares': 100, 'cost_price': 8.0},
            {'code': '510300', 'shares': 200, 'cost_price': 4.0},
        ]
        options = [{
            'code': '10001234',
            'status': 'holding',
            'position_side': 'long_right',
            'contracts': 1,
            'contract_unit': 10000,
        }]
        prices = {
            '600000': {'price': 10.0, 'pre_close': 9.0},
            '510300': {'price': 4.10, 'pre_close': 4.00},
            '10001234': {'current_price': 0.12, 'pre_close': 0.10},
        }

        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=options,
            prices=prices,
            today='2026-06-06',
            frozen=False,
        )

        self.assertEqual(snap['total_pnl'], 320.0)
        self.assertEqual(snap['breakdown']['stock_pnl'], 100.0)
        self.assertEqual(snap['breakdown']['etf_pnl'], 20.0)
        self.assertEqual(snap['breakdown']['option_pnl'], 200.0)
        self.assertEqual(snap['breakdown']['stock_count'], 1)
        self.assertEqual(snap['breakdown']['etf_count'], 1)
        self.assertEqual(snap['breakdown']['option_count'], 1)

    def test_tooltip_shows_stock_etf_and_option_breakdown(self):
        tip = format_holdings_pnl_tooltip({
            'date': '2026-06-18',
            'total_pnl': 4773.0,
            'stock_count': 6,
            'breakdown': {
                'stock_pnl': 4238.0,
                'etf_pnl': 0.0,
                'option_pnl': 535.0,
            },
        })

        self.assertIn('总盈亏: +4,773 元', tip)
        self.assertIn('个股: +4,238 元', tip)
        self.assertIn('ETF: +0 元', tip)
        self.assertIn('期权: +535 元', tip)

    def test_intraday_phase_requires_user_opened_portfolio_page(self):
        now = datetime(2026, 6, 8, 10, 0)

        self.assertIsNone(
            pnl_calendar_update_phase(now, is_trade_day=True, intraday_enabled=False)
        )
        self.assertEqual(
            pnl_calendar_update_phase(now, is_trade_day=True, intraday_enabled=True),
            'live',
        )

    def test_post_close_phase_does_not_require_opening_portfolio_page(self):
        now = datetime(2026, 6, 8, 15, 1)

        self.assertEqual(
            pnl_calendar_update_phase(now, is_trade_day=True, intraday_enabled=False),
            'freeze',
        )

    def test_empty_frozen_snapshot_is_stale_after_sources_are_added(self):
        old = {
            'date': '2026-06-18',
            'total_pnl': 0.0,
            'stock_count': 0,
            'missing_count': 0,
            'frozen': True,
        }
        source_key = holdings_pnl_source_key(
            holdings=[{'code': '000725', 'shares': 12200, 'cost_price': 6.55}],
            option_records=[{
                'code': '10011684',
                'status': 'holding',
                'position_side': 'long_right',
                'contracts': 1,
                'contract_unit': 10000,
                'expiry_date': '2026-07-22',
                'current_price': 0.1054,
                'pre_close': 0.1034,
            }],
        )

        self.assertFalse(frozen_snapshot_matches_sources(old, source_key))

    def test_frozen_snapshot_matches_when_source_key_is_unchanged(self):
        source_key = holdings_pnl_source_key(
            holdings=[{'code': '000725', 'shares': 12200, 'cost_price': 6.55}],
            option_records=[],
        )
        snap = {
            'date': '2026-06-18',
            'total_pnl': -1220.0,
            'frozen': True,
            'source_key': source_key,
            'breakdown': {'stock_pnl': -1220.0, 'etf_pnl': 0.0, 'option_pnl': 0.0},
        }

        self.assertTrue(frozen_snapshot_matches_sources(snap, source_key))

    def test_frozen_snapshot_without_breakdown_is_stale(self):
        source_key = holdings_pnl_source_key(
            holdings=[{'code': '000725', 'shares': 12200, 'cost_price': 6.55}],
            option_records=[],
        )
        snap = {
            'date': '2026-06-18',
            'total_pnl': -1220.0,
            'frozen': True,
            'source_key': source_key,
        }

        self.assertFalse(frozen_snapshot_matches_sources(snap, source_key))


if __name__ == '__main__':
    unittest.main()
