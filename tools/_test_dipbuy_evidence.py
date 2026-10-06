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


def test_parse_codes_preserves_or_restores_six_digits():
    from tools._backtest_dipbuy import parse_codes

    codes = parse_codes('600519,000001,2594, 1')
    ok(codes == ['600519', '000001', '002594', '000001'], f'代码归一化保留/补齐6位: {codes}')


def test_aggregate_has_value_dimensions():
    from tools._backtest_dipbuy import aggregate

    trades = [
        {'code': '000001', 'entry_date': '2025-01-02', 'year': '2025', 'regime': 'main_up', 'exit_reason': 'target', 'ret_pct': 4.0, 'r_multiple': 2.0},
        {'code': '000001', 'entry_date': '2025-02-02', 'year': '2025', 'regime': 'main_up', 'exit_reason': 'stop', 'ret_pct': -2.0, 'r_multiple': -1.0},
        {'code': '600519', 'entry_date': '2026-01-02', 'year': '2026', 'regime': 'decline', 'exit_reason': 'stop', 'ret_pct': -3.0, 'r_multiple': -1.0},
    ]
    report = aggregate(trades)
    ok(report['by_regime']['main_up']['n'] == 2, '按市场阶段分层')
    ok(report['by_stock']['000001']['n'] == 2, '按个股分层')
    ok(report['by_exit_reason']['stop']['n'] == 2, '按退出原因分层')


def test_market_regime_classifier_and_trade_annotation():
    from tools._backtest_dipbuy import backtest_one, classify_market_regime
    from tools._backtest_dipbuy import _ramp_with_pullback

    up = [{'open': 19.0, 'high': 19.2, 'low': 18.9, 'close': 19.1, 'volume': 800.0},
          {'open': 19.3, 'high': 21.0, 'low': 19.2, 'close': 20.8, 'volume': 1500.0}]
    df = _ramp_with_pullback(up)
    regime_df = df.copy()
    ok(classify_market_regime(regime_df, df.index[69]) == 'main_up', '上升指数识别为 main_up')
    trades = backtest_one(df, code='000001', regime_df=regime_df)
    ok(trades and trades[0]['regime'] == 'main_up', '交易记录写入市场阶段')


def test_index_secid_for_csi300():
    from core.kline_provider import _index_secid

    ok(_index_secid('000300') == '1.000300', '沪深300指数走 1.000300')


def test_evidence_and_markdown_outputs():
    from tools._backtest_dipbuy import aggregate, build_evidence, render_markdown_report

    trades = [
        {'code': '000001', 'entry_date': '2025-01-02', 'year': '2025', 'regime': 'main_up', 'exit_reason': 'target', 'ret_pct': 4.0, 'r_multiple': 2.0},
        {'code': '000002', 'entry_date': '2025-01-03', 'year': '2025', 'regime': 'main_up', 'exit_reason': 'target', 'ret_pct': 3.0, 'r_multiple': 1.5},
        {'code': '000003', 'entry_date': '2025-01-04', 'year': '2025', 'regime': 'decline', 'exit_reason': 'stop', 'ret_pct': -3.0, 'r_multiple': -1.0},
    ]
    report = aggregate(trades)
    evidence = build_evidence(report, min_trades=2)
    markdown = render_markdown_report(['000001', '000002', '000003'], 2, report, evidence)
    ok(evidence['enabled'] is True, '证据文件默认启用')
    ok(evidence['by_regime']['main_up']['expectancy_r'] > 0, '证据保留阶段期望R')
    ok('当前规则是否允许实盘买入' in markdown, 'Markdown 报告解释是否允许实盘')
    ok('main_up' in markdown and 'decline' in markdown, 'Markdown 报告包含阶段分层')


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
    test_parse_codes_preserves_or_restores_six_digits()
    test_aggregate_has_value_dimensions()
    test_market_regime_classifier_and_trade_annotation()
    test_index_secid_for_csi300()
    test_evidence_and_markdown_outputs()
    test_negative_evidence_blocks_triggered_dipbuy()
    test_missing_regime_evidence_blocks_triggered_dipbuy()
    print('\nALL DIPBUY EVIDENCE TESTS PASSED')
