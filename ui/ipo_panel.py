"""新股雷达面板：即将上市列表 + 近期上市首日监控。

数据源：
  - 新股列表：akshare stock_xgsglb_em / stock_new_ipo_cninfo（不走 push2，不含 py_mini_racer）
  - 实时行情：腾讯 qt.gtimg.cn
"""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import requests

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QSplitter,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush

from core.constants import DARK_BG, CHART_BG, MUTED, RED, GREEN
from core.data_worker import DataWorker, is_trading_time
from core.cache import save_panel_cache, load_panel_cache
from ui.widgets.table_utils import configure_no_truncation


_UPCOMING_HEADERS = ['股票代码', '股票名称', '发行价', '上市日期', '申购代码', '中签号公布日', '流通盘(亿股)', '所属行业', '中签率(%)', '发行PE']
_PURCHASABLE_HEADERS = ['股票代码', '股票简称', '申购代码', '申购日期', '发行价', '申购上限(万)', '中签号公布日', '上市日期']
_RECENT_HEADERS = ['代码', '名称', '发行价', '最新价', '较发行价涨幅(%)', '今日涨跌幅(%)', '封单量(万手)', '换手率(%)', '开板状态', '开板时间', '成交额(亿)']

_TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q='
_TENCENT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}
from core.paths import CACHE_DIR as _BASE_CACHE
_OPEN_TIME_CACHE_PATH = _BASE_CACHE / 'ipo_open_time.json'


def _load_open_time_cache():
    """加载开板时间持久化缓存。"""
    try:
        if _OPEN_TIME_CACHE_PATH.exists():
            return json.loads(_OPEN_TIME_CACHE_PATH.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_open_time_entry(code, date_str, time_str):
    """记录一条开板时间并持久化。"""
    data = _load_open_time_cache()
    data[f'{code}_{date_str}'] = time_str
    try:
        _OPEN_TIME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OPEN_TIME_CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False), encoding='utf-8'
        )
    except Exception:
        pass


def _code_to_tencent(code):
    code = str(code).strip()
    if code.startswith(('sh', 'sz', 'bj')):
        return code
    if code.startswith('92'):
        return f'bj{code}'
    if code.startswith(('6', '9')):
        return f'sh{code}'
    if code.startswith(('4', '8')):
        return f'bj{code}'
    return f'sz{code}'


def _parse_date(s):
    """解析多种日期格式，返回 date 或 None。"""
    s = str(s).strip().replace('/', '-')
    if not s or s.lower() in ('none', 'nat', 'nan', ''):
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(s[:8], '%Y%m%d').date()
    except (ValueError, TypeError):
        pass
    return None


def _safe_float(val):
    """安全转换为 float，NaN/None 返回 0.0。"""
    try:
        v = float(val)
        return 0.0 if math.isnan(v) else v
    except (ValueError, TypeError):
        return 0.0


def _fmt_date(s):
    """格式化日期字符串用于显示。"""
    d = _parse_date(s)
    if d is None:
        return '待定'
    return d.strftime('%Y-%m-%d')


