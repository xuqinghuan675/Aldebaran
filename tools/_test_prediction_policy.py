import json
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch


class PredictionPolicyTests(unittest.TestCase):
    def test_normalize_horizon_always_returns_three(self):
        from core.prediction_policy import normalize_horizon_days

        for value in (None, 1, 2, 3, 4, 5, 'bad'):
            with self.subTest(value=value):
                self.assertEqual(normalize_horizon_days(value), 3)

    def test_build_intel_coverage_levels(self):
        from core.prediction_policy import build_intel_coverage

        today = date(2026, 7, 3)
        fresh_news = [{'title': 'fresh', 'date': '2026-07-01 09:30:00'}]
        stale_news = [{'title': 'stale', 'date': '2026-06-20'}]
        fresh_event = [SimpleNamespace(title='fresh event', date=date(2026, 7, 2))]

        fresh = build_intel_coverage(fresh_news, fresh_event, today=today)
        self.assertEqual(fresh['coverage_level'], 'fresh')
        self.assertEqual(fresh['fresh_stock_news_count'], 1)
        self.assertEqual(fresh['fresh_intel_event_count'], 1)

        thin = build_intel_coverage(stale_news, [], today=today)
        self.assertEqual(thin['coverage_level'], 'thin')
        self.assertEqual(thin['fresh_stock_news_count'], 0)

        missing = build_intel_coverage([], [], today=today)
        self.assertEqual(missing['coverage_level'], 'missing')

    def test_agent_calibration_marks_missing_event_as_lowest_weight(self):
        from core.prediction_policy import build_agent_calibration_context

        ctx = build_agent_calibration_context(
            {'flow_profile': {'available': True, 'days': 3}},
            [],
            [],
            {
                'news': {'stance': 'neutral'},
                'technical': {'stance': 'bullish'},
            },
            today=date(2026, 7, 3),
        )

        event = ctx['agent_adjustments']['event']
        self.assertEqual(event['weight'], 'lowest')
        self.assertEqual(event['treatment'], 'missing_not_bearish')
        self.assertIn('情报缺口', ''.join(ctx['directives']))

    def test_agent_calibration_downweights_short_window_fundflow_bearish(self):
        from core.prediction_policy import build_agent_calibration_context

        ctx = build_agent_calibration_context(
            {
                'flow_profile': {'available': True, 'days': 1},
                'mainline_fit': {'fit': True, 'major_mainline': True},
            },
            [{'title': 'fresh', 'date': '2026-07-03'}],
            [],
            {
                'fundflow': {'stance': 'bearish'},
                'technical': {'stance': 'bullish'},
            },
            today=date(2026, 7, 3),
        )

        fundflow = ctx['agent_adjustments']['fundflow']
        self.assertEqual(fundflow['weight'], 'low')
        self.assertEqual(fundflow['treatment'], 'short_window_bearish_low_weight')
        self.assertTrue(fundflow['contrarian_noise'])

    def test_agent_calibration_uses_fundamental_bearish_as_protection(self):
        from core.prediction_policy import build_agent_calibration_context

        ctx = build_agent_calibration_context(
            {
                'flow_profile': {'available': True, 'days': 1},
                'technical_profile': {'risk_flags': ['技术过热']},
            },
            [],
            [],
            {
                'company': {'stance': 'bearish'},
                'technical': {'stance': 'bullish'},
            },
            today=date(2026, 7, 3),
        )

        company = ctx['agent_adjustments']['fundamental']
        self.assertEqual(company['treatment'], 'protective_downgrade')
        self.assertEqual(company['weight'], 'high_in_overheat_missing_intel')

    def test_agent_calibration_keeps_market_as_context_when_intel_missing(self):
        from core.prediction_policy import build_agent_calibration_context

        ctx = build_agent_calibration_context(
            {},
            [],
            [],
            {'macro': {'stance': 'bullish'}},
            today=date(2026, 7, 3),
        )

        market = ctx['agent_adjustments']['market']
        self.assertEqual(market['treatment'], 'context_only')
        self.assertFalse(market['can_trigger_buy'])


