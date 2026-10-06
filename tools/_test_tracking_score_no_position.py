import unittest
from datetime import date

from core.tracking import _score_no_position


def _task(task_class, expected=None):
    t = {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'task_class': task_class,
        'direction': 'bearish' if task_class == 'avoid' else 'neutral',
        'pred_snapshot': {},
    }
    if expected is not None:
        t['expected_range_pct'] = expected
    return t


class ScoreNoPositionTest(unittest.TestCase):
    def _grade(self, task_class, deadline, expected=None):
        g, _ = _score_no_position(
            _task(task_class, expected), None, date(2026, 6, 5), deadline, task_class)
        return g

    def test_avoid_big_fall_is_A(self):
        self.assertEqual(self._grade('avoid', 94.0), 'A')

    def test_avoid_small_fall_is_B(self):
        self.assertEqual(self._grade('avoid', 97.0), 'B')

    def test_avoid_flat_is_C(self):
        self.assertEqual(self._grade('avoid', 100.0), 'C')

    def test_avoid_small_rise_is_D(self):
        self.assertEqual(self._grade('avoid', 103.0), 'D')

    def test_avoid_big_rise_is_F(self):
        self.assertEqual(self._grade('avoid', 106.0), 'F')

    def test_watch_fall_is_A(self):
        self.assertEqual(self._grade('watch', 94.0), 'A')

    def test_watch_flat_is_A(self):
        self.assertEqual(self._grade('watch', 100.0), 'A')

    def test_watch_small_rise_within_4_is_A(self):
        self.assertEqual(self._grade('watch', 104.0), 'A')

    def test_watch_rise_just_over_4_is_D(self):
        self.assertEqual(self._grade('watch', 104.5), 'D')

    def test_watch_rise_6_is_D(self):
        self.assertEqual(self._grade('watch', 106.0), 'D')

    def test_watch_rise_over_6_is_F(self):
        self.assertEqual(self._grade('watch', 107.0), 'F')

    def test_watch_ignores_expected_range(self):
        # 区间已废弃: ±2 区间不再影响评级, 涨 5% 仍按绝对阈值判 D
        self.assertEqual(self._grade('watch', 105.0, [-2, 2]), 'D')

    def test_watch_tier_dodge_vs_flat(self):
        _, d = _score_no_position(_task('watch'), None, date(2026, 6, 5), 97.0, 'watch')
        self.assertEqual(d['outcome_tier'], 'watch_dodge')
        _, d2 = _score_no_position(_task('watch'), None, date(2026, 6, 5), 100.5, 'watch')
        self.assertEqual(d2['outcome_tier'], 'watch_flat')

    def test_residual_plan_does_not_crash(self):
        task = {
            'created_at': '2026-06-01T09:30:00', 'entry_price': 100.0,
            'task_class': 'watch', 'direction': 'neutral',
            'target_pct': 5.0, 'stop_pct': -3.0, 'pred_snapshot': {},
        }
        grade, _ = _score_no_position(task, None, date(2026, 6, 5), 95.0, 'watch')
        self.assertEqual(grade, 'A')


def _avoid_bp_task(target_pct=8.0, fail_level=112.0):
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'task_class': 'avoid',
        'direction': 'bearish',
        'pred_snapshot': {'task_blueprint': {
            'category': 'avoid', 'target_pct': target_pct, 'fail_level': fail_level,
        }},
    }


