import unittest

from core.predictor import derive_opportunity_profile


def _ctx(close, ma5, vr, status='candidate', main5=0.0, small5=0.0,
         rsi=50, kdj=50, turnover=5.0, trend='uptrend'):
    return {
        '_kline_last_close': close,
        'technical_profile': {
            'available': True, 'close': close, 'status': status,
            'setup_name': 'trend_breakout', 'trend_stage': trend,
            'rsi14': rsi, 'kdj_j': kdj, 'turnover': turnover,
            'volume_ratio': vr, 'ma': {'ma5': ma5, 'ma20': ma5},
            'entry_trigger': close * 1.02, 'fail_level': close * 0.95,
            'target_level': close * 1.1,
        },
        'flow_profile': {'available': True, 'days': 5, 'main_5d': main5, 'small_5d': small5},
    }


class OpportunityGradeTest(unittest.TestCase):
    def test_etf_trend_follow_opportunity_strips_leader_text(self):
        ctx = {
            'technical_profile': {'trend_stage': 'uptrend', 'volume_ratio': 1.1, 'ma': {'ma5': 5.0},
                                  'entry_trigger': 5.0, 'fail_level': 4.8, 'target_level': 5.5, 'close': 5.0,
                                  'available': True},
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 100.0, 'small_5d': -50.0},
            'mainline_fit': {'is_etf': True, 'major_mainline': False, 'rs_lead': True,
                             'sector_strong': True, 'intel_hit': False},
            '_kline_last_close': 5.0,
        }
        setup = {'setup_name': 'trend_continuation', 'status': 'triggered',
                 'entry_trigger': 5.0, 'fail_level': 4.8, 'target_level': 5.5}
        opp = derive_opportunity_profile(ctx, setup)
        blob = str(opp.get('not_holding_plan', '')) + str(opp.get('holding_plan', ''))
        for banned in ('主线', '龙头', '主力净流入'):
            self.assertNotIn(banned, blob)

    def test_low_volume_ratio_caps_at_C(self):
        prof = derive_opportunity_profile(_ctx(10, 9.8, vr=0.06), {})
        self.assertIn(prof['opportunity_grade'], ('C', 'D'))
        self.assertNotIn(prof['opportunity_grade'], ('S', 'A', 'B'))

    def test_main_out_retail_in_caps_at_C(self):
        prof = derive_opportunity_profile(_ctx(10, 9.8, vr=1.3, main5=-1.0, small5=1.0), {})
        self.assertIn(prof['opportunity_grade'], ('C', 'D'))

    def test_candidate_below_ma5_not_B(self):
        prof = derive_opportunity_profile(_ctx(10, 10.5, vr=1.3), {})
        self.assertNotEqual(prof['opportunity_grade'], 'B')

    def test_candidate_above_ma5_with_volume_is_B(self):
        prof = derive_opportunity_profile(_ctx(10, 9.8, vr=1.3), {})
        self.assertEqual(prof['opportunity_grade'], 'B')

    def test_triggered_needs_volume_1_5_for_S(self):
        prof = derive_opportunity_profile(_ctx(10, 9.8, vr=1.3, status='triggered'), {})
        self.assertNotEqual(prof['opportunity_grade'], 'S')

    def test_triggered_with_volume_1_5_is_S(self):
        prof = derive_opportunity_profile(_ctx(10, 9.8, vr=1.6, status='triggered'), {})
        self.assertEqual(prof['opportunity_grade'], 'S')

    def test_overheat_blocks_S(self):
        prof = derive_opportunity_profile(
            _ctx(10, 9.8, vr=1.6, status='triggered', turnover=25), {})
        self.assertNotEqual(prof['opportunity_grade'], 'S')

    def test_dipbuy_low_volume_not_killed(self):
        # 低吸缩量回踩是健康信号，不被放量门槛/流动性闸门误杀
        ctx = _ctx(10, 9.8, vr=0.7, trend='uptrend')
        ctx['technical_profile']['setup_name'] = 'pullback_buy'
        prof = derive_opportunity_profile(ctx, {'setup_name': 'pullback_buy'})
        self.assertIn(prof['opportunity_grade'], ('A', 'B'))


if __name__ == '__main__':
    unittest.main()
