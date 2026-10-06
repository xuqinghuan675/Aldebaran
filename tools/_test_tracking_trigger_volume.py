import unittest
from datetime import date

import pandas as pd

from core.tracking import _trigger_rows, _is_convertible_watch

# 前置 5 个交易日：high 均 < 110（不触发），volume 均 1000
_PRE = [
    ('2026-06-01', 105, 100, 103, 1000),
    ('2026-06-02', 106, 101, 104, 1000),
    ('2026-06-03', 106, 102, 105, 1000),
    ('2026-06-04', 107, 103, 106, 1000),
    ('2026-06-05', 108, 104, 107, 1000),
]


def _conv_df(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows], 'low': [r[2] for r in rows],
         'close': [r[3] for r in rows], 'volume': [r[4] for r in rows]},
        index=idx,
    )


def _watch_task():
    return {
        'status': 'open',
        'created_at': '2026-06-01T09:30:00',
        'deadline': '2026-06-30',
        'pred_snapshot': {'task_blueprint': {
            'category': 'bullish_watch', 'entry_trigger': 110.0, 'fail_level': 95.0,
        }},
    }


class TriggerVolumeTest(unittest.TestCase):
    def test_volume_surge_confirms_trigger(self):
        # 06-08 触发(high111)，量2000 vs 前5日均量1000 → 放量确认，放行
        df = _conv_df(_PRE + [('2026-06-08', 111, 106, 110, 2000)])
        hit = _trigger_rows(_watch_task(), df, date(2026, 6, 9))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], date(2026, 6, 8))

    def test_low_volume_breakout_not_converted(self):
        # 06-08 触发但量500 vs 均量1000 → 缩量假突破当天不转换
        df = _conv_df(_PRE + [('2026-06-08', 111, 106, 110, 500)])
        task = _watch_task()
        hit = _trigger_rows(task, df, date(2026, 6, 9))
        self.assertIsNone(hit)

    def test_low_volume_keeps_candidate_convertible(self):
        # 缩量假突破不得永久打死候选：不设 conversion_skip_reason，次日放量仍可转换
        df = _conv_df(_PRE + [('2026-06-08', 111, 106, 110, 500)])
        task = _watch_task()
        _trigger_rows(task, df, date(2026, 6, 9))
        self.assertIsNone(task.get('conversion_skip_reason'))
        self.assertTrue(_is_convertible_watch(task))

    def test_missing_volume_column_fails_open(self):
        # 无 volume 列 → 放行（不因数据缺失卡转换）
        rows = _PRE + [('2026-06-08', 111, 106, 110, 0)]
        idx = pd.DatetimeIndex([r[0] for r in rows])
        df = pd.DataFrame(
            {'high': [r[1] for r in rows], 'low': [r[2] for r in rows], 'close': [r[3] for r in rows]},
            index=idx,
        )
        hit = _trigger_rows(_watch_task(), df, date(2026, 6, 9))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], date(2026, 6, 8))

    def test_low_then_surge_picks_surge_day(self):
        # 06-08 缩量触发被跳过，06-09 放量触发 → 返回 06-09
        df = _conv_df(_PRE + [
            ('2026-06-08', 111, 106, 110, 500),
            ('2026-06-09', 112, 107, 111, 2500),
        ])
        hit = _trigger_rows(_watch_task(), df, date(2026, 6, 10))
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], date(2026, 6, 9))


if __name__ == '__main__':
    unittest.main()
