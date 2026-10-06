import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tracking


EXPECTED_DATE = '2026-06-22'


def _base_pred():
    return {
        'kind': 'stock',
        'code': '600000',
        'name': 'fresh',
        'direction': 'bullish',
        'final_rating': 'buy',
        'final_action': 'buy',
        'confidence': 6,
        'reasoning': 'fresh analysis',
        'strategy_reason': 'fresh strategy',
        'entry_price': 10.0,
        'target_pct': 8.0,
        'stop_pct': 4.0,
        'task_blueprint': {
            'category': 'buy',
            'direction': 'bullish',
            'entry_price': 10.0,
            'horizon_days': 3,
            'target_pct': 8.0,
            'stop_pct': 4.0,
        },
        '_kline_summary': f'kline summary as of {EXPECTED_DATE}: close 10.0',
        '_flow_profile': {'last': {'date': EXPECTED_DATE}, 'days': 1},
        '_money_flow_summary': f'flow summary {EXPECTED_DATE}: main inflow',
        '_stock_news_checked_at': f'{EXPECTED_DATE}T10:00:00',
        '_intel_checked_at': f'{EXPECTED_DATE}T10:00:00',
        '_stock_news': [{'title': 'old but still relevant', 'date': '2026-06-18 09:00:00'}],
    }


class TrackingFreshnessGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._file = Path(self._tmp.name) / 'tracking_tasks.json'
        self._file_patch = patch.object(tracking, '_DATA_FILE', self._file)
        self._date_patch = patch.object(
            tracking, '_latest_available_trade_date', return_value=EXPECTED_DATE
        )
        self._file_patch.start()
        self._date_patch.start()

    def tearDown(self):
        self._date_patch.stop()
        self._file_patch.stop()
        self._tmp.cleanup()

    def test_create_task_accepts_fresh_inputs(self):
        task = tracking.create_task(_base_pred(), [])
        self.assertEqual(task['code'], '600000')

    def test_create_task_rejects_reasoning_cache_prediction(self):
        pred = _base_pred()
        pred['from_reasoning_cache'] = True

        with self.assertRaisesRegex(ValueError, 'stale_reasoning_cache'):
            tracking.create_task(pred, [])

    def test_create_task_rejects_stale_kline_date(self):
        pred = _base_pred()
        pred['_kline_summary'] = 'kline summary as of 2026-06-18: close 10.0'

        with self.assertRaisesRegex(ValueError, 'stale_kline'):
            tracking.create_task(pred, [])

    def test_create_task_rejects_stale_flow_date(self):
        pred = _base_pred()
        pred['_flow_profile']['last']['date'] = '2026-06-18'

        with self.assertRaisesRegex(ValueError, 'stale_flow'):
            tracking.create_task(pred, [])

    def test_create_task_rejects_missing_news_check_date(self):
        pred = _base_pred()
        pred.pop('_stock_news_checked_at')

        with self.assertRaisesRegex(ValueError, 'stale_intel'):
            tracking.create_task(pred, [])

    def test_create_task_rejects_news_fetch_error(self):
        pred = _base_pred()
        pred['_stock_news_error'] = 'timeout'

        with self.assertRaisesRegex(ValueError, 'stale_intel'):
            tracking.create_task(pred, [])


if __name__ == '__main__':
    unittest.main()
