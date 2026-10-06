"""板块分时历史缓存：按口径分别落盘
~/.aldebaran/cache/sector_history_{concept|industry}_YYYYMMDD.json
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, date
from pathlib import Path

from core.constants import CACHE_DIR

logger = logging.getLogger(__name__)

# intel_feed.json 写锁 — 防止 append_intel_events 与 intel_fetcher.run_fetch
# 在不同后台线程上并发 read-modify-write 损坏文件。
_FEED_LOCK = threading.Lock()


def _warn_save_failed(name: str):
    logger.warning('[cache] 保存失败: %s', name, exc_info=True)


def classify_code(code: str) -> str:
    """按长度/数字分类: 8位纯数字→'option', 6位→'stock_or_etf', 其他→'unknown'。"""
    s = str(code).strip()
    if s.isdigit() and len(s) == 8:
        return 'option'
    if s.isdigit() and len(s) == 6:
        return 'stock_or_etf'
    return 'unknown'


def compute_buy(cur_shares: int, cur_cost: float, buy_shares: int, buy_price: float):
    """买入后新 (总份数, 加权成本)。"""
    total_shares = cur_shares + buy_shares
    if total_shares <= 0:
        return 0, 0.0
    total_cost = cur_cost * cur_shares + buy_price * buy_shares
    return total_shares, round(total_cost / total_shares, 3)


def compute_sell(cur_shares: int, sell_shares: int) -> int | None:
    """卖出后新份数；超持仓返回 None。"""
    if sell_shares > cur_shares:
        return None
    return cur_shares - sell_shares


from core.paths import HOME

# UI 偏好配置文件（与 cache 同级目录，但不在 cache 子目录里）
_UI_CONFIG_FILE = HOME / 'ui_config.json'


def load_ui_config():
    """读取 UI 偏好配置；失败返回 {}。"""
    try:
        if _UI_CONFIG_FILE.exists():
            return json.loads(_UI_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def save_ui_config(cfg):
    """保存 UI 偏好配置；失败静默。"""
    try:
        _UI_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UI_CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('ui_config')


def update_ui_config(**fields):
    """局部更新 UI 配置字段。"""
    cfg = load_ui_config()
    cfg.update(fields)
    save_ui_config(cfg)
    return cfg


DEFAULT_POSITION_VALUE = 80000


def get_default_position_value():
    try:
        v = int(load_ui_config().get('default_position_value', DEFAULT_POSITION_VALUE))
        return v if v > 0 else DEFAULT_POSITION_VALUE
    except Exception:
        return DEFAULT_POSITION_VALUE


def _path_for(d, kind="concept"):
    return CACHE_DIR / f"sector_history_{kind}_{d.strftime('%Y%m%d')}.json"


def _tracked_path_for(d, kind="concept"):
    return CACHE_DIR / f"sector_tracked_{kind}_{d.strftime('%Y%m%d')}.json"


def _latest_df_path_for(d, kind="concept"):
    return CACHE_DIR / f"sector_latest_df_{kind}_{d.strftime('%Y%m%d')}.json"


def _minute_path_for(d, kind="concept"):
    return CACHE_DIR / f"sector_minute_history_{kind}_{d.strftime('%Y%m%d')}.json"


def _panel_path_for(name):
    return CACHE_DIR / f"{name}.json"


def save_panel_cache(name, data):
    try:
        path = _panel_path_for(name)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed(f'panel_cache:{name}')


def load_panel_cache(name):
    try:
        path = _panel_path_for(name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_sector_history(history, d, kind="concept"):
    """history: list of (datetime, {sector_name: net_value})"""
    try:
        path = _path_for(d, kind)
        data = [(t.isoformat(), snap) for t, snap in history]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed(f'sector_history:{kind}:{d}')


def load_sector_history(d, kind="concept"):
    """读取当日缓存。返回 list of (datetime, snapshot) 或空列表。"""
    try:
        path = _path_for(d, kind)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        return [(datetime.fromisoformat(t), snap) for t, snap in data]
    except Exception:
        return []


def save_sector_tracked(sectors, d, kind="concept"):
    try:
        path = _tracked_path_for(d, kind)
        path.write_text(json.dumps(list(sectors), ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed(f'sector_tracked:{kind}:{d}')


def load_sector_tracked(d, kind="concept"):
    try:
        path = _tracked_path_for(d, kind)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        return [str(x) for x in data if str(x)]
    except Exception:
        return []


def save_sector_latest_df(df, d, kind="concept"):
    """保存最新一次成功拉取的板块快照（含 行业/代码/净额/涨跌幅）。"""
    try:
        path = _latest_df_path_for(d, kind)
        cols = [c for c in ('行业', '代码', '净额', '行业-涨跌幅') if c in df.columns]
        records = df[cols].to_dict(orient='records')
        path.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed(f'sector_latest_df:{kind}:{d}')


def save_sector_minute_history(history, d, kind="concept"):
    try:
        path = _minute_path_for(d, kind)
        data = [(t.isoformat(), snap) for t, snap in history]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed(f'sector_minute_history:{kind}:{d}')


def load_sector_minute_history(d, kind="concept"):
    try:
        path = _minute_path_for(d, kind)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        return [(datetime.fromisoformat(t), snap) for t, snap in data]
    except Exception:
        return []


def load_sector_latest_df(d, kind="concept"):
    """读取当日最新板块快照，返回 list[dict] 或空列表。"""
    try:
        path = _latest_df_path_for(d, kind)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        return [r for r in data if isinstance(r, dict)]
    except Exception:
        return []


def save_watchlist(codes):
    """保存自选股记录列表到 watchlist.json。"""
    try:
        path = CACHE_DIR / 'watchlist.json'
        path.write_text(json.dumps(codes, ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed('watchlist')


def load_watchlist():
    """读取自选股记录列表。自动迁移旧格式（纯代码列表→结构化记录）。"""
    try:
        path = CACHE_DIR / 'watchlist.json'
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        if not data:
            return []
        # 旧格式：['000001', '600519', ...] → 迁为 dict 列表
        if isinstance(data[0], str):
            migrated = [{
                'code': c,
                'shares': None,
                'cost_price': None,
                'cumulative_pnl': 0.0,
                'last_pnl_date': None,
                'theme': '',
            } for c in data]
            save_watchlist(migrated)
            return migrated
        # 补齐新字段默认值（不修改磁盘文件，仅内存补齐）
        _wl_defaults = {
            'trades': [], 'buy_date': None, 'name': '',
            'pre_close': None, 'last_close': None, 'last_close_date': None,
            'include_in_pnl': True, 'group': '',
        }
        for rec in data:
            for k, v in _wl_defaults.items():
                rec.setdefault(k, v)
        return data
    except Exception:
        return []


def save_watchlist_groups(names):
    """保存自选股分组名（有序列表，不含隐式的"未分组"）。"""
    try:
        path = CACHE_DIR / 'watchlist_groups.json'
        path.write_text(json.dumps(list(names), ensure_ascii=False), encoding='utf-8')
    except Exception:
        _warn_save_failed('watchlist_groups')


def load_watchlist_groups():
    """读取自选股分组名有序列表；失败返回 []。"""
    try:
        path = CACHE_DIR / 'watchlist_groups.json'
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, list):
            return [str(n) for n in data]
    except Exception:
        pass
    return []


def save_sector_constituents(sector_code, constituents):
    """保存板块成分股列表。按板块代码缓存，有效期 1 天。

    constituents: list of {'code': '600519', 'name': '贵州茅台'}
    """
    try:
        from datetime import datetime as _dt
        save_panel_cache(f'sector_constituents_{sector_code}', {
            'date': _dt.now().strftime('%Y-%m-%d'),
            'constituents': constituents,
        })
    except Exception:
        _warn_save_failed(f'sector_constituents:{sector_code}')


def load_sector_constituents(sector_code):
    """读取板块成分股列表。仅当天缓存有效，过期返回 None。"""
    try:
        from datetime import datetime as _dt
        data = load_panel_cache(f'sector_constituents_{sector_code}')
        if not data:
            return None
        if data.get('date') != _dt.now().strftime('%Y-%m-%d'):
            return None
        return data.get('constituents')
    except Exception:
        return None


def save_market_emotion_record(record, max_days=60):
    """保存大盘情绪日记录。同日覆盖，最多保留 max_days 天。

    record 示例：{'date': '2026-05-11', 'zt': 80, 'real_zt': 65, 'dt': 12,
                  'real_dt': 5, 'zb': 18, 'amount': 1.2e12, 'north': 35.2,
                  'seal_rate': 81.6}
    """
    try:
        dataset = load_panel_cache('market_emotion_history') or {}
        records = dataset.get('records') or []
        date_key = record.get('date', '')
        if not date_key:
            return
        records = [r for r in records if r.get('date') != date_key]
        records.append(record)
        records.sort(key=lambda r: r.get('date', ''), reverse=True)
        save_panel_cache('market_emotion_history', {'records': records[:max_days]})
    except Exception:
        date_key = record.get('date', '') if isinstance(record, dict) else ''
        _warn_save_failed(f'market_emotion:{date_key}')


def load_market_emotion_history():
    """读取大盘情绪历史记录列表（按日期倒序）。"""
    try:
        dataset = load_panel_cache('market_emotion_history') or {}
        records = dataset.get('records') or []
        return [r for r in records if r.get('date')]
    except Exception:
        return []


def save_watchlist_fund_history(code, record, max_days=10):
    """保存自选股单只股票的每日资金快照。同日覆盖，最多保留 max_days 天。

    record 示例：{'date': '2026-05-11', 'main_net': 12345678, 'main_pct': 5.12,
                  'super_net': ..., 'big_net': ..., 'name': '...'}
    """
    try:
        name = f"watchlist_fund_{code}"
        dataset = load_panel_cache(name) or {}
        records = dataset.get('records') or []
        date_key = record.get('date', '')
        if not date_key:
            return
        records = [r for r in records if r.get('date') != date_key]
        records.append(record)
        records.sort(key=lambda r: r.get('date', ''), reverse=True)
        save_panel_cache(name, {'code': code, 'records': records[:max_days]})
    except Exception:
        _warn_save_failed(f'watchlist_fund:{code}')


def load_watchlist_fund_history(code):
    """读取自选股单只股票的历史资金记录列表（按日期倒序）。"""
    try:
        name = f"watchlist_fund_{code}"
        dataset = load_panel_cache(name) or {}
        records = dataset.get('records') or []
        return [r for r in records if r.get('date')]
    except Exception:
        return []


# 虚拟持仓文件（用户数据，放根目录与 intel_config.json 同级）
# 注意：虚拟持仓功能已被追踪任务取代，保留仅为向后兼容
_VIRTUAL_PORTFOLIO_FILE = HOME / 'virtual_portfolio.json'


def load_virtual_portfolio() -> list:
    """读取虚拟持仓列表；失败返回 []。

    注意：虚拟持仓功能已被追踪任务取代，此函数保留仅为向后兼容。
    """
    try:
        if _VIRTUAL_PORTFOLIO_FILE.exists():
            data = json.loads(_VIRTUAL_PORTFOLIO_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_virtual_portfolio(positions: list):
    """保存虚拟持仓列表；失败静默。

    注意：虚拟持仓功能已被追踪任务取代，此函数保留仅为向后兼容。
    """
    try:
        _VIRTUAL_PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VIRTUAL_PORTFOLIO_FILE.write_text(
            json.dumps(positions, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('virtual_portfolio')


def cleanup_stale_sector_cache(keep_days: int = 7) -> int:
    """删除过期缓存文件，返回删除/清理项数。异常静默。

    清理范围：
    1. sector_*YYYYMMDD.json  — 超过 keep_days 天的历史缓存文件
    2. sector_constituents_*.json — date 字段不等于今天的成分股缓存
    3. ai_sector_analysis.json — 移除 date != today 的板块分析条目
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    deleted = 0

    # ── 1. sector_*YYYYMMDD.json 文件清理 ──
    try:
        cutoff = datetime.now().date() - timedelta(days=keep_days)
        pattern = re.compile(r'sector_.*?(\d{8})\.json$')
        for f in CACHE_DIR.iterdir():
            if not f.is_file():
                continue
            m = pattern.search(f.name)
            if not m:
                continue
            try:
                file_date = datetime.strptime(m.group(1), '%Y%m%d').date()
                if file_date < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except Exception:
                pass
    except Exception:
        pass

    # ── 2. sector_constituents_*.json 成分股缓存（date 字段判断）──
    try:
        for f in CACHE_DIR.iterdir():
            if not f.is_file():
                continue
            if not f.name.startswith('sector_constituents_'):
                continue
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or data.get('date') != today_str:
                    f.unlink(missing_ok=True)
                    deleted += 1
            except Exception:
                try:
                    f.unlink(missing_ok=True)
                    deleted += 1
                except Exception:
                    pass
    except Exception:
        pass

    # ── 3. ai_sector_analysis.json 移除过期板块条目 ──
    try:
        ai_path = CACHE_DIR / 'ai_sector_analysis.json'
        if ai_path.exists():
            data = json.loads(ai_path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                cleaned = {k: v for k, v in data.items()
                           if isinstance(v, dict) and v.get('date') == today_str}
                if len(cleaned) < len(data):
                    ai_path.write_text(
                        json.dumps(cleaned, ensure_ascii=False), encoding='utf-8'
                    )
                    deleted += len(data) - len(cleaned)
    except Exception:
        pass

    return deleted


from core.paths import INTEL_FEED_FILE as _INTEL_FEED_FILE  # noqa: E402


def append_intel_events(events: list[dict]) -> int:
    """追加情报条目到 intel_feed.json，按标题 MD5 去重，返回新增数量。

    整个 read-modify-write 段在 _FEED_LOCK 内执行，避免与 intel_fetcher.run_fetch
    并发时丢更新或写半截。
    """
    import hashlib
    from datetime import date, timedelta
    if not events:
        return 0
    with _FEED_LOCK:
        existing: list[dict] = []
        if _INTEL_FEED_FILE.exists():
            try:
                existing = json.loads(_INTEL_FEED_FILE.read_text(encoding='utf-8'))
            except Exception:
                existing = []
        existing_md5s = {
            hashlib.md5(e.get('title', '').encode()).hexdigest()[:8]
            for e in existing
        }
        today = date.today()
        today_str = today.strftime('%Y-%m-%d')
        today_key = today_str.replace('-', '')
        seq_nums = [
            int(e['id'].split('_')[1])
            for e in existing
            if '_' in e.get('id', '') and e.get('id', '').startswith(today_key + '_')
        ]
        next_seq = (max(seq_nums) + 1) if seq_nums else 1
        added = 0
        for ev in events:
            title = str(ev.get('title', '')).strip()
            if not title:
                continue
            h = hashlib.md5(title.encode()).hexdigest()[:8]
            if h in existing_md5s:
                continue
            entry = {
                'id': f'{today_key}_{next_seq:03d}',
                'title': title,
                'category': ev.get('category', 'macro'),
                'level': ev.get('level', 'info'),
                'direction': ev.get('direction', 'neutral'),
                'summary': ev.get('summary', title),
                'interpretation': ev.get('interpretation', ''),
                'trading_tip': ev.get('trading_tip', ''),
                'related_sectors': list(ev.get('related_sectors', [])),
                'timestamp': str(ev.get('timestamp', today_str)),
                'source': str(ev.get('source', 'agent')),
            }
            existing.append(entry)
            existing_md5s.add(h)
            next_seq += 1
            added += 1
        if added:
            try:
                cutoff = (today - timedelta(days=30)).strftime('%Y-%m-%d')
                existing = [e for e in existing if e.get('timestamp', '9999') >= cutoff]
                _INTEL_FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
                _INTEL_FEED_FILE.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2),
                    encoding='utf-8',
                )
            except Exception:
                _warn_save_failed('intel_feed')
    return added


