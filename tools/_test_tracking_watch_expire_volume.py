import unittest
from datetime import date

import pandas as pd

from core.tracking import _score_bullish_watch_expire

# 前 5 个交易日：high 均 < 110（不触发），volume 均 1000
_PRE = [
    ('2026-06-01', 105, 100, 103, 1000),
    ('2026-06-02', 106, 101, 104, 1000),
    ('2026-06-03', 106, 102, 105, 1000),
    ('2026-06-04', 107, 103, 106, 1000),
    ('2026-06-05', 108, 104, 107, 1000),
]


def _vdf(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {'high': [r[1] for r in rows], 'low': [r[2] for r in rows],
         'close': [r[3] for r in rows], 'volume': [r[4] for r in rows]},
        index=idx,
    )


def _watch_task():
    return {
        'created_at': '2026-06-01T09:30:00',
        'entry_price': 100.0,
        'pred_snapshot': {'task_blueprint': {
            'category': 'bullish_watch', 'entry_trigger': 110.0, 'fail_level': 95.0,
        }},
    }


class WatchExpireAboveTriggerTest(unittest.TestCase):
    def test_low_volume_break_above_trigger_is_B(self):
        # 未变身看多观察只按最终涨跌分 B/C/D，不再因缩量触价给 A。
        df = _vdf(_PRE + [('2026-06-08', 113, 108, 112, 500)])
        g, d = _score_bullish_watch_expire(_watch_task(), df, date(2026, 6, 8), 112.0)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')

    def test_volume_break_above_trigger_not_converted_is_B(self):
        df = _vdf(_PRE + [('2026-06-08', 113, 108, 112, 2000)])
        g, d = _score_bullish_watch_expire(_watch_task(), df, date(2026, 6, 8), 112.0)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')

    def test_no_error_when_close_above_trigger(self):
        df = _vdf(_PRE + [('2026-06-08', 113, 108, 112, 2000)])
        g, d = _score_bullish_watch_expire(_watch_task(), df, date(2026, 6, 8), 112.0)
        self.assertNotIn('error', d)
        self.assertEqual(g, 'B')

    def test_below_trigger_keeps_waited_right(self):
        # 收盘未过触发价 → 原 waited_right 逻辑不变（回归保护）
        df = _vdf(_PRE + [('2026-06-08', 108, 103, 105, 1000)])
        g, d = _score_bullish_watch_expire(_watch_task(), df, date(2026, 6, 8), 105.0)
        self.assertEqual(g, 'B')
        self.assertEqual(d['outcome_tier'], 'waited_right')

    def test_below_fail_keeps_setup_broken(self):
        # 收盘跌破失效价 → 原 setup_broken 不变
        df = _vdf(_PRE + [('2026-06-08', 100, 92, 94, 1000)])
        g, d = _score_bullish_watch_expire(_watch_task(), df, date(2026, 6, 8), 94.0)
        self.assertEqual(g, 'D')
        self.assertEqual(d['outcome_tier'], 'setup_broken')


if __name__ == '__main__':
    unittest.main()
