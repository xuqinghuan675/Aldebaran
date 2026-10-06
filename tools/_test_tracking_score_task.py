import unittest
from datetime import date

import pandas as pd

from core.tracking import score_task


def _df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows],
         'low': [r[2] for r in rows],
         'close': [r[3] for r in rows]},
        index=idx,
    )


def _avoid_task():
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'direction': 'bearish',
        'task_class': 'avoid',
        'pred_snapshot': {'_agent_technical': {'stance': 'bearish', 'confidence': 7}},
    }


class ScoreTaskRoutingTest(unittest.TestCase):
    def test_avoid_bearish_fall_hits_and_rated_well(self):
        df = _df([('2026-06-01', 101, 98, 100), ('2026-06-05', 99, 94, 95)])
        grade, detail = score_task(_avoid_task(), df, date(2026, 6, 5), 95.0)
        self.assertIn(grade, ('A', 'B'))
        self.assertEqual(detail['agent_hit_map']['technical']['status'], '命中')

    def test_avoid_bearish_rise_misleads_and_rated_poorly(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-05', 107, 104, 106)])
        grade, detail = score_task(_avoid_task(), df, date(2026, 6, 5), 106.0)
        self.assertIn(grade, ('D', 'F'))
        self.assertEqual(detail['agent_hit_map']['technical']['status'], '误导')

    def test_buy_routes_to_trigger_target(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'target_pct': 5.0, 'stop_pct': -3.0, 'direction': 'bullish',
            'task_class': 'buy', 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-05', 106, 100, 105)])
        grade, detail = score_task(task, df, date(2026, 6, 5), 105.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_watch_neutral_routes_no_position(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'direction': 'neutral', 'task_class': 'watch',
            'expected_range_pct': [-2, 2], 'pred_snapshot': {},
        }
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 101, 99, 101)])
        grade, detail = score_task(task, df, date(2026, 6, 5), 101.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['task_class'], 'watch')

    def test_avoid_no_entry_uses_signal_price(self):
        task = {'created_at': '2026-06-01T09:30:00', 'entry_price': 0,
                'task_class': 'avoid', 'direction': 'bearish', 'pred_snapshot': {}}
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 96, 94, 95)])
        grade, detail = score_task(task, df, date(2026, 6, 5), 95.0)
        self.assertNotIn('error', detail)
        self.assertEqual(grade, 'A')


def _bw_task(entry=10.0, trigger=10.8, fail=9.5):
    return {
        'created_at': '2026-06-01T09:30:00', 'entry_price': entry,
        'direction': 'bullish', 'task_class': 'watch',
        'pred_snapshot': {'task_blueprint': {
            'category': 'bullish_watch', 'entry_trigger': trigger,
            'fail_level': fail, 'triggered_target_pct': 11.0,
        }},
    }


class BullishWatchExpireTest(unittest.TestCase):
    def _score(self, deadline_price, task=None):
        df = _df([
            ('2026-06-01', 10.1, 9.9, 10.0),
            ('2026-06-05', max(deadline_price, 10.0), min(deadline_price, 9.9), deadline_price),
        ])
        return score_task(task or _bw_task(), df, date(2026, 6, 5), deadline_price)

    def test_between_entry_and_trigger_is_B(self):
        g, d = self._score(10.5)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')

    def test_between_fail_and_entry_is_C(self):
        g, d = self._score(9.8)
        self.assertEqual(g, 'C')
        self.assertEqual(d['outcome_tier'], 'waited_flat')

    def test_below_fail_is_D(self):
        g, d = self._score(9.2)
        self.assertEqual(g, 'D')
        self.assertEqual(d['outcome_tier'], 'setup_broken')

    def test_above_trigger_without_conversion_is_B(self):
        g, d = self._score(11.0)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')

    def test_small_rise_above_trigger_without_conversion_is_waiting_reasonable(self):
        task = _bw_task(entry=391.98, trigger=391.98, fail=366.501)
        g, d = self._score(395.47, task=task)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')
        self.assertEqual(d['final_pct'], 0.89)

    def test_below_fail_but_less_than_six_percent_drop_is_C(self):
        task = _bw_task(entry=100.0, trigger=110.0, fail=98.0)
        g, d = self._score(95.0, task=task)
        self.assertEqual(g, 'C')
        self.assertEqual(d['outcome_tier'], 'waited_flat')

    def test_drop_over_six_percent_is_D(self):
        task = _bw_task(entry=100.0, trigger=110.0, fail=95.0)
        g, d = self._score(93.9, task=task)
        self.assertEqual(g, 'D')
        self.assertEqual(d['outcome_tier'], 'setup_broken')

    def test_missing_levels_is_error_not_F(self):
        task = _bw_task()
        task['pred_snapshot']['task_blueprint'].pop('entry_trigger')
        g, d = self._score(10.5, task=task)
        self.assertEqual(g, '')
        self.assertIn('error', d)

    def test_converted_task_routes_to_buy_scoring(self):
        task = _bw_task()
        task.update({
            'converted_from_watch': True, 'task_class': 'buy',
            'entry_price': 10.8, 'target_pct': 11.0, 'stop_pct': -12.0,
            'trigger_date': '2026-06-02',
        })
        df = _df([('2026-06-02', 10.9, 10.6, 10.8), ('2026-06-05', 12.2, 11.5, 12.1)])
        g, d = score_task(task, df, date(2026, 6, 5), 12.1)
        self.assertEqual(g, 'A')
        self.assertEqual(d['trigger'], 'target')


if __name__ == '__main__':
    unittest.main()
