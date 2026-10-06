"""个股资金流数据拉取 + 5 日本地历史 + 摘要。

数据源：东财 push2 /api/qt/stock/fflow/kline/get
字段顺序：date, 主力净流入, 小单净流入, 中单净流入, 大单净流入, 超大单净流入

5 日历史：本地累积缓存（每日落盘一次今日收盘数据）
实时快照：每次调用返回当日截至当前的资金流（盘中也可用）

落盘：CACHE_DIR/money_flow/{code}_history.json
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'money_flow'
_NO_DATA_TEXT = '▶ 资金流向：暂无数据'


def _history_path(code: str) -> Path:
    return _CACHE_DIR / f'{code}_history.json'


def _load_history(code: str) -> list[dict]:
    try:
        p = _history_path(code)
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data[-30:]
    except Exception:
        pass
    return []


def _save_history(code: str, history: list[dict]):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _history_path(code).write_text(
        json.dumps(history[-30:], ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _secid(code: str) -> str:
    c = str(code).strip()[-6:]
    if c.startswith('92'):
        return f'0.{c}'
    return f'1.{c}' if c.startswith(('6', '5', '9')) else f'0.{c}'


def fetch_today_flow(code: str) -> dict | None:
    """从 push2 拉取今日个股资金流，返回 dict 或 None。

    字段：date / main / small / medium / large / xlarge （单位：元）
    """
    try:
        from core.net_setup import push2_get
        r = push2_get(
            '/api/qt/stock/fflow/kline/get',
            params={
                'lmt': '1',
                'klt': '101',
                'secid': _secid(code),
                'fields1': 'f1,f2,f3,f7',
                'fields2': 'f51,f52,f53,f54,f55,f56',
            },
            timeout=8,
        )
        klines = r.json().get('data', {}).get('klines', [])
        if not klines:
            return None
        parts = klines[-1].split(',')
        if len(parts) < 6:
            return None
        return {
            'date': parts[0].strip(),
            'main': float(parts[1]) if parts[1] else 0.0,
            'small': float(parts[2]) if parts[2] else 0.0,
            'medium': float(parts[3]) if parts[3] else 0.0,
            'large': float(parts[4]) if parts[4] else 0.0,
            'xlarge': float(parts[5]) if parts[5] else 0.0,
        }
    except Exception:
        return None


def fetch_flow_window(code: str, days: int = 20) -> list[dict]:
    """直接拉取最近N日资金流，失败返回本地历史兜底。"""
    try:
        from core.net_setup import push2_get
        r = push2_get(
            '/api/qt/stock/fflow/kline/get',
            params={
                'lmt': str(max(1, min(days, 60))),
                'klt': '101',
                'secid': _secid(code),
                'fields1': 'f1,f2,f3,f7',
                'fields2': 'f51,f52,f53,f54,f55,f56',
            },
            timeout=8,
        )
        klines = r.json().get('data', {}).get('klines', [])
        rows = []
        for line in klines:
            parts = line.split(',')
            if len(parts) < 6:
                continue
            rows.append({
                'date': parts[0].strip(),
                'main': float(parts[1]) if parts[1] else 0.0,
                'small': float(parts[2]) if parts[2] else 0.0,
                'medium': float(parts[3]) if parts[3] else 0.0,
                'large': float(parts[4]) if parts[4] else 0.0,
                'xlarge': float(parts[5]) if parts[5] else 0.0,
            })
        return rows[-days:]
    except Exception:
        return _load_history(code)[-days:]


def _sum_recent(rows: list[dict], key: str, days: int) -> float:
    return float(sum((r.get(key, 0.0) or 0.0) for r in rows[-days:]))


def _same_sign_streak(rows: list[dict], key: str = 'main') -> int:
    vals = [r.get(key, 0.0) or 0.0 for r in rows]
    if not vals or vals[-1] == 0:
        return 0
    sign = vals[-1] > 0
    streak = 0
    for v in reversed(vals):
        if v == 0 or (v > 0) != sign:
            break
        streak += 1
    return streak if sign else -streak


def build_flow_profile(code: str, rows: list[dict] | None = None) -> dict:
    rows = list(rows or fetch_flow_window(code, 20))
    if not rows:
        return {'available': False, 'reason': '资金流数据缺失'}
    out = {
        'available': True,
        'days': len(rows),
        'main_3d': _sum_recent(rows, 'main', min(3, len(rows))),
        'main_5d': _sum_recent(rows, 'main', min(5, len(rows))),
        'main_10d': _sum_recent(rows, 'main', min(10, len(rows))),
        'xlarge_5d': _sum_recent(rows, 'xlarge', min(5, len(rows))),
        'small_5d': _sum_recent(rows, 'small', min(5, len(rows))),
        'main_streak': _same_sign_streak(rows, 'main'),
        'last': rows[-1],
    }
    out['divergence'] = (
        'main_out_small_in' if out['main_5d'] < 0 and out['small_5d'] > 0
        else 'main_in_small_out' if out['main_5d'] > 0 and out['small_5d'] < 0
        else 'none'
    )
    return out


def update_history(code: str) -> dict | None:
    """获取今日资金流并更新本地 5 日历史，返回当日 dict。"""
    flow = fetch_today_flow(code)
    if flow is None:
        return None
    today = date.today().isoformat()
    if flow.get('date') != today:
        return flow

    history = _load_history(code)
    if not history or history[-1].get('date') != today:
        history.append(flow)
        _save_history(code, history)
    else:
        history[-1] = flow
        _save_history(code, history)
    return flow


def load_flow_history(code: str, days: int = 5) -> list[dict]:
    """返回最近 N 日资金流历史（从本地缓存）。"""
    return _load_history(code)[-days:]


def _fmt_yi(val: float) -> str:
    """元 → 亿元，带符号。"""
    yi = val / 1e8
    sign = '+' if yi >= 0 else ''
    return f'{sign}{yi:.2f} 亿'


def summarize(code: str, name: str | None = None) -> tuple[str, dict]:
    """返回 (prompt 段文本, 实时快照 dict)。

    快照 dict 字段：main_flow / small_flow / xlarge_flow / today_pct（与正负号的文字）
    """
    today_flow = update_history(code)
    window = fetch_flow_window(code, 20)
    flow_profile = build_flow_profile(code, window)

    # 实时快照（供数据层刷新，无需 AI）
    realtime: dict = {
        'main_flow': None,
        'small_flow': None,
        'xlarge_flow': None,
        'today_pct_main': None,
        'flow_profile': flow_profile,
    }

    if today_flow is None:
        return _NO_DATA_TEXT, realtime

    main = today_flow.get('main', 0.0) or 0.0
    small = today_flow.get('small', 0.0) or 0.0
    xlarge = today_flow.get('xlarge', 0.0) or 0.0

    realtime = {
        'main_flow': main,
        'small_flow': small,
        'xlarge_flow': xlarge,
        'flow_profile': flow_profile,
    }

    # 历史趋势（最近 5 日）
    history = load_flow_history(code, 5)
    if len(history) >= 3:
        # 本地历史按时间升序保存，连续性必须从最新一天向前数。
        main_vals = [h.get('main', 0) or 0 for h in reversed(history)]
        # 从最近一天向前数真正连续的天数
        consec = 1
        for i in range(1, len(main_vals)):
            if (main_vals[i] > 0) == (main_vals[0] > 0) and main_vals[0] != 0:
                consec += 1
            else:
                break
        if consec >= 3 and main_vals[0] > 0:
            trend = f'连续{consec}日主力净流入'
        elif consec >= 3 and main_vals[0] < 0:
            trend = f'连续{consec}日主力净流出'
        else:
            avg = sum(main_vals) / len(main_vals)
            trend = f'近{len(main_vals)}日主力平均净流入 {_fmt_yi(avg)}'
    else:
        trend = f'历史数据不足（仅{len(history)}日）'

    today_str = today_flow.get('date', date.today().isoformat())
    title = f'{name} {code}' if name else code
    lines = [
        f'▶ 资金流向（{title}，{today_str}）：',
        f'  主力净流入: {_fmt_yi(main)}（大单+超大单）',
        f'  超大单: {_fmt_yi(xlarge)}',
        f'  散户净流入: {_fmt_yi(small)}',
        f'  趋势: {trend}',
    ]
    if flow_profile.get('available'):
        days = int(flow_profile.get('days') or 0)
        if days < 3:
            lines.append(f'  样本: 仅{days}日资金数据，趋势待确认')
        else:
            lines.append(f'  近{min(3, days)}日主力: {_fmt_yi(flow_profile["main_3d"])}')
            if days >= 5:
                lines.append(
                    f'  近5日主力: {_fmt_yi(flow_profile["main_5d"])} / '
                    f'超大单: {_fmt_yi(flow_profile["xlarge_5d"])} / '
                    f'散户: {_fmt_yi(flow_profile["small_5d"])}'
                )
            if days >= 10:
                lines.append(f'  近10日主力: {_fmt_yi(flow_profile["main_10d"])}')
        streak = flow_profile.get('main_streak', 0)
        if streak >= 3:
            lines.append(f'  连续性: 主力连续{streak}日净流入')
        elif streak <= -3:
            lines.append(f'  连续性: 主力连续{abs(streak)}日净流出')
        if days >= 3 and flow_profile.get('divergence') == 'main_out_small_in':
            lines.append(f'  背离: 近{min(5, days)}日主力流出 / 散户接盘')
        elif days >= 3 and flow_profile.get('divergence') == 'main_in_small_out':
            lines.append(f'  背离: 近{min(5, days)}日主力买入 / 散户卖出')

    # 主力 vs 散户对比
    if abs(main) > 1e6 and abs(small) > 1e6:
        if main > 0 and small < 0:
            lines.append('  信号: 主力买入 / 散户卖出（可能分歧买入）')
        elif main < 0 and small > 0:
            lines.append('  信号: 主力卖出 / 散户接盘（注意风险）')

    return '\n'.join(lines), realtime
