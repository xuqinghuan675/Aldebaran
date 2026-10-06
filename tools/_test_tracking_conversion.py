import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from core import tracking


def _df(rows):
    return pd.DataFrame(
        rows,
        index=pd.to_datetime([r.pop('date') for r in rows]),
    )


def _bw_blueprint(**kw):
    bp = {
        'category': 'bullish_watch',
        'direction': 'bullish',
        'entry_price': 9.5,
        'confidence': 0.7,
        'entry_trigger': 10.0,
        'fail_level': 9.0,
        'triggered_target_pct': 20.0,
        'horizon_days': 3,
        'strategy_pattern': 'pullback_buy',
    }
    bp.update(kw)
    return bp


class SetupLevelsTest(unittest.TestCase):
    def test_reads_blueprint_levels(self):
        task = {'pred_snapshot': {'task_blueprint': _bw_blueprint(entry_trigger=10.5, fail_level=9.8)}}
        self.assertEqual(tracking._setup_levels(task), (10.5, 9.8, None))
        self.assertNotIn('conversion_skip_reason', task)

    def test_non_bullish_watch_returns_none_without_skip_reason(self):
        task = {'pred_snapshot': {'task_blueprint': {'category': 'watch'}}}
        self.assertIsNone(tracking._setup_levels(task))
        self.assertNotIn('conversion_skip_reason', task)

    def test_missing_levels_writes_skip_reason(self):
        task = {'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}}}
        self.assertIsNone(tracking._setup_levels(task))
        self.assertEqual(task['conversion_skip_reason'], 'missing_blueprint_levels')


class ConversionCheckTest(unittest.TestCase):
    def _watch_task(self):
        return {
            'id': 'watch-1',
            'status': 'open',
            'kind': 'stock',
            'code': '600000',
            'name': 'x',
            'direction': 'bullish',
            'final_action': 'watch_only',
            'task_class': 'watch',
            'final_rating': 'hold',
            'created_at': '2026-06-01T09:30:00',
            'horizon_days': 3,
            'confidence': 0.7,
            'pred_snapshot': {'task_blueprint': _bw_blueprint()},
        }

    def test_untriggered_watch_remains_open(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 9.5, 'high': 9.9, 'low': 9.2, 'close': 9.6},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(converted, [])
        self.assertEqual(tasks[0]['status'], 'open')
        self.assertEqual(tasks[0]['conversion_wait_reason']['type'], 'price_not_triggered')

    def test_creation_day_kline_never_triggers(self):
        # 预测当日的盘中行情发生在预测之前，即使 high 超过触发价也不构成触发
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-01', 'open': 10.5, 'high': 11.0, 'low': 9.8, 'close': 9.9},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 1))
        self.assertEqual(converted, [])
        self.assertEqual(tasks[0]['status'], 'open')
        self.assertNotIn('converted_from_watch', tasks[0])

    def test_converts_by_category_without_opportunity_level(self):
        task = self._watch_task()
        task['conversion_wait_reason'] = {'type': 'price_not_triggered'}
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(len(converted), 1)
        self.assertNotIn('conversion_wait_reason', task)

    def test_low_volume_touch_records_wait_reason_without_conversion(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-01', 'open': 9.5, 'high': 9.7, 'low': 9.3, 'close': 9.6, 'volume': 1000},
            {'date': '2026-06-02', 'open': 9.6, 'high': 9.8, 'low': 9.4, 'close': 9.7, 'volume': 1000},
            {'date': '2026-06-03', 'open': 9.7, 'high': 9.9, 'low': 9.5, 'close': 9.8, 'volume': 1000},
            {'date': '2026-06-04', 'open': 9.8, 'high': 9.9, 'low': 9.6, 'close': 9.8, 'volume': 1000},
            {'date': '2026-06-05', 'open': 9.8, 'high': 9.9, 'low': 9.6, 'close': 9.9, 'volume': 1000},
            {'date': '2026-06-08', 'open': 9.9, 'high': 10.2, 'low': 9.8, 'close': 10.1, 'volume': 500},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 8))

        self.assertEqual(converted, [])
        self.assertEqual(tasks[0]['status'], 'open')
        reason = tasks[0]['conversion_wait_reason']
        self.assertEqual(reason['type'], 'volume_unconfirmed')
        self.assertEqual(reason['date'], '2026-06-08')
        self.assertLess(reason['volume_ratio'], 1.5)

    def test_plain_watch_category_never_converts(self):
        task = self._watch_task()
        task['pred_snapshot']['task_blueprint'] = {'category': 'watch', 'expected_range': [-3, 2]}
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(converted, [])

    def test_gap_trigger_converts_inplace_at_open(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 10.8, 'high': 11.0, 'low': 10.4, 'close': 10.9},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(len(converted), 1)
        buy = converted[0]
        self.assertIs(buy, tasks[0])
        self.assertEqual(buy['id'], 'watch-1')
        self.assertEqual(buy['status'], 'open')
        self.assertTrue(buy['converted_from_watch'])
        self.assertEqual(buy['conversion_status'], 'converted')
        self.assertEqual(buy['conversion_origin'], 'conversion')
        self.assertEqual(buy['task_class'], 'buy')
        self.assertEqual(buy['final_action'], 'buy')
        self.assertEqual(buy['created_at'], '2026-06-01T09:30:00')
        self.assertEqual(buy['trigger_date'], '2026-06-02')
        self.assertEqual(buy['watch_origin']['entry_trigger'], 10.0)
        self.assertAlmostEqual(buy['entry_price'], 10.8)
        # AI 目标基于触发价口径换算到实际成本: 10*1.2=12 → (12-10.8)/10.8
        self.assertAlmostEqual(buy['target_pct'], (12.0 - 10.8) / 10.8 * 100)
        self.assertLess(buy['stop_pct'], 0)

    def test_triggered_target_uses_ai_intent(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 9.8, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        buy = converted[0]
        self.assertAlmostEqual(buy['entry_price'], 10.0)
        self.assertAlmostEqual(buy['target_pct'], 20.0)

    def test_convert_is_idempotent_after_inplace_conversion(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])
        first = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(len(first), 1)
        # 变身后该单 category 仍是 bullish_watch，但不得被再次选中重转
        again = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(again, [])
        self.assertFalse(tracking._is_convertible_watch(tasks[0]))

    def test_conversion_keeps_original_candidate_deadline(self):
        task = self._watch_task()
        task['deadline'] = '2026-06-06'
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 10.1, 'high': 10.3, 'low': 9.6, 'close': 10.2},
        ])
        tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(tasks[0]['deadline'], '2026-06-06')

    def test_trigger_after_original_deadline_never_converts(self):
        # 审判日之后的触发不构成转换，更不得延长周期
        task = self._watch_task()
        task['deadline'] = '2026-06-03'
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 9.5, 'high': 9.8, 'low': 9.4, 'close': 9.6},
            {'date': '2026-06-03', 'open': 9.6, 'high': 9.9, 'low': 9.5, 'close': 9.7},
            {'date': '2026-06-05', 'open': 10.1, 'high': 10.5, 'low': 10.0, 'close': 10.3},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 8))
        self.assertEqual(converted, [])
        self.assertEqual(tasks[0]['deadline'], '2026-06-03')
        self.assertNotIn('converted_from_watch', tasks[0])

    def test_gap_beyond_target_intraday_break_recovers_not_F(self):
        # 触发日跳空越过 AI 目标价 → target_pct 为负；过程击穿止损但审判日收回 → 不判 F（救回）
        task = self._watch_task()
        task['deadline'] = '2026-06-05'
        task['pred_snapshot']['task_blueprint'] = _bw_blueprint(triggered_target_pct=5.0)
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 11.0, 'high': 11.2, 'low': 10.8, 'close': 11.0},
            {'date': '2026-06-03', 'open': 10.9, 'high': 11.0, 'low': 8.5, 'close': 8.6},
            {'date': '2026-06-05', 'open': 10.0, 'high': 11.3, 'low': 10.0, 'close': 11.2},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 2))
        self.assertEqual(len(converted), 1)
        self.assertLess(tasks[0]['target_pct'], 0)
        # 06-03 盘中击穿止损，但 06-05 审判日收 11.2 收回止损线上 → 过程不算止损，按到期评（救回）
        grade, detail = tracking.score_task(tasks[0], df, date(2026, 6, 5), 11.2)
        self.assertEqual(grade, 'C')
        self.assertNotEqual(detail['outcome_tier'], 'T_stop')

    def test_limit_up_like_fill_is_marked_suspect(self):
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.0, 'low': 10.0, 'close': 10.0},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertTrue(converted[0]['fill_suspect'])

    def test_same_day_close_break_converts_and_flags_failed(self):
        # 触发日收盘8.9跌破失效价9.0 → 收盘确认下当日失败，同日保守结算
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 8.7, 'close': 8.9},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(len(converted), 1)
        self.assertTrue(converted[0]['trigger_day_failed'])

    def test_trigger_day_wick_closes_above_fail_not_failed(self):
        # 触发日盘中插针破失效(8.9<9.0)但收盘9.1拉回 → 收盘确认下不算当日失败，不同日截断
        tasks = [self._watch_task()]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 8.9, 'close': 9.1},
        ])
        converted = tracking.convert_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 3))
        self.assertEqual(len(converted), 1)
        self.assertFalse(converted[0]['trigger_day_failed'])