class HorizonIntegrationTests(unittest.TestCase):
    def test_decision_json_parser_forces_horizon_to_three(self):
        from core.agents.decision import _parse_prediction_json

        content = json.dumps({
            'direction': 'bullish',
            'confidence': 8,
            'final_action': 'buy',
            'category': 'buy',
            'entry_ref': 10,
            'target_pct': 6,
            'stop_pct': 3,
            'horizon_days': 5,
            'thesis': 'test',
        })

        parsed = _parse_prediction_json(content)
        self.assertEqual(parsed['horizon_days'], 3)

    def test_task_blueprint_forces_horizon_to_three(self):
        from core.predictor import _assemble_task_blueprint

        prediction = {
            'category': 'buy',
            'direction': 'bullish',
            'final_action': 'buy',
            'entry_price': 10,
            'confidence': 8,
            'target_pct': 6,
            'stop_pct': 3,
            'horizon_days': 5,
        }

        bp = _assemble_task_blueprint(prediction, {})
        self.assertEqual(bp['horizon_days'], 3)

    def test_horizon_directive_states_fixed_three_days(self):
        from core.agents.horizon import build_horizon_directive

        text = build_horizon_directive({})
        self.assertIn('固定 3 个交易日', text)
        self.assertNotIn('2 ~ 5', text)
        self.assertNotIn('2~5', text)

    def test_create_task_ignores_stale_prediction_horizon_date(self):
        from core import tracking

        today = date.today().isoformat()
        pred = {
            'kind': 'stock',
            'code': '000001',
            'name': '平安银行',
            'direction': 'bullish',
            'confidence': 0.8,
            'final_action': 'buy',
            'final_rating': 'buy',
            'reasoning': 'test',
            'horizon_date': '2099-01-01',
            '_kline_summary': f'最新交易日 {today}',
            '_flow_profile': {'last': {'date': today}},
            '_stock_news_checked_at': datetime.now().isoformat(timespec='seconds'),
            '_intel_checked_at': datetime.now().isoformat(timespec='seconds'),
            'task_blueprint': {
                'category': 'buy',
                'direction': 'bullish',
                'entry_price': 10,
                'target_pct': 6,
                'stop_pct': 3,
                'horizon_days': 5,
            },
        }

        with patch.object(tracking, 'load_tasks', return_value=[]), \
             patch.object(tracking, 'save_tasks'), \
             patch.object(tracking, 'append_tracking_event'), \
             patch('core.trade_calendar.add_trading_days', return_value='2026-07-08') as add_days:
            task = tracking.create_task(pred, [])

        add_days.assert_called_once_with(3)
        self.assertEqual(task['horizon_days'], 3)
        self.assertEqual(task['deadline'], '2026-07-08')


class PromptCalibrationIntegrationTests(unittest.TestCase):
    def test_synthesis_prompt_includes_calibration_context(self):
        from core.agents.synthesis import _build_variable_data

        text = _build_variable_data(
            '000001',
            '平安银行',
            'stock',
            {'stance': 'bullish'},
            {'stance': 'neutral'},
            {'stance': 'bullish'},
            news_report={'stance': 'neutral'},
            fundflow_report={'stance': 'bearish'},
            calibration_context={'directives': ['事件中性=情报缺口'], 'horizon_days': 3},
        )

        self.assertIn('Agent校准', text)
        self.assertIn('事件中性=情报缺口', text)

    def test_decision_prompt_includes_calibration_context(self):
        from core.agents.decision import _build_variable_data

        text = _build_variable_data(
            '000001',
            '平安银行',
            'stock',
            10.0,
            {'stance': 'bullish'},
            {},
            {'stance': 'neutral'},
            {'stance': 'bullish'},
            'F layer',
            '固定 3 个交易日',
            news_report={'stance': 'neutral'},
            fundflow_report={'stance': 'bearish'},
            calibration_context={'directives': ['资金短窗bearish低权'], 'horizon_days': 3},
        )

        self.assertIn('Agent校准', text)
        self.assertIn('资金短窗bearish低权', text)


if __name__ == '__main__':
    unittest.main()