def _fetch_upcoming_data():
    """获取新股申购列表。优先 stock_xgsglb_em，回退 stock_new_ipo_cninfo。"""
    today = datetime.now().date()
    cutoff = today - timedelta(days=90)

    try:
        df = ak.stock_xgsglb_em()
        records = []
        for _, row in df.iterrows():
            code = str(row.get('股票代码', '')).strip()
            if not code or not code.isdigit() or len(code) != 6:
                continue
            list_str = str(row.get('上市日期', '')).strip()
            sub_str = str(row.get('申购日期', '')).strip()
            list_dt = _parse_date(list_str)
            sub_dt = _parse_date(sub_str)
            is_upcoming = list_dt is None or list_dt > today
            is_recent = (list_dt and list_dt >= cutoff) or (sub_dt and sub_dt >= cutoff)
            if not (is_upcoming or is_recent):
                continue
            records.append({
                'code': code,
                'name': str(row.get('股票简称', '')).strip(),
                'issue_price': _safe_float(row.get('发行价格')),
                'list_date': _fmt_date(list_str),
                'sub_code': str(row.get('申购代码', '')).strip(),
                'sub_date': _fmt_date(sub_str),
                'sub_limit': _safe_float(row.get('申购上限', row.get('顶格申购需配市值', 0))),
                'lottery_date': _fmt_date(row.get('中签号公布日')),
                'issue_pe': _safe_float(row.get('发行市盈率')),
                'sub_rate': _safe_float(row.get('网上中签率')),
            })
        records.sort(key=lambda r: r['list_date'] if r['list_date'] != '待定' else '0000-00-00')
        return records
    except Exception:
        pass

    # 回退 cninfo
    df = ak.stock_new_ipo_cninfo()
    records = []
    for _, row in df.iterrows():
        code = str(row.get('证劵代码', '')).strip()
        if not code or not code.isdigit() or len(code) != 6:
            continue
        list_str = str(row.get('上市日期', '')).strip()
        sub_str = str(row.get('申购日期', '')).strip()
        list_dt = _parse_date(list_str)
        sub_dt = _parse_date(sub_str)
        is_upcoming = list_dt is None or list_dt > today
        is_recent = (list_dt and list_dt >= cutoff) or (sub_dt and sub_dt >= cutoff)
        if not (is_upcoming or is_recent):
            continue
        records.append({
            'code': code,
            'name': str(row.get('证券简称', '')).strip(),
            'issue_price': _safe_float(row.get('发行价')),
            'list_date': _fmt_date(list_str),
            'sub_code': _fmt_date(sub_str),
            'lottery_date': _fmt_date(row.get('中签公告日')),
            'issue_pe': _safe_float(row.get('发行市盈率')),
            'sub_rate': _safe_float(row.get('中签率')),
        })
    records.sort(key=lambda r: r['list_date'] if r['list_date'] != '待定' else '0000-00-00')
    return records