class MaintenanceTest(unittest.TestCase):
    def _task(self, i, code):
        task = ConversionCheckTest()._watch_task()
        task['id'] = f'w{i}'
        task['code'] = code
        return task

    def test_batch_limits_to_eight_codes_and_fetches_each_once(self):
        tasks = [self._task(i, f'60000{i}') for i in range(10)]
        calls = []
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])

        def fake_fetch(code, days=120):
            calls.append(code)
            return df

        with patch('core.tracking.save_tasks'), patch('core.tracking._fetch_daily_kline_for_tracking', fake_fetch):
            result = tracking.run_tracking_maintenance(tasks, {}, today=date(2026, 6, 3), batch_code_limit=8)

        self.assertEqual(len(set(calls)), 8)
        self.assertEqual(len(calls), 8)
        self.assertEqual(result['processed_codes'], 8)
        self.assertEqual(len(result['converted']), 8)

    def test_old_candidate_fetches_enough_days_once_per_code(self):
        tasks = [self._task(1, '600001')]
        tasks[0]['created_at'] = '2025-01-01T09:30:00'
        calls = []
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])

        def fake_fetch(code, days=120):
            calls.append((code, days))
            return df

        with patch('core.tracking.save_tasks'), patch('core.tracking._fetch_daily_kline_for_tracking', fake_fetch):
            tracking.run_tracking_maintenance(tasks, {}, only_ids={'w1'}, today=date(2026, 6, 3), batch_code_limit=8)

        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0][1], 120)

    def test_same_batch_converted_trigger_day_close_break_settles_F(self):
        # 触发日收盘8.9跌破失效价9.0 → 收盘确认下当日失败，同日保守结算判 F
        tasks = [self._task(1, '600001')]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 8.7, 'close': 8.9},
        ])

        with patch('core.tracking.save_tasks'), patch('core.tracking._fetch_daily_kline_for_tracking', return_value=df):
            result = tracking.run_tracking_maintenance(
                tasks, {}, only_ids={'w1'}, today=date(2026, 6, 10), batch_code_limit=8
            )

        self.assertEqual(len(result['converted']), 1)
        buy = result['converted'][0]
        self.assertEqual(buy['status'], 'closed')
        self.assertEqual(buy['grade'], 'F')
        self.assertEqual(buy['grade_detail']['outcome_tier'], 'T_stop')

    def test_same_batch_converted_watch_can_take_profit_A(self):
        tasks = [self._task(1, '600001')]
        tasks[0]['pred_snapshot']['task_blueprint'] = _bw_blueprint(triggered_target_pct=20.0)
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 12.2, 'low': 9.5, 'close': 11.0},
        ])

        with patch('core.tracking.save_tasks'), patch('core.tracking.save_tasks_merge'), patch(
            'core.tracking._fetch_daily_kline_for_tracking', return_value=df
        ):
            result = tracking.run_tracking_maintenance(
                tasks,
                {'600001': {'price': 12.2}},
                only_ids={'w1'},
                today=date(2026, 6, 3),
                batch_code_limit=8,
            )

        self.assertEqual(len(result['converted']), 1)
        self.assertEqual(len(result['settled']), 1)
        buy = tasks[0]
        self.assertEqual(buy['status'], 'closed')
        self.assertEqual(buy['grade'], 'A')
        self.assertEqual(buy['grade_detail']['trigger'], 'target')
        self.assertTrue(buy['tp_pending_recheck'])
        self.assertEqual(buy['grade_detail']['final_pct'], 22.0)

    def test_missing_levels_skip_reason_is_saved_and_not_requeued(self):
        tasks = [self._task(1, '600001')]
        tasks[0]['pred_snapshot'] = {'task_blueprint': {'category': 'bullish_watch'}}
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])

        with patch('core.tracking.save_tasks') as save_mock, patch('core.tracking._fetch_daily_kline_for_tracking', return_value=df):
            tracking.run_tracking_maintenance(tasks, {}, only_ids={'w1'}, today=date(2026, 6, 3), batch_code_limit=8)

        self.assertEqual(tasks[0]['conversion_skip_reason'], 'missing_blueprint_levels')
        save_mock.assert_called_once()
        self.assertEqual(tracking.pending_maintenance_ids(tasks, today=date(2026, 6, 3)), [])

    def test_wait_reason_is_saved_without_conversion_or_settlement(self):
        tasks = [self._task(1, '600001')]
        df = _df([
            {'date': '2026-06-01', 'open': 9.5, 'high': 9.7, 'low': 9.3, 'close': 9.6, 'volume': 1000},
            {'date': '2026-06-02', 'open': 9.6, 'high': 9.8, 'low': 9.4, 'close': 9.7, 'volume': 1000},
            {'date': '2026-06-03', 'open': 9.7, 'high': 9.9, 'low': 9.5, 'close': 9.8, 'volume': 1000},
            {'date': '2026-06-04', 'open': 9.8, 'high': 9.9, 'low': 9.6, 'close': 9.8, 'volume': 1000},
            {'date': '2026-06-05', 'open': 9.8, 'high': 9.9, 'low': 9.6, 'close': 9.9, 'volume': 1000},
            {'date': '2026-06-08', 'open': 9.9, 'high': 10.2, 'low': 9.8, 'close': 10.1, 'volume': 500},
        ])

        with patch('core.tracking.save_tasks_merge') as save_mock, patch(
            'core.tracking._fetch_daily_kline_for_tracking', return_value=df
        ):
            result = tracking.run_tracking_maintenance(
                tasks,
                {'600001': {'price': 10.2}},
                only_ids={'w1'},
                today=date(2026, 6, 8),
                batch_code_limit=8,
            )

        self.assertEqual(result['converted'], [])
        self.assertEqual(result['conversion_waiting'], 1)
        self.assertEqual(tasks[0]['conversion_wait_reason']['type'], 'volume_unconfirmed')
        save_mock.assert_called_once()


