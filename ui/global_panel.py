"""全球主要指数面板：3x3 网格展示美股 / 港股 / 英国等核心指数。

数据源（优先级）：
  1. 腾讯行情 qt.gtimg.cn（美股/港股/富时100 可直接获取，不走 push2）
  2. 东方财富 push2 API 作为回退（部分网络环境 push2 被封时自动跳过）

每个指数有独立的「最新行情时间」，由于各市场收盘时间不同，
必须在每张卡片上单独显示数据日期/时间，避免误导。
"""
from datetime import datetime, timezone, timedelta

import requests

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, Signal

from core.constants import DARK_BG, MUTED, RED, GREEN
from core.data_worker import DataWorker, is_trading_time
from core.cache import save_panel_cache, load_panel_cache
from core.net_setup import push2_get


# 12 个全球核心指数（按区域分组，4 行×3 张）
# 优先东财 push2（覆盖全部12个），push2 不可用时腾讯回退（美股3+港股3+英国富时，共7个）
_GLOBAL_INDICES = [
    # (display_name, tencent_code_or_None, eastmoney_f14_name_or_None)
    # 美股
    ('道琼斯',     'us.DJI',   '道琼斯'),
    ('纳斯达克',   'us.IXIC',  '纳斯达克'),
    ('标普500',    'us.INX',   '标普500'),
    # 港股
    ('恒生指数',   'hkHSI',    '恒生指数'),
    ('国企指数',   'hkHSCEI',  '国企指数'),
    ('红筹指数',   'hkHSCCI',  '红筹指数'),
    # 欧洲
    ('英国富时',   'ukUKX',    '英国富时100'),
    ('德国DAX',    None,       '德国DAX30'),
    ('法国CAC',    None,       '法国CAC40'),
    # 亚洲
    ('日经225',    None,       '日经225'),
    ('韩国KOSPI',  None,       '韩国综合指数'),
    ('印度SENSEX', None,       '印度孟买SENSEX30'),
]

_TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q='
_TENCENT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
}

_EASTMONEY_GLOBAL_PATH = '/api/qt/clist/get'
_EASTMONEY_GLOBAL_PARAMS = {
    'np': '2', 'fltt': '1', 'invt': '2',
    'fs': (
        'i:100.HSI,i:100.HSCEI,i:124.HSCCI,i:100.N225,'
        'i:100.FTSE,i:100.FCHI,i:100.GDAXI,i:100.DJIA,'
        'i:100.SPX,i:100.NDX,i:100.KS11,i:105.SENSEX'
    ),
    'fields': 'f12,f13,f14,f2,f3,f124',
    'fid': 'f3', 'pn': '1', 'pz': '200', 'po': '1',
    'dect': '1', 'wbp2u': '|0|0|0|web',
}
_SINA_GLOBAL_URL = 'https://hq.sinajs.cn/list='
_SINA_GLOBAL_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://finance.sina.com.cn/',
}
# disp_name → 新浪 gb_$ 代码（仅对腾讯没有的指数）
_SINA_GLOBAL_CODES = {
    '德国DAX':    'gb_$GDAXI',
    '法国CAC':    'gb_$FCHI',
    '日经225':    'gb_$N225',
    '韩国KOSPI':  'gb_$KS11',
    '印度SENSEX': 'gb_$SENSEX',
}

_EASTMONEY_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://quote.eastmoney.com/',
}


