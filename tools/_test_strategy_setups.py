r"""Deterministic tests for primary/secondary strategy setup propagation.

Run:
    python tools\_test_strategy_setups.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from core.kline_provider import build_technical_profile
from core.predictor import derive_opportunity_profile, derive_trade_setup
from core.predictor import _apply_trade_setup_constraints


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f'  ok: {msg}')


def _dipbuy_near_breakout_df() -> pd.DataFrame:
    rows = []
    for i in range(50):
        close = 10 + 0.15 * i
        rows.append({
            'open': close - 0.04,
            'high': close + 0.05,
            'low': close - 0.10,
            'close': close,
            'volume': 1000.0,
        })
    for i in range(19):
        close = 18.5 + 0.05 * i
        rows.append({
            'open': close - 0.03,
            'high': close + 0.05,
            'low': close - 0.10,
            'close': close,
            'volume': 1000.0,
        })
    rows.append({
        'open': 19.0,
        'high': 19.2,
        'low': 18.9,
        'close': 19.1,
        'volume': 600.0,
    })
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(pd.date_range('2025-01-01', periods=len(df), freq='D'))
    return df


def test_technical_profile_keeps_secondary_setup():
    profile = build_technical_profile('000001', df=_dipbuy_near_breakout_df())
    _assert(profile['setup_name'] == 'pullback_buy', 'technical profile primary setup is dipbuy')
    _assert(profile['secondary_setups'] == ['trend_breakout'],
            'technical profile keeps near-breakout as secondary setup')
    _assert(
        [x['setup_name'] for x in profile['setup_candidates']] == ['pullback_buy', 'trend_breakout'],
        'technical profile exposes ordered setup candidates',
    )


def test_secondary_setups_flow_into_trade_setup():
    ctx = {
        'market_phase': 'main_up',
        'technical_profile': {
            'available': True,
            'trend_stage': 'uptrend',
            'setup_name': 'pullback_buy',
            'secondary_setups': ['trend_breakout', 'event_momentum'],
            'setup_candidates': [
                {'setup_name': 'pullback_buy', 'status': 'triggered'},
                {'setup_name': 'trend_breakout', 'status': 'candidate'},
                {'setup_name': 'event_momentum', 'status': 'candidate'},
            ],
            'status': 'triggered',
            'entry_trigger': 10.0,
            'fail_level': 9.5,
            'target_level': 11.0,
            'risk_flags': [],
        },
        'flow_profile': {'available': False},
    }
    setup = derive_trade_setup(ctx)
    _assert(setup['setup_name'] == 'pullback_buy', 'primary setup remains pullback_buy')
    _assert(setup['secondary_setups'] == ['trend_breakout', 'event_momentum'],
            'secondary setups preserved in trade setup')
    _assert(len(setup['setup_candidates']) == 3, 'setup candidates preserved')


def test_secondary_setups_flow_into_prediction_snapshot():
    ctx = {
        'market_phase': 'main_up',
        'current_price': 10.0,
        '_kline_last_close': 10.0,
        'technical_profile': {
            'available': True,
            'trend_stage': 'uptrend',
            'setup_name': 'pullback_buy',
            'secondary_setups': ['trend_breakout'],
            'setup_candidates': [
                {'setup_name': 'pullback_buy', 'status': 'triggered'},
                {'setup_name': 'trend_breakout', 'status': 'candidate'},
            ],
            'status': 'triggered',
            'entry_trigger': 10.0,
            'fail_level': 9.5,
            'target_level': 11.0,
            'volume_ratio': 0.8,
            'risk_flags': [],
        },
        'flow_profile': {'available': False},
    }
    setup = derive_trade_setup(ctx)
    profile = derive_opportunity_profile(ctx, setup)
    prediction = {
        'direction': 'bullish',
        'final_action': 'buy',
        'final_rating': 'buy',
        'entry_ref': 10.0,
        'target_pct': 10.0,
        'stop_pct': -5.0,
        'horizon_days': 5,
    }
    _apply_trade_setup_constraints(
        prediction,
        setup,
        cognition=None,
        opportunity_profile=profile,
        market_phase='main_up',
        dipbuy_evidence={'by_regime': {'main_up': {'n': 50, 'expectancy_r': 0.2}}},
    )
    _assert(prediction['setup_type'] == 'pullback_buy', 'prediction keeps primary setup_type')
    _assert(prediction['secondary_setups'] == ['trend_breakout'],
            'prediction keeps secondary setups')
    _assert(prediction['setup_types'] == ['pullback_buy', 'trend_breakout'],
            'prediction exposes full setup_types list')


if __name__ == '__main__':
    for fn in (
        test_technical_profile_keeps_secondary_setup,
        test_secondary_setups_flow_into_trade_setup,
        test_secondary_setups_flow_into_prediction_snapshot,
    ):
        print(f'[{fn.__name__}]')
        fn()
    print('\nALL STRATEGY SETUP TESTS PASSED')
