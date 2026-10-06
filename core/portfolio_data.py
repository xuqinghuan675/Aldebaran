"""真实持仓 CRUD + KPI 计算。

落盘：BASE_DIR/portfolio.json（用户数据目录）
ID 格式：h_{YYYYMMDD}_{seq:03d}
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.paths import HOME
_PORTFOLIO_FILE = HOME / 'portfolio.json'


# ── 持久化 ──────────────────────────────────────────────

def load_holdings() -> list:
    """读取真实持仓列表；失败返回 []。"""
    try:
        if _PORTFOLIO_FILE.exists():
            data = json.loads(_PORTFOLIO_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return _ensure_holding_schema_v3(data)
    except Exception:
        pass
    return []


def save_holdings(holdings: list):
    """保存真实持仓列表；失败静默。"""
    try:
        _PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PORTFOLIO_FILE.write_text(
            json.dumps(holdings, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def _gen_holding_id() -> str:
    return 'h_' + uuid.uuid4().hex[:12]


def _ensure_holding_schema_v3(holdings: list[dict]) -> list[dict]:
    """补全 v3.0 schema 新字段并写回（老数据兼容迁移）。"""
    changed = False
    for h in holdings:
        if 'id' not in h:
            h['id'] = _gen_holding_id()
            changed = True
        for k, default in [
            ('cost_price', 0.0),
            ('shares', 0),
            ('buy_date', ''),
            ('stop_loss_pct', None),
            ('notes', ''),
            ('sector_hint', ''),
        ]:
            if k not in h:
                h[k] = default
                changed = True
    if changed:
        save_holdings(holdings)
    return holdings


# ── ID 生成 ──────────────────────────────────────────────

def _next_id(holdings: list) -> str:
    today = date.today().strftime('%Y%m%d')
    prefix = f'h_{today}_'
    existing = [h['id'] for h in holdings if isinstance(h.get('id'), str) and h['id'].startswith(prefix)]
    seq = len(existing) + 1
    return f'{prefix}{seq:03d}'


# ── CRUD ─────────────────────────────────────────────────

def add_holding(data: dict) -> str:
    """添加一条真实持仓，返回新建 id。

    必填字段：code / name / cost_price / shares
    可选字段：buy_date / note / stop_loss_pct / target_price
    """
    holdings = load_holdings()
    new_code = ''.join(ch for ch in str(data.get('code', '')) if ch.isdigit())[-6:]
    for h in holdings:
        exist_code = ''.join(ch for ch in str(h.get('code', '')) if ch.isdigit())[-6:]
        if new_code and exist_code == new_code:
            raise ValueError(f'该股票（{new_code}）已在真实持仓中，不能重复添加')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    hid = _next_id(holdings)
    entry = {
        'id': hid,
        'code': str(data.get('code', '')).strip(),
        'name': str(data.get('name', '')).strip(),
        'cost_price': float(data.get('cost_price', 0)),
        'shares': int(data.get('shares', 0)),
        'buy_date': str(data.get('buy_date', date.today().isoformat())),
        'note': str(data.get('note', '')),
        'stop_loss_pct': float(data.get('stop_loss_pct', -8.0)),
        'target_price': float(data['target_price']) if data.get('target_price') else None,
        'created_at': now,
        'last_modified': now,
    }
    holdings.append(entry)
    save_holdings(holdings)
    return hid


def update_holding(hid: str, **fields) -> bool:
    """局部更新持仓字段；返回是否找到并更新。"""
    holdings = load_holdings()
    for h in holdings:
        if h.get('id') == hid:
            h.update(fields)
            h['last_modified'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_holdings(holdings)
            return True
    return False


def remove_holding(hid: str) -> bool:
    """删除持仓；返回是否找到并删除。"""
    holdings = load_holdings()
    before = len(holdings)
    holdings = [h for h in holdings if h.get('id') != hid]
    if len(holdings) < before:
        save_holdings(holdings)
        return True
    return False


# ── KPI 计算 ─────────────────────────────────────────────

def compute_kpis(holding: dict, current_price: Optional[float]) -> dict:
    """计算持仓 KPI。

    Returns:
        market_value: 当前市值（元）
        pnl_amount: 盈亏金额（元）
        pnl_pct: 盈亏百分比
        days_held: 持有天数
        stop_triggered: 是否触及止损线
        target_reached: 是否达到目标价
    """
    cost = holding.get('cost_price', 0) or 0
    shares = holding.get('shares', 0) or 0
    buy_date_str = holding.get('buy_date', '')

    cur = current_price or cost
    market_value = round(cur * shares, 2)
    cost_total = round(cost * shares, 2)
    pnl_amount = round(market_value - cost_total, 2)
    pnl_pct = round((cur - cost) / cost * 100, 2) if cost else 0.0

    days_held = 0
    try:
        if buy_date_str:
            delta = date.today() - date.fromisoformat(buy_date_str)
            days_held = delta.days
    except Exception:
        pass

    stop_pct = holding.get('stop_loss_pct')
    stop_triggered = False
    if stop_pct is not None and cost > 0:
        stop_triggered = pnl_pct <= stop_pct

    target_price = holding.get('target_price')
    target_reached = False
    if target_price and current_price:
        target_reached = current_price >= target_price

    return {
        'market_value': market_value,
        'cost_total': cost_total,
        'pnl_amount': pnl_amount,
        'pnl_pct': pnl_pct,
        'days_held': days_held,
        'stop_triggered': stop_triggered,
        'target_reached': target_reached,
        'current_price': cur,
    }
