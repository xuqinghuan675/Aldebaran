import unittest

from core.strategy_profile import derive_strategy_profile


class StrategyProfileTest(unittest.TestCase):
    def test_etf_trend_follow_strips_leader_semantics_keeps_buy(self):
        ctx = {
            'technical_profile': {'trend_stage': 'uptrend', 'volume_ratio': 1.1,
                                  'entry_trigger': 5.0, 'fail_level': 4.8, 'target_level': 5.4},
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 100.0, 'small_5d': -50.0},
            'mainline_fit': {'is_etf': True, 'major_mainline': False, 'minor_mainline': False,
                             'rs_lead': True, 'sector_strong': True, 'intel_hit': False},
        }
        setup = {'setup_name': 'trend_continuation', 'status': 'triggered',
                 'entry_trigger': 5.0, 'fail_level': 4.8, 'target_level': 5.4}
        profile = derive_strategy_profile(ctx, setup, {'opportunity_grade': 'A'})

        blob = str(profile)
        for banned in ('龙头', '主线契合', '主力', '主升浪', '龙回头'):
            self.assertNotIn(banned, blob)
        self.assertEqual(profile['strategy_family'], 'etf_trend_follow')
        self.assertTrue(profile['buy_ready'])

    def test_non_etf_trend_follow_unchanged(self):
        ctx = {
            'technical_profile': {'trend_stage': 'uptrend', 'volume_ratio': 1.1,
                                  'entry_trigger': 20.0, 'fail_level': 18.5, 'target_level': 23.0},
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 100.0, 'small_5d': -50.0},
            'mainline_fit': {'is_etf': False, 'major_mainline': True, 'rs_lead': True,
                             'sector_strong': False, 'intel_hit': False},
        }
        setup = {'setup_name': 'trend_continuation', 'status': 'triggered',
                 'entry_trigger': 20.0, 'fail_level': 18.5, 'target_level': 23.0}
        profile = derive_strategy_profile(ctx, setup, {'opportunity_grade': 'A'})
        self.assertEqual(profile['strategy_family'], 'trend_follow_momentum')
        self.assertIn('主线契合', str(profile['current_match']))

    def test_pullback_triggered_is_low_absorb_profile(self):
        ctx = {
            'technical_profile': {
                'available': True,
                'setup_name': 'pullback_buy',
                'status': 'triggered',
                'trend_stage': 'uptrend',
                'entry_trigger': 10.2,
                'fail_level': 9.7,
                'target_level': 11.2,
                'volume_ratio': 0.8,
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 1.0, 'small_5d': -0.5},
        }
        setup = {
            'setup_name': 'pullback_buy',
            'status': 'triggered',
            'entry_trigger': 10.2,
            'fail_level': 9.7,
            'target_level': 11.2,
        }

        profile = derive_strategy_profile(ctx, setup)

        self.assertEqual(profile['strategy_family'], 'pullback_low_absorb')
        self.assertEqual(profile['strategy_name'], '强趋势缩量回踩低吸')
        self.assertEqual(profile['stage'], 'triggered')
        self.assertGreaterEqual(len(profile['strategy_candidates']), 2)
        self.assertEqual(profile['strategy_candidates'][0]['strategy_family'], 'pullback_low_absorb')
        self.assertIn('strategy_knowledge', profile)
        self.assertIn('适用前提', profile['strategy_knowledge'][0])
        self.assertIn('回踩不破9.7', profile['trigger_conditions'][0])
        self.assertIn('跌破9.7', profile['invalidation'])
        self.assertNotIn('category', profile)

    def test_breakout_candidate_stays_forming_observation(self):
        ctx = {
            'technical_profile': {
                'available': True,
                'setup_name': 'trend_breakout',
                'status': 'candidate',
                'entry_trigger': 20.0,
                'fail_level': 18.8,
                'target_level': 22.0,
                'volume_ratio': 1.1,
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 0.2, 'small_5d': -0.1},
        }
        setup = {'setup_name': 'trend_breakout', 'status': 'candidate', 'entry_trigger': 20.0}

        profile = derive_strategy_profile(ctx, setup)

        self.assertEqual(profile['strategy_family'], 'breakout_momentum')
        self.assertEqual(profile['stage'], 'forming')
        self.assertIn('观察', profile['action_hint'])
        self.assertFalse(profile['buy_ready'])
        self.assertTrue(any('20.0' in item for item in profile['missing_conditions']))

    def test_avoid_status_is_failed_profile(self):
        ctx = {
            'technical_profile': {
                'available': True,
                'setup_name': 'downtrend_avoid',
                'status': 'avoid',
                'fail_level': 8.5,
                'risk_flags': ['跌破20日平台低点'],
            }
        }

        profile = derive_strategy_profile(ctx, {'setup_name': 'downtrend_avoid', 'status': 'avoid'})

        self.assertEqual(profile['strategy_family'], 'risk_avoid')
        self.assertEqual(profile['stage'], 'failed')
        self.assertIn('回避', profile['action_hint'])

    def test_oversold_setup_with_avoid_status_is_risk_avoid(self):
        ctx = {
            'technical_profile': {
                'available': True,
                'setup_name': 'oversold_rebound',
                'status': 'avoid',
                'fail_level': 8.5,
            }
        }

        profile = derive_strategy_profile(ctx, {'setup_name': 'oversold_rebound', 'status': 'avoid'})

        self.assertEqual(profile['strategy_family'], 'risk_avoid')
        self.assertEqual(profile['stage'], 'failed')
        self.assertIn('回避', profile['action_hint'])

    def test_pullback_forming_records_flow_negative_gap(self):
        ctx = {
            'technical_profile': {
                'available': True,
                'setup_name': 'pullback_buy',
                'status': 'candidate',
                'entry_trigger': 10.2,
                'fail_level': 9.7,
                'volume_ratio': 0.7,
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': -1.0, 'small_5d': 0.5},
        }

        profile = derive_strategy_profile(ctx, {'setup_name': 'pullback_buy', 'status': 'candidate'})

        self.assertEqual(profile['stage'], 'forming')
        self.assertIn('资金流出未解除', profile['missing_conditions'])


    def test_trend_continuation_with_main_inflow_grades_S(self):
        from core.predictor import derive_opportunity_profile

        ctx = {
            'technical_profile': {
                'available': True, 'setup_name': 'trend_continuation', 'status': 'triggered',
                'close': 100.0, 'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0,
                'volume_ratio': 0.8, 'ma': {'ma5': 98.0, 'ma20_slope_pct': 1.0},
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 2.0e9, 'small_5d': -1.0},
        }
        ts = {'setup_name': 'trend_continuation', 'status': 'triggered',
              'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0}

        op = derive_opportunity_profile(ctx, ts)
        self.assertEqual(op['opportunity_grade'], 'S')

        ctx2 = dict(ctx, flow_profile={'available': True, 'days': 5, 'main_5d': -1.0e9, 'small_5d': 1.0})
        op2 = derive_opportunity_profile(ctx2, ts)
        self.assertNotEqual(op2['opportunity_grade'], 'S')

    def test_strong_uptrend_is_trend_continuation_triggered(self):
        import pandas as pd
        from core.kline_provider import build_technical_profile

        rows = []
        for i in range(60):
            close = 10 + 0.2 * i
            rows.append({'open': close - 0.05, 'high': close + 0.1, 'low': close - 0.08,
                         'close': close, 'volume': 1000.0})
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(pd.date_range('2025-01-01', periods=len(df), freq='D'))

        prof = build_technical_profile('000001', df=df)

        self.assertEqual(prof['setup_name'], 'trend_continuation')
        self.assertEqual(prof['status'], 'triggered')

    def test_market_dip_leader_still_trend_continuation(self):
        # 主升浪龙头被大盘性回调短暂打穿MA5，但站上MA60且贴近60日新高 → 仍算趋势延续触发
        import pandas as pd
        from core.kline_provider import build_technical_profile

        rows = []
        for i in range(62):
            close = 8 + 0.3 * i
            rows.append({'open': close - 0.05, 'high': close + 0.1, 'low': close - 0.08,
                         'close': close, 'volume': 1000.0})
        for c in (26.0, 25.6, 25.4):
            rows.append({'open': c + 0.1, 'high': c + 0.2, 'low': c - 0.1, 'close': c, 'volume': 1000.0})
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(pd.date_range('2025-01-01', periods=len(df), freq='D'))

        prof = build_technical_profile('000001', df=df)

        self.assertEqual(prof['setup_name'], 'trend_continuation')
        self.assertEqual(prof['status'], 'triggered')

    def test_major_mainline_rs_lead_grades_S_without_inflow(self):
        from core.predictor import derive_opportunity_profile

        base_tech = {
            'available': True, 'setup_name': 'trend_continuation', 'status': 'triggered',
            'close': 100.0, 'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0,
            'volume_ratio': 0.8, 'ma': {'ma5': 98.0, 'ma20_slope_pct': 1.0},
        }
        ts = {'setup_name': 'trend_continuation', 'status': 'triggered',
              'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0}
        # 无主力净流入(main_5d=0)，但大主线AI + RS领先 → 仍给 S
        ctx = {
            'technical_profile': base_tech,
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 0.0, 'small_5d': 0.0},
            'mainline_fit': {'major_mainline': True, 'rs_lead': True},
        }
        self.assertEqual(derive_opportunity_profile(ctx, ts)['opportunity_grade'], 'S')

        # 大主线但 RS 不领先、且无资金 → 不给 S（仍要一项硬证据）
        ctx2 = dict(ctx, mainline_fit={'major_mainline': True, 'rs_lead': False})
        self.assertNotEqual(derive_opportunity_profile(ctx2, ts)['opportunity_grade'], 'S')

    def test_trend_follow_overbought_leader_still_grades_S(self):
        from core.predictor import derive_opportunity_profile

        ctx = {
            'technical_profile': {
                'available': True, 'setup_name': 'trend_continuation', 'status': 'triggered',
                'close': 100.0, 'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0,
                'volume_ratio': 0.8, 'rsi14': 85.0, 'kdj_j': 92.0, 'turnover': 10.0,
                'ma': {'ma5': 98.0, 'ma20_slope_pct': 1.0},
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 2.0e9, 'small_5d': -1.0},
        }
        ts = {'setup_name': 'trend_continuation', 'status': 'triggered',
              'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0}

        op = derive_opportunity_profile(ctx, ts)
        self.assertEqual(op['opportunity_grade'], 'S')

    def test_trend_follow_topping_signal_not_S(self):
        from core.predictor import derive_opportunity_profile

        ctx = {
            'technical_profile': {
                'available': True, 'setup_name': 'trend_continuation', 'status': 'triggered',
                'close': 100.0, 'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0,
                'volume_ratio': 0.8, 'rsi14': 85.0, 'kdj_j': 92.0, 'turnover': 10.0,
                'ma': {'ma5': 98.0, 'ma20_slope_pct': 1.0},
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 2.0e9, 'small_5d': -1.0},
        }
        ts = {'setup_name': 'trend_continuation', 'status': 'triggered',
              'entry_trigger': 100.0, 'fail_level': 92.0, 'target_level': 120.0,
              'risk_flags': ['长上影冲高回落']}

        op = derive_opportunity_profile(ctx, ts)
        self.assertNotEqual(op['opportunity_grade'], 'S')

    def test_trend_continuation_buy_ready_requires_mainline_fit(self):
        ctx = {
            'technical_profile': {
                'available': True, 'setup_name': 'trend_continuation', 'status': 'triggered',
                'entry_trigger': 20.0, 'fail_level': 18.5, 'target_level': 23.0, 'volume_ratio': 1.1,
            },
            'flow_profile': {'available': True, 'days': 5, 'main_5d': 2.0e9, 'small_5d': -1.0,
                             'divergence': 'main_in_small_out'},
        }
        setup = {'setup_name': 'trend_continuation', 'status': 'triggered',
                 'entry_trigger': 20.0, 'fail_level': 18.5, 'target_level': 23.0}

        profile = derive_strategy_profile(ctx, setup, {'opportunity_grade': 'A'})
        self.assertEqual(profile['strategy_family'], 'trend_follow_momentum')
        self.assertEqual(profile['stage'], 'triggered')
        self.assertTrue(profile['buy_ready'])

        ctx2 = dict(ctx, flow_profile={'available': True, 'days': 5, 'main_5d': 0.0, 'small_5d': 0.0})
        profile2 = derive_strategy_profile(ctx2, setup, {'opportunity_grade': 'A'})
        self.assertEqual(profile2['strategy_family'], 'trend_follow_momentum')
        self.assertFalse(profile2['buy_ready'])
        self.assertTrue(any('主线契合' in m for m in profile2['missing_conditions']))


if __name__ == '__main__':
    unittest.main()
