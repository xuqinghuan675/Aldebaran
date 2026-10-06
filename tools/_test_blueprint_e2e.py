import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from core.predictor import _sync_category, _assemble_task_blueprint
from core import tracking


def _df(rows):
    return pd.DataFrame(
        rows,
        index=pd.to_datetime([r['date'] for r in rows]),
    )


def _g_prediction(**kw):
    p = {
        'code': '600000', 'name': '浦发银行', 'kind': 'stock',
        'direction': 'bullish', 'final_action': 'buy',
        'opportunity_grade': 'S', 'confidence': 7,
        'entry_price': 10.0, 'entry_ref': 10.0,
        'target_pct': 8.0, 'stop_pct': 4.0, 'horizon_days': 3,
        'expected_range_pct': None, 'entry_trigger': None, 'fail_level': None,
        'final_rating': 'buy', 'user_suitability': 'medium',
        'reasoning': 'e2e', 'neutral_type': None,
    }
    p.update(kw)
    return p


def _create(pred, setup=None):
    _sync_category(pred)
    pred['task_blueprint'] = _assemble_task_blueprint(pred, setup or {})
    with patch('core.tracking.load_tasks', return_value=[]), \
         patch('core.tracking.save_tasks'), \
         patch('core.tracking.append_tracking_event'):
        task = tracking.create_task(pred, [])
    task['created_at'] = '2026-06-01T09:30:00'
    task['deadline'] = '2026-06-05'
    return task


