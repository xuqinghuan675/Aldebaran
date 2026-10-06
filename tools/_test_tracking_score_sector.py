import unittest
from datetime import date

import pandas as pd

from core.tracking import score_sector_task


def _df(last_close):
    closes = [100.0] * 25 + [last_close]
    idx = pd.date_range('2026-05-20', periods=len(closes), freq='D')
    return pd.DataFrame(
        {'open': closes,
         'high': [c * 1.01 for c in closes],
         'low': [c * 0.99 for c in closes],
         'close': closes,
         'volume': [1e6] * len(closes)},
        index=idx,
    )


def _task():
    return {
        'created_at': '2026-06-13',
        'deadline': '2026-06-14',
        'entry_price': 100.0,
        'target_pct': 5.0,
        'stop_pct': -2.5,
        'direction': '多',
        'kind': 'sector',
    }


class ScoreSectorTest(unittest.TestCase):
    def test_small_gain_not_broken_stop_is_gamma(self):
        grade, _ = score_sector_task(_task(), _df(101.0), [])
        self.assertEqual(grade, 'γ')

    def test_flat_is_gamma(self):
        grade, _ = score_sector_task(_task(), _df(100.0), [])
        self.assertEqual(grade, 'γ')

    def test_small_loss_above_stop_is_gamma(self):
        grade, _ = score_sector_task(_task(), _df(99.0), [])
        self.assertEqual(grade, 'γ')

    def test_break_stop_is_delta(self):
        grade, _ = score_sector_task(_task(), _df(97.0), [])
        self.assertEqual(grade, 'δ')

    def test_half_target_is_beta(self):
        grade, _ = score_sector_task(_task(), _df(103.0), [])
        self.assertEqual(grade, 'β')

    def test_reach_target_is_alpha(self):
        grade, _ = score_sector_task(_task(), _df(106.0), [])
        self.assertEqual(grade, 'α')


if __name__ == '__main__':
    unittest.main()
