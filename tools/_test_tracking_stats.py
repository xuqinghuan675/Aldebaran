import os
import unittest

import pandas as pd

from core.tracking import get_tracking_stats, _grade_buy_expire, _infer_risk_flags


def _index_df(closes: dict) -> pd.DataFrame:
    idx = pd.to_datetime(list(closes.keys()))
    return pd.DataFrame({'close': list(closes.values())}, index=idx)


class MarketBenchmarkTest(unittest.TestCase):
    def _buy_task(self, code, final_pct, created, settle):
        return {
            'status': 'closed', 'kind': 'stock', 'task_class': 'buy',
            'grade': 'A', 'direction': 'bullish', 'source': 'local',
            'code': code, 'created_at': f'{created}T09:00:00',
            'grade_detail': {'final_pct': final_pct, 'settle_date': settle},
        }

    def test_excess_is_final_minus_same_window_market(self):
        index_df = _index_df({
            '2026-01-02': 100.0, '2026-01-03': 102.0,
            '2026-01-06': 105.0, '2026-01-07': 110.0,
        })
        # 大盘同期 +5%（100→105），个股 +10% → alpha +5%
        tasks = [self._buy_task('600000', 10.0, '2026-01-02', '2026-01-06')]
        ss = get_tracking_stats(tasks, index_df=index_df)['skill_stats']
        self.assertEqual(ss['benchmark_kind'], 'market')
        self.assertAlmostEqual(ss['benchmark'], 5.0, places=4)
        self.assertAlmostEqual(ss['buckets']['buy']['mean'], 5.0, places=4)

    def test_falls_back_to_internal_when_no_index(self):
        tasks = [self._buy_task('600000', 10.0, '2026-01-02', '2026-01-06')]
        ss = get_tracking_stats(tasks)['skill_stats']
        self.assertEqual(ss['benchmark_kind'], 'internal')
        self.assertAlmostEqual(ss['buckets']['buy']['mean'], 10.0, places=4)

    def test_benchmark_uses_trigger_date_when_present(self):
        index_df = _index_df({
            '2026-01-02': 100.0, '2026-01-03': 102.0,
            '2026-01-06': 105.0, '2026-01-07': 110.0,
        })
        t = self._buy_task('600000', 10.0, '2026-01-02', '2026-01-07')
        t['trigger_date'] = '2026-01-06'
        # 基准窗口从触发日起算: 105→110
        ss = get_tracking_stats([t], index_df=index_df)['skill_stats']
        self.assertAlmostEqual(ss['benchmark'], (110.0 - 105.0) / 105.0 * 100, places=4)


def _t(tc, grade, direction):
    return {
        'status': 'closed', 'kind': 'stock', 'task_class': tc,
        'grade': grade, 'direction': direction, 'source': 'local',
    }


class StatsByDirectionTest(unittest.TestCase):
    def test_by_class_counts(self):
        tasks = [
            _t('buy', 'A', 'bullish'), _t('buy', 'F', 'bullish'),
            _t('avoid', 'A', 'bearish'),
            _t('watch', 'C', 'neutral'),
        ]
        bc = get_tracking_stats(tasks)['by_class']
        self.assertEqual(bc['buy']['total'], 2)
        self.assertEqual(bc['buy']['win'], 1)
        self.assertEqual(bc['avoid']['total'], 1)
        self.assertEqual(bc['avoid']['win'], 1)
        self.assertEqual(bc['watch']['total'], 1)
        self.assertEqual(bc['watch']['win'], 1)

    def test_missing_task_class_not_counted_no_inference(self):
        tasks = [
            {'status': 'closed', 'kind': 'stock', 'grade': 'A', 'direction': 'bullish', 'source': 'local'},
            {'status': 'closed', 'kind': 'stock', 'grade': 'D', 'direction': 'bearish', 'source': 'local'},
        ]
        bc = get_tracking_stats(tasks)['by_class']
        self.assertEqual(bc['buy']['total'], 0)
        self.assertEqual(bc['avoid']['total'], 0)


def _ct(tc, code, final_pct, grade='C'):
    return {
        'status': 'closed', 'kind': 'stock', 'task_class': tc,
        'code': code, 'grade': grade,
        'grade_detail': {'final_pct': final_pct},
        'source': 'local',
    }


