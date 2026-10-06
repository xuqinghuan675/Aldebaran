import unittest
from datetime import date

import pandas as pd

from core.tracking import _score_buy_task, _plan_quality


def _df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows],
         'low': [r[2] for r in rows],
         'close': [r[3] for r in rows]},
        index=idx,
    )


def _task():
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'target_pct': 5.0,
        'stop_pct': -3.0,
        'direction': 'bullish',
        'task_class': 'buy',
        'pred_snapshot': {},
    }


def _bull_no_plan():
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'direction': 'bullish',
        'task_class': 'buy',
        'pred_snapshot': {},
    }


def _task_pos_stop():
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'target_pct': 6.0,
        'stop_pct': 3.0,
        'direction': 'bullish',
        'task_class': 'buy',
        'pred_snapshot': {},
    }


class PositiveStopConventionTest(unittest.TestCase):
    def test_positive_stop_hits_target_is_A(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-02', 107, 100, 102)])
        grade, detail = _score_buy_task(_task_pos_stop(), df, date(2026, 6, 2), 102.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_deadline_close_below_fixed_stop_is_F(self):
        # 审判日收盘跌破统一止损线(个股10%) → F，记实际跌幅
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-02', 95, 88, 89)])
        grade, detail = _score_buy_task(_task_pos_stop(), df, date(2026, 6, 2), 89.0)
        self.assertEqual(grade, 'F')
        self.assertEqual(detail['trigger'], 'stop')
        self.assertEqual(detail['final_pct'], -11.0)

    def test_positive_stop_same_day_double_touch_target_wins_A(self):
        df = _df([('2026-06-01', 107, 96, 100)])
        grade, detail = _score_buy_task(_task_pos_stop(), df, date(2026, 6, 1), 100.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_positive_stop_risk_reward_computed(self):
        pq = _plan_quality(_task_pos_stop(), 100.0, 6.0, 3.0)
        self.assertEqual(pq['risk_reward'], 2.0)
        self.assertTrue(pq['has_stop'])


class ScoreBuyTest(unittest.TestCase):
    def test_hits_target_is_A(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-02', 106, 100, 105)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 2), 105.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_intraday_break_stop_recovers_by_deadline_not_F(self):
        # 过程中收盘跌破止损线，审判日收回线上 → 不算止损，按到期评（救回误杀）
        df = _df([('2026-06-01', 101, 85, 88), ('2026-06-02', 99, 95, 98)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 2), 98.0)
        self.assertNotEqual(grade, 'F')
        self.assertEqual(detail['trigger'], 'expire')

    def test_deadline_loss_within_stop_is_D(self):
        # 审判日跌但未破10%止损线 → 到期亏损 D，不是 F
        df = _df([('2026-06-01', 101, 90, 92), ('2026-06-02', 95, 91, 92)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 2), 92.0)
        self.assertEqual(grade, 'D')
        self.assertEqual(detail['trigger'], 'expire')

    def test_deadline_intraday_wick_below_stop_closes_above_not_F(self):
        # 审判日盘中插针跌破止损线，但收盘拉回线上 → 不算止损(只认收盘)，按到期评 D
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-02', 104, 89, 99)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 2), 99.0)
        self.assertEqual(grade, 'D')
        self.assertEqual(detail['trigger'], 'expire')

    def test_etf_uses_5pct_stop_line(self):
        etf = {'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
               'target_pct': 4.0, 'stop_pct': -2.0, 'direction': 'bullish',
               'task_class': 'buy', 'kind': 'etf', 'code': '159915', 'pred_snapshot': {}}
        # 审判日跌4% 未破5%线 → 不判F
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-02', 99, 95.1, 96)])
        grade, _ = _score_buy_task(dict(etf), df, date(2026, 6, 2), 96.0)
        self.assertNotEqual(grade, 'F')
        # 审判日跌6% 破5%线 → F
        df2 = _df([('2026-06-01', 101, 99, 100), ('2026-06-02', 97, 93, 94)])
        grade2, d2 = _score_buy_task(dict(etf), df2, date(2026, 6, 2), 94.0)
        self.assertEqual(grade2, 'F')
        self.assertEqual(d2['trigger'], 'stop')

    def test_wick_below_stop_closes_above_then_target_is_A(self):
        # 下影线扫到95(<止损97)但收101，次日到目标 → A（收盘确认止损，不被插针扫出）
        df = _df([('2026-06-01', 102, 95, 101), ('2026-06-02', 107, 100, 106)])
        grade, detail = _score_buy_task(_task_pos_stop(), df, date(2026, 6, 5), 106.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_same_day_double_touch_target_wins_A(self):
        df = _df([('2026-06-01', 106, 96, 100)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 1), 100.0)
        self.assertEqual(grade, 'A')
        self.assertEqual(detail['trigger'], 'target')

    def test_expire_half_target_is_B(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-02', 103, 99, 103)])
        grade, _ = _score_buy_task(_task(), df, date(2026, 6, 2), 103.0)
        self.assertEqual(grade, 'B')

    def test_expire_small_gain_is_C(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-02', 102, 99, 101)])
        grade, _ = _score_buy_task(_task(), df, date(2026, 6, 2), 101.0)
        self.assertEqual(grade, 'C')

    def test_expire_small_loss_is_D(self):
        df = _df([('2026-06-01', 101, 98.5, 100), ('2026-06-02', 101, 98, 99)])
        grade, _ = _score_buy_task(_task(), df, date(2026, 6, 2), 99.0)
        self.assertEqual(grade, 'D')

    def test_outcome_tier_on_target(self):
        df = _df([('2026-06-01', 102, 99, 101), ('2026-06-02', 106, 100, 105)])
        _, detail = _score_buy_task(_task(), df, date(2026, 6, 2), 105.0)
        self.assertEqual(detail['outcome_tier'], 'T_target')

    def test_path_stops_at_trigger_day(self):
        df = _df([('2026-06-01', 102, 99, 101),
                  ('2026-06-02', 106, 100, 105),
                  ('2026-06-03', 82, 80, 81)])
        grade, detail = _score_buy_task(_task(), df, date(2026, 6, 5), 81.0)
        self.assertEqual(grade, 'A')
        self.assertGreaterEqual(detail['trough_pct'], -1.5)

    def test_bull_no_plan_small_gain_is_B(self):
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 103, 99, 102)])
        grade, detail = _score_buy_task(_bull_no_plan(), df, date(2026, 6, 5), 102.0)
        self.assertEqual(grade, 'B')
        self.assertEqual(detail['trigger'], 'expire')

    def test_bull_no_plan_loss_is_F(self):
        df = _df([('2026-06-01', 101, 99, 100), ('2026-06-05', 94, 93, 93.5)])
        grade, _ = _score_buy_task(_bull_no_plan(), df, date(2026, 6, 5), 93.5)
        self.assertEqual(grade, 'F')

    def test_bull_no_plan_small_loss_is_D(self):
        df = _df([('2026-06-01', 101, 98.5, 100), ('2026-06-05', 101, 98, 99)])
        grade, detail = _score_buy_task(_bull_no_plan(), df, date(2026, 6, 5), 99.0)
        self.assertEqual(grade, 'D')
        self.assertEqual(detail['outcome_tier'], 'bull_small_loss')


if __name__ == '__main__':
    unittest.main()
