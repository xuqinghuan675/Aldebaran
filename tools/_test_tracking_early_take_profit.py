import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from core import tracking


def _df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows], 'low': [r[2] for r in rows], 'close': [r[3] for r in rows]},
        index=idx,
    )


def _open_buy_task(target_pct=5.0):
    today = date.today()
    return {
        'id': 't1', 'code': '600000', 'name': '测试', 'status': 'open',
        'task_class': 'buy', 'direction': 'bullish',
        'entry_price': 100.0, 'target_pct': target_pct, 'stop_pct': -3.0,
        'created_at': (today - timedelta(days=2)).isoformat(),
        'deadline': (today + timedelta(days=3)).isoformat(),  # 未到审判日
        'pred_snapshot': {},
    }


class EarlyTakeProfitTest(unittest.TestCase):
    def _run(self, task, kline):
        with patch('core.tracking.save_tasks_merge'), patch('core.tracking.append_tracking_event'):
            return tracking.try_settle_due_tasks(
                [task], {'600000': {'price': 106.0}},
                only_ids={task['id']}, kline_cache={task['code']: kline})

    def test_open_buy_hits_target_early_settles_A(self):
        # 进行中 buy 单（审判日未到），K线已触及目标价 105 → 提前止盈 A、结算 closed
        today = date.today()
        df = _df([
            (pd.Timestamp(today - timedelta(days=2)), 101, 99, 100),
            (pd.Timestamp(today - timedelta(days=1)), 106, 100, 104),  # high 106 ≥ 105
            (pd.Timestamp(today), 105, 102, 104),
        ])
        settled = self._run(_open_buy_task(), df)
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]['status'], 'closed')
        self.assertEqual(settled[0]['grade'], 'A')
        self.assertEqual(settled[0]['grade_detail']['trigger'], 'target')
        self.assertTrue(settled[0]['tp_pending_recheck'])
        self.assertEqual(settled[0]['tp_marked_date'], today.isoformat())
        self.assertEqual(settled[0]['grade_detail']['final_pct'], 6.0)

    def test_open_buy_below_target_stays_open(self):
        # 进行中 buy 单未触及目标价 → 保持 open、不结算（不误判止损/到期）
        today = date.today()
        task = _open_buy_task()
        df = _df([
            (pd.Timestamp(today - timedelta(days=2)), 101, 99, 100),
            (pd.Timestamp(today - timedelta(days=1)), 103, 98, 99),  # high 103 < 105
            (pd.Timestamp(today), 102, 97, 98),
        ])
        settled = self._run(task, df)
        self.assertEqual(len(settled), 0)
        self.assertEqual(task['status'], 'open')

    def test_open_watch_not_early_settled(self):
        # 进行中中性观望单不走盘中止盈（只有 buy 单提前止盈）
        today = date.today()
        task = _open_buy_task()
        task['task_class'] = 'watch'
        task['direction'] = 'neutral'
        df = _df([
            (pd.Timestamp(today - timedelta(days=1)), 110, 100, 108),
            (pd.Timestamp(today), 110, 105, 108),
        ])
        settled = self._run(task, df)
        self.assertEqual(len(settled), 0)
        self.assertEqual(task['status'], 'open')

    def test_take_profit_reachable_price_gate(self):
        # 现价闸门：现价站上目标价才纳入扫描
        task = _open_buy_task()  # 目标价 105
        self.assertTrue(tracking._take_profit_reachable(task, {'600000': {'price': 106.0}}))
        self.assertFalse(tracking._take_profit_reachable(task, {'600000': {'price': 104.0}}))

    def test_maintenance_price_gate_skips_buy_below_target_without_fetch(self):
        task = _open_buy_task()
        task['created_at'] = date.today().isoformat()
        task['deadline'] = (date.today() + timedelta(days=3)).isoformat()
        calls = []

        def fake_fetch(code, days=120):
            calls.append(code)
            return _df([(pd.Timestamp(date.today()), 106, 100, 104)])

        with patch('core.tracking._fetch_daily_kline_for_tracking', fake_fetch):
            result = tracking.run_tracking_maintenance(
                [task],
                {'600000': {'price': 104.0}},
                today=date.today(),
                batch_code_limit=8,
            )

        self.assertEqual(calls, [])
        self.assertEqual(result['processed_codes'], 0)
        self.assertEqual(task['status'], 'open')

    def test_tp_pending_recheck_updates_to_period_high_on_deadline(self):
        today = date.today()
        task = _open_buy_task()
        task.update({
            'status': 'closed',
            'grade': 'A',
            'deadline': today.isoformat(),
            'created_at': (today - timedelta(days=4)).isoformat(),
            'tp_pending_recheck': True,
            'tp_marked_date': (today - timedelta(days=2)).isoformat(),
            'grade_detail': {
                'grade': 'A',
                'task_class': 'buy',
                'trigger': 'target',
                'final_pct': 5.0,
                'settle_date': (today - timedelta(days=2)).isoformat(),
            },
        })
        df = _df([
            (pd.Timestamp(today - timedelta(days=4)), 101, 99, 100),
            (pd.Timestamp(today - timedelta(days=2)), 106, 100, 104),
            (pd.Timestamp(today), 112, 103, 105),
        ])

        with patch('core.tracking.save_tasks_merge'), patch('core.tracking.append_tracking_event'):
            settled = tracking.try_settle_due_tasks(
                [task],
                {'600000': {'price': 105.0}},
                only_ids={task['id']},
                kline_cache={task['code']: df},
            )

        self.assertEqual(len(settled), 1)
        self.assertEqual(task['status'], 'closed')
        self.assertFalse(task.get('tp_pending_recheck'))
        self.assertNotIn('tp_marked_date', task)
        self.assertEqual(task['grade_detail']['final_pct'], 12.0)
        self.assertEqual(task['grade_detail']['settle_date'], today.isoformat())

    def test_tp_pending_recheck_below_target_clears_flag_keeps_grade(self):
        # 复核未达标：持有期最高没真到目标价 → 清掉待复核标记、保持原 A、不再无限重排
        today = date.today()
        task = _open_buy_task()
        task.update({
            'status': 'closed',
            'grade': 'A',
            'deadline': today.isoformat(),
            'created_at': (today - timedelta(days=4)).isoformat(),
            'tp_pending_recheck': True,
            'tp_marked_date': (today - timedelta(days=2)).isoformat(),
            'grade_detail': {
                'grade': 'A', 'task_class': 'buy', 'trigger': 'target',
                'final_pct': 5.0, 'settle_date': (today - timedelta(days=2)).isoformat(),
            },
        })
        df = _df([
            (pd.Timestamp(today - timedelta(days=4)), 101, 99, 100),
            (pd.Timestamp(today - timedelta(days=2)), 103, 100, 102),  # 最高 103 → 3% < 5%
            (pd.Timestamp(today), 103, 100, 101),
        ])
        with patch('core.tracking.save_tasks_merge') as save_mock, patch('core.tracking.append_tracking_event'):
            settled = tracking.try_settle_due_tasks(
                [task], {'600000': {'price': 101.0}},
                only_ids={task['id']}, kline_cache={task['code']: df},
            )
        self.assertEqual(settled, [])
        self.assertFalse(task.get('tp_pending_recheck'))
        self.assertNotIn('tp_marked_date', task)
        self.assertEqual(task['grade'], 'A')
        save_mock.assert_called_once()
        self.assertFalse(tracking._tp_recheck_due(task, today))


if __name__ == '__main__':
    unittest.main()
