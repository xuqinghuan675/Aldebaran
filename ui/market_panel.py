"""大盘温度计面板：9 指数 3×3 网格 + 涨停/跌停/炸板 情绪三数。

差异化原则④：第一眼单一最关键信息。K 线在持仓面板独立查看。

数据源：
  - 指数行情：优先 akshare(push2)，push2 不可用时腾讯 qt.gtimg.cn 回退
  - akshare.stock_market_activity_legu()                     市场活跃度（与东财 App 一致）
  - akshare.stock_zt_pool_zbgc_em(date=YYYYMMDD)             炸板股池

重要说明（淘过的坑）：
  - stock_zt_pool_em 后台硬截断 100 行，涨停超 100 不准；不能用于计数
  - stock_zt_pool_dtgc_em 只返「连续跌停」子集，严重偏少；不能用于计数
  - legu 只返最近一个交易日数据，本身含「统计日期」字段
  - 炸板从 zbgc_em 拉 (真炸板未封回)，不是从涨停池「炸板次数>0」派生
"""
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests
import numpy as np

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from core.constants import DARK_BG, CHART_BG, MUTED, RED, GREEN, WIDGET_BG
from core.data_worker import DataWorker, is_trading_time
from core.board_rules import count_mainland_emotion_rows, same_source_mainland_emotion_count
from core.cache import (
    save_panel_cache, load_panel_cache,
    save_market_emotion_record, load_market_emotion_history,
)


# 关注的 9 个 A 股核心指数（3×3 网格，按行优先填充）
# 第一行：主板三大
# 第二行：宽基代表
# 第三行：风格/小盘
_INDEX_CODES = [
    ('000001', '上证指数'),
    ('399001', '深证成指'),
    ('399006', '创业板指'),
    ('000300', '沪深300'),
    ('000016', '上证50'),
    ('000905', '中证500'),
    ('000852', '中证1000'),
    ('000688', '科创50'),
    ('899050', '北证50'),
]


# 腾讯行情 API 的代码映射
_TENCENT_CODE_MAP = {
    '000001': 'sh000001',
    '399001': 'sz399001',
    '399006': 'sz399006',
    '000300': 'sh000300',
    '000016': 'sh000016',
    '000905': 'sh000905',
    '000852': 'sh000852',
    '000688': 'sh000688',
    '899050': 'bj899050',
}

_TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q='


def _fetch_indices_tencent():
    """通过腾讯行情接口获取 A 股指数实时行情（备选方案，push2 不可用时使用）。"""
    codes_str = ','.join(_TENCENT_CODE_MAP[code] for code, _ in _INDEX_CODES)
    r = requests.get(
        _TENCENT_QUOTE_URL + codes_str,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
        timeout=10,
    )
    r.raise_for_status()
    text = r.content.decode('gbk', 'replace')

    result_map = {}
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        var_name = line.split('=')[0]  # e.g. v_sh000001
        val = line.split('=', 1)[1].strip('"')
        fields = val.split('~')
        if len(fields) < 33:
            continue
        # fields: [1]=名称 [2]=代码 [3]=最新价 [4]=昨收 [32]=涨跌幅%
        tc_code = var_name.replace('v_', '')  # sh000001
        result_map[tc_code] = fields

    indices = []
    for code, name in _INDEX_CODES:
        tc_code = _TENCENT_CODE_MAP.get(code, '')
        fields = result_map.get(tc_code)
        if fields is None:
            indices.append((code, name, None, None))
        else:
            try:
                price = float(fields[3])
                pct = float(fields[32])
            except (ValueError, TypeError, IndexError):
                price, pct = None, None
            indices.append((code, name, price, pct))
    return indices


# 北向资金（沪深港通）数据接口——直接访问东财 datacenter REST，比 akshare 包装更稳定
_NORTH_URL = (
    'https://datacenter-web.eastmoney.com/api/data/v1/get'
    '?sortColumns=TRADE_DATE&sortTypes=-1'
    '&pageSize=1&pageNumber=1'
    '&reportName=RPT_MUTUAL_DEAL_HISTORY'
    '&columns=ALL&source=WEB&client=WEB'
    '&filter=(MUTUAL_TYPE%3D%22001%22)'
)


