"""core/dipbuy.py 确定性单测：构造强势缩量回踩样本，验证低吸触发/否决/价位。

跑：python tools\\_test_dipbuy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from core.dipbuy import evaluate_dipbuy


def _df(last_bar: dict, *, n: int = 69, start: float = 10.0, step: float = 0.15,
        up: bool = True, vol: float = 1000.0) -> pd.DataFrame:
    """n 根线性斜坡（up=True 上升 / False 下降）+ 1 根自定义末根（last_bar）。"""
    rows = []
    for i in range(n):
        c = start + step * i if up else start - step * i
        rows.append({'open': c - 0.05, 'high': c + 0.05, 'low': c - 0.10,
                     'close': c, 'volume': vol})
    rows.append(last_bar)
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(pd.date_range('2025-01-01', periods=len(df), freq='D'))
    return df


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f'  ok: {msg}')


def test_triggered():
    # 强势缩量回踩 + 收阳企稳 → triggered
    last = {'open': 18.8, 'high': 19.1, 'low': 18.7, 'close': 19.0, 'volume': 600.0}
    r = evaluate_dipbuy(_df(last))
    _assert(r['status'] == 'triggered', f"强势缩量回踩+收阳→triggered (got {r['status']}, veto={r['veto']})")
    _assert(r['entry'] is not None and abs(r['entry'] - 19.0) < 0.01, 'entry≈close')
    _assert(r['stop'] is not None and r['stop'] < r['entry'], 'stop<entry')
    rr = (r['target'] - r['entry']) / (r['entry'] - r['stop'])
    _assert(abs(rr - 2.0) < 0.01, f'盈亏比≈2 (got {rr:.2f})')
    _assert(all(r['sub_signals'][k] for k in ('强势', '缩量回踩', '企稳', '无否决')), '四子信号齐')


def test_long_upper_veto_candidate():
    # 强势缩量回踩，但长上影 → 否决降为 candidate（非 triggered）
    last = {'open': 18.9, 'high': 19.8, 'low': 18.85, 'close': 19.0, 'volume': 600.0}
    r = evaluate_dipbuy(_df(last))
    _assert(r['status'] == 'candidate', f"长上影→candidate (got {r['status']})")
    _assert('长上影冲高回落' in r['veto'], '长上影计入 veto')


def test_high_volume_not_dip():
    # 放量阴线 → 非缩量 → 不是低吸（status None）
    last = {'open': 19.2, 'high': 19.25, 'low': 18.9, 'close': 19.0, 'volume': 2000.0}
    r = evaluate_dipbuy(_df(last))
    _assert(r['status'] is None, f"放量阴线→None (got {r['status']})")
    _assert(r['sub_signals']['缩量回踩'] is False, '缩量回踩=False')


def test_downtrend_no_knife():
    # 下跌趋势回踩 → 非强势 → None（不接飞刀）
    last = {'open': 6.0, 'high': 6.1, 'low': 5.9, 'close': 6.0, 'volume': 600.0}
    r = evaluate_dipbuy(_df(last, start=20.0, up=False))
    _assert(r['status'] is None, f"下跌回踩→None (got {r['status']})")
    _assert(r['sub_signals']['强势'] is False, '强势=False')


def test_no_ind_no_crash():
    # 不传 ind 也能跑（回测路径），结果与触发一致
    last = {'open': 18.8, 'high': 19.1, 'low': 18.7, 'close': 19.0, 'volume': 600.0}
    r = evaluate_dipbuy(_df(last), ind=None)
    _assert(r['status'] == 'triggered', '缺 ind 仍 triggered，不报错')


def test_short_df():
    last = {'open': 18.8, 'high': 19.1, 'low': 18.7, 'close': 19.0, 'volume': 600.0}
    r = evaluate_dipbuy(_df(last, n=30))
    _assert(r['status'] is None, '不足60根→None')


if __name__ == '__main__':
    for fn in (test_triggered, test_long_upper_veto_candidate, test_high_volume_not_dip,
               test_downtrend_no_knife, test_no_ind_no_crash, test_short_df):
        print(f'[{fn.__name__}]')
        fn()
    print('\nALL DIPBUY TESTS PASSED')
