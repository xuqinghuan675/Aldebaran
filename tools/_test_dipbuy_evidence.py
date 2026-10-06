"""Dip-buy evidence gate and reporting tests."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f'  ok: {msg}')


def test_kline_cache_payload_handles_datetime_index():
    from core.kline_provider import _kline_cache_payload

    df = pd.DataFrame({
        'datetime': pd.to_datetime(['2026-01-01', '2026-01-02']),
        'open': [1.0, 1.1],
        'close': [1.1, 1.2],
    })
    df.index = pd.to_datetime(df['datetime'])
    df.index.name = 'datetime'

    payload = _kline_cache_payload(df)
    json.dumps(payload, ensure_ascii=False, default=str)
    ok(payload['columns'].count('datetime') == 1, '缓存 payload 只保留一个 datetime 列')
    ok(len(payload['rows']) == 2, '缓存 payload 保留全部行')





def test_index_secid_for_csi300():
    from core.kline_provider import _index_secid

    ok(_index_secid('000300') == '1.000300', '沪深300指数走 1.000300')



def test_negative_evidence_blocks_triggered_dipbuy():
    from core.predictor import _apply_trade_setup_constraints

    pred = {
        'direction': 'bullish',
        'final_action': 'buy',
        'final_rating': 'buy',
        'entry_ref': 10.0,
        'target_pct': 6.0,
        'stop_pct': 3.0,
    }
    setup = {'setup_name': 'pullback_buy', 'status': 'triggered', 'position_hint': '半仓'}
    opportunity = {'opportunity_grade': 'S', 'risk_reward': 2.0}
    evidence = {
        'enabled': True,
        'min_trades': 20,
        'regime': 'decline',
        'overall': {'n': 100, 'expectancy_r': 0.1},
        'by_regime': {'decline': {'n': 25, 'expectancy_r': -0.2}},
    }
    _apply_trade_setup_constraints(
        pred, setup, cognition=None, opportunity_profile=opportunity,
        market_phase='decline', dipbuy_evidence=evidence,
    )
    ok(pred['final_action'] == 'watch_only', '负期望阶段把低吸买点降级为观察')
    ok(pred['entry_ref'] is None, '观察状态清空入场价')
    ok('历史证据' in pred.get('_trade_block_reason', ''), '降级原因写入历史证据')


def test_missing_regime_evidence_blocks_triggered_dipbuy():
    from core.predictor import _apply_trade_setup_constraints

    pred = {
        'direction': 'bullish',
        'final_action': 'buy',
        'final_rating': 'buy',
        'entry_ref': 10.0,
        'target_pct': 6.0,
        'stop_pct': 3.0,
    }
    setup = {'setup_name': 'pullback_buy', 'status': 'triggered'}
    opportunity = {'opportunity_grade': 'S', 'risk_reward': 2.0}
    evidence = {
        'enabled': True,
        'min_trades': 20,
        'overall': {'n': 100, 'expectancy_r': 0.4},
        'by_regime': {'main_up': {'n': 30, 'expectancy_r': 0.3}},
    }
    _apply_trade_setup_constraints(
        pred, setup, cognition=None, opportunity_profile=opportunity,
        market_phase='ice', dipbuy_evidence=evidence,
    )
    ok(pred['final_action'] == 'watch_only', '缺少当前阶段证据时不使用总体正期望放行')


if __name__ == '__main__':
    test_kline_cache_payload_handles_datetime_index()
    test_index_secid_for_csi300()
    test_negative_evidence_blocks_triggered_dipbuy()
    test_missing_regime_evidence_blocks_triggered_dipbuy()
    print('\nALL DIPBUY EVIDENCE TESTS PASSED')