def _fetch_north_net():
    """北向资金当日净流入。直接请求东财 datacenter REST API。
    返回 (净额亿元, 日期字符串) 或 (None, None)。
    """
    try:
        r = requests.get(_NORTH_URL, timeout=8)
        r.raise_for_status()
        j = r.json()
        data = j.get('result', {}).get('data')
        if not data:
            return None, None
        row = data[0]
        trade_date = str(row.get('TRADE_DATE', ''))[:10]
        for key in ('NET_DEAL_AMT', 'NET_BUY_AMT', 'FUND_INFLOW'):
            val = row.get(key)
            if val is not None:
                net = float(val)
                if abs(net) > 1e9:
                    return net / 1e8, trade_date
                elif abs(net) > 1e5:
                    return net / 1e4, trade_date
                return net, trade_date
    except Exception:
        pass
    return None, None


def _fetch_lianban(ds):
    """连板数据：强势池优先，失败回退涨停池。

    返回 (max_height, board_count, detail)：
      max_height — 最高连板高度（None 表示完全无数据）
      board_count — 连板家数（连板数 >= 2）
      detail — [{'code','name','height'}, ...] 按连板数降序
    """
    df = None
    for fetch in (lambda: ak.stock_zt_pool_strong_em(date=ds),
                  lambda: ak.stock_zt_pool_em(date=ds)):
        try:
            cand = fetch()
        except Exception:
            cand = None
        if cand is not None and len(cand) > 0 and '连板数' in cand.columns:
            df = cand
            break

    if df is None:
        return None, 0, []

    code_col = next((c for c in ('代码', '股票代码') if c in df.columns), None)
    name_col = next((c for c in ('名称', '股票简称') if c in df.columns), None)

    detail = []
    for _, row in df.iterrows():
        try:
            height = int(row['连板数'])
        except (TypeError, ValueError):
            continue
        if height < 2:
            continue
        detail.append({
            'code': str(row.get(code_col, '')).strip() if code_col else '',
            'name': str(row.get(name_col, '')).strip() if name_col else '',
            'height': height,
        })

    detail.sort(key=lambda x: x['height'], reverse=True)
    try:
        max_height = int(df['连板数'].max())
    except (TypeError, ValueError):
        max_height = None
    return max_height, len(detail), detail