class BlueprintEndToEndTest(unittest.TestCase):
    def test_buy_full_loop_target_hit_is_A(self):
        task = _create(_g_prediction())
        self.assertEqual(task['task_class'], 'buy')
        self.assertEqual(task['pred_snapshot']['task_blueprint']['category'], 'buy')
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.3, 'low': 9.9, 'close': 10.2},
            {'date': '2026-06-05', 'open': 10.2, 'high': 10.9, 'low': 10.1, 'close': 10.7},
        ])
        grade, detail = tracking.score_task(task, df, date(2026, 6, 5), 10.7)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['outcome_tier'], 'T_target')
        task.update(status='closed', grade=grade, grade_detail=detail)
        stats = tracking.get_tracking_stats([task])
        self.assertEqual(stats['by_class']['buy']['total'], 1)
        self.assertEqual(stats['grade_dist']['A'], 1)
        prompt = tracking.build_retrospective_prompt(task, [], [])
        self.assertIn('目标涨幅: 8.0%', prompt)
        self.assertIn('止损线: -4.0%', prompt)

    def test_avoid_full_loop_drop_target_is_A(self):
        pred = _g_prediction(direction='bearish', final_action='watch_only',
                             opportunity_grade='D', target_pct=5.0, fail_level=10.8,
                             final_rating='hold')
        task = _create(pred)
        self.assertEqual(task['task_class'], 'avoid')
        self.assertEqual(pred['category'], 'avoid')
        df = _df([
            {'date': '2026-06-05', 'open': 9.6, 'high': 9.7, 'low': 9.4, 'close': 9.5},
        ])
        grade, detail = tracking.score_task(task, df, date(2026, 6, 5), 9.5)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['outcome_tier'], 'drop_target')
        task.update(status='closed', grade=grade, grade_detail=detail)
        stats = tracking.get_tracking_stats([task])
        self.assertEqual(stats['by_class']['avoid']['total'], 1)
        self.assertTrue(tracking.build_retrospective_prompt(task, [], []))

    def test_watch_full_loop_watch_flat_is_A(self):
        pred = _g_prediction(direction='neutral', final_action='hold',
                             opportunity_grade='C', target_pct=None, stop_pct=None,
                             expected_range_pct=[-3.0, 2.0], neutral_type='neutral_range',
                             final_rating='hold')
        task = _create(pred)
        self.assertEqual(task['task_class'], 'watch')
        self.assertEqual(task['expected_range_pct'], [-3.0, 2.0])
        df = _df([
            {'date': '2026-06-05', 'open': 10.0, 'high': 10.2, 'low': 9.9, 'close': 10.1},
        ])
        grade, detail = tracking.score_task(task, df, date(2026, 6, 5), 10.1)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['outcome_tier'], 'watch_flat')
        task.update(status='closed', grade=grade, grade_detail=detail)
        stats = tracking.get_tracking_stats([task])
        self.assertEqual(stats['by_class']['watch']['total'], 1)

    def test_bullish_watch_trigger_conversion_then_buy_scoring(self):
        pred = _g_prediction(final_action='watch_only', opportunity_grade='A',
                             target_pct=8.0, entry_trigger=10.5, fail_level=9.8,
                             final_rating='hold')
        task = _create(pred)
        self.assertEqual(pred['category'], 'bullish_watch')
        self.assertEqual(task['task_class'], 'watch')
        df = _df([
            {'date': '2026-06-02', 'open': 10.4, 'high': 10.6, 'low': 10.2, 'close': 10.5},
            {'date': '2026-06-05', 'open': 10.6, 'high': 11.5, 'low': 10.5, 'close': 11.4},
        ])
        converted = tracking.convert_triggered_watch_tasks(
            [task], {'600000': df}, today=date(2026, 6, 2))
        self.assertEqual(len(converted), 1)
        self.assertEqual(task['task_class'], 'buy')
        self.assertEqual(task['deadline'], '2026-06-05')
        self.assertAlmostEqual(task['entry_price'], 10.5)
        self.assertAlmostEqual(task['target_pct'], 8.0)
        grade, detail = tracking.score_task(task, df, date(2026, 6, 5), 11.4)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['outcome_tier'], 'T_target')
        task.update(status='closed', grade=grade, grade_detail=detail)
        stats = tracking.get_tracking_stats([task])
        self.assertEqual(stats['by_class']['buy']['total'], 1)
        self.assertEqual(stats['conversion']['converted'], 1)
        self.assertEqual(stats['conversion']['rate'], 1.0)

    def test_bullish_watch_untriggered_expire_is_BCD_only(self):
        pred = _g_prediction(final_action='watch_only', opportunity_grade='B',
                             target_pct=8.0, entry_trigger=10.5, fail_level=9.8,
                             final_rating='hold')
        task = _create(pred)
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.3, 'low': 9.9, 'close': 10.2},
            {'date': '2026-06-05', 'open': 10.2, 'high': 10.4, 'low': 10.0, 'close': 10.3},
        ])
        converted = tracking.convert_triggered_watch_tasks(
            [task], {'600000': df}, today=date(2026, 6, 5))
        self.assertEqual(converted, [])
        grade, detail = tracking.score_task(task, df, date(2026, 6, 5), 10.3)
        self.assertEqual(grade, 'B')
        self.assertEqual(detail['outcome_tier'], 'waited_right')
        task.update(status='closed', grade=grade, grade_detail=detail)
        stats = tracking.get_tracking_stats([task])
        self.assertEqual(stats['by_class']['watch']['total'], 1)
        self.assertEqual(stats['conversion']['converted'], 0)
        self.assertEqual(stats['conversion']['rate'], 0.0)

    def test_downgraded_buy_without_trigger_is_rejected_not_mislabeled(self):
        pred = _g_prediction(final_action='watch_only', opportunity_grade='C')
        _sync_category(pred)
        pred['task_blueprint'] = _assemble_task_blueprint(pred, {})
        self.assertEqual(pred['category'], 'watch')
        self.assertIsNone(pred['task_blueprint'])
        with self.assertRaises(ValueError):
            with patch('core.tracking.load_tasks', return_value=[]), \
                 patch('core.tracking.save_tasks'), \
                 patch('core.tracking.append_tracking_event'):
                tracking.create_task(pred, [])


if __name__ == '__main__':
    unittest.main()