class AvoidBlueprintScoreTest(unittest.TestCase):
    def _score(self, deadline, **kw):
        return _score_no_position(_avoid_bp_task(**kw), None, date(2026, 6, 5), deadline, 'avoid')

    def test_drop_target_is_A(self):
        g, d = self._score(92.0)
        self.assertEqual(g, 'A')
        self.assertEqual(d['outcome_tier'], 'drop_target')

    def test_drop_half_is_B(self):
        g, d = self._score(95.5)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'drop_half')

    def test_flat_is_C(self):
        g, _ = self._score(100.0)
        self.assertEqual(g, 'C')
        g, _ = self._score(98.0)
        self.assertEqual(g, 'C')

    def test_small_rise_below_fail_is_D(self):
        g, d = self._score(105.0)
        self.assertEqual(g, 'D')
        self.assertEqual(d['outcome_tier'], 'rise_missed')

    def test_breakout_fail_level_is_F(self):
        g, d = self._score(113.0)
        self.assertEqual(g, 'F')
        self.assertEqual(d['outcome_tier'], 'breakout_missed')

    def test_breakout_check_has_priority(self):
        g, _ = self._score(112.0)
        self.assertEqual(g, 'F')

    def test_missing_bp_fields_falls_back_to_fixed_thresholds(self):
        g, _ = self._score(94.0, fail_level=0)
        self.assertEqual(g, 'A')


def _kdf(rows):
    import pandas as pd
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows], 'low': [r[2] for r in rows], 'close': [r[3] for r in rows]},
        index=idx,
    )


class AvoidIntradayScanTest(unittest.TestCase):
    # entry=100, target=8 → 目标跌幅价=92；fail_level=112
    def _score(self, df, deadline):
        return _score_no_position(_avoid_bp_task(), df, date(2026, 6, 5), deadline, 'avoid')

    def test_intraday_spike_above_fail_closes_back_not_F(self):
        # 盘中冲到 113 涨破失效价 112 但收盘回落 105：收盘确认下不算破位，按收盘兜底判 D
        df = _kdf([('2026-06-01', 101, 99, 100), ('2026-06-03', 113, 104, 105), ('2026-06-05', 106, 103, 105)])
        g, d = self._score(df, 105.0)
        self.assertEqual(g, 'D')
        self.assertEqual(d['outcome_tier'], 'rise_missed')

    def test_close_above_fail_is_F(self):
        # 收盘 113 站上失效价 112 → 破位 F
        df = _kdf([('2026-06-01', 101, 99, 100), ('2026-06-03', 114, 108, 113)])
        g, d = self._score(df, 113.0)
        self.assertEqual(g, 'F')
        self.assertEqual(d['outcome_tier'], 'breakout_intraday')

    def test_intraday_drop_target_then_close_up_is_A(self):
        # 盘中跌到 91 触及目标跌幅价 92，收盘回升 98（目标盘中触及即兑现）
        df = _kdf([('2026-06-01', 101, 99, 100), ('2026-06-03', 99, 91, 98), ('2026-06-05', 100, 97, 98)])
        g, d = self._score(df, 98.0)
        self.assertEqual(g, 'A')
        self.assertEqual(d['outcome_tier'], 'drop_target')

    def test_intraday_spike_then_drop_target_is_A(self):
        # 盘中冲高(收109未站上失效112)后跌达目标：收盘未破位，目标兑现 A
        df = _kdf([('2026-06-03', 113, 108, 109), ('2026-06-05', 95, 91, 92)])
        g, d = self._score(df, 92.0)
        self.assertEqual(g, 'A')
        self.assertEqual(d['outcome_tier'], 'drop_target')

    def test_drop_target_first_then_spike_is_A(self):
        # 先跌达目标(06-03)后盘中冲高(06-05)：目标优先，先到先算 → A
        df = _kdf([('2026-06-01', 101, 99, 100), ('2026-06-03', 99, 91, 95), ('2026-06-05', 113, 104, 105)])
        g, d = self._score(df, 105.0)
        self.assertEqual(g, 'A')
        self.assertEqual(d['outcome_tier'], 'drop_target')
        self.assertEqual(d['final_pct'], -8.0)
        self.assertEqual(d['settle_date'], '2026-06-03')
        self.assertEqual(d['deadline_price'], 92.0)

    def test_no_intraday_touch_uses_close_fallback(self):
        # 盘中既没破 112 也没到 92，收盘 95.5 → 收盘回退 B drop_half
        df = _kdf([('2026-06-01', 101, 99, 100), ('2026-06-05', 100, 95, 95.5)])
        g, d = self._score(df, 95.5)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'drop_half')


if __name__ == '__main__':
    unittest.main()