def _fetch_tencent_global():
    """通过腾讯行情拿可用的全球指数。返回 dict[tencent_code -> (price, pct, ts_str)]。"""
    tc_codes = [tc for _, tc, _ in _GLOBAL_INDICES if tc]
    if not tc_codes:
        return {}
    codes_str = ','.join(tc_codes)
    r = requests.get(
        _TENCENT_QUOTE_URL + codes_str,
        headers=_TENCENT_HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    text = r.content.decode('gbk', 'replace')

    result = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        var_name = line.split('=')[0]          # v_us.DJI
        val = line.split('=', 1)[1].strip('"')
        fields = val.split('~')
        if len(fields) < 33:
            continue
        tc_code = var_name.replace('v_', '')    # us.DJI
        try:
            price = float(fields[3])
            pct = float(fields[32])
        except (ValueError, TypeError, IndexError):
            continue
        # 时间戳: fields[30] 格式 "2026-05-08 12:51:40" 或 "2026/05/08 18:31:01"
        ts = ''
        raw_ts = fields[30] if len(fields) > 30 else ''
        if raw_ts:
            ts = raw_ts.replace('/', '-')
        result[tc_code] = (price, pct, ts)
    return result


def _fetch_eastmoney_global():
    """通过东财 push2 拿全球指数。返回 dict[f14_name -> (price, pct, ts_str)]。"""
    r = push2_get(
        _EASTMONEY_GLOBAL_PATH,
        params=_EASTMONEY_GLOBAL_PARAMS,
        headers=_EASTMONEY_HEADERS,
        timeout=12,
    )
    r.raise_for_status()
    data = r.json()
    diff = data.get('data', {}).get('diff', {})

    result = {}
    for item in (diff.values() if isinstance(diff, dict) else diff):
        name = item.get('f14', '')
        try:
            price = float(item['f2']) / 100
            pct = float(item['f3']) / 100
        except (ValueError, TypeError, KeyError):
            continue
        ts = ''
        try:
            ts_epoch = int(item.get('f124', 0))
            if ts_epoch > 0:
                dt = datetime.fromtimestamp(
                    ts_epoch, tz=timezone(timedelta(hours=8)),
                )
                ts = dt.strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError, OSError):
            pass
        result[name] = (price, pct, ts)
    return result