class BackfillTest(unittest.TestCase):
    def test_closed_triggered_watch_converts_inplace_and_reopens(self):
        task = ConversionCheckTest()._watch_task()
        task['status'] = 'closed'
        task['grade'] = 'A'
        task['grade_detail'] = {'final_pct': 3.0}
        task['closed_at'] = '2026-06-05T15:00:00'
        tasks = [task]
        df = _df([
            {'date': '2026-06-02', 'open': 10.0, 'high': 10.2, 'low': 9.5, 'close': 10.1},
        ])
        backfilled = tracking.backfill_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 5))
        self.assertEqual(len(backfilled), 1)
        self.assertIs(backfilled[0], tasks[0])
        self.assertEqual(tasks[0]['conversion_origin'], 'backfill')
        self.assertEqual(tasks[0]['task_class'], 'buy')
        self.assertTrue(tasks[0]['converted_from_watch'])
        self.assertEqual(tasks[0]['status'], 'open')
        self.assertIsNone(tasks[0]['grade'])
        again = tracking.backfill_triggered_watch_tasks(tasks, {'600000': df}, today=date(2026, 6, 5))
        self.assertEqual(again, [])


class PriceGateTest(unittest.TestCase):
    def _candidate(self, code='600000', deadline='2099-01-01'):
        return {
            'id': 'w1', 'code': code, 'status': 'open', 'kind': 'stock',
            'deadline': deadline, 'created_at': '2026-06-11',
            'pred_snapshot': {'task_blueprint': _bw_blueprint(entry_trigger=10.0)},
        }

    def test_gate_blocks_candidate_below_trigger(self):
        t = self._candidate()
        ids = tracking.pending_maintenance_ids(
            [t], today=date(2026, 6, 12), current_prices={'600000': {'price': 9.5}})
        self.assertEqual(ids, [])

    def test_gate_passes_candidate_at_or_above_trigger(self):
        t = self._candidate()
        ids = tracking.pending_maintenance_ids(
            [t], today=date(2026, 6, 12), current_prices={'600000': {'price': 10.0}})
        self.assertEqual(ids, ['w1'])

    def test_skip_codes_blocks_even_if_reached(self):
        t = self._candidate()
        ids = tracking.pending_maintenance_ids(
            [t], today=date(2026, 6, 12), current_prices={'600000': {'price': 11.0}},
            skip_codes={'600000'})
        self.assertEqual(ids, [])

    def test_none_prices_means_no_gate(self):
        t = self._candidate()
        ids = tracking.pending_maintenance_ids([t], today=date(2026, 6, 12))
        self.assertEqual(ids, ['w1'])


