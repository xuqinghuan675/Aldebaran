import unittest

from core.agents.base import agent_dict_to_text
from core.agents.decision import _parse_prediction_json
from core.predictor import (
    _apply_strategy_contract, _assemble_task_blueprint, _apply_trade_setup_constraints,
    _promote_strong_buy,
)


def _triggered_profile():
    return {
        'strategy_family': 'pullback_low_absorb',
        'strategy_name': '强趋势缩量回踩低吸',
        'stage': 'triggered',
        'buy_ready': True,
        'strategy_candidates': [
            {
                'strategy_family': 'pullback_low_absorb',
                'strategy_name': '强趋势缩量回踩低吸',
                'stage': 'triggered',
                'match_score': 92,
                'buy_ready': True,
                'reason': '强趋势缩量回踩且企稳确认',
            }
        ],
    }


class StrategyContractTest(unittest.TestCase):
    def test_agent_text_includes_strategy_report(self):
        report = {
            'analyst': '技术面',
            'stance': 'bullish',
            'confidence': 6,
            'summary': '形态接近低吸触发',
            'strategy_report': {
                'selected_strategy': '强趋势缩量回踩低吸',
                'stage': 'triggered',
                'buy_ready': True,
                'reason': '回踩不破支撑且量价企稳',
            },
        }

        text = agent_dict_to_text(report)

        self.assertIn('策略报告', text)
        self.assertIn('强趋势缩量回踩低吸', text)
        self.assertIn('triggered', text)

    def test_g_json_keeps_strategy_contract_fields(self):
        content = '''{
          "direction": "bullish",
          "confidence": 7,
          "final_action": "buy",
          "category": "buy",
          "entry_ref": 10.0,
          "target_pct": 8.0,
          "stop_pct": 4.0,
          "horizon_days": 3,
          "final_rating": "buy",
          "strategy_family": "pullback_low_absorb",
          "strategy_name": "强趋势缩量回踩低吸",
          "strategy_stage": "triggered",
          "buy_strategy": "强趋势缩量回踩低吸",
          "strategy_reason": "回踩不破支撑且证据通过",
          "thesis": "按低吸策略轻仓试错"
        }'''

        pred = _parse_prediction_json(content)

        self.assertEqual(pred['strategy_family'], 'pullback_low_absorb')
        self.assertEqual(pred['buy_strategy'], '强趋势缩量回踩低吸')
        self.assertEqual(pred['strategy_stage'], 'triggered')

    def test_buy_without_strategy_is_filled_from_triggered_profile(self):
        pred = {
            'direction': 'bullish',
            'final_action': 'buy',
            'final_rating': 'buy',
            'entry_ref': 10.0,
        }

        _apply_strategy_contract(pred, _triggered_profile())

        self.assertEqual(pred['buy_strategy'], '强趋势缩量回踩低吸')
        self.assertEqual(pred['strategy_family'], 'pullback_low_absorb')
        self.assertEqual(pred['strategy_stage'], 'triggered')

    def test_buy_with_forming_strategy_is_downgraded(self):
        pred = {
            'direction': 'bullish',
            'final_action': 'buy',
            'final_rating': 'buy',
            'entry_ref': 10.0,
        }
        profile = dict(_triggered_profile(), stage='forming', buy_ready=False)

        _apply_strategy_contract(pred, profile)

        self.assertEqual(pred['final_action'], 'watch_only')
        self.assertEqual(pred['final_rating'], 'hold')
        self.assertIsNone(pred['entry_ref'])
        self.assertIn('策略原型未触发', pred['_trade_block_reason'])

    def test_buy_downgrade_resyncs_category_and_preserves_category_ai(self):
        from core.predictor import _sync_category

        pred = {
            'direction': 'bullish',
            'final_action': 'buy',
            'final_rating': 'buy',
            'category': 'buy',
            'opportunity_grade': 'B',
            'entry_ref': 10.0,
            'target_pct': 8.0,
            'stop_pct': 4.0,
            'horizon_days': 3,
        }
        profile = dict(_triggered_profile(), stage='forming', buy_ready=False)
        profile['strategy_candidates'][0]['stage'] = 'forming'
        profile['strategy_candidates'][0]['buy_ready'] = False

        _apply_strategy_contract(pred, profile)
        _sync_category(pred)

        self.assertEqual(pred['final_action'], 'watch_only')
        self.assertNotEqual(pred['category'], 'buy')
        self.assertEqual(pred['category_ai'], 'buy')
        self.assertIsNone(pred.get('contract_conflict'))

    def test_buy_blueprint_carries_strategy_contract(self):
        pred = {
            'category': 'buy',
            'direction': 'bullish',
            'final_action': 'buy',
            'entry_price': 10.0,
            'entry_ref': 10.0,
            'target_pct': 8.0,
            'stop_pct': 4.0,
            'horizon_days': 3,
            'confidence': 7,
            'strategy_family': 'pullback_low_absorb',
            'strategy_name': '强趋势缩量回踩低吸',
            'strategy_stage': 'triggered',
            'buy_strategy': '强趋势缩量回踩低吸',
            'strategy_reason': '回踩不破支撑且证据通过',
        }

        bp = _assemble_task_blueprint(pred, {'setup_name': 'pullback_buy'})

        self.assertEqual(bp['strategy_family'], 'pullback_low_absorb')
        self.assertEqual(bp['strategy_stage'], 'triggered')
        self.assertEqual(bp['buy_strategy'], '强趋势缩量回踩低吸')


