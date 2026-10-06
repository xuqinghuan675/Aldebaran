import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.predictor import (
    _safe_price, _assemble_task_blueprint, _derive_final_category,
    _sync_category, _validate_prediction_contract,
)
from core import tracking


class SafePriceTest(unittest.TestCase):
    def test_cleans_currency_symbols(self):
        self.assertEqual(_safe_price('¥10.50'), 10.5)
        self.assertEqual(_safe_price('$1,234.5'), 1234.5)
        self.assertEqual(_safe_price('10.8元'), 10.8)
        self.assertEqual(_safe_price(10), 10.0)

    def test_rejects_invalid(self):
        self.assertIsNone(_safe_price(None))
        self.assertIsNone(_safe_price(0))
        self.assertIsNone(_safe_price(-3))
        self.assertIsNone(_safe_price('突破 10.5'))
        self.assertIsNone(_safe_price(''))


def _pred(**kw):
    p = {
        'category': 'buy', 'direction': 'bullish', 'final_action': 'buy',
        'entry_price': 10.5, 'entry_ref': 10.5, 'confidence': 7,
        'target_pct': 8.0, 'stop_pct': 4.0, 'horizon_days': 3,
        'expected_range_pct': None,
    }
    p.update(kw)
    return p


class AssembleBlueprintTest(unittest.TestCase):
    def test_buy_blueprint(self):
        bp = _assemble_task_blueprint(_pred(), {'setup_name': 'pullback_buy'})
        self.assertEqual(bp['category'], 'buy')
        self.assertEqual(bp['direction'], 'bullish')
        self.assertEqual(bp['entry_price'], 10.5)
        self.assertEqual(bp['target_pct'], 8.0)
        self.assertEqual(bp['stop_pct'], 4.0)
        self.assertEqual(bp['horizon_days'], 3)
        self.assertEqual(bp['confidence'], 0.7)
        self.assertEqual(bp['strategy_pattern'], 'pullback_buy')

    def test_horizon_normalized_to_fixed_three(self):
        self.assertEqual(_assemble_task_blueprint(_pred(horizon_days=8), {})['horizon_days'], 3)
        self.assertEqual(_assemble_task_blueprint(_pred(horizon_days=1), {})['horizon_days'], 3)
        self.assertEqual(_assemble_task_blueprint(_pred(horizon_days=None), {})['horizon_days'], 3)

    def test_negative_stop_normalized_positive(self):
        bp = _assemble_task_blueprint(_pred(stop_pct=-4.0), {})
        self.assertEqual(bp['stop_pct'], 4.0)

    def test_buy_missing_target_returns_none_with_error(self):
        pred = _pred(target_pct=None)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('_blueprint_error', pred)

    def test_buy_with_watch_only_action_rejected_as_conflict(self):
        # category=buy 但 final_action=watch_only 是 G 自相矛盾 → 拒单，不静默改写成 bullish_watch
        pred = _pred(final_action='watch_only', entry_trigger=11.0, fail_level=9.8)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('contract_conflict', pred['_blueprint_error'])

    def test_buy_downgraded_without_trigger_returns_none(self):
        pred = _pred(final_action='watch_only')
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('_blueprint_error', pred)

    def test_avoid_blueprint(self):
        pred = _pred(category='avoid', direction='bearish', final_action='watch_only',
                     target_pct=8.0, fail_level=12.0)
        bp = _assemble_task_blueprint(pred, {})
        self.assertEqual(bp['category'], 'avoid')
        self.assertEqual(bp['target_pct'], 8.0)
        self.assertEqual(bp['fail_level'], 12.0)
        self.assertNotIn('stop_pct', bp)

    def test_avoid_fail_level_must_be_above_entry(self):
        pred = _pred(category='avoid', final_action='watch_only', target_pct=8.0, fail_level=9.0)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))

    def test_watch_blueprint(self):
        pred = _pred(category='watch', direction='neutral', final_action='hold',
                     expected_range_pct=[-3, 2])
        bp = _assemble_task_blueprint(pred, {})
        self.assertEqual(bp['category'], 'watch')
        self.assertEqual(bp['direction'], 'neutral')
        self.assertEqual(bp['expected_range'], [-3.0, 2.0])

    def test_watch_missing_or_inverted_range_returns_none(self):
        self.assertIsNone(_assemble_task_blueprint(
            _pred(category='watch', final_action='hold'), {}))
        self.assertIsNone(_assemble_task_blueprint(
            _pred(category='watch', final_action='hold', expected_range_pct=[2, -3]), {}))

    def test_bullish_watch_levels_fall_back_to_trade_setup(self):
        pred = _pred(category='bullish_watch', final_action='watch_only')
        bp = _assemble_task_blueprint(
            pred, {'entry_trigger': '10.80', 'fail_level': '¥9.80', 'setup_name': 's'})
        self.assertEqual(bp['entry_trigger'], 10.8)
        self.assertEqual(bp['fail_level'], 9.8)
        self.assertEqual(bp['strategy_pattern'], 's')

    def test_bullish_watch_prefers_g_layer_levels(self):
        pred = _pred(category='bullish_watch', final_action='watch_only',
                     entry_trigger=11.0, fail_level=10.0)
        bp = _assemble_task_blueprint(pred, {'entry_trigger': 99, 'fail_level': 88})
        self.assertEqual(bp['entry_trigger'], 11.0)
        self.assertEqual(bp['fail_level'], 10.0)

    def test_bullish_watch_fail_must_be_below_trigger(self):
        pred = _pred(category='bullish_watch', final_action='watch_only',
                     entry_trigger=10.0, fail_level=10.5)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))

    def test_invalid_category_returns_none(self):
        pred = _pred(category='hold')
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('_blueprint_error', pred)

    def test_contradictory_bearish_buy_rejected(self):
        # AI 自相矛盾：既看跌又买入 → 拒单，不得洗成看多买入单
        pred = _pred(category='buy', direction='bearish')
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('contract_conflict', pred['_blueprint_error'])

    def test_contradictory_bullish_avoid_rejected(self):
        pred = _pred(category='avoid', direction='bullish', final_action='watch_only',
                     target_pct=8.0, fail_level=12.0)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('contract_conflict', pred['_blueprint_error'])

    def test_contradictory_bearish_bullish_watch_rejected(self):
        pred = _pred(category='bullish_watch', direction='bearish', final_action='watch_only',
                     entry_trigger=11.0, fail_level=9.8)
        self.assertIsNone(_assemble_task_blueprint(pred, {}))
        self.assertIn('contract_conflict', pred['_blueprint_error'])

    def test_confidence_scale_preserved_when_already_normalized(self):
        bp = _assemble_task_blueprint(_pred(confidence=0.6), {})
        self.assertEqual(bp['confidence'], 0.6)

    def test_confidence_integer_scale_always_divided_by_10(self):
        # G 层 confidence 是 1~10 整数：1=极低 → 0.1，不得被当成已归一化的 1.0
        self.assertEqual(_assemble_task_blueprint(_pred(confidence=1), {})['confidence'], 0.1)
        self.assertEqual(_assemble_task_blueprint(_pred(confidence=10), {})['confidence'], 1.0)

    def test_avoid_fail_level_never_falls_back_to_trade_setup(self):
        # trade_setup 的 fail_level 是价格下方支撑，对 avoid（涨破=踏空）语义相反，禁止回退；
        # 失效价缺失时用 stop_pct 兜底出现价上方的失效价，而非用 setup 的 9.0
        pred = _pred(category='avoid', direction='bearish', final_action='watch_only',
                     target_pct=8.0)
        bp = _assemble_task_blueprint(pred, {'fail_level': 9.0})
        self.assertIsNotNone(bp)
        self.assertGreater(bp['fail_level'], bp['entry_price'])
        self.assertNotEqual(bp['fail_level'], 9.0)

    def test_avoid_without_stop_pct_still_errors(self):
        # 失效价与 stop_pct 都缺时无法兜底，仍拒单
        pred = _pred(category='avoid', direction='bearish', final_action='watch_only',
                     target_pct=8.0, stop_pct=0)
        self.assertIsNone(_assemble_task_blueprint(pred, {'fail_level': 9.0}))
        self.assertIn('_blueprint_error', pred)

    def test_bullish_watch_trigger_not_below_current_price(self):
        # 个股已站上日线触发价时，等待价改用现价口径，不再"等站上一个已被站上的陈旧触发价"
        pred = _pred(category='bullish_watch', direction='bullish', final_action='watch_only',
                     entry_price=10.5, entry_ref=None, entry_trigger=9.0,
                     fail_level=8.0, target_pct=8.0)
        bp = _assemble_task_blueprint(pred, {})
        self.assertIsNotNone(bp)
        self.assertEqual(bp['entry_trigger'], 10.5)
        self.assertLess(bp['fail_level'], bp['entry_trigger'])

    def test_bullish_watch_trigger_above_current_kept(self):
        # 真正未触发的突破候选（触发价在现价上方）保持原触发价不动
        pred = _pred(category='bullish_watch', direction='bullish', final_action='watch_only',
                     entry_price=10.5, entry_ref=None, entry_trigger=11.5,
                     fail_level=9.8, target_pct=8.0)
        bp = _assemble_task_blueprint(pred, {})
        self.assertEqual(bp['entry_trigger'], 11.5)

    def test_success_clears_stale_error(self):
        pred = _pred()
        pred['_blueprint_error'] = '旧错误'
        bp = _assemble_task_blueprint(pred, {})
        self.assertIsNotNone(bp)
        self.assertNotIn('_blueprint_error', pred)