class StatsConversionTest(unittest.TestCase):
    def test_conversion_rate_counts_pending_candidates_in_denominator(self):
        def bw():
            return {'pred_snapshot': {'task_blueprint': {'category': 'bullish_watch'}}}

        tasks = [
            # 仍在等触发的 open 候选 → 进分母（待触发）
            {'status': 'open', 'task_class': 'watch', **bw()},
            # 已触发原地变身为 buy → 分子 + 分母
            {'status': 'open', 'task_class': 'buy', 'converted_from_watch': True},
            # 到期未触发、已关闭候选 → 进分母
            {'status': 'closed', 'task_class': 'watch', **bw()},
            # 普通 watch（非 bullish_watch）不计
            {'status': 'closed', 'task_class': 'watch',
             'pred_snapshot': {'task_blueprint': {'category': 'watch'}}},
        ]
        stats = tracking.get_tracking_stats(tasks)
        self.assertEqual(stats['conversion']['converted'], 1)
        self.assertEqual(stats['conversion']['total_candidates'], 3)
        self.assertAlmostEqual(stats['conversion']['rate'], 1 / 3)

    def test_fill_suspect_excluded_from_main_buy_rate(self):
        tasks = [
            {'status': 'closed', 'kind': 'stock', 'task_class': 'buy', 'grade': 'A', 'direction': 'bullish'},
            {'status': 'closed', 'kind': 'stock', 'task_class': 'buy', 'grade': 'F', 'direction': 'bullish', 'fill_suspect': True},
        ]
        stats = tracking.get_tracking_stats(tasks)
        self.assertEqual(stats['by_class']['buy']['total'], 1)
        self.assertEqual(stats['fill_suspect']['total'], 1)


if __name__ == '__main__':
    unittest.main()
