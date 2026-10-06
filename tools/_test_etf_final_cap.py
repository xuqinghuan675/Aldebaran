import unittest

from core.predictor import _apply_etf_final_cap


class EtfFinalCapTest(unittest.TestCase):
    def test_caps_target_and_stop(self):
        p = {'direction': 'bullish', 'final_action': 'buy',
             'target_pct': 16.0, 'stop_pct': 7.0, 'opportunity_grade': 'S', 'category': 'buy'}
        _apply_etf_final_cap(p, True, 5.0)
        self.assertLessEqual(p['target_pct'], 8.0)
        self.assertLessEqual(p['stop_pct'], 5.0)

    def test_breaks_2to1_downgrades_buy_not_raise_target(self):
        p = {'direction': 'bullish', 'final_action': 'buy',
             'target_pct': 9.0, 'stop_pct': 5.0, 'opportunity_grade': 'S', 'category': 'buy'}
        _apply_etf_final_cap(p, True, 5.0)
        self.assertLessEqual(p['target_pct'], 8.0)
        self.assertNotEqual(p['final_action'], 'buy')

    def test_non_etf_untouched(self):
        p = {'direction': 'bullish', 'final_action': 'buy',
             'target_pct': 16.0, 'stop_pct': 7.0, 'category': 'buy'}
        _apply_etf_final_cap(p, False, 5.0)
        self.assertEqual(p['target_pct'], 16.0)
        self.assertEqual(p['stop_pct'], 7.0)

    def test_bearish_untouched(self):
        p = {'direction': 'bearish', 'final_action': 'watch_only',
             'target_pct': 16.0, 'stop_pct': 7.0}
        _apply_etf_final_cap(p, True, 5.0)
        self.assertEqual(p['target_pct'], 16.0)


if __name__ == '__main__':
    unittest.main()
