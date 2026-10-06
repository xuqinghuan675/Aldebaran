import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.utils import mytt


class CleanRoomIndicatorTest(unittest.TestCase):
    def test_source_has_no_external_vendoring_marker(self):
        text = (ROOT / "core" / "utils" / "mytt.py").read_text(encoding="utf-8")
        self.assertNotIn("mpquant", text.lower())
        self.assertNotIn("adapted from", text.lower())

    def test_moving_average(self):
        result = mytt.MA([1.0, 2.0, 3.0, 4.0], 2)
        self.assertTrue(np.isnan(result[0]))
        np.testing.assert_allclose(result[1:], [1.5, 2.5, 3.5])

    def test_weighted_moving_average(self):
        result = mytt.WMA([1.0, 2.0, 3.0, 4.0], 3)
        self.assertTrue(np.isnan(result[0]))
        self.assertTrue(np.isnan(result[1]))
        self.assertAlmostEqual(float(result[2]), 14.0 / 6.0)
        self.assertAlmostEqual(float(result[3]), 20.0 / 6.0)

    def test_cross(self):
        result = mytt.CROSS([1.0, 2.0, 4.0], [2.0, 2.0, 3.0])
        np.testing.assert_array_equal(result, [False, False, True])

    def test_filter_suppresses_following_bars_without_mutating_input(self):
        source = np.array([True, True, False, True, True, False], dtype=bool)
        before = source.copy()
        result = mytt.FILTER(source, 2)
        np.testing.assert_array_equal(source, before)
        np.testing.assert_array_equal(result, [True, False, False, True, False, False])

    def test_indicator_shapes(self):
        close = np.linspace(10.0, 20.0, 80)
        high = close + 0.8
        low = close - 0.7
        volume = np.linspace(1000.0, 2000.0, 80)

        dif, dea, hist = mytt.MACD(close)
        upper, mid, lower = mytt.BOLL(close)
        atr = mytt.ATR(close, high, low, 14)
        mfi = mytt.MFI(close, high, low, volume, 14)

        for series in (dif, dea, hist, upper, mid, lower, atr, mfi):
            self.assertEqual(len(series), len(close))

        self.assertTrue(np.isfinite(mid[-1]))
        self.assertTrue(np.isfinite(atr[-1]))

    def test_valuewhen(self):
        result = mytt.VALUEWHEN(
            [False, True, False, False, True],
            [10.0, 20.0, 30.0, 40.0, 50.0],
        )
        self.assertTrue(np.isnan(result[0]))
        np.testing.assert_allclose(result[1:], [20.0, 20.0, 20.0, 50.0])


if __name__ == "__main__":
    unittest.main()
