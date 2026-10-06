import unittest

from core.agents import decision


class DecisionPromptEtfTest(unittest.TestCase):
    def test_strategy_family_enum_has_etf(self):
        import inspect
        blob = inspect.getsource(decision)
        self.assertIn('etf_trend_follow', blob)

    def test_prompt_has_etf_rule(self):
        import inspect
        blob = inspect.getsource(decision)
        self.assertIn('ETF', blob)
        self.assertTrue('一篮子' in blob or '指数' in blob)


if __name__ == '__main__':
    unittest.main()