# ── 持仓视角自选列表（用户数据）──
_POSITION_WL_FILE = HOME / 'position_watchlist.json'


def load_position_watchlist() -> list:
    """加载持仓视角自选列表；不存在或异常返回空列表。"""
    try:
        if _POSITION_WL_FILE.exists():
            data = json.loads(_POSITION_WL_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_position_watchlist(records: list) -> None:
    """全量覆盖写入持仓视角自选列表；失败静默。"""
    try:
        _POSITION_WL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _POSITION_WL_FILE.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('position_watchlist')


# ── PnL 日历缓存 ──

def _pnl_calendar_path():
    return CACHE_DIR / 'pnl_calendar.json'


def load_pnl_calendar() -> list[dict]:
    """读取每日 PnL 快照列表。"""
    try:
        p = _pnl_calendar_path()
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []


def save_pnl_calendar(records: list[dict]) -> None:
    """全量覆盖写入每日 PnL 快照。"""
    try:
        _pnl_calendar_path().write_text(
            json.dumps(records, ensure_ascii=False), encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('pnl_calendar')


# ── 真实持仓月历缓存（独立于自选股月历）──
_HOLDINGS_PNL_CAL_FILE = CACHE_DIR / 'holdings_pnl_calendar.json'


def load_holdings_pnl_calendar() -> list[dict]:
    """读取真实持仓每日 PnL 快照列表。"""
    try:
        if not _HOLDINGS_PNL_CAL_FILE.exists():
            return []
        return json.loads(_HOLDINGS_PNL_CAL_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []


def save_holdings_pnl_calendar(records: list[dict]) -> None:
    """全量覆盖写入真实持仓每日 PnL 快照。"""
    try:
        _HOLDINGS_PNL_CAL_FILE.write_text(
            json.dumps(records, ensure_ascii=False), encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('holdings_pnl_calendar')


# ── 期权自选缓存 ──

def _option_wl_path():
    return CACHE_DIR / 'option_watchlist.json'


def normalize_option_record(rec: dict) -> dict:
    """将旧格式期权记录补齐为新台账结构，不破坏已有数据。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 核心字段兼容
    rec.setdefault('code', '')
    rec.setdefault('name', '')
    rec.setdefault('underlying_code', '')
    rec.setdefault('underlying_name', '')
    rec.setdefault('option_type', 'call')
    rec.setdefault('position_side', 'long_right')
    rec.setdefault('strike_price', 0.0)
    rec.setdefault('expiry_date', '')
    rec.setdefault('contract_unit', 10000)
    rec.setdefault('contracts', 0)
    # 旧字段映射
    if 'open_price' not in rec and 'cost_price' in rec:
        rec['open_price'] = rec.pop('cost_price')
    rec.setdefault('open_price', 0.0)
    if 'current_price' not in rec and 'price' in rec:
        rec['current_price'] = rec.pop('price')
    rec.setdefault('current_price', 0.0)
    rec.setdefault('pre_close', 0.0)
    if 'contracts' in rec:
        old_shares = rec.pop('shares', None)
        if old_shares is not None and rec['contracts'] == 0:
            rec['contracts'] = old_shares
    rec.setdefault('contracts', 0)
    # 清除旧字段残留
    for old_key in ('price', 'cost_price', 'shares', 'pct', 'volume'):
        rec.pop(old_key, None)
    rec.setdefault('open_date', '')
    rec.setdefault('status', 'holding')
    rec.setdefault('risk_note', '')
    # 浮点数类型安全
    rec['strike_price'] = float(rec.get('strike_price', 0) or 0)
    rec['open_price'] = float(rec.get('open_price', 0) or 0)
    rec['current_price'] = float(rec.get('current_price', 0) or 0)
    rec['pre_close'] = float(rec.get('pre_close', 0) or 0)
    rec['contract_unit'] = int(rec.get('contract_unit', 10000) or 10000)
    rec['contracts'] = int(rec.get('contracts', 0) or 0)
    if 'created_at' not in rec:
        rec['created_at'] = now
    rec['updated_at'] = now
    return rec


def option_days_to_expiry(expiry_date: str) -> int | None:
    """计算距到期剩余天数；日期不可解析或为空返回 None。"""
    if not expiry_date:
        return None
    try:
        if ' ' in expiry_date:
            expiry_date = expiry_date.split(' ')[0]
        exp = datetime.strptime(expiry_date, '%Y-%m-%d').date()
        return (exp - date.today()).days
    except Exception:
        return None


def compute_option_metrics(rec: dict) -> dict:
    """根据期权台账记录计算关键指标。"""
    days = option_days_to_expiry(rec.get('expiry_date', ''))
    position_side = rec.get('position_side', 'long_right')
    contracts = int(rec.get('contracts', 0) or 0)
    contract_unit = int(rec.get('contract_unit', 10000) or 10000)
    pre_close = float(rec.get('pre_close', 0) or 0)
    current_price = float(rec.get('current_price', 0) or 0)

    direction = -1 if position_side == 'short_obligation' else 1
    needs_price = current_price <= 0 or pre_close <= 0
    premium_cost = None
    market_value = None
    floating_pnl = None
    notional_exposure = None

    strike_price = float(rec.get('strike_price', 0) or 0)
    if contracts > 0 and contract_unit > 0 and strike_price > 0:
        notional_exposure = round(strike_price * contract_unit * contracts, 3)

    if contracts > 0 and contract_unit > 0:
        if pre_close > 0:
            premium_cost = round(pre_close * contract_unit * contracts, 3)
        if current_price > 0:
            market_value = round(current_price * contract_unit * contracts, 3)
        if not needs_price:
            floating_pnl = round(
                direction * contracts * contract_unit * (current_price - pre_close), 3)

    if days is None:
        expiry_level = 'unknown'
    elif days < 0:
        expiry_level = 'expired'
    elif days <= 1:
        expiry_level = 'd1'
    elif days <= 3:
        expiry_level = 'd3'
    elif days <= 7:
        expiry_level = 'd7'
    else:
        expiry_level = 'normal'

    return {
        'days_to_expiry': days,
        'premium_cost': premium_cost,
        'market_value': market_value,
        'floating_pnl': floating_pnl,
        'notional_exposure': notional_exposure,
        'needs_price': needs_price,
        'unsupported_short': False,
        'expiry_level': expiry_level,
    }


def compute_option_total_daily_pnl() -> float:
    """有效持仓期权的当日收益合计（带方向求和，剔除已平仓/到期/无效合约）。"""
    total = 0.0
    for rec in load_option_watchlist():
        if rec.get('status', 'holding') != 'holding':
            continue
        m = compute_option_metrics(rec)
        days = m['days_to_expiry']
        if days is None or days < 0:
            continue
        if m['floating_pnl'] is not None:
            total += m['floating_pnl']
    return round(total, 3)


def load_option_watchlist() -> list[dict]:
    """读取期权持仓列表，兼容旧格式自动补齐。"""
    try:
        p = _option_wl_path()
        if not p.exists():
            return []
        records = json.loads(p.read_text(encoding='utf-8'))
        return [normalize_option_record(r) for r in records]
    except Exception:
        return []


def save_option_watchlist(codes: list[dict]) -> None:
    """全量覆盖写入期权持仓列表。"""
    try:
        _option_wl_path().write_text(
            json.dumps(codes, ensure_ascii=False), encoding='utf-8',
        )
    except Exception:
        _warn_save_failed('option_watchlist')


# ── 加自选共享 helper ──

def add_stock_to_watchlist(code: str, shares: int = 100, cost_price: float = 0.0,
                           buy_date: str = '', name: str = '', theme: str = '',
                           group: str = '') -> dict | None:
    """向 watchlist.json 追加一只股票。返回新记录；同代码同组已存在返回 None。

    同代码不同组允许重复（组内唯一、跨组可重复）。
    """
    wl = load_watchlist()
    if any(r.get('code') == code and (r.get('group') or '') == (group or '') for r in wl):
        return None
    bd = buy_date or datetime.now().strftime('%Y-%m-%d')
    rec = {
        'code': code, 'name': name, 'shares': shares, 'cost_price': cost_price,
        'buy_date': bd, 'theme': theme, 'group': group,
        'cumulative_pnl': 0.0, 'last_pnl_date': None,
        'trades': [{
            'date': bd, 'direction': 'buy', 'shares': shares,
            'price': cost_price, 'timestamp': datetime.now().isoformat(),
            'source': 'add_stock_to_watchlist',
        }],
        'pre_close': None, 'last_close': None, 'last_close_date': None,
        'include_in_pnl': True,
    }
    wl.append(rec)
    save_watchlist(wl)
    return rec
