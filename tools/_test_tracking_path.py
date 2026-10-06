import unittest
from datetime import date

import pandas as pd

from core.tracking import _score_no_position, _score_buy_task


def _df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows],
         'low': [r[2] for r in rows],
         'close': [r[3] for r in rows]},
        index=idx,
    )


class PathExtremesTest(unittest.TestCase):
    def test_watch_peak_trough_from_real_kline(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'task_class': 'watch', 'direction': 'neutral', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 105, 99, 100), ('2026-06-05', 101, 90, 98)])
        _, detail = _score_no_position(task, df, date(2026, 6, 5), 98.0, 'watch')
        self.assertEqual(detail['peak_pct'], 5.0)
        self.assertEqual(detail['trough_pct'], -10.0)
        self.assertTrue(detail['path_data_ok'])

    def test_buy_peak_trough_from_real_kline(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'target_pct': 5.0, 'stop_pct': -3.0, 'task_class': 'buy',
            'direction': 'bullish', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-05', 104, 98, 102)])
        _, detail = _score_buy_task(task, df, date(2026, 6, 5), 102.0)
        self.assertEqual(detail['peak_pct'], 4.0)
        self.assertEqual(detail['trough_pct'], -2.0)
        self.assertTrue(detail['path_data_ok'])

    def test_no_kline_falls_back_without_crash(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'task_class': 'watch', 'direction': 'neutral', 'pred_snapshot': {},
        }
        _, detail = _score_no_position(task, None, date(2026, 6, 5), 98.0, 'watch')
        self.assertFalse(detail['path_data_ok'])

    def test_buy_detail_has_path_quality_clean(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'target_pct': 5.0, 'stop_pct': -3.0, 'task_class': 'buy',
            'direction': 'bullish', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 102, 99.5, 101), ('2026-06-05', 103, 99.6, 102)])
        _, detail = _score_buy_task(task, df, date(2026, 6, 5), 102.0)
        self.assertEqual(detail['path_quality'], 'P_clean')

    def test_no_position_detail_has_path_quality(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'task_class': 'watch', 'direction': 'neutral', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 101, 98, 99)])
        _, detail = _score_no_position(task, df, date(2026, 6, 5), 99.0, 'watch')
        self.assertEqual(detail['path_quality'], 'neutral')

    def test_no_entry_uses_signal_day_close(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 0,
            'task_class': 'avoid', 'direction': 'bearish', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 96, 94, 95)])
        grade, detail = _score_no_position(task, df, date(2026, 6, 5), 95.0, 'avoid')
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['entry_price'], 100.0)


if __name__ == '__main__':
    unittest.main()
