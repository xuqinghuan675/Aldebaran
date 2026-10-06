from __future__ import annotations

from datetime import date, datetime
from typing import Iterable


def pnl_calendar_update_phase(
    now: datetime,
    *,
    is_trade_day: bool,
    intraday_enabled: bool,
) -> str | None:
    if not is_trade_day:
        return None
    hm = now.hour * 100 + now.minute
    if (930 <= hm <= 1130) or (1300 <= hm < 1500):
        return 'live' if intraday_enabled else None
    if hm >= 1500:
        return 'freeze'
    return None


def _option_expired(expiry_date: str) -> bool:
    if not expiry_date:
        return False
    try:
        d = expiry_date.split(' ')[0]
        return (datetime.strptime(d, '%Y-%m-%d').date() - date.today()).days < 0
    except Exception:
        return False


def holdings_pnl_source_key(*, holdings: Iterable[dict], option_records: Iterable[dict]) -> str:
    parts: list[str] = []
    for h in holdings:
        code = str(h.get('code', '')).strip()
        shares = int(h.get('shares') or 0)
        if not code or shares <= 0:
            continue
        cost = float(h.get('cost_price') or 0)
        parts.append(f'S:{code}:{shares}:{cost:.6f}')

    for o in option_records:
        if o.get('status') != 'holding':
            continue
        if _option_expired(o.get('expiry_date', '')):
            continue
        code = str(o.get('code', '')).strip()
        contracts = int(o.get('contracts') or 0)
        unit = int(o.get('contract_unit') or 10000)
        if not code or contracts <= 0:
            continue
        side = str(o.get('position_side') or 'long_right')
        expiry = str(o.get('expiry_date') or '')
        current_price = float(o.get('current_price') or 0)
        pre_close = float(o.get('pre_close') or 0)
        parts.append(
            f'O:{code}:{side}:{contracts}:{unit}:{expiry}:{current_price:.6f}:{pre_close:.6f}'
        )

    return '|'.join(sorted(parts))


def frozen_snapshot_matches_sources(snapshot: dict, source_key: str) -> bool:
    if not snapshot.get('frozen'):
        return False
    old_key = snapshot.get('source_key')
    if old_key is None:
        return not source_key and not snapshot.get('stock_count') and not snapshot.get('total_pnl')
    return old_key == source_key and isinstance(snapshot.get('breakdown'), dict)


def _is_etf_holding(code: str) -> bool:
    try:
        from core.data_source import is_etf_code
        return is_etf_code(code)
    except Exception:
        c = str(code).strip().lower()
        if c.startswith(('sh', 'sz', 'bj')):
            c = c[2:]
        return len(c) == 6 and c.isdigit() and c.startswith(
            ('15', '16', '18', '50', '51', '52', '56', '58')
        )


def _fmt_money(value) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    sign = '+' if v >= 0 else ''
    return f'{sign}{v:,.0f} 元'


def format_holdings_pnl_tooltip(snapshot: dict) -> str:
    date_str = str(snapshot.get('date') or '')
    count = int(snapshot.get('stock_count') or 0)
    if count <= 0:
        return f'{date_str}\n无数据' if date_str else '无数据'

    total = snapshot.get('total_pnl', 0)
    breakdown = snapshot.get('breakdown') or {}
    lines = [
        date_str,
        f'总盈亏: {_fmt_money(total)}',
        f'个股: {_fmt_money(breakdown.get("stock_pnl", 0))}',
        f'ETF: {_fmt_money(breakdown.get("etf_pnl", 0))}',
        f'期权: {_fmt_money(breakdown.get("option_pnl", 0))}',
        f'涉及: {count}只',
    ]
    return '\n'.join(lines)


def _positive_float_or_none(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v != v:
        return None
    return v


def build_holding_day_change(holding: dict, quote: dict | None) -> dict:
    quote = quote or {}
    cost = float(holding.get('cost_price') or 0)
    shares = int(holding.get('shares') or 0)
    quote_price = _positive_float_or_none(quote.get('price') or quote.get('current_price'))
    current_price = quote_price if quote_price is not None else cost
    pre_close = _positive_float_or_none(quote.get('pre_close') or holding.get('pre_close'))

    day_change = None
    day_pnl = None
    if quote_price is not None and pre_close is not None and shares > 0:
        raw_day_change = quote_price - pre_close
        day_change = round(raw_day_change, 4)
        day_pnl = round(raw_day_change * shares, 2)

    return {
        'current_price': current_price,
        'pre_close': pre_close,
        'day_change': day_change,
        'day_pnl': day_pnl,
    }


def build_holdings_pnl_snapshot(
    *,
    holdings: Iterable[dict],
    option_records: Iterable[dict],
    prices: dict,
    today: date | str,
    frozen: bool,
) -> dict:
    today_str = today.isoformat() if isinstance(today, date) else str(today)
    holdings_list = list(holdings)
    option_list = list(option_records)
    source_key = holdings_pnl_source_key(holdings=holdings_list, option_records=option_list)
    total_pnl = 0.0
    total_value = 0.0
    total_cost = 0.0
    count = 0
    missing = 0
    breakdown = {
        'stock_pnl': 0.0,
        'etf_pnl': 0.0,
        'option_pnl': 0.0,
        'stock_count': 0,
        'etf_count': 0,
        'option_count': 0,
    }

    for h in holdings_list:
        code = h.get('code', '')
        shares = h.get('shares') or 0
        cost = h.get('cost_price') or 0
        if not code or not shares:
            continue
        pdata = prices.get(code) or {}
        metrics = build_holding_day_change(h, pdata)
        cur_price = metrics['current_price']
        day_pnl = metrics['day_pnl']
        if day_pnl is not None:
            total_pnl += day_pnl
            total_value += cur_price * shares
            total_cost += cost * shares
            count += 1
            if _is_etf_holding(code):
                breakdown['etf_pnl'] += day_pnl
                breakdown['etf_count'] += 1
            else:
                breakdown['stock_pnl'] += day_pnl
                breakdown['stock_count'] += 1
        else:
            missing += 1

    for o in option_list:
        if o.get('status') != 'holding':
            continue
        if _option_expired(o.get('expiry_date', '')):
            continue
        contracts = o.get('contracts') or 0
        unit = o.get('contract_unit') or 10000
        if not contracts:
            continue
        ocode = str(o.get('code', '')).strip()
        q = prices.get(ocode) or {}
        cur_price = q.get('price') or q.get('current_price') or o.get('current_price') or 0
        pre_close = q.get('pre_close') or o.get('pre_close') or 0
        if not cur_price or not pre_close:
            missing += 1
            continue
        direction = -1.0 if o.get('position_side') == 'short_obligation' else 1.0
        option_pnl = direction * (cur_price - pre_close) * contracts * unit
        total_pnl += option_pnl
        breakdown['option_pnl'] += option_pnl
        breakdown['option_count'] += 1
        count += 1

    rounded_breakdown = {
        'stock_pnl': round(breakdown['stock_pnl'], 2),
        'etf_pnl': round(breakdown['etf_pnl'], 2),
        'option_pnl': round(breakdown['option_pnl'], 2),
        'stock_count': breakdown['stock_count'],
        'etf_count': breakdown['etf_count'],
        'option_count': breakdown['option_count'],
    }

    return {
        'date': today_str,
        'total_pnl': round(total_pnl, 2),
        'stock_count': count,
        'missing_count': missing,
        'frozen': frozen,
        'source_key': source_key,
        'breakdown': rounded_breakdown,
        'summary': {'total_value': total_value, 'total_cost': total_cost},
    }