def _fetch_market_snapshot():
    """市场快照：指数 + 涨停/跌停/炸板 + 涨跌家数 + 成交额 + 北向资金。
    返回 dict：
      indices: list[(code, name, price, pct)]   按 _INDEX_CODES 顺序，缺失填 None
      zt_count, dt_count, zb_count: int
      data_date: date    实际取到数据的日期（盘后/周末会回退）
      is_live: bool      True=今日且数据日期==今天
      up_count, down_count, flat_count: int|None  涨/跌/平家数（legu 源）
      total_amount: float|None                    两市成交额（元）
      north_net: float|None                       北向净流入（亿元）
      north_date: str|None                        北向数据日期
    """
    # ---- 指数（优先东财 push2，不可用时腾讯回退）----
    idx_df = None
    try:
        idx_df = ak.stock_zh_index_spot_em(symbol='沪深重要指数')
        idx_df['代码'] = idx_df['代码'].astype(str)
        idx_map = {row['代码']: row for _, row in idx_df.iterrows()}
        indices = []
        for code, name in _INDEX_CODES:
            row = idx_map.get(code)
            if row is None:
                indices.append((code, name, None, None))
            else:
                try:
                    price = float(row['最新价'])
                    pct = float(row['涨跌幅'])
                except (ValueError, TypeError):
                    price, pct = None, None
                indices.append((code, name, price, pct))
    except Exception:
        # 回退到腾讯行情（push2 被封时使用）
        try:
            indices = _fetch_indices_tencent()
        except Exception:
            indices = [(code, name, None, None) for code, name in _INDEX_CODES]

    # ---- 涨停/跌停：使用东财官方市场活跃度接口 ----
    today = datetime.now().date()
    zt_count = dt_count = real_zt = real_dt = 0
    up_count = down_count = flat_count = None
    data_date = today
    legu_ok = False
    try:
        legu = ak.stock_market_activity_legu()
        legu_map = dict(zip(legu['item'], legu['value']))
        zt_count = int(float(legu_map.get('涨停', 0)))
        dt_count = int(float(legu_map.get('跌停', 0)))
        real_zt = int(float(legu_map.get('真实涨停', 0)))
        real_dt = int(float(legu_map.get('真实跌停', 0)))
        if '上涨' in legu_map:
            try:
                up_count = int(float(legu_map['上涨']))
            except (ValueError, TypeError):
                pass
        if '下跌' in legu_map:
            try:
                down_count = int(float(legu_map['下跌']))
            except (ValueError, TypeError):
                pass
        if '平盘' in legu_map:
            try:
                flat_count = int(float(legu_map['平盘']))
            except (ValueError, TypeError):
                pass
        # 「统计日期」格式例：'2026-05-07 15:00:00'
        sd = legu_map.get('统计日期')
        if sd:
            try:
                data_date = datetime.strptime(str(sd)[:10], '%Y-%m-%d').date()
            except ValueError:
                pass
        legu_ok = zt_count > 0 or dt_count > 0
    except Exception:
        pass

    # ---- 炸板：独立拉炸板股池（使用 data_date 对齐）----
    zb_count = 0
    ds = data_date.strftime('%Y%m%d')
    if legu_ok:
        try:
            zt_df = ak.stock_zt_pool_em(date=ds)
            adjusted = same_source_mainland_emotion_count(zt_count, zt_df)
            if adjusted is not None:
                zt_count = adjusted
        except Exception:
            pass
        try:
            dt_df = ak.stock_zt_pool_dtgc_em(date=ds)
            adjusted = same_source_mainland_emotion_count(dt_count, dt_df)
            if adjusted is not None:
                dt_count = adjusted
        except Exception:
            pass
    try:
        zb_df = ak.stock_zt_pool_zbgc_em(date=ds)
        zb_count = count_mainland_emotion_rows(zb_df)
    except Exception:
        pass

    # ---- 连板：强势池优先，失败回退涨停池 ----
    lb_count, lb_board_count, lb_detail = _fetch_lianban(ds)

    # 如 legu 完全失败（如未开盘状态），回退到老路径拿个近似值
    if not legu_ok:
        for offset in range(0, 11):
            try_date = today - timedelta(days=offset)
            if try_date.weekday() >= 5:
                continue
            ds_fb = try_date.strftime('%Y%m%d')
            try:
                cand = ak.stock_zt_pool_em(date=ds_fb)
            except Exception:
                cand = pd.DataFrame()
            if cand is not None and len(cand) > 0:
                zt_count = count_mainland_emotion_rows(cand)  # 可能被截断为 100，仅作提示
                try:
                    dt_df = ak.stock_zt_pool_dtgc_em(date=ds_fb)
                    dt_count = count_mainland_emotion_rows(dt_df)
                except Exception:
                    dt_count = 0
                try:
                    zb_df = ak.stock_zt_pool_zbgc_em(date=ds_fb)
                    zb_count = count_mainland_emotion_rows(zb_df)
                except Exception:
                    zb_count = 0
                data_date = try_date
                break

    is_live = (data_date == today) and is_trading_time()

    # ---- 两市成交额（从指数 DataFrame 提取，零额外请求）----
    total_amount = None
    if idx_df is not None and '成交额' in idx_df.columns:
        try:
            mask = idx_df['代码'].isin(('000001', '399001'))
            total_amount = float(idx_df.loc[mask, '成交额'].sum())
        except Exception:
            pass

    # ---- 北向资金（东财 datacenter，独立请求）----
    north_net, north_date = _fetch_north_net()

    # ---- 封板率（北向不可用时的替代指标）----
    seal_rate = None
    denom = zt_count + zb_count
    if denom > 0:
        seal_rate = zt_count / denom * 100

    return {
        'indices': indices,
        'zt_count': zt_count,
        'dt_count': dt_count,
        'zb_count': zb_count,
        'lb_count': lb_count,
        'lb_board_count': lb_board_count,
        'lb_detail': lb_detail,
        'real_zt': real_zt,
        'real_dt': real_dt,
        'data_date': data_date,
        'is_live': is_live,
        'legu_ok': legu_ok,
        'up_count': up_count,
        'down_count': down_count,
        'flat_count': flat_count,
        'total_amount': total_amount,
        'north_net': north_net,
        'north_date': north_date,
        'seal_rate': seal_rate,
    }


