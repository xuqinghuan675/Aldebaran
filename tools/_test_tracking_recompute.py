import unittest
from datetime import date

import pandas as pd

from core.tracking import recompute_task


def _df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows],
         'low': [r[2] for r in rows],
         'close': [r[3] for r in rows]},
        index=idx,
    )


class RecomputeTest(unittest.TestCase):
    def test_recompute_keeps_task_class_immutable(self):
        task = {
            'id': '1', 'status': 'closed', 'direction': 'bearish',
            'task_class': 'avoid', 'final_action': 'watch_only',
            'created_at': '2026-06-01T09:30:00',
            'deadline': '2026-06-05', 'entry_price': 100.0, 'code': '600519',
            'name': 'x', 'grade': 'F', 'retrospective': '旧错误复盘',
            'pred_snapshot': {'_agent_technical': {'stance': 'bearish', 'confidence': 7}},
        }
        df = _df([('2026-06-01', 101, 98, 100), ('2026-06-05', 99, 94, 95)])
        ok = recompute_task(task, df)
        self.assertTrue(ok)
        self.assertEqual(task['task_class'], 'avoid')
        self.assertIn(task['grade'], ('A', 'B'))
        self.assertIsNone(task['retrospective'])
        self.assertEqual(
            task['grade_detail']['agent_hit_map']['technical']['status'], '命中')

    def test_bullish_watch_only_recomputed_to_watch(self):
        task = {
            'id': '1', 'status': 'closed', 'direction': 'bullish',
            'task_class': 'watch', 'final_action': 'watch_only',
            'created_at': '2026-06-01T09:30:00', 'deadline': '2026-06-05',
            'entry_price': 100.0, 'code': 'x', 'name': '达实',
            'target_pct': 22.0, 'stop_pct': -11.0, 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-05', 104, 80, 82)])
        recompute_task(task, df)
        self.assertEqual(task['task_class'], 'watch')
        self.assertEqual(task['grade'], 'A')

    def test_recompute_refreshes_kline_at_close_with_scoring_kline(self):
        task = {
            'id': '1', 'status': 'closed', 'direction': 'bullish',
            'task_class': 'buy', 'final_action': 'buy',
            'created_at': '2026-06-01T09:30:00',
            'deadline': '2026-06-05',
            'entry_price': 100.0, 'target_pct': 5.0, 'stop_pct': -3.0,
            'code': 'x', 'name': 'x', 'pred_snapshot': {},
            'kline_at_close': [{'d': '2026-06-05', 'c': 999.0}],
        }
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-05', 104, 100, 103)])

        self.assertTrue(recompute_task(task, df))

        self.assertEqual(task['grade_detail']['deadline_price'], 103.0)
        self.assertEqual(task['kline_at_close'][-1]['d'], '2026-06-05')
        self.assertEqual(task['kline_at_close'][-1]['c'], 103.0)

    def test_missing_kline_returns_false(self):
        task = {
            'id': '1', 'status': 'closed', 'direction': 'bullish',
            'final_action': 'buy', 'created_at': '2026-06-01T09:30:00',
            'deadline': '2026-06-05', 'entry_price': 100.0, 'code': 'x',
            'target_pct': 5, 'stop_pct': -3, 'pred_snapshot': {},
        }
        ok = recompute_task(task, _df([]))
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