def _fetch_recent_quotes(stocks):
    """从腾讯获取近期新股实时行情（含封单量），并附加东财换手率。"""
    if not stocks:
        return []
    tc_codes = ','.join(_code_to_tencent(s['code']) for s in stocks)
    r = requests.get(
        _TENCENT_QUOTE_URL + tc_codes,
        headers=_TENCENT_HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    text = r.content.decode('gbk', 'replace')

    quotes = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        val = line.split('=', 1)[1].strip('"')
        fields = val.split('~')
        if len(fields) < 33:
            continue
        code = fields[2].strip()
        price = _safe_float(fields[3])
        pct = _safe_float(fields[32])
        amount_wan = _safe_float(fields[37]) if len(fields) > 37 else 0.0
        # 腾讯 fields[38] = 换手率(%)，避免额外 push2 依赖
        turnover_pct = _safe_float(fields[38]) if len(fields) > 38 else 0.0
        ask1_qty = _safe_float(fields[46]) if len(fields) > 46 else 0.0
        quotes[code] = {
            'price': price, 'pct': pct,
            'amount_wan': amount_wan, 'name': fields[1],
            'ask1_qty': ask1_qty,
            'turnover': turnover_pct,
        }

    today = datetime.now().date()
    rows = []
    for s in stocks:
        code = s['code']
        q = quotes.get(code, {})
        issue_price = s.get('issue_price') or 0.0
        current_price = q.get('price', 0.0)
        list_dt = _parse_date(s.get('list_date', ''))
        gain_vs_issue = 0.0
        if issue_price > 0 and current_price > 0:
            gain_vs_issue = (current_price - issue_price) / issue_price * 100
        today_pct = q.get('pct', 0.0)
        amount_yi = q.get('amount_wan', 0.0) / 10000.0

        # 状态判定
        if current_price <= 0:
            if list_dt and list_dt > today:
                status = 'not_listed'
            elif list_dt and list_dt == today:
                status = 'not_opened'
            else:
                status = 'suspended'
        elif abs(today_pct) >= 9.9:
            status = 'limit_up'
        else:
            status = 'opened'

        is_today = (list_dt == today) if list_dt else False
        ask1_qty = q.get('ask1_qty', 0.0)
        ask1_wan = ask1_qty / 10000.0 if status == 'limit_up' and ask1_qty > 0 else 0.0

        rows.append({
            'code': code,
            'name': q.get('name') or s.get('name', ''),
            'issue_price': issue_price,
            'price': current_price,
            'gain_vs_issue': gain_vs_issue,
            'today_pct': today_pct,
            'status': status,
            'is_today': is_today,
            'amount_yi': amount_yi,
            'ask1_wan': ask1_wan,
            'turnover': q.get('turnover') if q.get('turnover') else None,
        })
    return rows


# ---- 个股详情缓存（流通盘 / 行业）----

_DETAIL_CACHE_VALID_SECS = 86400  # 1 天


def _ipo_detail_cache_path(code):
    return _BASE_CACHE / f'ipo_stock_info_{code}.json'


def _load_ipo_detail_cache(code):
    p = _ipo_detail_cache_path(code)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            if time.time() - data.get('_ts', 0) < _DETAIL_CACHE_VALID_SECS:
                return data
    except Exception:
        pass
    return None


def _save_ipo_detail_cache(code, data):
    p = _ipo_detail_cache_path(code)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload['_ts'] = time.time()
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def _fetch_single_stock_detail(code):
    """获取单只股票的流通盘/行业，优先读1天缓存，失败返回空 dict。"""
    cached = _load_ipo_detail_cache(code)
    if cached:
        return code, cached
    info = {}
    try:
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            cols = df.columns.tolist()
            key_col, val_col = cols[0], cols[1]
            kv = dict(zip(df[key_col].astype(str), df[val_col].astype(str)))
            for key in ('流通A股', '流通股本', '流通股份'):
                raw = kv.get(key, '').replace(',', '').strip()
                if raw and raw not in ('—', '-', '', 'None', 'nan'):
                    try:
                        if '亿' in raw:
                            info['float_shares'] = _safe_float(raw.replace('亿', ''))
                        elif '万' in raw:
                            info['float_shares'] = _safe_float(raw.replace('万', '')) / 10000
                        else:
                            info['float_shares'] = _safe_float(raw) / 10000
                    except Exception:
                        pass
                    break
            for key in ('行业', '所属行业', '所属板块'):
                val = kv.get(key, '').strip()
                if val and val not in ('—', '-', '', 'None', 'nan'):
                    info['industry'] = val
                    break
    except Exception:
        pass
    _save_ipo_detail_cache(code, info)
    return code, info


def _fetch_industry_pe_map():
    """获取行业市盈率映射 {行业名: PE}，失败返回空字典。"""
    try:
        df = ak.stock_sector_pe_ratio_em(symbol='行业板块')
        if df is not None and not df.empty:
            name_col = pe_col = None
            for col in df.columns:
                c = str(col)
                if '行业' in c or '板块' in c or '名称' in c:
                    name_col = col
                if '市盈' in c or ('P' in c and 'E' in c):
                    pe_col = col
            if name_col and pe_col:
                return {
                    str(row[name_col]).strip(): _safe_float(row[pe_col])
                    for _, row in df.iterrows()
                    if _safe_float(row[pe_col]) > 0
                }
    except Exception:
        pass
    return {}


def _fetch_upcoming_details(records):
    """后台并发（最多4线程）获取每只新股的流通盘/行业/行业PE。"""
    if not records:
        return records
    industry_pe_map = _fetch_industry_pe_map()
    codes = [r['code'] for r in records]
    details = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_single_stock_detail, c): c for c in codes}
        for future in as_completed(futures):
            try:
                code, info = future.result()
                details[code] = info
            except Exception:
                pass
    enriched = []
    for rec in records:
        r = dict(rec)
        info = details.get(rec['code'], {})
        r['float_shares'] = info.get('float_shares')
        r['industry'] = info.get('industry', '')
        r['industry_pe'] = industry_pe_map.get(r.get('industry', ''))
        enriched.append(r)
    return enriched