# ---------- 单个指数大字卡片 ----------
class IndexTile(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName('indexTile')
        self.setStyleSheet(
            f"QFrame#indexTile {{ background-color: #1e2545; border-radius: 0px;"
            f" border: none; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_lbl = QLabel('—')
        self.name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")

        self.price_lbl = QLabel('—')
        self.price_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.price_lbl.setStyleSheet("color: #ffffff; font-size: 22px; font-weight: bold;")

        self.pct_lbl = QLabel('—')
        self.pct_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pct_lbl.setStyleSheet(f"color: {MUTED}; font-size: 14px; font-weight: bold;")

        layout.addWidget(self.name_lbl)
        layout.addWidget(self.price_lbl)
        layout.addWidget(self.pct_lbl)

    def update_data(self, name, price, pct):
        self.name_lbl.setText(name)
        if price is None or pct is None:
            self.price_lbl.setText('—')
            self.pct_lbl.setText('—')
            self.price_lbl.setStyleSheet("color: #888888; font-size: 22px; font-weight: bold;")
            self.pct_lbl.setStyleSheet(f"color: {MUTED}; font-size: 14px; font-weight: bold;")
            return
        self.price_lbl.setText(f"{price:,.3f}")
        sign = '+' if pct >= 0 else ''
        self.pct_lbl.setText(f"{sign}{pct:.2f}%")
        # A股惯例：涨红跌绿
        color = RED if pct > 0 else (GREEN if pct < 0 else MUTED)
        self.price_lbl.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: bold;")
        self.pct_lbl.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")


# ---------- 情绪三数（涨停/跌停/炸板） ----------
class EmotionTile(QFrame):
    def __init__(self, label, color, parent=None):
        super().__init__(parent)
        self._color = color
        self.setObjectName('emotionTile')
        self.setStyleSheet(
            f"QFrame#emotionTile {{ background-color: #1e2545; border-radius: 0px;"
            f" border: none; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_lbl = QLabel(label)
        self.label_lbl.setStyleSheet(f"color: {MUTED}; font-size: 14px;")

        self.value_lbl = QLabel('—')
        self.value_lbl.setStyleSheet(f"color: {color}; font-size: 34px; font-weight: bold;")

        # 副数（可选）：提示「真实涨停/跌停」，剖出 ST 噪声
        self.sub_lbl = QLabel('')
        self.sub_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")

        layout.addWidget(self.label_lbl)
        layout.addWidget(self.value_lbl)
        layout.addWidget(self.sub_lbl)

    def update_value(self, n, sub_text=''):
        self.value_lbl.setText(str(n))
        self.sub_lbl.setText(sub_text)


# ---------- 市场宽度信息栏 ----------
class MarketBreadthBar(QFrame):
    """涨跌家数 + 两市成交 + 北向资金 信息栏"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('breadthBar')
        self.setStyleSheet(
            f"QFrame#breadthBar {{ background-color: {WIDGET_BG}; border-radius: 8px;"
            f" border: 1px solid #1a3a6a; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        lay.setSpacing(6)

        self.up_lbl = QLabel('↑ —')
        self.up_lbl.setStyleSheet(f"color: {RED}; font-size: 13px; font-weight: bold;")
        self.down_lbl = QLabel('↓ —')
        self.down_lbl.setStyleSheet(f"color: {GREEN}; font-size: 13px; font-weight: bold;")
        self.flat_lbl = QLabel('→ —')
        self.flat_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")

        sep1 = QLabel('│')
        sep1.setStyleSheet("color: #444;")
        self.amount_lbl = QLabel('成交 —')
        self.amount_lbl.setStyleSheet("color: #e0a040; font-size: 13px; font-weight: bold;")

        sep2 = QLabel('│')
        sep2.setStyleSheet("color: #444;")
        self.north_lbl = QLabel('北向 —')
        self.north_lbl.setStyleSheet("color: #5dade2; font-size: 13px; font-weight: bold;")

        for w in [self.up_lbl, self.down_lbl, self.flat_lbl, sep1,
                  self.amount_lbl, sep2, self.north_lbl]:
            lay.addWidget(w)
        lay.addStretch(1)

    def update_data(self, up=None, down=None, flat=None, amount=None,
                    north=None, north_date=None, seal_rate=None):
        self.up_lbl.setText(f"↑ {up}" if up is not None else "↑ —")
        self.down_lbl.setText(f"↓ {down}" if down is not None else "↓ —")
        self.flat_lbl.setText(f"→ {flat}" if flat is not None else "→ —")

        if amount and amount > 0:
            if amount >= 1e12:
                self.amount_lbl.setText(f"成交 {amount / 1e12:.2f}万亿")
            elif amount >= 1e8:
                self.amount_lbl.setText(f"成交 {amount / 1e8:.0f}亿")
            else:
                self.amount_lbl.setText("成交 —")
        else:
            self.amount_lbl.setText("成交 —")

        if north is not None:
            sign = '+' if north >= 0 else ''
            color = RED if north > 0 else (GREEN if north < 0 else MUTED)
            self.north_lbl.setText(f"北向 {sign}{north:.1f}亿")
            self.north_lbl.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: bold;")
            if north_date:
                self.north_lbl.setToolTip(f"北向资金净流入  数据日期: {north_date}")
        elif seal_rate is not None:
            color = RED if seal_rate >= 70 else ('#e0a040' if seal_rate >= 50 else GREEN)
            self.north_lbl.setText(f"封板 {seal_rate:.0f}%")
            self.north_lbl.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: bold;")
            self.north_lbl.setToolTip(
                "封板率 = 涨停数 / (涨停数 + 炸板数)\n"
                "≥ 70% 多头强势 | 50~70% 中性 | < 50% 多头溃败")
        else:
            self.north_lbl.setText("—")
            self.north_lbl.setStyleSheet(
                "color: #5dade2; font-size: 13px; font-weight: bold;")
            self.north_lbl.setToolTip("")


# ---------- 主面板 ----------
class MarketPanel(QWidget):
    """大盘温度计：9 指数 3×3 网格 + 涨停/跌停/炸板情绪三数。"""

    status_changed = Signal(str, str)        # 'loading'|'success'|'error', text
    point_count_changed = Signal(int)        # 兼容主窗口接口（本面板永远 emit 0）
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self._last_snap = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # ---- 顶部：9 指数 3×3 网格 ----
        idx_grid = QGridLayout()
        idx_grid.setSpacing(2)
        self.tiles = []
        for i, (code, name) in enumerate(_INDEX_CODES):
            tile = IndexTile()
            tile.name_lbl.setText(name)
            row, col = divmod(i, 3)
            idx_grid.addWidget(tile, row, col)
            self.tiles.append(tile)
        # 三列均匀拉伸
        for c in range(3):
            idx_grid.setColumnStretch(c, 1)
        for r in range(3):
            idx_grid.setRowStretch(r, 1)
        root.addLayout(idx_grid, stretch=6)

        # ---- 指数区与情绪区分割线 ----
        h_sep = QFrame()
        h_sep.setFrameShape(QFrame.Shape.HLine)
        h_sep.setFixedHeight(1)
        h_sep.setStyleSheet('background-color: #2a3560; border: none;')
        root.addWidget(h_sep)

        # ---- 中部：涨停 / 跌停 / 炸板（紧凑版）+ 右侧含义说明按钮 ----
        emo_row = QHBoxLayout()
        emo_row.setSpacing(10)
        self.zt_tile = EmotionTile('涨停', RED)
        self.dt_tile = EmotionTile('跌停', GREEN)
        self.zb_tile = EmotionTile('炸板', '#f0c040')
        self.lb_tile = EmotionTile('最高连板', '#f0a040')
        emo_row.addWidget(self.zt_tile, stretch=1)
        emo_row.addWidget(self.dt_tile, stretch=1)
        emo_row.addWidget(self.zb_tile, stretch=1)
        emo_row.addWidget(self.lb_tile, stretch=1)

        # 含义说明按钮（圆形 "?"）
        self.help_btn = QPushButton('?')
        self.help_btn.setFixedSize(28, 28)
        self.help_btn.setToolTip('点击查看「涨停/跌停/炸板/最高连板」含义说明')
        self.help_btn.setStyleSheet("""
            QPushButton {
                background-color: #2a2a44;
                color: #ccc;
                border: 1px solid #555;
                border-radius: 14px;
                font-size: 14px;
                font-weight: bold;
                padding: 0;
            }
            QPushButton:hover {
                background-color: #e94560;
                color: #fff;
                border-color: #e94560;
            }
        """)
        self.help_btn.clicked.connect(self._show_help_dialog)
        emo_row.addWidget(self.help_btn, alignment=Qt.AlignmentFlag.AlignTop)

        # 连板明细按钮
        self._lb_detail = []
        self.lb_detail_btn = QPushButton('连板明细')
        self.lb_detail_btn.setFixedHeight(28)
        self.lb_detail_btn.setEnabled(False)
        self.lb_detail_btn.setStyleSheet("""
            QPushButton {
                background-color: #2a2a44; color: #f0a040;
                border: 1px solid #555; border-radius: 6px;
                font-size: 12px; padding: 0 10px;
            }
            QPushButton:hover { background-color: #f0a040; color: #1a1a2e; }
            QPushButton:disabled { color: #666; border-color: #333; }
        """)
        self.lb_detail_btn.clicked.connect(self._show_lb_detail_dialog)
        emo_row.addWidget(self.lb_detail_btn, alignment=Qt.AlignmentFlag.AlignTop)

        root.addLayout(emo_row, stretch=1)

        # ---- 市场宽度：涨跌家数 / 成交额 / 北向 ----
        self.breadth_bar = MarketBreadthBar()
        root.addWidget(self.breadth_bar)

        # ---- 情绪历史趋势图（折叠区域） ----
        self._trend_visible = False
        self.trend_btn = QPushButton('📈 历史趋势')
        self.trend_btn.setFixedHeight(28)
        self.trend_btn.setStyleSheet("""
            QPushButton {
                background-color: #1a2a50; color: #5dade2; border: 1px solid #333;
                border-radius: 4px; font-size: 12px; font-weight: bold; padding: 2px 14px;
            }
            QPushButton:hover { background-color: #e94560; color: #fff; }
        """)
        self.trend_btn.clicked.connect(self._toggle_trend)
        root.addWidget(self.trend_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.trend_fig = Figure(facecolor=DARK_BG, tight_layout=True)
        self.trend_canvas = FigureCanvas(self.trend_fig)
        self.trend_canvas.setMinimumHeight(220)
        self.trend_canvas.setVisible(False)
        root.addWidget(self.trend_canvas)

        # ---- 底部：数据时间 / 来源 ----
        self.meta_lbl = QLabel('等待首次刷新…')
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meta_lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.meta_lbl)

        self.setStyleSheet(f"background-color: {DARK_BG};")

        # 启动时渲染情绪趋势（如有缓存）
        self._render_emotion_trend()

        cached = load_panel_cache('market_snapshot')
        if cached:
            try:
                cached['data_date'] = datetime.strptime(cached['data_date'], '%Y-%m-%d').date()
                self._last_snap = cached
                self._render_snapshot(cached, frozen=True)
            except Exception:
                pass

    # ---- 公共 API ----
    def refresh(self):
        """手动刷新（任何时间均可触发）"""
        self._fetch()

    def auto_refresh(self):
        """自动刷新：仅交易时间，避免非盘中重复抓取"""
        if is_trading_time():
            self._fetch()

    def current_point_count(self):
        return 0

    # ---- 含义说明弹窗 ----
    def _show_help_dialog(self):
        text = (
            "<b style='color:#e84444'>涨停</b>：当日封板涨停的股票总数（含 ST）。<br>"
            "&nbsp;&nbsp;其中 <b>真实涨停</b>：剖除 ST／*ST 后的正常股涨停数，"
            "更能反映主流资金点火热情。<br><br>"
            "<b style='color:#2ecc71'>跌停</b>：当日封板跌停的股票总数（含 ST）。<br>"
            "&nbsp;&nbsp;其中 <b>真实跌停</b>：剖除 ST／*ST 后的正常股跌停数。"
            "ST 股跌停多为退市风险事件，不应纳入市场情绪判断。<br><br>"
            "<b style='color:#f0c040'>炸板</b>：当日曾触及涨停但未能守住封板的股票数。<br>"
            "&nbsp;&nbsp;炸板增多 = 多头不坚决、资金出逃；是市场情绪转弱的领先信号。<br><br>"
            "<b style='color:#f0a040'>最高连板</b>：当日强势池中连续涨停的最高板数。<br>"
            "&nbsp;&nbsp;反映短线情绪强度与持续性。无法获取时显示 '—'。<br><br>"
            "<span style='color:#888'>数据源：东方财富官方「市场活跃度」 + 「炸板股池」 + 「强势池」接口</span>"
        )
        box = QMessageBox(self)
        box.setWindowTitle('情绪指标含义说明')
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setStyleSheet(
            "QMessageBox { background-color: #1a1a2e; }"
            "QMessageBox QLabel { color: #ddd; font-size: 13px; min-width: 380px; }"
            "QPushButton { background-color: #0f3460; color: #fff; "
            "border: 1px solid #444; border-radius: 4px; padding: 6px 18px; min-width: 60px; }"
            "QPushButton:hover { background-color: #e94560; }"
        )
        box.exec()

    def _show_lb_detail_dialog(self):
        if not self._lb_detail:
            return
        rows = ''.join(
            f"<tr><td style='padding:3px 14px 3px 0;color:#f0a040;font-weight:bold'>{d['height']}板</td>"
            f"<td style='padding:3px 14px 3px 0;color:#aaa'>{d['code']}</td>"
            f"<td style='padding:3px 0;color:#fff'>{d['name']}</td></tr>"
            for d in self._lb_detail
        )
        text = (
            f"<b style='color:#f0a040'>连板明细</b>（共 {len(self._lb_detail)} 家，按连板数降序）<br><br>"
            f"<table>{rows}</table>"
        )
        box = QMessageBox(self)
        box.setWindowTitle('连板明细')
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setStyleSheet(
            "QMessageBox { background-color: #1a1a2e; }"
            "QMessageBox QLabel { color: #ddd; font-size: 13px; min-width: 320px; }"
            "QPushButton { background-color: #0f3460; color: #fff; "
            "border: 1px solid #444; border-radius: 4px; padding: 6px 18px; min-width: 60px; }"
            "QPushButton:hover { background-color: #e94560; }"
        )
        box.exec()

    # ---- 内部 ----
    def _fetch(self):
        if self.worker and self.worker.isRunning():
            return
        self.status_changed.emit('loading', '正在获取数据...')
        self.worker = DataWorker(fetcher=_fetch_market_snapshot)
        self.worker.data_ready.connect(self._on_data_ready)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.start()

    def _on_data_ready(self, snap):
        self._last_snap = snap
        cache_snap = dict(snap)
        cache_snap['data_date'] = snap['data_date'].strftime('%Y-%m-%d')
        save_panel_cache('market_snapshot', cache_snap)

        # 写入情绪历史（当日最后一次成功刷新覆盖）
        save_market_emotion_record({
            'date': snap['data_date'].strftime('%Y-%m-%d'),
            'zt': snap.get('zt_count', 0),
            'real_zt': snap.get('real_zt', 0),
            'dt': snap.get('dt_count', 0),
            'real_dt': snap.get('real_dt', 0),
            'zb': snap.get('zb_count', 0),
            'lb': snap.get('lb_count'),
            'amount': snap.get('total_amount'),
            'north': snap.get('north_net'),
            'seal_rate': snap.get('seal_rate'),
        })

        self._render_snapshot(snap)
        self._render_emotion_trend()

    def _render_snapshot(self, snap, frozen=False):
        for tile, (code, name, price, pct) in zip(self.tiles, snap['indices']):
            tile.update_data(name, price, pct)

        real_zt = snap.get('real_zt', 0)
        real_dt = snap.get('real_dt', 0)
        self.zt_tile.update_value(
            snap['zt_count'],
            f"真实 {real_zt}" if real_zt else ''
        )
        self.dt_tile.update_value(
            snap['dt_count'],
            f"真实 {real_dt}" if real_dt else ''
        )
        self.zb_tile.update_value(snap['zb_count'])

        lb = snap.get('lb_count')
        lb_board = snap.get('lb_board_count') or 0
        self._lb_detail = snap.get('lb_detail') or []
        if lb is not None:
            self.lb_tile.update_value(lb, f'{lb}板 · {lb_board}家')
        else:
            self.lb_tile.update_value('—', '暂无数据')
        self.lb_detail_btn.setEnabled(bool(self._lb_detail))

        self.breadth_bar.update_data(
            up=snap.get('up_count'),
            down=snap.get('down_count'),
            flat=snap.get('flat_count'),
            amount=snap.get('total_amount'),
            north=snap.get('north_net'),
            north_date=snap.get('north_date'),
            seal_rate=snap.get('seal_rate'),
        )

        now = datetime.now()
        data_date = snap['data_date']
        is_live = snap['is_live']
        if is_live:
            label = f"盘中（{data_date.strftime('%Y-%m-%d')}）"
        elif data_date == now.date():
            label = f"今日收盘（{data_date.strftime('%Y-%m-%d')}）"
        else:
            label = f"昨日收盘（{data_date.strftime('%Y-%m-%d')}）"

        self.meta_lbl.setText(
            f"数据时间: {now.strftime('%H:%M:%S')} | 涨跌停统计: {label} | 数据源: 东方财富"
        )

        now_t = now.strftime("%H:%M:%S")
        status_suffix = ' [已冻结]' if frozen else ('' if is_live else ' [非盘中]')
        self.status_changed.emit('success', f"上次刷新: {now_t}{status_suffix}")
        self.bottom_status_changed.emit(
            f"数据更新于 {now_t} | 涨停 {snap['zt_count']} / 跌停 {snap['dt_count']} / 炸板 {snap['zb_count']}{status_suffix}"
        )

    def _on_error(self, msg):
        if self._last_snap:
            self._render_snapshot(self._last_snap, frozen=True)
            self.bottom_status_changed.emit(f"大盘数据获取失败，显示冻结数据: {msg}")
            return
        self.status_changed.emit('error', '获取失败')
        self.bottom_status_changed.emit(f"错误: {msg}")

    # ---- 情绪历史趋势图 ----
    def _toggle_trend(self):
        self._trend_visible = not self._trend_visible
        self.trend_canvas.setVisible(self._trend_visible)
        self.trend_btn.setText('📈 收起趋势' if self._trend_visible else '📈 历史趋势')
        if self._trend_visible:
            self._render_emotion_trend()

    def _render_emotion_trend(self):
        """渲染近30/60日情绪趋势折线图。"""
        if not self._trend_visible and not hasattr(self, '_trend_ever_rendered'):
            return
        self._trend_ever_rendered = True

        records = load_market_emotion_history()
        fig = self.trend_fig
        fig.clear()

        if not records or len(records) < 2:
            ax = fig.add_subplot(111)
            ax.set_facecolor(CHART_BG)
            ax.text(0.5, 0.5, '情绪历史数据不足（需至少2个交易日）',
                    transform=ax.transAxes, ha='center', va='center',
                    fontsize=12, color=MUTED)
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_color('#444444')
            self.trend_canvas.draw_idle()
            return

        # 按日期正序，取最近60条
        sorted_recs = sorted(records, key=lambda r: r['date'])[-60:]
        dates = [r['date'][5:] for r in sorted_recs]  # MM-DD
        zt_vals = [int(r.get('zt', 0)) for r in sorted_recs]
        dt_vals = [int(r.get('dt', 0)) for r in sorted_recs]
        zb_vals = [int(r.get('zb', 0)) for r in sorted_recs]
        # 情绪分 = 涨停 - 跌停 - 炸板*0.5
        emotion_score = [zt - dt - zb * 0.5 for zt, dt, zb in zip(zt_vals, dt_vals, zb_vals)]

        x = np.arange(len(dates))

        ax = fig.add_subplot(111)
        ax.set_facecolor(CHART_BG)

        # 左Y轴：涨停/跌停/炸板
        ln1 = ax.plot(x, zt_vals, color=RED, linewidth=1.5, label='涨停', marker='', alpha=0.9)
        ln2 = ax.plot(x, dt_vals, color=GREEN, linewidth=1.5, label='跌停', marker='', alpha=0.9)
        ln3 = ax.plot(x, zb_vals, color='#f0c040', linewidth=1.3, label='炸板', linestyle='--', alpha=0.8)

        ax.set_ylabel('个数', color=MUTED, fontsize=9)
        ax.tick_params(axis='y', colors='white', labelsize=8)

        # 右Y轴：情绪分
        ax2 = ax.twinx()
        ln4 = ax2.plot(x, emotion_score, color='#5dade2', linewidth=1.8, label='情绪分', alpha=0.7)
        ax2.axhline(y=0, color='#5dade2', linewidth=0.5, alpha=0.3, linestyle=':')
        ax2.set_ylabel('情绪分', color='#5dade2', fontsize=9)
        ax2.tick_params(axis='y', colors='#5dade2', labelsize=8)

        # X轴标签（间隔显示避免拥挤）
        step = max(1, len(dates) // 10)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(dates[::step], fontsize=8, rotation=30)
        ax.tick_params(axis='x', colors='white', labelsize=8)

        # 图例
        lns = ln1 + ln2 + ln3 + ln4
        labs = [l.get_label() for l in lns]
        ax.legend(lns, labs, loc='upper left', fontsize=8,
                  facecolor=DARK_BG, edgecolor='#444', labelcolor='white')

        ax.set_title(f'近{len(sorted_recs)}日情绪趋势（涨停/跌停/炸板/情绪分）',
                     fontsize=11, fontweight='bold', color='white', pad=8)
        ax.grid(color='#444444', linewidth=0.3, alpha=0.4)
        for spine in ax.spines.values():
            spine.set_color('#444444')
        for spine in ax2.spines.values():
            spine.set_color('#444444')

        fig.tight_layout()
        self.trend_canvas.draw()