def _fetch_sina_global():
    """新浪 hq.sinajs.cn gb_$ 海外指数兜底（仅补腾讯无法覆盖的德/法/日/韩/印）。
    返回 dict[disp_name -> (price, pct, ts_str)]。
    格式: var hq_str_gb_$GDAXI="德国DAX指数,price,prev,pct,chg,high,low,time,date";
    """
    codes_str = ','.join(_SINA_GLOBAL_CODES.values())
    r = requests.get(
        _SINA_GLOBAL_URL + codes_str,
        headers=_SINA_GLOBAL_HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    text = r.content.decode('gbk', 'replace')
    rev = {v: k for k, v in _SINA_GLOBAL_CODES.items()}
    result = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or 'hq_str_' not in line:
            continue
        try:
            var_part = line.split('=')[0]          # var hq_str_gb_$GDAXI
            sina_code = var_part.split('hq_str_')[1].strip()
            disp = rev.get(sina_code)
            if not disp:
                continue
            val = line.split('=', 1)[1].strip().strip('"')
            if not val:
                continue
            fields = val.split(',')
            if len(fields) < 4:
                continue
            price = float(fields[1])
            pct   = float(fields[3])
            ts = ''
            if len(fields) >= 9:
                date_str = fields[8].strip()   # MM/DD/YYYY
                time_str = fields[7].strip()   # HH:MM:SS
                m, d, y = date_str.split('/')
                ts = f"{y}-{m}-{d} {time_str}"
            result[disp] = (price, pct, ts)
        except Exception:
            continue
    return result


def _fetch_global_snapshot():
    """全球指数快照。返回 list[(display_name, price, pct, ts_str)]，缺失填 None。"""
    # 优先东财 push2（覆盖全部12个指数）
    em_data = {}
    try:
        em_data = _fetch_eastmoney_global()
    except Exception:
        pass

    # 腾讯回退（push2 不可用或缺失时补充，覆盖美股3+港股3+英国富时）
    tc_data = {}
    need_tc = any(
        em is None or em not in em_data
        for _, tc, em in _GLOBAL_INDICES if tc
    )
    if need_tc or not em_data:
        try:
            tc_data = _fetch_tencent_global()
        except Exception:
            pass

    # 新浪兜底（补德/法/日/韩/印，仅当 push2 取不到时）
    sina_data = {}
    need_sina = any(
        tc is None and (em is None or em not in em_data)
        for _, tc, em in _GLOBAL_INDICES
    )
    if need_sina:
        try:
            sina_data = _fetch_sina_global()
        except Exception:
            pass

    out = []
    for disp, tc_code, em_name in _GLOBAL_INDICES:
        # 先查东财
        if em_name and em_name in em_data:
            price, pct, ts = em_data[em_name]
            out.append((disp, price, pct, ts))
            continue
        # 再查腾讯
        if tc_code and tc_code in tc_data:
            price, pct, ts = tc_data[tc_code]
            out.append((disp, price, pct, ts))
            continue
        # 最后查新浪
        if disp in sina_data:
            price, pct, ts = sina_data[disp]
            out.append((disp, price, pct, ts))
            continue
        out.append((disp, None, None, None))
    return out


# ---------- 单个全球指数卡片（含独立时间戳） ----------
class GlobalIndexTile(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.Box)
        self.setObjectName('globalTile')
        self.setStyleSheet(
            f"QFrame#globalTile {{ background-color: {DARK_BG}; border-radius: 8px;"
            f" border: 1px solid #1e3060; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_lbl = QLabel('—')
        self.name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_lbl.setStyleSheet(f"color: {MUTED}; font-size: 13px;")

        self.price_lbl = QLabel('—')
        self.price_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.price_lbl.setStyleSheet("color: #ffffff; font-size: 24px; font-weight: bold;")

        self.pct_lbl = QLabel('—')
        self.pct_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pct_lbl.setStyleSheet(f"color: {MUTED}; font-size: 15px; font-weight: bold;")

        self.ts_lbl = QLabel('—')
        self.ts_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ts_lbl.setStyleSheet("color: #777; font-size: 10px;")

        layout.addWidget(self.name_lbl)
        layout.addWidget(self.price_lbl)
        layout.addWidget(self.pct_lbl)
        layout.addWidget(self.ts_lbl)

    def update_data(self, name, price, pct, ts):
        self.name_lbl.setText(name)
        if price is None or pct is None:
            self.price_lbl.setText('—')
            self.pct_lbl.setText('—')
            self.ts_lbl.setText('数据不可用')
            self.price_lbl.setStyleSheet("color: #888; font-size: 24px; font-weight: bold;")
            self.pct_lbl.setStyleSheet(f"color: {MUTED}; font-size: 15px; font-weight: bold;")
            return
        self.price_lbl.setText(f"{price:,.3f}")
        sign = '+' if pct >= 0 else ''
        self.pct_lbl.setText(f"{sign}{pct:.2f}%")
        # A股惯例：涨红跌绿
        color = RED if pct > 0 else (GREEN if pct < 0 else MUTED)
        self.price_lbl.setStyleSheet(f"color: {color}; font-size: 24px; font-weight: bold;")
        self.pct_lbl.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold;")
        # 时间戳格式化：YYYY-MM-DD HH:MM:SS → MM-DD HH:MM
        try:
            dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
            self.ts_lbl.setText(dt.strftime('%m-%d %H:%M'))
        except (ValueError, TypeError):
            self.ts_lbl.setText(ts[:16] if ts else '—')


# ---------- 主面板 ----------
class GlobalPanel(QWidget):
    """全球主要指数面板。"""

    status_changed = Signal(str, str)
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self._last_items = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # 顶部标题（区域分组提示）
        title_lbl = QLabel('🌍 全球主要指数（美股 / 港股 / 欧洲 / 亚洲）')
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet("color: #ffffff; font-size: 16px; font-weight: bold;")
        root.addWidget(title_lbl)

        # 分区显示（美股 / 港股 / 欧洲 / 亚洲），每区 1 行 3 张
        _REGION_LABELS = ['美股', '港股', '欧洲', '亚洲']
        self.tiles = []
        for g, region in enumerate(_REGION_LABELS):
            hdr_w = QWidget()
            hdr_lay = QHBoxLayout(hdr_w)
            hdr_lay.setContentsMargins(4, 8, 4, 2)
            hdr_lay.setSpacing(8)
            hdr_lbl = QLabel(region)
            hdr_lbl.setStyleSheet(
                'color: #607090; font-size: 11px; font-weight: bold;'
            )
            hdr_line = QFrame()
            hdr_line.setFrameShape(QFrame.Shape.HLine)
            hdr_line.setFixedHeight(1)
            hdr_line.setStyleSheet('background-color: #1e3060; border: none;')
            hdr_lay.addWidget(hdr_lbl)
            hdr_lay.addWidget(hdr_line, stretch=1)
            root.addWidget(hdr_w)
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(10)
            for j in range(3):
                idx = g * 3 + j
                disp = _GLOBAL_INDICES[idx][0]
                tile = GlobalIndexTile()
                tile.name_lbl.setText(disp)
                row_lay.addWidget(tile, stretch=1)
                self.tiles.append(tile)
            root.addWidget(row_w, stretch=1)

        # 底部元信息
        self.meta_lbl = QLabel('等待首次刷新…')
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meta_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.meta_lbl)

        self.note_lbl = QLabel(
            '注：非A股交易时段显示最近收盘价，各市场时区不同；具体时间见每张卡片'
        )
        self.note_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note_lbl.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(self.note_lbl)

        self.setStyleSheet(f"background-color: {DARK_BG};")
        cached = load_panel_cache('global_snapshot')
        if cached and cached.get('items'):
            self._last_items = cached.get('items')
            self._render_items(self._last_items, cached.get('fetch_time'), frozen=True)

    # ---- 公共 API ----
    def refresh(self):
        self._fetch()

    def auto_refresh(self):
        # 自动刷新仅A股交易时段（避免深夜频繁请求）；手动刷新不受限制
        if is_trading_time():
            self._fetch()

    def current_point_count(self):
        return 0

    # ---- 内部 ----
    def _fetch(self):
        if self.worker and self.worker.isRunning():
            return
        self.status_changed.emit('loading', '正在获取全球指数...')
        self.worker = DataWorker(fetcher=_fetch_global_snapshot)
        self.worker.data_ready.connect(self._on_data_ready)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.start()

    def _on_data_ready(self, items):
        now_t = datetime.now().strftime("%H:%M:%S")
        self._last_items = items
        save_panel_cache('global_snapshot', {
            'fetch_time': now_t,
            'items': items,
        })
        self._render_items(items, now_t)

    def _render_items(self, items, fetch_time=None, frozen=False):
        for tile, (name, price, pct, ts) in zip(self.tiles, items):
            tile.update_data(name, price, pct, ts)

        now = datetime.now()
        now_t = fetch_time or now.strftime("%H:%M:%S")
        frozen_text = ' [已冻结]' if frozen else ''
        self.meta_lbl.setText(
            f"抓取时间: {now.strftime('%Y-%m-%d')} {now_t} | 数据源: 东财行情+腾讯回退{frozen_text}"
        )
        self.status_changed.emit('success', f"上次刷新: {now_t}{frozen_text}")
        self.bottom_status_changed.emit(
            f"全球指数已更新 {now_t} | 共 {len([t for t in items if t[1] is not None])}/{len(items)} 个有效{frozen_text}"
        )

    def _on_error(self, msg):
        if self._last_items:
            self._render_items(self._last_items, frozen=True)
            self.status_changed.emit('error', f"获取失败，显示缓存: {msg}")
            self.bottom_status_changed.emit(f"全球指数获取失败，显示冻结数据: {msg}")
            return
        self.status_changed.emit('error', '获取失败')
        self.bottom_status_changed.emit(f"错误: {msg}")
