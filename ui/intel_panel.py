"""intel_panel.py — 事件情报面板（v3.0 纯信息流）。

定位：资金语境下的事件解释器。
- 左侧：按分类过滤的事件列表（today 优先 + 按 level 分级展示窗口）
- 右侧：选中事件的结构化详情卡 / 今日主线聚合视图
- 数据源：data/intel_feed.json（手动维护 + AI 自动获取）
- 资金联动：通过 SectorPanel.get_market_context() 只读快照
- 「🤖 分析当前板块」按钮 → _SectorTrackWorker → 自动建立板块追踪任务
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, date
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSplitter, QSizePolicy, QButtonGroup,
    QInputDialog, QLineEdit, QMessageBox,
    QDialog, QApplication, QPlainTextEdit,
)
from PySide6.QtCore import Qt, Signal, QThread

import core.intel_fetcher as _fetcher

from core.constants import CHART_BG, MUTED, RED, GREEN
from core.cache import (
    load_sector_latest_df, load_sector_history, load_sector_minute_history,
    load_market_emotion_history, load_sector_constituents, save_sector_constituents,
)
from core.net_setup import push2_get
from core.intel_models import IntelEvent

# 复用 sector_panel 的别名表（intel 词 → 面板口径名）
try:
    from ui.sector_panel import _FOCUS_SECTOR_ALIASES as _SP_ALIASES
except Exception:
    _SP_ALIASES = {}
_ALIAS_FORWARD = dict(_SP_ALIASES)
_ALIAS_REVERSE = {v: k for k, v in _SP_ALIASES.items()}

# 扩展映射：intel 白名单词 → 面板口径名候选（一对多）
_INTEL_TO_SECTOR_EXPAND = {
    '有色金属': ['有色金属', '贵金属', '工业金属', '小金属'],
    '军工':     ['国防军工', '航天', '航空装备', '通用航空'],
    '机器人':   ['机器人概念', '减速器', '工业母机'],
    'AI算力':   ['算力', 'CPO概念', '光通信模块'],
    '数据中心': ['IDC概念', '算力', '云计算'],
}


def _match_event_to_sector(event, sector_name: str) -> bool:
    """判断 event 是否与 sector_name 相关。四级匹配：
    ① 精确  ② 扩展映射  ③ alias 双向反查  ④ 模糊子串（≥2 字，排除「非」前缀）
    """
    related = event.related_sectors or []
    if not related:
        return False
    if sector_name in related:
        return True
    # ② 扩展映射
    for r in related:
        candidates = _INTEL_TO_SECTOR_EXPAND.get(r)
        if candidates and sector_name in candidates:
            return True
    # ③ 别名反查（双向）
    for r in related:
        if _ALIAS_FORWARD.get(r) == sector_name:
            return True
        if _ALIAS_REVERSE.get(r) == sector_name:
            return True
        if _ALIAS_FORWARD.get(sector_name) == r:
            return True
        if _ALIAS_REVERSE.get(sector_name) == r:
            return True
    # ④ 模糊子串
    sn = str(sector_name)
    if sn.startswith('非') or len(sn) < 2:
        return False
    for r in related:
        rs = str(r)
        if rs.startswith('非') or len(rs) < 2:
            continue
        if rs in sn or sn in rs:
            return True
    return False

# ---------- 常量 ----------
from core.paths import INTEL_FEED_FILE, INTEL_SETTINGS_FILE  # noqa: E402
_FEED_PATH = str(INTEL_FEED_FILE)
_DISPLAY_DAYS = {'critical': 5, 'important': 4, 'info': 3}  # 超出则不加载，配合 _EXPIRY_DAYS 形成灰化缓冲期
_SETTINGS_PATH = str(INTEL_SETTINGS_FILE)


_FILTER_ORDER = [
    ('', '全部'),
    ('ai', 'AI科技'),
    ('semiconductor', '半导体'),
    ('new_energy', '新能源'),
    ('robotics', '机器人低空'),
    ('medical', '医药'),
    ('consumer', '消费'),
    ('macro', '宏观流动性'),
    ('overseas', '海外映射'),
    ('military', '军工'),
    ('gold', '黄金有色'),
    ('finance', '金融'),
    ('real_estate', '地产'),
    ('policy', '政策监管'),
]


# ---------- 工具函数 ----------
_INTEL_FS_RE = re.compile(r'font-size\s*:\s*(\d+(?:\.\d+)?)(px|pt)\b')


def _set_font_scaled_style(widget, original_css: str):
    """动态控件专用：存储原始 CSS 并立即应用按当前 app 字号缩放后的版本。
    解决每次重建控件时字号不随全局设置变化的问题。
    """
    widget.setProperty('_orig_ss', original_css)
    try:
        app = QApplication.instance()
        pt = app.font().pointSize() if app else 10
        if pt == 10:
            widget.setStyleSheet(original_css)
            return
        scale = pt / 10.0
        def _repl(m):
            val = float(m.group(1))
            unit = m.group(2)
            if unit == 'pt':
                return f'font-size: {max(7, round(val * scale, 1))}pt'
            return f'font-size: {max(8, int(round(val * scale)))}px'
        widget.setStyleSheet(_INTEL_FS_RE.sub(_repl, original_css))
    except Exception:
        widget.setStyleSheet(original_css)


def _load_feed() -> list[IntelEvent]:
    """读取 intel_feed.json，按 level 分级显示窗口（critical 5天/important 4天/info 3天）。"""
    if not os.path.exists(_FEED_PATH):
        return []
    try:
        with open(_FEED_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return []

    today = date.today()
    events: list[IntelEvent] = []
    for d in raw:
        try:
            ev = IntelEvent.from_dict(d)
            if not ev.date:
                continue
            max_days = _DISPLAY_DAYS.get(ev.level, 3)
            if (today - ev.date).days <= max_days:
                events.append(ev)
        except Exception:
            continue

    events.sort(key=lambda e: (0 if e.date == today else 1, -(e.date.toordinal() if e.date else 0)))
    return events


# =========================================================
# 事件列表项（左侧每行）
# =========================================================
class _EventItem(QFrame):
    clicked = Signal(str)  # emits event id

    def __init__(self, event: IntelEvent, heat: float = 0.0,
                 warn_switch: bool = False, signal: str = 'pending',
                 expired: bool = False, parent=None):
        super().__init__(parent)
        self._event_id = event.id
        self.setObjectName('eventItem')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        _bg = '#0c1020' if expired else '#111a30'
        _border = '#151f30' if expired else '#1a2744'
        _bg_hover = '#0f1525' if expired else '#142038'
        _border_hover = '#1a2a50' if expired else '#3a5a9a'
        self.setStyleSheet(f"""
            QFrame#eventItem {{
                background-color: {_bg};
                border-radius: 6px;
                border: 1px solid {_border};
            }}
            QFrame#eventItem:hover {{
                background-color: {_bg_hover};
                border: 1px solid {_border_hover};
            }}
        """)
        self.setFixedHeight(84 if warn_switch else 68)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        # 左：信号状态色条
        _DOT_CLR = {'resonance': '#2ECC71', 'divergence': '#E74C3C', 'pending': '#7F8C8D', 'expired': '#2a2a2a'}
        dot = QLabel()
        dot.setFixedSize(6, 36)
        dot.setStyleSheet(
            f"background-color: {_DOT_CLR.get(signal, '#7F8C8D')}; border-radius: 3px;"
        )
        layout.addWidget(dot)

        # 中：标题 + 分类标签行
        mid = QVBoxLayout()
        mid.setSpacing(3)

        title_lbl = QLabel(event.title)
        _tw = 'bold' if heat > 10 else 'normal'
        _tc = '#555555' if expired else '#e8e8e8'
        _set_font_scaled_style(title_lbl, f"color: {_tc}; font-size: 13px; font-weight: {_tw};")
        title_lbl.setWordWrap(False)
        mid.addWidget(title_lbl)

        tag_row = QHBoxLayout()
        tag_row.setSpacing(4)
        tag_row.setContentsMargins(0, 0, 0, 0)

        cat_tag = QLabel(event.category_label)
        _set_font_scaled_style(cat_tag,
            "color: #7a8eaa; font-size: 10px; background-color: #1a2540;"
            " border-radius: 3px; padding: 1px 5px;"
        )
        tag_row.addWidget(cat_tag)

        dir_tag = QLabel(event.direction_label)
        _set_font_scaled_style(dir_tag,
            f"color: {event.direction_color}; font-size: 10px; background-color: #12172a;"
            f" border-radius: 3px; padding: 1px 5px;"
        )
        tag_row.addWidget(dir_tag)

        date_lbl = QLabel(event.timestamp)
        _set_font_scaled_style(date_lbl, f"color: {MUTED}; font-size: 10px;")
        tag_row.addWidget(date_lbl)
        if event.source:
            src_lbl = QLabel(f'\u00b7 {event.source}')
            _set_font_scaled_style(src_lbl, "color: #3a5580; font-size: 10px;")
            tag_row.addWidget(src_lbl)
        tag_row.addStretch()
        mid.addLayout(tag_row)

        if warn_switch:
            warn_lbl = QLabel('\u26a0 主线可能切换')
            _set_font_scaled_style(warn_lbl,
                'color: #f0a040; font-size: 9px; background: transparent;'
            )
            mid.addWidget(warn_lbl)

        layout.addLayout(mid, stretch=1)

        # 右：热度分数 + level 文字
        right_col = QVBoxLayout()
        right_col.setSpacing(2)
        right_col.setContentsMargins(0, 0, 0, 0)
        heat_lbl = QLabel(f'{heat:.1f}')
        heat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        _set_font_scaled_style(heat_lbl, 'color: #445566; font-size: 10px; background: transparent;')
        level_lbl = QLabel(event.level_label)
        level_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        _set_font_scaled_style(level_lbl,
            f"color: {event.level_color}; font-size: 10px; font-weight: bold;"
        )
        right_col.addWidget(heat_lbl)
        right_col.addStretch()
        right_col.addWidget(level_lbl)
        layout.addLayout(right_col)

    def set_selected(self, selected: bool):
        if selected:
            self.setStyleSheet("""
                QFrame#eventItem {
                    background-color: #1a1040;
                    border-radius: 6px;
                    border: 1px solid #e94560;
                    border-left: 3px solid #e94560;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame#eventItem {
                    background-color: #111a30;
                    border-radius: 6px;
                    border: 1px solid #1a2744;
                }
                QFrame#eventItem:hover {
                    background-color: #142038;
                    border: 1px solid #3a5a9a;
                }
            """)

    def mousePressEvent(self, _event):
        self.clicked.emit(self._event_id)


# =========================================================
# 板块 Chip 标签
# =========================================================
class _SectorChip(QLabel):
    def __init__(self, text: str, matched: bool, inflow: Optional[dict] = None, parent=None):
        net = inflow.get('net', 0) if inflow else None
        pct = inflow.get('pct', 0) if inflow else None
        display = text
        if net is not None:
            sign = '+' if net >= 0 else ''
            pct_sign = '+' if (pct or 0) >= 0 else ''
            pct_str = f'  {pct_sign}{pct:.1f}%' if pct is not None else ''
            display = f"{text}  {sign}{net:.1f}亿{pct_str}"
        super().__init__(display, parent)
        if matched and net is not None:
            color = RED if net >= 0 else GREEN
            bg = '#1e0a10' if net >= 0 else '#0a1e12'
            border = '#4a1020' if net >= 0 else '#0a3018'
        elif matched:
            color = '#e8e8e8'
            bg = '#1a2a45'
            border = '#3a5080'
        else:
            color = MUTED
            bg = '#131c2e'
            border = '#1e2a40'
        self.setStyleSheet(
            f"color: {color}; background-color: {bg}; border: 1px solid {border};"
            f" border-radius: 4px; padding: 3px 8px; font-size: 12px;"
        )


# =========================================================
# 板块聚合卡（可点击跳转情报）
# =========================================================
class _SectorCard(QFrame):
    def __init__(self, event_id: Optional[str], signal_emitter, parent=None):
        super().__init__(parent)
        self._event_id = event_id
        self._signal_emitter = signal_emitter

    def mousePressEvent(self, ev):
        if self._event_id:
            self._signal_emitter(self._event_id)


# =========================================================
# 右侧详情卡
# =========================================================
class _DetailCard(QScrollArea):
    back_requested = Signal()
    event_jump_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName('detailScroll')
        self.setStyleSheet("QScrollArea#detailScroll { background-color: #111a30; border: none; }")

        self._container = QWidget()
        self._container.setStyleSheet("background-color: #111a30;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(16)
        self._layout.addStretch()
        self.setWidget(self._container)

        self._show_placeholder()

    # ---- 清空并重建内容 ----
    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_placeholder(self):
        self._clear()
        lbl = QLabel('← 选择左侧事件查看详情')
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {MUTED}; font-size: 14px;")
        self._layout.addStretch()
        self._layout.addWidget(lbl)
        self._layout.addStretch()

    def show_aggregation(self, events: list, inflows: dict):
        """今日主线聚合卡——无事件选中时的默认右侧视图。"""
        self._clear()

        # ---- 聚合计算（跳过超期事件）----
        sector_data: dict = {}
        for event in events:
            if _is_expired(event):
                continue
            for sector in event.related_sectors:
                if sector not in sector_data:
                    sector_data[sector] = {
                        'events': [], 'bullish': 0, 'bearish': 0, 'neutral': 0,
                    }
                sector_data[sector]['events'].append(event)
                sector_data[sector][event.direction] = (
                    sector_data[sector].get(event.direction, 0) + 1
                )

        qualifying = {k: v for k, v in sector_data.items() if len(v['events']) >= 2}

        for sector, data in qualifying.items():
            signals, heat_sum, best_ev, best_h = [], 0.0, None, -1.0
            for ev in data['events']:
                sig = _compute_event_signal(ev, inflows)
                h = _compute_heat(ev, inflows)
                signals.append(sig)
                heat_sum += h
                if h > best_h:
                    best_h, best_ev = h, ev
            data['signal'] = (
                'divergence' if 'divergence' in signals else
                'resonance' if all(s == 'resonance' for s in signals) else
                'pending'
            )
            data['heat'] = heat_sum
            data['best_event_id'] = best_ev.id if best_ev else None
            flow = _find_inflow(sector, inflows)
            data['net'] = flow['net'] if flow else None

        sorted_sectors = sorted(
            qualifying.items(), key=lambda x: x[1]['heat'], reverse=True
        )[:5]

        # ---- 空态 ----
        if not sorted_sectors:
            msg = QLabel('今日情报不足\n同一板块需至少 2 条情报才能生成聚合卡')
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _set_font_scaled_style(msg, f'color: {MUTED}; font-size: 13px;')
            self._layout.addStretch()
            self._layout.addWidget(msg)
            self._layout.addStretch()
            return

        # ---- 标题行 ----
        hdr_row = QHBoxLayout()
        hdr_lbl = QLabel('今日主线聚合')
        _set_font_scaled_style(hdr_lbl,
            'color: #c8d0e8; font-size: 15px; font-weight: bold; background: transparent;'
        )
        total_q = len(qualifying)
        disp_n = len(sorted_sectors)
        count_text = (
            f'共 {total_q} 个主线（显示前 {disp_n}）'
            if total_q > disp_n else
            f'共 {total_q} 个主线板块'
        )
        cnt_lbl = QLabel(count_text)
        _set_font_scaled_style(cnt_lbl, f'color: {MUTED}; font-size: 11px; background: transparent;')
        hdr_row.addWidget(hdr_lbl)
        hdr_row.addStretch()
        hdr_row.addWidget(cnt_lbl)
        hdr_w = QWidget()
        hdr_w.setLayout(hdr_row)
        self._layout.addWidget(hdr_w)

        # ---- 卡片网格 ----
        _SIG_COLORS = {
            'resonance': '#2ECC71', 'divergence': '#E74C3C', 'pending': '#7F8C8D',
        }
        _SIG_LABELS = {'resonance': '共振', 'divergence': '背离', 'pending': '待验证'}
        _SIG_WORDS = {'resonance': '可信', 'divergence': '存疑', 'pending': '待观察'}

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setSpacing(10)
        grid.setContentsMargins(0, 0, 0, 0)

        for i, (sector, data) in enumerate(sorted_sectors):
            gr, gc = divmod(i, 2)
            sig = data['signal']
            sc = _SIG_COLORS[sig]

            card = _SectorCard(data.get('best_event_id'), self.event_jump_requested.emit)
            card.setStyleSheet(
                f'QFrame {{ background-color: #131d35; border-radius: 8px;'
                f' border: 1px solid #1e2e50; border-left: 3px solid {sc}; }}'
                f'QFrame:hover {{ background-color: #192540; }}'
            )
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(5)

            r1 = QHBoxLayout()
            nm = QLabel(sector)
            _set_font_scaled_style(nm,
                'color: #e0e8f8; font-size: 14px; font-weight: bold; background: transparent;'
            )
            sl_l = QLabel(_SIG_LABELS[sig])
            _set_font_scaled_style(sl_l,
                f'color: {sc}; font-size: 10px; font-weight: bold; background: transparent;'
            )
            r1.addWidget(nm)
            r1.addStretch()
            r1.addWidget(sl_l)
            cl.addLayout(r1)

            total = len(data['events'])
            b = data.get('bullish', 0)
            be = data.get('bearish', 0)
            n = data.get('neutral', 0)
            parts = [f'{total}条情报']
            if b:
                parts.append(f'利好{b}')
            if be:
                parts.append(f'利空{be}')
            if n:
                parts.append(f'中性{n}')
            ct_lbl = QLabel(' · '.join(parts))
            _set_font_scaled_style(ct_lbl,
                f'color: {MUTED}; font-size: 11px; background: transparent;'
            )
            cl.addWidget(ct_lbl)

            net = data.get('net')
            if net is not None:
                fcolor = RED if net >= 0 else GREEN
                fsign = '+' if net >= 0 else ''
                fl_lbl = QLabel(f'净流入 {fsign}{net:.1f}亿')
                _set_font_scaled_style(fl_lbl,
                    f'color: {fcolor}; font-size: 12px; font-weight: bold; background: transparent;'
                )
            else:
                fl_lbl = QLabel('资金数据待更新')
                _set_font_scaled_style(fl_lbl,
                    f'color: {MUTED}; font-size: 12px; background: transparent;'
                )
            cl.addWidget(fl_lbl)

            flow_w = (
                '持续流入' if (net is not None and net > 0) else
                '出现流出' if (net is not None and net <= 0) else
                '暂无数据'
            )
            summ = f'{total}条情报指向该板块，资金{flow_w}，信号{_SIG_WORDS[sig]}。'
            sm_lbl = QLabel(summ)
            sm_lbl.setWordWrap(True)
            _set_font_scaled_style(sm_lbl, f'color: #6a7a9a; font-size: 11px; background: transparent;')
            cl.addWidget(sm_lbl)

            grid.addWidget(card, gr, gc)

        self._layout.addWidget(grid_w)
        self._layout.addStretch()

    @staticmethod
    def _section_divider(title: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)
        line_l = QFrame()
        line_l.setFrameShape(QFrame.Shape.HLine)
        line_l.setFixedHeight(1)
        line_l.setStyleSheet("background-color: #1e3060; border: none;")
        lbl = QLabel(title)
        _set_font_scaled_style(lbl, f"color: {MUTED}; font-size: 11px; font-weight: bold;")
        lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        line_r = QFrame()
        line_r.setFrameShape(QFrame.Shape.HLine)
        line_r.setFixedHeight(1)
        line_r.setStyleSheet("background-color: #1e3060; border: none;")
        lay.addWidget(line_l, stretch=1)
        lay.addWidget(lbl)
        lay.addWidget(line_r, stretch=1)
        return w

    def update_event(self, event: IntelEvent, inflows: dict):
        """渲染事件详情。inflows: {板块名: 净额(亿)}（来自 get_market_context 展平）"""
        self._clear()

        # ── 返回按鈕 ──
        back_btn = QPushButton('← 返回主线概览')
        back_btn.setStyleSheet("""
            QPushButton {
                color: #4a6a99; font-size: 11px;
                background-color: transparent; border: none;
                text-align: left; padding: 0;
            }
            QPushButton:hover { color: #7aa0cc; }
        """)
        back_btn.clicked.connect(self.back_requested.emit)
        self._layout.addWidget(back_btn)

        # ── 信号判断 ──
        _sig = _compute_event_signal(event, inflows)
        _SIG_CLR = {'resonance': '#2ECC71', 'divergence': '#E74C3C', 'pending': '#7F8C8D', 'expired': '#555555'}
        _SIG_WORD = {'resonance': '共振信号', 'divergence': '背离信号', 'pending': '待验证', 'expired': '⏳ 已超出时效窗口'}
        _SIG_DESC = {
            'resonance': '情报情绪与板块资金方向一致，信号可信',
            'divergence': '情报情绪与板块资金方向相反，信号存疑',
            'pending': '板块资金数据不足，信号待观察',
            'expired': '仅供回看，不参与信号计算；资金数据仍实时显示',
        }
        _sc = _SIG_CLR.get(_sig, '#7F8C8D')
        _bg = '#333333' if _sig == 'expired' else f'{_sc}33'
        sig_frame = QFrame()
        sig_frame.setFixedHeight(52)
        sig_frame.setStyleSheet(
            f'QFrame {{ background-color: {_bg}; border-radius: 6px;'
            f' border-left: 3px solid {_sc}; }}'
        )
        sig_lay = QHBoxLayout(sig_frame)
        sig_lay.setContentsMargins(12, 8, 12, 8)
        sig_lay.setSpacing(10)
        sig_word_lbl = QLabel(_SIG_WORD[_sig])
        _set_font_scaled_style(sig_word_lbl,
            f'color: {_sc}; font-size: 15px; font-weight: bold; background: transparent;'
        )
        sig_desc_lbl = QLabel(_SIG_DESC[_sig])
        _set_font_scaled_style(sig_desc_lbl, 'color: #8898b8; font-size: 11px; background: transparent;')
        sig_lay.addWidget(sig_word_lbl)
        sig_lay.addWidget(sig_desc_lbl)
        sig_lay.addStretch()
        self._layout.addWidget(sig_frame)

        # ── 标题区 ──
        title_lbl = QLabel(event.title)
        title_lbl.setWordWrap(True)
        _set_font_scaled_style(title_lbl,
            "color: #ffffff; font-size: 16px; font-weight: bold; line-height: 1.4;"
        )
        self._layout.addWidget(title_lbl)

        # 元信息行
        meta_row = QHBoxLayout()
        meta_row.setSpacing(12)
        for text, color in [
            (event.category_label, '#5dade2'),
            (event.direction_label, event.direction_color),
            (event.level_label, event.level_color),
            (event.timestamp, MUTED),
        ]:
            m = QLabel(text)
            _set_font_scaled_style(m,
                f"color: {color}; font-size: 11px; background-color: #12172a;"
                f" border-radius: 3px; padding: 2px 6px;"
            )
            meta_row.addWidget(m)
        if event.source:
            src = QLabel(f"来源：{event.source}")
            _set_font_scaled_style(src, f"color: {MUTED}; font-size: 11px;")
            meta_row.addWidget(src)
        meta_row.addStretch()
        self._layout.addLayout(meta_row)

        # ── 一句话摘要 ──
        self._layout.addWidget(self._section_divider('摘要'))
        summary_lbl = QLabel(event.summary)
        summary_lbl.setWordWrap(True)
        _set_font_scaled_style(summary_lbl, "color: #cccccc; font-size: 13px; line-height: 1.6;")
        self._layout.addWidget(summary_lbl)

        # ── 市场解读 ──
        self._layout.addWidget(self._section_divider('市场解读'))
        interp_lbl = QLabel(event.interpretation)
        interp_lbl.setWordWrap(True)
        _set_font_scaled_style(interp_lbl,
            f"color: #b8c8e8; font-size: 12px; line-height: 1.7;"
            f" background-color: #0e1929; border-radius: 6px; padding: 10px 12px;"
        )
        self._layout.addWidget(interp_lbl)

        # ── 操作提示 ──
        if event.trading_tip:
            self._layout.addWidget(self._section_divider('操作提示'))
            tip_color = {
                'bullish': '#2ecc71', 'bearish': '#e74c3c', 'neutral': '#f0b429',
            }.get(event.direction, '#f0b429')
            tip_frame = QFrame()
            tip_frame.setStyleSheet(
                f'QFrame {{ background-color: {tip_color}18;'
                f' border-radius: 6px; border-left: 3px solid {tip_color}; }}'
            )
            tip_lay = QHBoxLayout(tip_frame)
            tip_lay.setContentsMargins(12, 9, 12, 9)
            tip_lbl = QLabel(event.trading_tip)
            tip_lbl.setWordWrap(True)
            _set_font_scaled_style(tip_lbl,
                f'color: {tip_color}; font-size: 13px; font-weight: bold;'
                f' background: transparent;'
            )
            tip_lay.addWidget(tip_lbl)
            self._layout.addWidget(tip_frame)

        # ── 关联板块 ──
        self._layout.addWidget(self._section_divider('关联板块'))
        chips_widget = QWidget()
        chips_layout = QHBoxLayout(chips_widget)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)
        for sector in event.related_sectors:
            inflow = _find_inflow(sector, inflows)
            matched = inflow is not None
            chip = _SectorChip(sector, matched, inflow)
            chips_layout.addWidget(chip)
        chips_layout.addStretch()
        self._layout.addWidget(chips_widget)

        # ── 实时资金反馈 ──
        matched_flows = [
            (s, _find_inflow(s, inflows))
            for s in event.related_sectors
            if _find_inflow(s, inflows) is not None
        ]
        self._layout.addWidget(self._section_divider('实时资金反馈'))
        if matched_flows:
            matched_flows.sort(key=lambda x: -(x[1]['net'] if x[1] else 0))
            max_net = max(abs(f['net']) for _, f in matched_flows if f) or 1.0
            for sector, flow in matched_flows:
                net = flow['net']
                pct = flow.get('pct', 0)
                row = QHBoxLayout()
                row.setSpacing(8)
                name_lbl = QLabel(sector)
                _set_font_scaled_style(name_lbl, "color: #aaaaaa; font-size: 12px;")
                name_lbl.setFixedWidth(120)
                sign = '+' if net >= 0 else ''
                pct_sign = '+' if pct >= 0 else ''
                color = RED if net >= 0 else GREEN
                val_lbl = QLabel(f"{sign}{net:.1f}亿  {pct_sign}{pct:.1f}%")
                _set_font_scaled_style(val_lbl, f"color: {color}; font-size: 13px; font-weight: bold;")
                bar_w = max(4, int(abs(net) / max_net * 120))
                bar = QFrame()
                bar.setFixedHeight(6)
                bar.setFixedWidth(bar_w)
                bar.setStyleSheet(f"background-color: {color}; border-radius: 3px;")
                row.addWidget(name_lbl)
                row.addWidget(bar)
                row.addWidget(val_lbl)
                row.addStretch()
                row_w = QWidget()
                row_w.setLayout(row)
                self._layout.addWidget(row_w)
            if event.direction == 'bullish':
                _total = sum(f['net'] for _, f in matched_flows)
                _neg = sum(1 for _, f in matched_flows if f['net'] < 0)
                if _neg >= max(1, len(matched_flows) // 2) and _total < 0:
                    _dw = QFrame()
                    _dw.setStyleSheet(
                        'QFrame { background-color: #2a1204; border: 1px solid #c05010;'
                        ' border-radius: 6px; }'
                    )
                    _dl = QHBoxLayout(_dw)
                    _dl.setContentsMargins(12, 9, 12, 9)
                    _dl.setSpacing(8)
                    _icon = QLabel('⚠️')
                    _set_font_scaled_style(_icon, 'font-size: 14px; background: transparent;')
                    _txt = QLabel(
                        f'资金与情绪背离：利好标注，但相关板块合计净流出 {abs(_total):.0f}亿\n'
                        '主力资金持续出逃 — 警惕利好出尽，建议观望'
                    )
                    _txt.setWordWrap(True)
                    _set_font_scaled_style(_txt, 'color: #e88040; font-size: 12px; background: transparent;')
                    _dl.addWidget(_icon)
                    _dl.addWidget(_txt, stretch=1)
                    self._layout.addWidget(_dw)
        else:
            no_data = QLabel('暂无实时资金数据（板块面板刷新后自动更新）')
            _set_font_scaled_style(no_data, f"color: {MUTED}; font-size: 12px;")
            self._layout.addWidget(no_data)

        self._layout.addStretch()


# =========================================================
# 主面板
# =========================================================
class IntelPanel(QWidget):
    status_changed = Signal(str, str)
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sector_panel_ref = None   # 由 main_window 注入
        self._events: list[IntelEvent] = []
        self._filtered: list[IntelEvent] = []
        self._current_category = ''
        self._selected_id: Optional[str] = None
        self._item_widgets: dict[str, _EventItem] = {}
        self._sort_mode: str = self._load_sort_mode()
        self._heat_scores: dict[str, float] = {}
        self._signal_states: dict[str, str] = {}
        self._warn_event_ids: set[str] = set()
        self._sector_track_worker = None   # _SectorTrackWorker
        self._setup_ui()
        self._detail_card.back_requested.connect(self._on_back_to_overview)
        self._detail_card.event_jump_requested.connect(self._on_item_clicked)

    # ---- UI 构建 ----
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部标题栏
        header = QWidget()
        header.setFixedHeight(48)
        header.setStyleSheet(f"background-color: {CHART_BG}; border-bottom: 1px solid #2a3f6a;")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(16, 0, 16, 0)
        h_lay.setSpacing(12)

        icon_lbl = QLabel('🔍')
        icon_lbl.setStyleSheet("font-size: 18px;")
        title_lbl = QLabel('事件情报')
        title_lbl.setStyleSheet("color: #ffffff; font-size: 15px; font-weight: bold;")
        title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        h_lay.addWidget(icon_lbl)
        h_lay.addWidget(title_lbl, stretch=1)

        self._meta_lbl = QLabel('未加载')
        self._meta_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self._meta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h_lay.addWidget(self._meta_lbl)

        refresh_btn = QPushButton('↻ 刷新')
        refresh_btn.setFixedSize(68, 28)
        refresh_btn.clicked.connect(self.refresh)
        h_lay.addWidget(refresh_btn)

        self._auto_fetch_btn = QPushButton('🤖 自动获取')
        self._auto_fetch_btn.setFixedSize(90, 28)
        self._auto_fetch_btn.setToolTip('通过 DeepSeek AI 自动抓取财经快讯并结构化入库')
        self._auto_fetch_btn.clicked.connect(self._on_auto_fetch)
        h_lay.addWidget(self._auto_fetch_btn)

        self._sector_track_btn = QPushButton('🤖 分析当前板块')
        self._sector_track_btn.setFixedSize(112, 28)
        self._sector_track_btn.setToolTip('AI 分析当前筛选板块并自动建立追踪任务（需先选中板块过滤）')
        self._sector_track_btn.setEnabled(False)
        self._sector_track_btn.clicked.connect(self._on_sector_analyze_and_track)
        h_lay.addWidget(self._sector_track_btn)

        root.addWidget(header)

        # ── 今日结论栏 ──
        conclusion_bar = QWidget()
        conclusion_bar.setFixedHeight(38)
        conclusion_bar.setStyleSheet(
            "background-color: #08101e; border-bottom: 1px solid #1e3060;"
        )
        c_lay = QHBoxLayout(conclusion_bar)
        c_lay.setContentsMargins(0, 0, 16, 0)
        c_lay.setSpacing(0)
        c_accent = QFrame()
        c_accent.setFixedWidth(3)
        c_accent.setStyleSheet("background-color: #e94560; border-radius: 1px;")
        self._conclusion_lbl = QLabel('正在加载…')
        self._conclusion_lbl.setWordWrap(True)
        self._conclusion_lbl.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._conclusion_lbl.setStyleSheet(
            "color: #c8d0e8; font-size: 13px; padding-left: 10px; background: transparent;"
        )
        c_lay.addSpacing(12)
        c_lay.addWidget(c_accent)
        c_lay.addWidget(self._conclusion_lbl, stretch=1)
        self._conclusion_bar = conclusion_bar
        root.addWidget(conclusion_bar)

        # ── AI 分析免责声明 ──
        disclaimer = QLabel('⚠️ AI分析可能有误，内容仅供参考，不构成投资建议')
        disclaimer.setStyleSheet('color: #ffaa55; font-size: 11px; font-weight: bold;')
        disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(disclaimer)

        # 过滤栏
        filter_bar = QWidget()
        filter_bar.setFixedHeight(44)
        filter_bar.setStyleSheet(
            f"background-color: #111a30; border-bottom: 1px solid #1e2d50;"
        )
        f_lay = QHBoxLayout(filter_bar)
        f_lay.setContentsMargins(12, 6, 12, 6)
        f_lay.setSpacing(4)

        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for key, label in _FILTER_ORDER:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #556688;
                    border: 1px solid #1e2d50;
                    border-radius: 4px;
                    font-size: 11px;
                    padding: 0px 10px;
                }
                QPushButton:checked {
                    background-color: #1c0d1a;
                    color: #e94560;
                    border: 1px solid #e94560;
                    border-top: 2px solid #e94560;
                    font-weight: bold;
                }
                QPushButton:hover:!checked {
                    color: #99bbdd;
                    background-color: #131e35;
                    border-color: #3a5080;
                }
            """)
            if key == '':
                btn.setChecked(True)
            btn.clicked.connect(lambda _checked, k=key: self._on_filter(k))
            self._filter_group.addButton(btn)
            f_lay.addWidget(btn)
        f_lay.addStretch()

        root.addWidget(filter_bar)

        # 主体 splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(
            "QSplitter::handle { background-color: #1e3060; }"
        )

        # 左：事件列表
        left_widget = QWidget()
        left_widget.setObjectName('leftPanel')
        left_widget.setStyleSheet(f"QWidget#leftPanel {{ background-color: #0d1627; }}")
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(4)

        # 排序切换栏
        sort_bar = QWidget()
        sort_bar.setFixedHeight(32)
        sort_bar.setStyleSheet('background-color: transparent;')
        sb_lay = QHBoxLayout(sort_bar)
        sb_lay.setContentsMargins(0, 0, 0, 4)
        sb_lay.setSpacing(4)
        self._sort_btns: dict[str, QPushButton] = {}
        for _sm, _sl in [('heat', '热度排序'), ('time', '时间排序')]:
            btn = QPushButton(_sl)
            btn.setFixedHeight(24)
            btn.setCheckable(True)
            btn.setChecked(_sm == self._sort_mode)
            btn.setStyleSheet("""
                QPushButton {
                    color: #556688; font-size: 10px;
                    background-color: transparent;
                    border: 1px solid #1e2d50; border-radius: 3px; padding: 0 8px;
                }
                QPushButton:checked {
                    color: #c8d0e8; background-color: #1a2d50; border-color: #3a5a9a;
                }
                QPushButton:hover:!checked { color: #99bbdd; }
            """)
            btn.clicked.connect(lambda _c, m=_sm: self._on_sort_toggle(m))
            self._sort_btns[_sm] = btn
            sb_lay.addWidget(btn)
        sb_lay.addStretch()
        left_layout.addWidget(sort_bar)

        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_scroll.setStyleSheet(
            "QScrollArea { background-color: #0d1627; border: none; }"
        )
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background-color: #0d1627;")
        self._list_vbox = QVBoxLayout(self._list_container)
        self._list_vbox.setContentsMargins(0, 0, 0, 0)
        self._list_vbox.setSpacing(4)
        self._list_vbox.addStretch()
        self._list_scroll.setWidget(self._list_container)
        left_layout.addWidget(self._list_scroll)

        # 右：详情卡
        self._detail_card = _DetailCard()

        left_widget.setMinimumWidth(300)
        splitter.addWidget(left_widget)
        splitter.addWidget(self._detail_card)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

        root.addWidget(splitter, stretch=1)

    # ---- 公共 API ----
    def refresh(self):
        self._events = _load_feed()
        self._apply_filter()
        self._heat_scores = self._compute_all_heats()
        self._signal_states = self._compute_all_signals()
        self._warn_event_ids = self._compute_warn_event_ids()
        self._rebuild_list()
        now = datetime.now().strftime('%H:%M:%S')
        self._meta_lbl.setText(f"共 {len(self._events)} 条事件 | {now}")
        self.point_count_changed.emit(len(self._events))
        self.bottom_status_changed.emit(f'情报面板已更新 {now}')
        if self._selected_id and self._selected_id in {e.id for e in self._filtered}:
            self._show_detail(self._selected_id)
        else:
            self._selected_id = None
            self._detail_card.show_aggregation(self._events, self._get_inflows())
        self._update_conclusion()

    def reapply_font(self):
        """字号切换后立即重建动态内容（不重新拉数据）。"""
        self._rebuild_list()
        if self._selected_id and self._selected_id in {e.id for e in self._filtered}:
            self._show_detail(self._selected_id)
        else:
            self._detail_card.show_aggregation(self._events, self._get_inflows())

    def auto_refresh(self):
        """定时触发：刷新资金反馈（选中事件或叙事视图）。"""
        if self._selected_id:
            self._show_detail(self._selected_id)
        else:
            self._detail_card.show_aggregation(self._events, self._get_inflows())
        self._heat_scores = self._compute_all_heats()
        self._signal_states = self._compute_all_signals()
        self._warn_event_ids = self._compute_warn_event_ids()
        self._update_conclusion()

    def current_point_count(self) -> int:
        return len(self._events)

    # ---- 自动获取 ----
    def _on_auto_fetch(self):
        """点击"🤖 自动获取"按钮时触发。"""
        api_key = _fetcher.load_api_key()

        if not api_key:
            key, ok = QInputDialog.getText(
                self, 'DeepSeek API Key',
                '请输入 DeepSeek API Key（仅保存在本地 aldebaran_data/intel_config.json）：',
                QLineEdit.EchoMode.Password,
            )
            if not ok or not key.strip():
                return
            api_key = key.strip()
            _fetcher.save_api_key(api_key)

        target, ok = QInputDialog.getInt(
            self, '自动获取情报',
            '目标获取条数（一批批排队抓取直到达标）：',
            20, 1, 1000, 1,
        )
        if not ok:
            return
        target = max(1, target)

        self._auto_fetch_btn.setEnabled(False)
        self._auto_fetch_btn.setText('获取中…')
        self.status_changed.emit('loading', f'正在自动获取财经快讯（目标 {target} 条）…')

        self._fetch_worker = _FetchWorker(api_key, target)
        self._fetch_worker.progress.connect(self._on_fetch_progress)
        self._fetch_worker.finished.connect(self._on_fetch_done)
        self._fetch_worker.start()

    def _on_fetch_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self.status_changed.emit('loading', f'{msg}（{current}/{total}）')
        else:
            self.status_changed.emit('loading', msg)

    def _on_fetch_done(self, added: int, error: str, note: str, stopped_early: bool):
        self._auto_fetch_btn.setEnabled(True)
        self._auto_fetch_btn.setText('🤖 自动获取')
        if added > 0:
            self.refresh()
        if error:
            QMessageBox.warning(self, '自动获取失败', error)
            self.status_changed.emit('error', f'自动获取失败: {error}')
        elif stopped_early:
            QMessageBox.information(self, '自动获取已停止', note)
            self.status_changed.emit('success', f'自动获取已停止，共新增 {added} 条情报')
        else:
            detail = f'  [{note}]' if note else ''
            self.status_changed.emit('success', f'自动获取完成，新增 {added} 条情报{detail}')

    def _on_sector_analyze_and_track(self):
        """分析当前筛选板块并自动建立追踪任务。"""
        from core.intel_models import CATEGORY_LABELS
        category = self._current_category
        if not category:
            return
        api_key = _fetcher.load_api_key()
        if not api_key:
            key, ok = QInputDialog.getText(
                self, 'DeepSeek API Key',
                '请输入 DeepSeek API Key：',
                QLineEdit.EchoMode.Password,
            )
            if not ok or not key.strip():
                return
            api_key = key.strip()
            _fetcher.save_api_key(api_key)

        sector_name = CATEGORY_LABELS.get(category, category)
        if self._sector_track_worker and self._sector_track_worker.isRunning():
            return

        self._sector_track_btn.setEnabled(False)
        self._sector_track_btn.setText('⏳ 分析中…')

        self._sector_track_worker = _SectorTrackWorker(
            sector_name=sector_name,
            category=category,
            api_key=api_key,
            intel_events=list(self._filtered),
            parent=self,
        )
        self._sector_track_worker.finished.connect(self._on_sector_track_done)
        self._sector_track_worker.error.connect(self._on_sector_track_error)
        self._sector_track_worker.start()

    def _on_sector_track_done(self, sector_name: str, result_text: str, task_id: str):
        self._sector_track_btn.setEnabled(bool(self._current_category))
        self._sector_track_btn.setText('🤖 分析当前板块')
        dlg = QDialog(self)
        dlg.setWindowTitle(f'板块分析 — {sector_name}')
        dlg.resize(720, 520)
        lay = QVBoxLayout(dlg)
        text_edit = QPlainTextEdit(result_text)
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet(
            'background:#0d1627; color:#c8d0e8; font-size:13px; border:none;'
        )
        lay.addWidget(text_edit)
        if task_id:
            tip = QLabel(f'✅ 已建立追踪任务 {task_id}')
            tip.setStyleSheet('color:#2ecc71; font-size:12px;')
            lay.addWidget(tip)
        close_btn = QPushButton('关闭')
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    def _on_sector_track_error(self, msg: str):
        self._sector_track_btn.setEnabled(bool(self._current_category))
        self._sector_track_btn.setText('🤖 分析当前板块')
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, '分析失败', msg)

    # ---- 内部方法 ----
    def _on_filter(self, category: str):
        self._current_category = category
        self._sector_track_btn.setEnabled(bool(category))
        self._apply_filter()
        self._rebuild_list()
        self._selected_id = None
        self._detail_card.show_aggregation(self._events, self._get_inflows())

    def _on_back_to_overview(self):
        if self._selected_id and self._selected_id in self._item_widgets:
            self._item_widgets[self._selected_id].set_selected(False)
        self._selected_id = None
        self._detail_card.show_aggregation(self._events, self._get_inflows())

    def _apply_filter(self):
        if self._current_category:
            self._filtered = [e for e in self._events if e.category == self._current_category]
        else:
            self._filtered = list(self._events)

    def _rebuild_list(self):
        self._item_widgets.clear()
        # 清空旧 items（保留最后的 stretch）
        while self._list_vbox.count():
            item = self._list_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        today = date.today()
        last_date: Optional[date] = None

        if self._sort_mode == 'heat':
            display_evs = sorted(
                self._filtered,
                key=lambda e: (1 if self._signal_states.get(e.id) == 'expired' else 0,
                               -self._heat_scores.get(e.id, 0.0)),
            )
        else:
            display_evs = sorted(
                self._filtered,
                key=lambda e: 1 if self._signal_states.get(e.id) == 'expired' else 0,
            )

        for event in display_evs:
            # 日期分组标题（仅时间排序模式显示）
            if self._sort_mode == 'time' and event.date != last_date:
                last_date = event.date
                if event.date == today:
                    label_text = '今日'
                elif event.date:
                    label_text = event.date.strftime('%m月%d日')
                else:
                    label_text = '未知日期'
                date_lbl = QLabel(label_text)
                _set_font_scaled_style(date_lbl,
                    f"color: {MUTED}; font-size: 10px; font-weight: bold;"
                    f" padding: 4px 4px 2px 4px;"
                )
                self._list_vbox.addWidget(date_lbl)

            item_w = _EventItem(
                event,
                heat=self._heat_scores.get(event.id, 0.0),
                warn_switch=(event.id in self._warn_event_ids),
                signal=self._signal_states.get(event.id, 'pending'),
                expired=(self._signal_states.get(event.id) == 'expired'),
            )
            item_w.clicked.connect(self._on_item_clicked)
            self._item_widgets[event.id] = item_w
            self._list_vbox.addWidget(item_w)

        if not self._filtered:
            empty = QLabel('暂无事件\n请编辑 data/intel_feed.json 添加')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            self._list_vbox.addWidget(empty)

        self._list_vbox.addStretch()

    def _on_item_clicked(self, event_id: str):
        if self._selected_id and self._selected_id in self._item_widgets:
            self._item_widgets[self._selected_id].set_selected(False)
        self._selected_id = event_id
        if event_id in self._item_widgets:
            self._item_widgets[event_id].set_selected(True)
        self._show_detail(event_id)

    def _show_detail(self, event_id: str):
        event = next((e for e in self._events if e.id == event_id), None)
        if event is None:
            return
        inflows = self._get_inflows()
        self._detail_card.update_event(event, inflows)

    def _get_inflows(self) -> dict:
        """从 SectorPanel 获取资金快照并展平为 {板块名: 净额} 字典。"""
        if self._sector_panel_ref is None:
            return {}
        try:
            ctx = self._sector_panel_ref.get_market_context()
            merged = {}
            for kind_data in ctx.values():
                merged.update(kind_data)
            return merged
        except Exception:
            return {}

    def _load_sort_mode(self) -> str:
        try:
            with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f).get('sort_mode', 'heat')
        except Exception:
            return 'heat'

    def _save_sort_mode(self, mode: str):
        data: dict = {}
        try:
            with open(_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            pass
        data['sort_mode'] = mode
        try:
            os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
            with open(_SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _on_sort_toggle(self, mode: str):
        self._sort_mode = mode
        self._save_sort_mode(mode)
        for m, btn in self._sort_btns.items():
            btn.setChecked(m == mode)
        self._rebuild_list()

    def _compute_all_heats(self) -> dict:
        inflows = self._get_inflows()
        return {e.id: _compute_heat(e, inflows) for e in self._events}

    def _compute_all_signals(self) -> dict:
        inflows = self._get_inflows()
        return {e.id: _compute_event_signal(e, inflows) for e in self._events}

    def _compute_warn_event_ids(self) -> set:
        if not self._heat_scores:
            return set()
        sector_entries: dict = {}
        for event in self._events:
            h = self._heat_scores.get(event.id, 0.0)
            for sector in event.related_sectors:
                if sector not in sector_entries:
                    sector_entries[sector] = []
                sector_entries[sector].append((event.timestamp, event.id, h))
        warn_ids: set = set()
        for entries in sector_entries.values():
            if len(entries) < 3:
                continue
            entries.sort(key=lambda x: x[0])
            last3 = entries[-3:]
            h1, h2, h3 = last3[0][2], last3[1][2], last3[2][2]
            if h1 > h2 > h3 and h1 > 0 and h3 < h1 * 0.7:
                warn_ids.add(last3[2][1])
        return warn_ids

    def _compute_divergence_alert(self) -> dict:
        """聚合板块信号，检测全场/大多数背离条件。

        复用 _signal_states 对每个板块多数投票，统计背离板块比例。
        返回 {'state': 'full'|'major'|'normal', 'N': int, 'D': int, 'best_resonance': str}
        """
        sector_events: dict = {}
        for event in self._events:
            for sector in event.related_sectors:
                sector_events.setdefault(sector, []).append(event)

        qualifying = {s: evs for s, evs in sector_events.items() if len(evs) >= 2}
        N = len(qualifying)
        if N < 3:
            return {'state': 'normal', 'N': N, 'D': 0, 'best_resonance': ''}

        inflows = self._get_inflows()
        D = 0
        best_resonance = ''
        best_resonance_net = -1.0
        best_div_event = None
        best_div_heat = -1.0
        for sector, evs in qualifying.items():
            signals = [s for e in evs
                       if (s := self._signal_states.get(e.id, 'pending')) != 'expired']
            if not signals:
                continue
            resonance_cnt = signals.count('resonance')
            divergence_cnt = signals.count('divergence')
            pending_cnt = signals.count('pending')
            if divergence_cnt > resonance_cnt and divergence_cnt > pending_cnt:
                D += 1
                for ev in evs:
                    if self._signal_states.get(ev.id) == 'divergence':
                        h = self._heat_scores.get(ev.id, 0.0)
                        if h > best_div_heat:
                            best_div_heat, best_div_event = h, ev
            elif resonance_cnt > divergence_cnt and resonance_cnt > pending_cnt:
                flow = _find_inflow(sector, inflows)
                net = abs(flow['net']) if flow else 0.0
                if net > best_resonance_net:
                    best_resonance_net = net
                    best_resonance = sector

        if D == N:
            state = 'full'
        elif D / N >= 0.75:
            state = 'major'
        else:
            state = 'normal'
        return {
            'state': state, 'N': N, 'D': D,
            'best_resonance': best_resonance,
            'best_div_event': best_div_event,
        }

    def _build_reasoning_chain(self, alert: dict) -> str:
        """根据最佳背离情报和实时资金流生成三步推理链文字。
        数据不足时返回空串，调用方不追加推理链。
        """
        ev = alert.get('best_div_event')
        if ev is None:
            return ''
        dir_label = {'bullish': '利好', 'bearish': '利空', 'neutral': '中性'}.get(
            ev.direction, '—'
        )
        inflows = self._get_inflows()
        sector_parts = []
        total_net = 0.0
        for sector in ev.related_sectors[:4]:
            flow = _find_inflow(sector, inflows)
            if flow is None:
                continue
            net = flow['net']
            total_net += net
            sign = '+' if net >= 0 else ''
            sector_parts.append(f'{sector} {sign}{net:.0f}亿')
        if not sector_parts:
            return ''
        flow_dir = '净流出' if total_net < 0 else '净流入'
        br = alert.get('best_resonance', '')
        line1 = f'① 「{ev.title}」→ 消息面：{dir_label}'
        line2 = f'② 关联资金：{"　".join(sector_parts)}，合计{flow_dir} {abs(total_net):.0f}亿'
        if alert['state'] == 'major' and br:
            line3 = f'③ 多数板块背离，仅「{br}」方向资金共振'
        else:
            line3 = '③ 资金方向相反 → 背离，主力疑似派发，建议观望'
        return f'{line1}\n{line2}\n{line3}'

    def _update_conclusion(self):
        """重新计算今日结论并更新结论栏标签。"""
        _normal_bg = "background-color: #08101e; border-bottom: 1px solid #1e3060;"
        today = date.today()
        today_evs = [e for e in self._events if e.date == today]
        if not today_evs:
            self._conclusion_lbl.setText('今日暂无情报数据')
            self._conclusion_bar.setStyleSheet(_normal_bg)
            self._conclusion_bar.setFixedHeight(38)
            return

        # ── 全场背离预警（优先级最高）──
        alert = self._compute_divergence_alert()
        if alert['state'] in ('full', 'major'):
            N, D = alert['N'], alert['D']
            br = alert['best_resonance']
            if alert['state'] == 'full':
                headline = (
                    f'⚠️ 全场背离：{N}个主线板块资金均与消息面相反，'
                    '市场整体处于派发状态，注意风险。'
                )
                bg = '#3a1a1a'
                a_level = 'critical'
                a_key = 'intel_full_divergence'
                a_title = '全场资金背离预警'
            else:
                br_part = f'，{br}是当前唯一共振方向' if br else ''
                headline = f'⚠️ 普遍背离：{D}/{N}个主线板块出现资金背离{br_part}。'
                bg = '#2a1a0a'
                a_level = 'warning'
                a_key = 'intel_major_divergence'
                a_title = '普遍资金背离预警'
            chain = self._build_reasoning_chain(alert)
            if chain:
                full_text = f'{headline}\n{chain}'
                self._conclusion_bar.setFixedHeight(96)
            else:
                full_text = headline
                self._conclusion_bar.setFixedHeight(38)
            self._conclusion_lbl.setText(full_text)
            self._conclusion_bar.setStyleSheet(
                f"background-color: {bg}; border-bottom: 1px solid #1e3060;"
            )
            ac = getattr(self, 'alert_center', None)
            if ac:
                ac.try_trigger(
                    event_type='intel',
                    title=a_title,
                    message=headline,
                    level=a_level,
                    key=a_key,
                    cooldown=1800,
                )
            return

        # ── 正常结论（恢复背景色）──
        self._conclusion_bar.setStyleSheet(_normal_bg)
        inflows = self._get_inflows()
        _prio = {'resonance': 2, 'divergence': 1, 'pending': 0}
        sector_best: dict = {}
        for ev in today_evs:
            for sector in ev.related_sectors:
                flow = _find_inflow(sector, inflows)
                abs_net = abs(flow['net']) if flow else 0.0
                if flow is None or ev.direction == 'neutral':
                    sig_type = 'pending'
                else:
                    net = flow['net']
                    is_resonant = (
                        (ev.direction == 'bullish' and net > 0) or
                        (ev.direction == 'bearish' and net < 0)
                    )
                    sig_type = 'resonance' if is_resonant else 'divergence'
                prev = sector_best.get(sector)
                if prev is None or abs_net > prev['abs_net'] or (
                    abs_net == prev['abs_net'] and
                    _prio[sig_type] > _prio[prev['type']]
                ):
                    sector_best[sector] = {'type': sig_type, 'abs_net': abs_net}
        resonance_list = sorted(
            [(s, d['abs_net']) for s, d in sector_best.items() if d['type'] == 'resonance'],
            key=lambda x: -x[1],
        )
        divergence_list = sorted(
            [(s, d['abs_net']) for s, d in sector_best.items() if d['type'] == 'divergence'],
            key=lambda x: -x[1],
        )
        pending_list = sorted(
            [(s, d['abs_net']) for s, d in sector_best.items() if d['type'] == 'pending'],
            key=lambda x: -x[1],
        )
        parts = []
        if resonance_list:
            parts.append(f'{resonance_list[0][0]}共振最强')
        if divergence_list:
            parts.append(f'{divergence_list[0][0]}出现资金背离')
        if pending_list and parts:
            parts.append(f'{pending_list[0][0]}处于待验证阶段')
        text = (
            '当前暂无明确共振信号，建议观望。'
            if not parts else
            '，'.join(parts) + '。'
        )
        self._conclusion_lbl.setText(text)


# =========================================================
# 工具：模糊匹配板块名
# =========================================================
def _find_inflow(sector: str, inflows: dict) -> Optional[dict]:
    """在 inflows 字典中查找包含 sector 关键词的板块，返回 {'net': float, 'pct': float}。"""
    def _wrap(v):
        return v if isinstance(v, dict) else {'net': float(v), 'pct': 0.0}
    if sector in inflows:
        return _wrap(inflows[sector])
    for key, val in inflows.items():
        if sector in key or key in sector:
            return _wrap(val)
    return None


_EXPIRY_DAYS = {'critical': 3, 'important': 2, 'info': 1}


def _is_expired(event: IntelEvent) -> bool:
    """判断情报是否超出时效窗口（critical 3天/important 2天/info 1天）。"""
    age = (date.today() - event.date).days if event.date else 999
    return age > _EXPIRY_DAYS.get(event.level, 1)


def _compute_event_signal(event: IntelEvent, inflows: dict) -> str:
    """根据情报情绪×关联板块资金方向，计算单条情报信号状态。
    全部共振→'resonance'，存在背离→'divergence'，其余→'pending'。"""
    if _is_expired(event):
        return 'expired'
    if not event.related_sectors or event.direction == 'neutral':
        return 'pending'
    signals = []
    for sector in event.related_sectors:
        flow = _find_inflow(sector, inflows)
        if flow is None:
            signals.append('pending')
            continue
        net = flow['net']
        is_resonant = (
            (event.direction == 'bullish' and net > 0) or
            (event.direction == 'bearish' and net < 0)
        )
        signals.append('resonance' if is_resonant else 'divergence')
    if not signals:
        return 'pending'
    if 'divergence' in signals:
        return 'divergence'
    if all(s == 'resonance' for s in signals):
        return 'resonance'
    return 'pending'


def _compute_heat(event: IntelEvent, inflows: dict) -> float:
    """计算情报热度分数：情报权重 × 最强板块共振强度 × 时效衰减。"""
    if _is_expired(event):
        return 0.0
    today = date.today()
    days_ago = (today - event.date).days if event.date else 7
    if days_ago <= 0:
        decay = 1.0
    elif days_ago == 1:
        decay = 0.6
    elif days_ago == 2:
        decay = 0.3
    else:
        decay = 0.1
    weight = 1.5 if event.direction in ('bullish', 'bearish') else 0.8
    best_strength = 0.0
    for sector in event.related_sectors:
        flow = _find_inflow(sector, inflows)
        if flow is None:
            continue
        abs_net = abs(flow['net'])
        net = flow['net']
        if event.direction == 'neutral':
            factor = 0.3
        elif (
            (event.direction == 'bullish' and net > 0) or
            (event.direction == 'bearish' and net < 0)
        ):
            factor = 1.0
        else:
            factor = 0.5
        best_strength = max(best_strength, abs_net * factor)
    return weight * best_strength * decay


# ======================================================================
# 后台抓取线程
# ======================================================================

class _FetchWorker(QThread):
    """在后台线程执行 intel_fetcher.run_fetch，一批批排队抓取直到达成目标条数。

    每批调用一次 run_fetch；累计新增达到 target 即停。若连续两批新增均 <5 条，
    说明可抓取的新情报已枯竭，自动停止并汇报。
    """

    progress = Signal(int, int, str)        # (current, total, msg)
    finished = Signal(int, str, str, bool)  # (total_added, error, note, stopped_early)

    def __init__(self, api_key: str, target: int, limit: int = 120,
                 llm_limit: int = 80, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._target = target
        self._limit = limit
        self._llm_limit = llm_limit

    def run(self):
        total_added = 0
        batch_no = 0
        last_summary = ''
        low_streak = 0
        while total_added < self._target:
            batch_no += 1
            done = total_added
            added, error, summary = _fetcher.run_fetch(
                api_key=self._api_key,
                limit=self._limit,
                llm_limit=self._llm_limit,
                progress_cb=lambda cur, tot, msg, _b=batch_no, _d=done:
                    self.progress.emit(
                        _d, self._target,
                        f'第{_b}批（已入库 {_d}/{self._target}）· {msg}'
                    ),
            )
            if error:
                self.finished.emit(total_added, error, summary, False)
                return
            total_added += added
            last_summary = summary
            if total_added >= self._target:
                break
            low_streak = low_streak + 1 if added < 5 else 0
            if low_streak >= 2:
                self.finished.emit(
                    total_added, '',
                    f'情报获取难以达到目标数量，已自动停止'
                    f'（目标 {self._target} 条，实际新增 {total_added} 条）',
                    True,
                )
                return
        self.finished.emit(total_added, '', last_summary, False)



# ─────────────────────────────────────────────────────────────────────
# _SectorTrackWorker — 分析板块 + 自动建追踪任务
# ─────────────────────────────────────────────────────────────────────

class _SectorTrackWorker(QThread):
    """后台：analyze_sector → parse direction → fetch_sector_kline → create_task。"""

    finished = Signal(str, str, str)   # (sector_name, result_text, task_id)
    error    = Signal(str)

    def __init__(self, sector_name: str, category: str, api_key: str,
                 intel_events: list, parent=None):
        super().__init__(parent)
        self._sector_name = sector_name
        self._category = category
        self._api_key = api_key
        self._intel_events = intel_events

    def run(self):
        from datetime import date
        from core.intel_fetcher import analyze_sector
        from core.kline_provider import fetch_sector_kline
        from core import tracking

        # 空 context（板块快速分析模式，不依赖当日资金流）
        context: dict = {'fund_flow': None, 'timeline': [], 'emotion': None,
                         'minute': None, 'constituents': None, 'top_sectors': []}
        reasoning, result, err, _cache_key, _now = analyze_sector(
            sector_name=self._sector_name,
            kind='concept',
            api_key=self._api_key,
            intel_events=self._intel_events,
            context=context,
        )
        if err:
            self.error.emit(f'AI 分析失败：{err}')
            return

        # 优先解析 JSON spec，fallback 到关键词扫描
        spec = None
        m = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', result)
        if m:
            try:
                spec = json.loads(m.group(1))
            except Exception:
                spec = None

        _DIR_MAP = {'bullish': '多', 'bearish': '空', 'neutral': '观望'}
        if spec and isinstance(spec, dict):
            direction = _DIR_MAP.get(str(spec.get('direction', '')).lower(), '观望')
            try:
                target_pct = float(spec.get('target_pct', 5.0))
            except (TypeError, ValueError):
                target_pct = 5.0
            try:
                stop_pct = float(spec.get('stop_pct', -2.5))
            except (TypeError, ValueError):
                stop_pct = -2.5
            try:
                horizon_days = max(1, int(spec.get('horizon_days', 5)))
            except (TypeError, ValueError):
                horizon_days = 5
            try:
                confidence = float(spec.get('confidence', 5)) / 10.0
            except (TypeError, ValueError):
                confidence = 0.5
        else:
            # Fallback：关键词扫描（兼容旧缓存 / 模型不输出 JSON 时）
            direction = '多'
            result_lower = result.lower()
            if any(w in result_lower for w in ('看空', '利空', '空头', 'bearish', '下跌', '回调风险')):
                direction = '空'
            try:
                from core.user_profile import load_profile
                prof = load_profile()
                horizon_days = int(prof.get('horizon_days', 5) or 5)
            except Exception:
                horizon_days = 5
            target_pct = 5.0
            stop_pct = -2.5
            confidence = 0.6

        # 入场价：板块指数最新收盘
        entry_price = 0.0
        try:
            df = fetch_sector_kline(self._sector_name, days=5)
            if df is not None and not df.empty:
                entry_price = float(df['close'].iloc[-1])
        except Exception:
            pass

        pred = {
            'kind': 'sector',
            'code': self._category,
            'name': self._sector_name,
            'direction': direction,
            'entry_price': entry_price,
            'target_pct': target_pct,
            'stop_pct': stop_pct,
            'horizon_days': horizon_days,
            'confidence': confidence,
            'reasoning': result[:500],
            'prediction_id': _cache_key,
        }

        task_id = ''
        try:
            task = tracking.create_task(pred, self._intel_events)
            task_id = task.get('id', '')
        except Exception as e:
            # 建任务失败不影响展示分析结果（板块快速分析无 blueprint，暂不入跟踪）
            import logging
            logging.getLogger(__name__).warning('[intel] 建追踪被拒: %s', e)

        # 回写情报库（板块分析结论落档；title 含日期防去重穿透）
        try:
            from core import cache
            from datetime import date as _d
            import logging
            dir_label = '看多' if direction == '多' else '看空'
            today_s = _d.today().strftime('%Y-%m-%d')
            added = cache.append_intel_events([{
                'title': f'板块分析（{today_s}）：{self._sector_name} — {dir_label}',
                'category': self._category,
                'level': 'info',
                'direction': 'bullish' if direction == '多' else 'bearish',
                'summary': result[:200],
                'related_sectors': [self._sector_name],
                'source': 'agent',
            }])
            if added == 0:
                logging.getLogger(__name__).debug(
                    '[sector-track] 情报回写跳过（去重命中）'
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                '[sector-track] 情报回写失败: %s', e
            )

        self.finished.emit(self._sector_name, result, task_id)
