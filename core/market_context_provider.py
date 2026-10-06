"""轻量市场快照提供器 — 大盘情绪 + 全球主要指数。

独立于 ui 面板，可在 worker 线程中直接调用。
缓存目录: CACHE_DIR/market/
  emotion.json  TTL: 交易时段 300s / 非交易 1800s
  global.json   TTL: 900s (15 min)

不写入 stock_context 4h 缓存（数据生命周期不同）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from core import http_client
from core.board_rules import count_mainland_emotion_rows, same_source_mainland_emotion_count
from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'market'
_EMOTION_CACHE = _CACHE_DIR / 'emotion.json'
_GLOBAL_CACHE = _CACHE_DIR / 'global.json'
_GLOBAL_TTL = 900  # 15 min

# 4 大核心全球指数（腾讯 code → 显示名）
_GLOBAL_CODES = [
    ('us.DJI',  '道琼斯'),
    ('us.IXIC', '纳斯达克'),
    ('us.INX',  '标普500'),
    ('hk.HSI',  '恒生指数'),
]
_TENCENT_URL = 'https://qt.gtimg.cn/q='
_TENCENT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
}
_PHASE_THRESHOLDS = {
    'ice_zt_max': 30,
    'ice_dt_min': 30,
    'ice_breadth_ratio': 0.6,
    'decline_dt_zt_ratio': 2.0,
    'retreat_zt_max': 50,
    'retreat_zb_ratio': 0.4,
    'main_up_zt_min': 80,
    'main_up_dt_max': 10,
    'main_up_seal_rate': 0.7,
    'repair_zt_min': 30,
    'repair_zt_max': 80,
}


# ─────────────────────────── helpers ────────────────────────────

def _is_trading_time() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 930 <= hm <= 1130 or 1300 <= hm <= 1500


def _emotion_ttl() -> int:
    return 300 if _is_trading_time() else 1800


def _cache_fresh(path: Path, ttl: int) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return (time.time() - float(data.get('fetched_at', 0))) < ttl
    except Exception:
        return False


def _write_cache(path: Path, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(float(v or 0))
    except Exception:
        return default


# ─────────────────────────── emotion ────────────────────────────

def _try_fetch_zbgc() -> int | None:
    try:
        import akshare as ak
        df = ak.stock_zt_pool_zbgc_em(date=datetime.now().strftime('%Y%m%d'))
        return count_mainland_emotion_rows(df)
    except Exception:
        return None


def _try_fetch_zt_count() -> int | None:
    try:
        import akshare as ak
        df = ak.stock_zt_pool_em(date=datetime.now().strftime('%Y%m%d'))
        return count_mainland_emotion_rows(df) if df is not None else None
    except Exception:
        return None


def _try_fetch_dt_count() -> int | None:
    try:
        import akshare as ak
        df = ak.stock_zt_pool_dtgc_em(date=datetime.now().strftime('%Y%m%d'))
        return count_mainland_emotion_rows(df) if df is not None else None
    except Exception:
        return None


def _fallback_emotion_snapshot() -> dict | None:
    """当 stock_market_activity_legu 不可用时，用涨跌停池子兜底。"""
    zt = _try_fetch_zt_count()
    dt = _try_fetch_dt_count()
    zb = _try_fetch_zbgc()
    if zt is None and dt is None:
        return None
    return {
        'zt': zt or 0,
        'dt': dt or 0,
        'zb': zb or 0,
        'real_zt': 0,
        'real_dt': 0,
        'up': 0,
        'down': 0,
        'amount': None,
        'fetched_at': time.time(),
        '_fallback': True,
    }


def get_emotion_snapshot(force: bool = False) -> dict | None:
    """获取大盘情绪三数（涨停/跌停/炸板+上涨下跌家数+成交额）。

    返回 dict 或 None（失败时静默降级）。
    """
    if not force and _cache_fresh(_EMOTION_CACHE, _emotion_ttl()):
        try:
            return json.loads(_EMOTION_CACHE.read_text(encoding='utf-8'))
        except Exception:
            pass
    try:
        import akshare as ak
        df = ak.stock_market_activity_legu()
        m = dict(zip(df['item'], df['value']))
        zb = _try_fetch_zbgc()
        zt_raw = int(float(m.get('涨停', 0)))
        dt_raw = int(float(m.get('跌停', 0)))
        try:
            zt_pool = ak.stock_zt_pool_em(date=datetime.now().strftime('%Y%m%d'))
            adjusted = same_source_mainland_emotion_count(zt_raw, zt_pool)
            if adjusted is not None:
                zt_raw = adjusted
        except Exception:
            pass
        try:
            dt_pool = ak.stock_zt_pool_dtgc_em(date=datetime.now().strftime('%Y%m%d'))
            adjusted = same_source_mainland_emotion_count(dt_raw, dt_pool)
            if adjusted is not None:
                dt_raw = adjusted
        except Exception:
            pass
        snap: dict = {
            'zt':      zt_raw,
            'dt':      dt_raw,
            'zb':      int(zb or 0),
            'real_zt': int(float(m.get('真实涨停', 0))),
            'real_dt': int(float(m.get('真实跌停', 0))),
            'up':      int(float(m.get('上涨', 0))),
            'down':    int(float(m.get('下跌', 0))),
            'amount':  None,
            'fetched_at': time.time(),
        }
        amt_raw = m.get('成交额') or m.get('总成交额')
        if amt_raw is not None:
            try:
                snap['amount'] = float(amt_raw)
            except (ValueError, TypeError):
                pass
        _write_cache(_EMOTION_CACHE, snap)
        return snap
    except Exception:
        fallback = _fallback_emotion_snapshot()
        if fallback is not None:
            _write_cache(_EMOTION_CACHE, fallback)
        return fallback


def get_market_phase(snap: dict | None = None) -> str:
    if not snap:
        return 'unknown'
    zt = _safe_int(snap.get('zt'))
    dt = _safe_int(snap.get('dt'))
    zb = _safe_int(snap.get('zb'))
    up = _safe_int(snap.get('up'))
    down = _safe_int(snap.get('down'))
    total_boards = zt + zb
    seal_rate = zt / total_boards if total_boards > 0 else 0.0

    if (zt < _PHASE_THRESHOLDS['ice_zt_max'] and dt > _PHASE_THRESHOLDS['ice_dt_min']) or (
        down > 0 and up < down * _PHASE_THRESHOLDS['ice_breadth_ratio']
    ):
        return 'ice'
    if zt > 0 and dt > zt * _PHASE_THRESHOLDS['decline_dt_zt_ratio'] and up < down:
        return 'decline'
    if total_boards > 0 and zt < _PHASE_THRESHOLDS['retreat_zt_max'] and zb / total_boards > _PHASE_THRESHOLDS['retreat_zb_ratio']:
        return 'retreat'
    if zt > _PHASE_THRESHOLDS['main_up_zt_min'] and dt < _PHASE_THRESHOLDS['main_up_dt_max'] and seal_rate > _PHASE_THRESHOLDS['main_up_seal_rate']:
        return 'main_up'
    if _PHASE_THRESHOLDS['repair_zt_min'] <= zt <= _PHASE_THRESHOLDS['repair_zt_max'] and up > down:
        return 'repair'
    return 'unknown'


# ─────────────────────────── global indices ──────────────────────

def get_global_snapshot(force: bool = False) -> dict | None:
    """获取 4 大全球指数快照（道琼斯/纳斯达克/标普500/恒生）。

    返回 dict 或 None。
    """
    if not force and _cache_fresh(_GLOBAL_CACHE, _GLOBAL_TTL):
        try:
            return json.loads(_GLOBAL_CACHE.read_text(encoding='utf-8'))
        except Exception:
            pass
    try:
        codes_str = ','.join(c for c, _ in _GLOBAL_CODES)
        resp = http_client.get(_TENCENT_URL + codes_str, headers=_TENCENT_HEADERS, timeout=8)
        resp.raise_for_status()
        text = resp.content.decode('gbk', 'replace')

        indices = []
        for line in text.strip().split(';'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            var_name = line.split('=')[0]     # e.g. v_us.DJI
            tc_code = var_name.replace('v_', '')  # us.DJI
            disp_name = next((n for c, n in _GLOBAL_CODES if c == tc_code), None)
            if disp_name is None:
                continue
            val = line.split('=', 1)[1].strip().strip('"')
            fields = val.split('~')
            if len(fields) < 33:
                continue
            try:
                price = float(fields[3])
                pct = float(fields[32])
            except (ValueError, TypeError, IndexError):
                continue
            indices.append({'name': disp_name, 'price': price, 'pct': pct})

        if not indices:
            return None
        snap = {'indices': indices, 'fetched_at': time.time()}
        _write_cache(_GLOBAL_CACHE, snap)
        return snap
    except Exception:
        return None


# ─────────────────────────── text formatters ─────────────────────

def format_emotion_text(snap: dict | None) -> str:
    """将 emotion snapshot 转为 prompt 注入文本。"""
    if not snap:
        return ''
    zt = snap.get('zt', 0)
    dt = snap.get('dt', 0)
    zb = snap.get('zb', 0)
    real_zt = snap.get('real_zt', 0)
    up = snap.get('up', 0)
    down = snap.get('down', 0)
    amount = snap.get('amount')
    phase = get_market_phase(snap)

    parts: list[str] = []
    if phase != 'unknown':
        parts.append(f'情绪阶段估计 {phase}（仅用于降置信度）')
    if zt or dt:
        real_str = f'（真实{real_zt}）' if real_zt else ''
        zb_str = f'/ 炸板 {zb}家' if zb else ''
        seal_str = ''
        if zb:
            seal_rate = zt / (zt + zb) * 100 if (zt + zb) > 0 else 0
            seal_str = f' 封板率{seal_rate:.0f}%'
        parts.append(f'涨停 {zt}{real_str} / 跌停 {dt} {zb_str}{seal_str}')
    if up or down:
        parts.append(f'上涨 {up}家 / 下跌 {down}家')
    if amount:
        parts.append(f'成交额 {amount / 1e12:.2f}万亿')

    return '## 大盘情绪\n▶ ' + '，'.join(parts) if parts else ''


def format_global_text(snap: dict | None) -> str:
    """将 global snapshot 转为 prompt 注入文本（最多 4 条）。"""
    if not snap or not snap.get('indices'):
        return ''
    lines = ['## 全球主要指数']
    for idx in snap['indices'][:4]:
        name = idx.get('name', '')
        pct = idx.get('pct', 0.0)
        sign = '+' if pct >= 0 else ''
        lines.append(f'▶ {name}: {sign}{pct:.2f}%')
    return '\n'.join(lines)