class SkillStatsTest(unittest.TestCase):
    def test_same_code_direction_collapses_to_one_unit(self):
        ss = get_tracking_stats([
            _ct('avoid', 'SH600519', -4.0),
            _ct('avoid', 'SH600519', -6.0),
        ])['skill_stats']
        self.assertEqual(ss['buckets']['avoid']['n'], 1)
        self.assertAlmostEqual(ss['buckets']['avoid']['mean'], -5.0)

    def test_direction_change_creates_two_units(self):
        ss = get_tracking_stats([
            _ct('avoid', '600519', -5.0),
            _ct('buy', '600519', 2.0),
        ])['skill_stats']
        self.assertEqual(ss['buckets']['avoid']['n'], 1)
        self.assertEqual(ss['buckets']['buy']['n'], 1)

    def test_code_prefix_normalized_for_grouping(self):
        ss = get_tracking_stats([
            _ct('watch', '512480', -1.0),
            _ct('watch', 'SH512480', -3.0),
        ])['skill_stats']
        self.assertEqual(ss['buckets']['watch']['n'], 1)
        self.assertAlmostEqual(ss['buckets']['watch']['mean'], -2.0)

    def test_excess_and_spread(self):
        tasks = []
        for i in range(5):
            tasks.append(_ct('buy', f'B{i}', -2.0))
            tasks.append(_ct('avoid', f'A{i}', -6.0))
        ss = get_tracking_stats(tasks)['skill_stats']
        self.assertAlmostEqual(ss['benchmark'], -4.0)
        self.assertAlmostEqual(ss['buckets']['buy']['excess'], 2.0)
        self.assertAlmostEqual(ss['buckets']['avoid']['excess'], -2.0)
        self.assertAlmostEqual(ss['spread'], 4.0)

    def test_below_threshold_returns_none(self):
        ss = get_tracking_stats([_ct('buy', 'B1', -2.0)])['skill_stats']
        self.assertEqual(ss['buckets']['buy']['n'], 1)
        self.assertIsNone(ss['buckets']['buy']['excess'])
        self.assertIsNone(ss['spread'])


_REAL_DATA = os.path.expanduser('~/.aldebaran/tracking_tasks.json')


class SkillStatsRealDataTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(_REAL_DATA), '本机无 tracking_tasks.json，跳过')
    def test_real_data_regression(self):
        import json
        with open(_REAL_DATA, encoding='utf-8') as f:
            data = json.load(f)
        # 活数据由运行中的 App 实时改写、样本数会变，这里只校验结构健壮、不崩
        ss = get_tracking_stats(data)['skill_stats']
        self.assertIn('benchmark_kind', ss)
        for cls in ('buy', 'avoid', 'watch'):
            ex = ss['buckets'][cls]['excess']
            self.assertTrue(ex is None or isinstance(ex, float))


class BuyGradeExpireTest(unittest.TestCase):
    def test_no_plan_small_drop_is_not_realized(self):
        self.assertEqual(_grade_buy_expire(-1.0, False, 0.0), 'D')
        self.assertEqual(_grade_buy_expire(-1.9, False, 0.0), 'D')

    def test_no_plan_tiers(self):
        self.assertEqual(_grade_buy_expire(5.0, False, 0.0), 'A')
        self.assertEqual(_grade_buy_expire(2.0, False, 0.0), 'B')
        self.assertEqual(_grade_buy_expire(1.0, False, 0.0), 'C')
        self.assertEqual(_grade_buy_expire(0.0, False, 0.0), 'C')
        self.assertEqual(_grade_buy_expire(-5.0, False, 0.0), 'D')
        self.assertEqual(_grade_buy_expire(-6.0, False, 0.0), 'F')

    def test_plan_and_no_plan_share_realized_threshold(self):
        self.assertEqual(_grade_buy_expire(-0.5, True, 6.0), 'D')
        self.assertEqual(_grade_buy_expire(-0.5, False, 0.0), 'D')
        self.assertEqual(_grade_buy_expire(0.0, True, 6.0), 'C')
        self.assertEqual(_grade_buy_expire(0.0, False, 0.0), 'C')

    def test_plan_half_target_is_B(self):
        self.assertEqual(_grade_buy_expire(3.0, True, 6.0), 'B')
        self.assertEqual(_grade_buy_expire(2.9, True, 6.0), 'C')


class RiskFlagRRGateTest(unittest.TestCase):
    def _pred(self, target, stop):
        return {
            'direction': 'bullish', 'final_action': 'buy', 'final_rating': 'buy',
            'entry_price': 10.0, 'target_pct': target, 'stop_pct': stop,
            'confidence': 7, 'thesis': '', 'reasoning': '', 'invalidation': '',
        }

    def test_rr_below_2_flagged_insufficient(self):
        # 评级闸门与买入契约对齐到 2.0：盈亏比 1.7 应判"盈亏比不足"（硬拦截为观察）
        self.assertIn('盈亏比不足', _infer_risk_flags(self._pred(3.4, 2.0)))

    def test_rr_at_2_not_flagged(self):
        self.assertNotIn('盈亏比不足', _infer_risk_flags(self._pred(4.0, 2.0)))


if __name__ == '__main__':
    unittest.main()
