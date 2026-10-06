import unittest

from core.tracking_display import reconcile_view_to_category
from core.predictor import _derive_final_category, _derive_decision_view, _assemble_task_blueprint
from core.tracking import _is_convertible_watch


class ReconcileVetoTest(unittest.TestCase):
    def test_veto_skips_category_restore(self):
        out = reconcile_view_to_category(
            'bullish_watch', '看空回避', 'avoid', 'avoid_watch', 'B',
            f_layer_veto=True)
        self.assertEqual(out, ('看空回避', 'avoid', 'avoid_watch'))

    def test_no_veto_still_restores(self):
        out = reconcile_view_to_category(
            'bullish_watch', '看空回避', 'avoid', 'avoid_watch', 'B',
            f_layer_veto=False)
        self.assertEqual(out[0], '看多观察')


class FinalCategoryVetoTest(unittest.TestCase):
    def test_veto_forces_watch_not_bullish_watch(self):
        pred = {'direction': 'bullish', 'final_action': 'watch_only',
                'opportunity_grade': 'S', 'f_layer_veto': True}
        self.assertEqual(_derive_final_category(pred), 'watch')

    def test_no_veto_bullish_is_bullish_watch(self):
        pred = {'direction': 'bullish', 'final_action': 'watch_only',
                'opportunity_grade': 'B', 'f_layer_veto': False}
        self.assertEqual(_derive_final_category(pred), 'bullish_watch')

    def test_veto_does_not_weaken_avoid(self):
        # 否决+方向看空/分层D 是一致看空，应保持 avoid，不被 veto 压成 watch
        pred = {'direction': 'bearish', 'final_action': 'watch_only',
                'opportunity_grade': 'D', 'f_layer_veto': True}
        self.assertEqual(_derive_final_category(pred), 'avoid')


class DecisionViewVetoTest(unittest.TestCase):
    def test_bearish_veto_presses_to_avoid(self):
        pred = {'direction': 'bullish', 'final_action': 'watch_only', 'category': 'watch',
                'opportunity_grade': 'B', 'f_layer_veto': True,
                'f_layer_direction': 'bearish', 'f_layer_confidence': 6}
        dv = _derive_decision_view(pred, {}, {'opportunity_grade': 'B', 'setup_phase': '候选未触发'}, {})
        self.assertEqual(dv['bias_label'], '看空回避')
        self.assertEqual(dv['readiness'], 'avoid')

    def test_s_triggered_veto_is_risk_vetoed(self):
        pred = {'direction': 'bullish', 'final_action': 'watch_only', 'category': 'watch',
                'opportunity_grade': 'S', 'f_layer_veto': True,
                'f_layer_direction': 'bearish', 'f_layer_confidence': 6}
        dv = _derive_decision_view(pred, {}, {'opportunity_grade': 'S', 'setup_phase': '已触发'}, {})
        self.assertEqual(dv['readiness'], 'risk_vetoed')
        self.assertIn('利空否决', dv['fusion_label'])


class BlueprintVetoTest(unittest.TestCase):
    def test_blueprint_carries_f_layer_veto(self):
        pred = {'category': 'watch', 'direction': 'bullish', 'final_action': 'watch_only',
                'entry_price': 10, 'entry_ref': 10, 'confidence': 5,
                'expected_range_pct': [3, 8], 'horizon_days': 3, 'f_layer_veto': True}
        bp = _assemble_task_blueprint(pred, {'setup_name': 'trend_breakout'})
        self.assertIsNotNone(bp)
        self.assertTrue(bp.get('f_layer_veto'))

    def test_veto_sample_not_convertible_even_if_bullish_watch(self):
        # 防御加固：即便 category 误为 bullish_watch，veto 也不可转换
        task = {'status': 'open', 'pred_snapshot': {'task_blueprint': {
            'category': 'bullish_watch', 'f_layer_veto': True}}}
        self.assertFalse(_is_convertible_watch(task))


if __name__ == '__main__':
    unittest.main()