class ContractValidationTest(unittest.TestCase):
    def test_grade_d_flipped_bearish_becomes_avoid(self):
        # D/bearish 是安全闸门：强制 avoid（合法 post-G 覆盖），不算契约冲突
        pred = _pred(category='buy', direction='bearish', final_action='watch_only',
                     opportunity_grade='D')
        _sync_category(pred)
        self.assertEqual(pred['category'], 'avoid')
        self.assertEqual(pred['category_ai'], 'buy')
        self.assertNotIn('contract_conflict', pred)

    def test_f_layer_veto_forces_watch_not_conflict(self):
        # F 层否决是合法 post-G 降级：buy→watch，保留否决闸门，不算冲突
        pred = _pred(category='buy', direction='bullish', final_action='buy',
                     opportunity_grade='S', f_layer_veto=True)
        _sync_category(pred)
        self.assertEqual(pred['category'], 'watch')
        self.assertNotIn('contract_conflict', pred)

    def test_grade_c_buy_is_contract_conflict_not_silent_rewrite(self):
        # grade=C + category=buy + final_action=watch_only 自相矛盾 → 保留 G 原值并标记冲突，不静默改 watch
        pred = _pred(category='buy', final_action='watch_only', opportunity_grade='C')
        _sync_category(pred)
        self.assertEqual(pred['category'], 'buy')
        self.assertIn('contract_conflict', pred)

    def test_validate_returns_none_when_g_matches_contract(self):
        self.assertIsNone(_validate_prediction_contract(
            _pred(category='bullish_watch', direction='bullish',
                  final_action='watch_only', opportunity_grade='B')))

    def test_validate_flags_watch_on_bullish_strong_grade(self):
        self.assertIsNotNone(_validate_prediction_contract(
            _pred(category='watch', direction='bullish',
                  final_action='watch_only', opportunity_grade='A')))

    def test_validate_flags_avoid_on_bullish(self):
        self.assertIsNotNone(_validate_prediction_contract(
            _pred(category='avoid', direction='bullish',
                  final_action='watch_only', opportunity_grade='A')))

    def test_validate_skips_safety_gate_avoid(self):
        # D/bearish 由 _sync 强制为 avoid，校验时与契约期望一致，不报冲突
        self.assertIsNone(_validate_prediction_contract(
            _pred(category='avoid', direction='bearish',
                  final_action='watch_only', opportunity_grade='D')))

    def test_bullish_watch_only_becomes_bullish_watch(self):
        self.assertEqual(_derive_final_category(
            _pred(direction='bullish', final_action='watch_only', opportunity_grade='B')),
            'bullish_watch')

    def test_triggered_buy_stays_buy(self):
        self.assertEqual(_derive_final_category(
            _pred(direction='bullish', final_action='buy', opportunity_grade='S')), 'buy')

    def test_neutral_defaults_to_watch(self):
        self.assertEqual(_derive_final_category(
            _pred(direction='neutral', final_action='hold')), 'watch')

    def test_bearish_always_avoid(self):
        self.assertEqual(_derive_final_category(
            _pred(direction='bearish', final_action='reduce')), 'avoid')

    def test_cache_replay_preserves_original_category_ai(self):
        # 缓存命中路径会再次 _sync_category：category_ai 必须保住 AI 首轮原始值
        pred = _pred(category='buy', final_action='watch_only', opportunity_grade='C')
        _sync_category(pred)
        self.assertEqual(pred['category_ai'], 'buy')
        self.assertEqual(pred['category'], 'buy')
        _sync_category(pred)
        self.assertEqual(pred['category_ai'], 'buy')

    def test_synced_category_never_contradicts_direction(self):
        for direction in ('bullish', 'bearish', 'neutral'):
            for action in ('buy', 'watch_only', 'hold', 'sell', 'reduce'):
                for grade in ('S', 'A', 'B', 'C', 'D', ''):
                    cat = _derive_final_category(
                        _pred(direction=direction, final_action=action, opportunity_grade=grade))
                    if direction == 'bearish':
                        self.assertEqual(cat, 'avoid')
                    if cat in ('buy', 'bullish_watch'):
                        self.assertNotEqual(direction, 'bearish')
                    if cat == 'avoid':
                        self.assertTrue(direction == 'bearish' or grade == 'D')


class CreateTaskGuardTest(unittest.TestCase):
    def test_create_task_without_blueprint_raises(self):
        with self.assertRaises(ValueError):
            tracking.create_task({'code': 'x', 'name': 'y'}, [])

    def test_create_task_with_invalid_blueprint_raises(self):
        with self.assertRaises(ValueError):
            tracking.create_task({'task_blueprint': {'no_category': True}}, [])


if __name__ == '__main__':
    unittest.main()
