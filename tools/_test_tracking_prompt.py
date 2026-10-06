import unittest

from core.tracking import _plan_brief


class PlanBriefTest(unittest.TestCase):
    def test_buy_with_plan_shows_target(self):
        s = _plan_brief({'task_class': 'buy', 'direction': 'bullish',
                         'target_pct': 22.1, 'stop_pct': -11.0})
        self.assertIn('目标涨幅', s)
        self.assertIn('22.1', s)

    def test_buy_no_plan_no_zero_pct(self):
        s = _plan_brief({'task_class': 'buy', 'direction': 'bullish',
                         'target_pct': 0, 'stop_pct': 0})
        self.assertNotIn('0.0%', s)
        self.assertIn('看多', s)

    def test_avoid_no_misleading_target(self):
        s = _plan_brief({'task_class': 'avoid', 'direction': 'bearish'})
        self.assertIn('回避', s)
        self.assertNotIn('目标跌幅', s)
        self.assertNotIn('0.0%', s)

    def test_watch_no_misleading_target(self):
        s = _plan_brief({'task_class': 'watch', 'direction': 'neutral'})
        self.assertIn('观望', s)
        self.assertNotIn('目标跌幅', s)
        self.assertNotIn('0.0%', s)


if __name__ == '__main__':
    unittest.main()