class IpoPanel(QWidget):
    """新股雷达面板。"""

    status_changed = Signal(str, str)
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker_upcoming = None
        self.worker_recent = None
        self.worker_detail = None
        self._upcoming_records = []
        self._recent_stocks = []
        self._last_recent_rows = None
        self._prev_pct = {}
        self._open_times = {}
        self._last_full_fetch = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # 顶部标题
        title = QLabel('🆕 新股雷达')
        title.setStyleSheet('color: #ffffff; font-size: 18px; font-weight: bold;')
        root.addWidget(title)

        # ---- 顶区：今日 / 近期可申购（P0-C）----
        purchasable_widget = QWidget()
        purchasable_layout = QVBoxLayout(purchasable_widget)
        purchasable_layout.setContentsMargins(0, 0, 0, 0)
        purchasable_layout.setSpacing(4)
        self._purchasable_label = QLabel('今日可申购')
        self._purchasable_label.setStyleSheet(
            f'color: #ffcc66; font-size: 13px; font-weight: bold;'
        )
        purchasable_layout.addWidget(self._purchasable_label)
        self.table_purchasable = self._make_table(_PURCHASABLE_HEADERS)
        self.table_purchasable.setMaximumHeight(130)
        purchasable_layout.addWidget(self.table_purchasable)
        root.addWidget(purchasable_widget)

        # 分割器：上区（即将上市） + 下区（近期监控）
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setStyleSheet("QSplitter::handle { background-color: #333; height: 3px; }")

        # ---- 上区：即将上市列表 ----
        upper_widget = QWidget()
        upper_layout = QVBoxLayout(upper_widget)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(4)

        upper_label = QLabel('即将上市')
        upper_label.setStyleSheet(f'color: {MUTED}; font-size: 13px; font-weight: bold;')
        upper_layout.addWidget(upper_label)

        self.table_upcoming = self._make_table(_UPCOMING_HEADERS)
        upper_layout.addWidget(self.table_upcoming)
        splitter.addWidget(upper_widget)

        # ---- 下区：近期上市首日监控 ----
        lower_widget = QWidget()
        lower_layout = QVBoxLayout(lower_widget)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(4)

        lower_label = QLabel(
            f'<span style="color:{MUTED}; font-size:13px; font-weight:bold;">'
            f'近期上市首日监控（最近10个交易日）</span>'
            f'&nbsp;&nbsp;<span style="color:#ffcc66; font-size:11px;">■ 今日上市</span>'
            f'&nbsp;&nbsp;<span style="color:#ffaa00; font-size:11px;">○ 未开盘</span>'
            f'&nbsp;&nbsp;<span style="color:{MUTED}; font-size:11px;">○ 停牌</span>'
        )
        lower_label.setTextFormat(Qt.TextFormat.RichText)
        lower_layout.addWidget(lower_label)

        self.table_recent = self._make_table(_RECENT_HEADERS)
        lower_layout.addWidget(self.table_recent)
        splitter.addWidget(lower_widget)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, stretch=1)

        # 底部状态
        self.meta_lbl = QLabel('等待首次刷新…')
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meta_lbl.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
        root.addWidget(self.meta_lbl)

        self.setStyleSheet(f'background-color: {DARK_BG};')
        self._load_all_cache()

    # ---- 公共 API ----

    def refresh(self):
        """切到本页触发：即将上市列表变化慢，5 分钟内重复进入只刷新实时行情，
        避免每次切 tab 都重打 akshare 列表 + 逐只详情导致主线程卡顿。"""
        if self._upcoming_records and (time.time() - self._last_full_fetch) < 300:
            self._identify_recent_stocks()
            self._fetch_recent()
            return
        self._last_full_fetch = time.time()
        self._fetch_upcoming()

    def auto_refresh(self):
        """自动刷新：交易时段仅更新近期行情部分。"""
        if is_trading_time():
            self._fetch_recent()

    def current_point_count(self):
        return self.table_upcoming.rowCount()

    # ---- 内部 ----

    def _make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        configure_no_truncation(table)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setStyleSheet(f'''
            QTableWidget {{
                background-color: {CHART_BG};
                alternate-background-color: #1d2d4a;
                color: #dddddd;
                border: none;
                border-radius: 8px;
                font-size: 12px;
                selection-background-color: #2a3a5e;
            }}
            QHeaderView::section {{
                background-color: #111a30;
                color: #aaaaaa;
                border: none;
                padding: 8px 6px;
                font-weight: bold;
            }}
            QTableWidget::item {{
                padding: 6px 4px;
                border: none;
            }}
        ''')
        h = table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def _load_all_cache(self):
        self._open_times = _load_open_time_cache()
        # 上区缓存
        cached_upcoming = load_panel_cache('ipo_upcoming')
        if cached_upcoming and cached_upcoming.get('records'):
            self._upcoming_records = cached_upcoming['records']
            self._fill_upcoming_table(self._upcoming_records)
            self._fill_purchasable_table(self._upcoming_records)
            self._identify_recent_stocks()

        # 下区缓存
        cached_recent = load_panel_cache('ipo_recent')
        if cached_recent and cached_recent.get('rows'):
            self._last_recent_rows = cached_recent['rows']
            self._fill_recent_table(cached_recent['rows'])
            ft = cached_recent.get('fetch_time', '')
            self.meta_lbl.setText(f"新股数据 [缓存] | 上次刷新: {ft}")

    # ---- 上区：即将上市 ----

    def _fetch_upcoming(self):
        if self.worker_upcoming and self.worker_upcoming.isRunning():
            return
        self.status_changed.emit('loading', '正在获取新股列表...')
        self.worker_upcoming = DataWorker(fetcher=_fetch_upcoming_data)
        self.worker_upcoming.data_ready.connect(self._on_upcoming_ready)
        self.worker_upcoming.error_occurred.connect(self._on_upcoming_error)
        self.worker_upcoming.start()

    def _on_upcoming_ready(self, records):
        self._upcoming_records = records
        save_panel_cache('ipo_upcoming', {
            'records': records,
            'fetch_time': datetime.now().strftime('%H:%M:%S'),
        })
        self._fill_upcoming_table(records)
        self._fill_purchasable_table(records)
        self._identify_recent_stocks()
        self._fetch_recent()
        self._fetch_details(records)
        self.point_count_changed.emit(len(records))
        now_t = datetime.now().strftime('%H:%M:%S')
        self.status_changed.emit('success', f'上次刷新: {now_t}')

    def _on_upcoming_error(self, msg):
        # 回退缓存
        if self._upcoming_records:
            self.bottom_status_changed.emit(f'新股列表获取失败，显示缓存: {msg}')
            self.status_changed.emit('success', '上次刷新: [缓存]')
        else:
            cached = load_panel_cache('ipo_upcoming')
            if cached and cached.get('records'):
                self._upcoming_records = cached['records']
                self._fill_upcoming_table(cached['records'])
                self._identify_recent_stocks()
                self.bottom_status_changed.emit(f'新股列表获取失败，已回退缓存: {msg}')
            else:
                self.status_changed.emit('error', '获取失败')
                self.bottom_status_changed.emit(f'新股雷达错误: {msg}')

    def _fetch_details(self, records):
        if self.worker_detail and self.worker_detail.isRunning():
            return
        recs = list(records)
        self.worker_detail = DataWorker(fetcher=lambda: _fetch_upcoming_details(recs))
        self.worker_detail.data_ready.connect(self._on_details_ready)
        self.worker_detail.error_occurred.connect(self._on_details_error)
        self.worker_detail.start()

    def _on_details_ready(self, enriched):
        self._upcoming_records = enriched
        save_panel_cache('ipo_upcoming', {
            'records': enriched,
            'fetch_time': datetime.now().strftime('%H:%M:%S'),
        })
        self._fill_upcoming_table(enriched)

    def _on_details_error(self, msg):
        self.bottom_status_changed.emit(f'流通盘/行业获取失败（已显示基础数据）: {msg}')

    def _fill_purchasable_table(self, records):
        """顶区：今日可申购（申购日期 == 今日）；无数据时显示最近3日内可申购。"""
        today = datetime.now().date()
        purchasable = [r for r in records if _parse_date(r.get('sub_date', '')) == today]
        if not purchasable:
            recent_cutoff = today - timedelta(days=3)
            purchasable = [
                r for r in records
                if _parse_date(r.get('sub_date', '')) and
                   recent_cutoff <= _parse_date(r.get('sub_date', '')) <= today
            ]
        purchasable.sort(
            key=lambda r: r.get('sub_date', '') or '0000-00-00'
        )
        count = len(purchasable)
        if count:
            self._purchasable_label.setText(
                f'今日可申购 ({count} 只)  '
                f'<span style="color:#aaa;font-size:11px;">申购日期=今日高亮</span>'
            )
            self._purchasable_label.setTextFormat(Qt.TextFormat.RichText)
        else:
            self._purchasable_label.setText('今日无可申购新股')

        self.table_purchasable.setRowCount(len(purchasable))
        for r, rec in enumerate(purchasable):
            is_today_sub = (_parse_date(rec.get('sub_date', '')) == today)
            sub_limit = rec.get('sub_limit', 0)
            values = [
                rec.get('code', ''),
                rec.get('name', ''),
                rec.get('sub_code', ''),
                rec.get('sub_date', '') or '待定',
                f"{rec['issue_price']:.3f}" if rec.get('issue_price') else '—',
                f'{sub_limit:.0f}' if sub_limit > 0 else '—',
                rec.get('lottery_date', '') or '待定',
                rec.get('list_date', '') or '待定',
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if is_today_sub:
                    item.setBackground(QBrush(QColor('#3d2c1a')))
                    item.setForeground(QBrush(QColor('#ffcc66')))
                self.table_purchasable.setItem(r, c, item)
        # 无数据时收起表格高度，有数据时展示
        self.table_purchasable.setVisible(bool(purchasable))

    def _fill_upcoming_table(self, records):
        today = datetime.now().date()
        self.table_upcoming.setRowCount(len(records))
        for r, rec in enumerate(records):
            float_shares = rec.get('float_shares')
            industry = rec.get('industry') or '—'
            sub_rate = rec.get('sub_rate', 0.0)
            issue_pe = rec.get('issue_pe', 0.0)
            industry_pe = rec.get('industry_pe')

            if issue_pe > 0:
                pe_text = f'{issue_pe:.1f}倍'
                if industry_pe and industry_pe > 0:
                    diff = issue_pe - industry_pe
                    sign = '+' if diff >= 0 else ''
                    pe_text = f'{issue_pe:.1f}倍 ({sign}{diff:.0f})'
            else:
                pe_text = '—'

            values = [
                rec.get('code', ''),
                rec.get('name', ''),
                f"{rec['issue_price']:.3f}" if rec.get('issue_price') else '—',
                rec.get('list_date', ''),
                rec.get('sub_code', ''),
                rec.get('lottery_date', ''),
                f'{float_shares:.2f}' if float_shares is not None else '—',
                industry,
                f'{sub_rate:.4f}' if sub_rate > 0 else '—',
                pe_text,
            ]
            list_dt = _parse_date(rec.get('list_date', ''))
            within_7d = False
            if list_dt:
                delta = (list_dt - today).days
                within_7d = -2 <= delta <= 7

            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if within_7d:
                    item.setBackground(QBrush(QColor('#3d2c1a')))
                    item.setForeground(QBrush(QColor('#ffcc66')))
                else:
                    if c == 6 and float_shares is not None:
                        if float_shares < 2:
                            item.setForeground(QBrush(QColor(RED)))
                        elif float_shares < 5:
                            item.setForeground(QBrush(QColor('#ff8800')))
                    elif c == 8 and sub_rate > 0:
                        if sub_rate < 0.005:
                            item.setForeground(QBrush(QColor(RED)))
                        elif sub_rate < 0.02:
                            item.setForeground(QBrush(QColor('#ff8800')))
                        elif sub_rate < 0.1:
                            item.setForeground(QBrush(QColor('#ffcc66')))
                self.table_upcoming.setItem(r, c, item)

    def _identify_recent_stocks(self):
        """从 upcoming_records 筛选最近14天内上市的股票（约10个交易日）。"""
        today = datetime.now().date()
        cutoff = today - timedelta(days=14)
        recent = []
        for rec in self._upcoming_records:
            list_dt = _parse_date(rec.get('list_date', ''))
            if list_dt and cutoff <= list_dt <= today:
                recent.append({
                    'code': rec['code'],
                    'name': rec.get('name', ''),
                    'issue_price': rec.get('issue_price', 0.0),
                    'list_date': rec.get('list_date', ''),
                })
        self._recent_stocks = recent

    # ---- 下区：近期上市首日监控 ----

    def _fetch_recent(self):
        if not self._recent_stocks:
            return
        if self.worker_recent and self.worker_recent.isRunning():
            return
        stocks = list(self._recent_stocks)
        self.worker_recent = DataWorker(fetcher=lambda: _fetch_recent_quotes(stocks))
        self.worker_recent.data_ready.connect(self._on_recent_ready)
        self.worker_recent.error_occurred.connect(self._on_recent_error)
        self.worker_recent.start()

    def _on_recent_ready(self, rows):
        today_str = datetime.now().strftime('%Y-%m-%d')
        now_hm = datetime.now().strftime('%H:%M')
        ac = getattr(self, 'alert_center', None)
        for row in rows:
            code = row['code']
            name = row.get('name', code)
            pct = row.get('today_pct', 0.0)
            prev = self._prev_pct.get(code)
            key = f'{code}_{today_str}'
            if prev is not None and prev >= 9.9 and pct < 9.9 and row.get('status') not in ('suspended', 'not_opened', 'not_listed'):
                # 炸板：涨停 → 开板（跳过停牌/未开盘/未上市，避免误报）
                if key not in self._open_times:
                    self._open_times[key] = now_hm
                    _save_open_time_entry(code, today_str, now_hm)
                if ac and ac.get_setting('ipo_explode_enabled', True):
                    ac.try_trigger(
                        event_type='ipo_explode',
                        title=f'💥 新股炸板: {name}',
                        message=f'{name}({code}) 涨停打开，当前 {pct:+.2f}%',
                        level='warning',
                        key=f'ipo_explode:{code}',
                        cooldown=300,
                    )
            elif prev is not None and prev < 9.9 and prev > 0 and pct >= 9.9:
                # 回封：开板 → 重新涨停
                if ac and ac.get_setting('ipo_reseal_enabled', True):
                    ac.try_trigger(
                        event_type='ipo_reseal',
                        title=f'🔒 新股回封: {name}',
                        message=f'{name}({code}) 重新封板，当前 {pct:+.2f}%',
                        level='warning',
                        key=f'ipo_reseal:{code}',
                        cooldown=300,
                    )
            self._prev_pct[code] = pct
        self._last_recent_rows = rows
        save_panel_cache('ipo_recent', {
            'rows': rows,
            'fetch_time': datetime.now().strftime('%H:%M:%S'),
        })
        self._fill_recent_table(rows)
        now_t = datetime.now().strftime('%H:%M:%S')
        self.meta_lbl.setText(
            f"新股 {len(self._upcoming_records)} 只 | 近期监控 {len(rows)} 只 | {now_t}"
        )
        self.bottom_status_changed.emit(f'新股雷达已更新 {now_t}')

    def _on_recent_error(self, msg):
        if self._last_recent_rows:
            self._fill_recent_table(self._last_recent_rows)
            self.bottom_status_changed.emit(f'近期行情获取失败，显示缓存: {msg}')
        else:
            cached = load_panel_cache('ipo_recent')
            if cached and cached.get('rows'):
                self._last_recent_rows = cached['rows']
                self._fill_recent_table(cached['rows'])
            else:
                self.bottom_status_changed.emit(f'近期行情错误: {msg}')

    _STATUS_LABEL = {
        'not_listed':  ('未上市', MUTED),
        'not_opened':  ('未开盘', '#ffaa00'),
        'suspended':   ('停牌',   MUTED),
        'limit_up':    ('封板中', RED),
        'opened':      ('已开板', MUTED),
    }

    def _fill_recent_table(self, rows):
        today_str = datetime.now().strftime('%Y-%m-%d')
        sorted_rows = sorted(rows, key=lambda r: (0 if r.get('is_today') else 1, r.get('code', '')))
        self.table_recent.setRowCount(len(sorted_rows))
        for r, row in enumerate(sorted_rows):
            gain_vs = row.get('gain_vs_issue', 0.0)
            today_pct = row.get('today_pct', 0.0)
            amount_yi = row.get('amount_yi', 0.0)
            is_today = row.get('is_today', False)
            has_price = row.get('price', 0) > 0
            ask1_wan = row.get('ask1_wan', 0.0)
            turnover = row.get('turnover')

            # 兼容旧缓存（无 status 字段）
            status = row.get('status', '')
            if not status:
                if not has_price:
                    status = 'not_opened'
                elif row.get('is_open', False):
                    status = 'opened'
                else:
                    status = 'limit_up'
            status_text, status_color = self._STATUS_LABEL.get(status, ('—', MUTED))

            # 最新价
            price_text = f"{row['price']:.3f}" if has_price else f'— {status_text}'

            # 封单量（万手）
            if status == 'limit_up' and ask1_wan > 0:
                if ask1_wan >= 10:
                    bid_text = f'{ask1_wan:.1f} 强封'
                elif ask1_wan < 3:
                    bid_text = f'{ask1_wan:.1f} 弱封'
                else:
                    bid_text = f'{ask1_wan:.1f}'
            else:
                bid_text = '—'

            # 换手率
            turnover_text = f'{turnover:.2f}' if turnover is not None else '—'

            # 开板时间
            key = f'{row.get("code", "")}_{today_str}'
            open_time = self._open_times.get(key)
            if status in ('not_listed', 'not_opened', 'suspended', 'limit_up'):
                open_time_text = '—'
            elif open_time:
                open_time_text = f'已开板 {open_time}'
            else:
                open_time_text = '已开板 (启动前)'

            values = [
                row.get('code', ''),
                row.get('name', ''),
                f"{row['issue_price']:.3f}" if row.get('issue_price') else '—',
                price_text,
                f"{'+' if gain_vs >= 0 else ''}{gain_vs:.2f}" if has_price else '—',
                f"{'+' if today_pct >= 0 else ''}{today_pct:.2f}" if has_price else '—',
                bid_text,
                turnover_text,
                status_text,
                open_time_text,
                f"{amount_yi:.2f}" if amount_yi > 0 else '—',
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if is_today:
                    item.setBackground(QBrush(QColor('#3d2c1a')))
                if c == 1:  # 名称
                    item.setForeground(QBrush(QColor('#ffcc66' if is_today else '#ffffff')))
                elif c == 3 and not has_price:  # 最新价无数据
                    item.setForeground(QBrush(QColor(status_color)))
                elif c == 4:  # 较发行价涨幅
                    item.setForeground(self._value_brush(gain_vs) if has_price else QBrush(QColor(MUTED)))
                elif c == 5:  # 今日涨跌幅
                    item.setForeground(self._value_brush(today_pct) if has_price else QBrush(QColor(MUTED)))
                elif c == 6 and ask1_wan > 0:  # 封单量
                    item.setForeground(QBrush(QColor(RED)))
                elif c == 8:  # 开板状态
                    item.setForeground(QBrush(QColor(status_color)))
                elif c == 9 and status == 'opened':  # 开板时间
                    item.setForeground(QBrush(QColor(GREEN)))
                self.table_recent.setItem(r, c, item)

    def _value_brush(self, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return QBrush(QColor(MUTED))
        if v > 0:
            return QBrush(QColor(RED))
        if v < 0:
            return QBrush(QColor(GREEN))
        return QBrush(QColor(MUTED))