class BuyRiskRewardGateTest(unittest.TestCase):
    def _setup(self):
        return {'setup_name': 'trend_continuation', 'status': 'triggered',
                'entry_trigger': 10.0, 'fail_level': 9.0, 'target_level': 12.0}

    def _pred(self):
        return {'direction': 'bullish', 'final_action': 'buy', 'final_rating': 'buy',
                'entry_ref': 10.0, 'target_pct': 8.0, 'stop_pct': 4.0}

    def test_buy_blocked_when_rr_below_2(self):
        pred = self._pred()
        opp = {'opportunity_grade': 'S', 'risk_reward': 1.7, 'setup_phase': '已触发'}
        _apply_trade_setup_constraints(pred, self._setup(), None, opp)
        self.assertEqual(pred['final_action'], 'watch_only')
        self.assertIn('2.0', pred['_trade_block_reason'])

    def test_buy_kept_when_rr_at_least_2(self):
        pred = self._pred()
        opp = {'opportunity_grade': 'S', 'risk_reward': 2.5, 'setup_phase': '已触发'}
        _apply_trade_setup_constraints(pred, self._setup(), None, opp)
        self.assertEqual(pred['final_action'], 'buy')


class PromoteStrongBuyTest(unittest.TestCase):
    def _observe_pred(self):
        return {'direction': 'bullish', 'final_action': 'watch_only', 'final_rating': 'hold',
                'category': 'bullish_watch', 'strategy_stage': 'triggered',
                'f_layer_veto': False, '_trade_block_reason': '主力连续性未确认'}

    def _s_opp(self, rr=2.0):
        return {'opportunity_grade': 'S', 'risk_reward': rr}

    def test_strong_s_promoted_to_buy(self):
        pred = self._observe_pred()
        _promote_strong_buy(pred, self._s_opp(), _triggered_profile())
        self.assertEqual(pred['final_action'], 'buy')
        self.assertEqual(pred['category'], 'buy')
        self.assertEqual(pred['category_ai'], 'bullish_watch')
        self.assertTrue(pred['_strong_buy_promoted'])
        self.assertNotIn('_trade_block_reason', pred)
        _apply_strategy_contract(pred, _triggered_profile())
        self.assertTrue(pred.get('buy_strategy'))

    def test_not_promoted_when_veto(self):
        pred = dict(self._observe_pred(), f_layer_veto=True)
        _promote_strong_buy(pred, self._s_opp(), _triggered_profile())
        self.assertEqual(pred['final_action'], 'watch_only')

    def test_not_promoted_when_grade_not_s(self):
        pred = self._observe_pred()
        _promote_strong_buy(pred, {'opportunity_grade': 'B', 'risk_reward': 2.0}, _triggered_profile())
        self.assertEqual(pred['final_action'], 'watch_only')

    def test_not_promoted_when_rr_below_2(self):
        pred = self._observe_pred()
        _promote_strong_buy(pred, self._s_opp(rr=1.8), _triggered_profile())
        self.assertEqual(pred['final_action'], 'watch_only')

    def test_not_promoted_when_not_buy_ready(self):
        profile = dict(_triggered_profile(), buy_ready=False)
        profile['strategy_candidates'][0]['buy_ready'] = False
        pred = self._observe_pred()
        _promote_strong_buy(pred, self._s_opp(), profile)
        self.assertEqual(pred['final_action'], 'watch_only')

    def test_not_promoted_when_holding_position(self):
        # 持仓管理态(hold)不得被提升成新买入
        pred = dict(self._observe_pred(), final_action='hold')
        _promote_strong_buy(pred, self._s_opp(), _triggered_profile())
        self.assertEqual(pred['final_action'], 'hold')


if __name__ == '__main__':
    unittest.main()
