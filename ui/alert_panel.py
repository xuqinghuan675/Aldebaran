"""AlertPanel — 提醒日志 + 设置页面。

显示今日/近10日提醒日志，支持类型筛选、清空，以及规则开关和阈值设置。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QPushButton, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QGridLayout, QScrollArea, QFrame,
    QSplitter,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from core.constants import DARK_BG, CHART_BG, MUTED, RED, GREEN

_POPUP_SIZE_LABELS = [('small', '小（13pt / 1/8屏）'), ('medium', '中（19pt / 1/5屏）'), ('large', '大（31pt / 1/3屏）')]

_TYPE_LABELS = {
    '': '全部',
    'watchlist_pct': '自选股涨跌',
    'ipo_explode': '新股炸板',
    'ipo_reseal': '新股回封',
    'sector_top3': '板块TOP3',
    'resonance': '资金共振',
}

_LEVEL_COLORS = {
    'info': '#5dade2',
    'warning': '#f0c040',
    'critical': '#e94560',
}

_LEVEL_ZH = {
    'info': '信息',
    'warning': '警告',
    'critical': '严重',
}

_LOG_HEADERS = ['时间', '类型', '级别', '标题', '消息']


class AlertPanel(QWidget):
    """提醒设置与日志面板。"""

    status_changed = Signal(str, str)
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, alert_center, parent=None):
        super().__init__(parent)
        self.ac = alert_center
        self.ac.alert_triggered.connect(self._on_new_alert)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # 标题栏
        title_row = QHBoxLayout()
        title = QLabel('🔔 提醒中心')
        title.setStyleSheet('color: #ffffff; font-size: 16px; font-weight: bold;')
        title_row.addWidget(title)
        title_row.addStretch()
        root.addLayout(title_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ---- 上半部：设置区 ----
        settings_widget = self._build_settings()
        splitter.addWidget(settings_widget)

        # ---- 下半部：日志区 ----
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)

        log_toolbar = QHBoxLayout()
        log_toolbar.setSpacing(8)
        log_label = QLabel('📋 提醒日志')
        log_label.setStyleSheet('color: #ffffff; font-size: 13px; font-weight: bold;')
        log_toolbar.addWidget(log_label)

        self._type_filter = QComboBox()
        for key, label in _TYPE_LABELS.items():
            self._type_filter.addItem(label, key)
        self._type_filter.setFixedWidth(120)
        self._type_filter.currentIndexChanged.connect(self._refresh_log)
        log_toolbar.addWidget(self._type_filter)

        self._days_combo = QComboBox()
        self._days_combo.addItem('今日', 1)
        self._days_combo.addItem('近3日', 3)
        self._days_combo.addItem('近10日', 10)
        self._days_combo.setFixedWidth(80)
        self._days_combo.currentIndexChanged.connect(self._refresh_log)
        log_toolbar.addWidget(self._days_combo)

        log_toolbar.addStretch()

        test_btn = QPushButton('测试通知')
        test_btn.setFixedHeight(26)
        test_btn.setToolTip('发送一条测试提醒，验证桌面弹窗是否正常工作')
        test_btn.setStyleSheet("""
            QPushButton { background-color: #1a3a2a; color: #44cc88; border: 1px solid #44cc88;
                border-radius: 3px; font-size: 11px; padding: 2px 10px; }
            QPushButton:hover { background-color: #44cc88; color: #000; }
        """)
        test_btn.clicked.connect(self._test_notification)
        log_toolbar.addWidget(test_btn)

        clear_btn = QPushButton('清空今日')
        clear_btn.setFixedHeight(26)
        clear_btn.setStyleSheet("""
            QPushButton { background-color: #3a1a1a; color: #e84444; border: 1px solid #e84444;
                border-radius: 3px; font-size: 11px; padding: 2px 10px; }
            QPushButton:hover { background-color: #e84444; color: #fff; }
        """)
        clear_btn.clicked.connect(self._clear_today)
        log_toolbar.addWidget(clear_btn)

        log_layout.addLayout(log_toolbar)

        self._summary_lbl = QLabel('今日共振 0 次  TOP3突破 0 次  新股炸板 0 次')
        self._summary_lbl.setStyleSheet(
            f'color: {MUTED}; font-size: 11px; padding: 2px 4px; background: transparent;')
        log_layout.addWidget(self._summary_lbl)

        self._log_table = QTableWidget(0, len(_LOG_HEADERS))
        self._log_table.setHorizontalHeaderLabels(_LOG_HEADERS)
        self._log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._log_table.verticalHeader().setVisible(False)
        self._log_table.setAlternatingRowColors(True)
        self._log_table.setShowGrid(False)
        self._log_table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {CHART_BG};
                alternate-background-color: #1d2d4a;
                color: #dddddd; border: none; border-radius: 6px; font-size: 11px;
            }}
            QHeaderView::section {{
                background-color: #111a30; color: #aaaaaa;
                border: none; padding: 6px; font-weight: bold; font-size: 10px;
            }}
            QTableWidget::item {{ padding: 4px; border: none; }}
        """)
        h = self._log_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        h.resizeSection(3, 200)
        log_layout.addWidget(self._log_table)

        splitter.addWidget(log_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter)

        self.setStyleSheet(f"background-color: {DARK_BG};")
        self._refresh_log()

    # ---- 设置区构建 ----
    def _build_settings(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setMaximumHeight(300)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(5)

        s = self.ac.get_all_settings()

        _CARD = (
            f"QFrame {{ background-color: #0b1628; border: 1px solid #1e2d4a;"
            f" border-radius: 6px; }}"
        )
        _CHK = (
            "QCheckBox {{ spacing: 6px; font-size: 12px; font-weight: bold;"
            " color: {color}; background: transparent; }}"
            "QCheckBox::indicator {{ width: 15px; height: 15px; border: 2px solid #555;"
            " border-radius: 3px; background-color: #0d1525; }}"
            "QCheckBox::indicator:checked {{ background-color: #e94560; border-color: #e94560; }}"
        )
        _MUTED_S = f'color: {MUTED}; font-size: 11px; background: transparent;'
        _SPIN = (
            'QSpinBox, QDoubleSpinBox {'
            'background-color: #1a2540; color: #ddd; border: 1px solid #444;'
            'border-radius: 3px; padding: 1px 4px; font-size: 12px; }'
            'QSpinBox::up-button, QDoubleSpinBox::up-button {'
            'background-color: #1e2d50; border-left: 1px solid #444;'
            'border-bottom: 1px solid #333; width: 20px; border-top-right-radius: 3px; }'
            'QSpinBox::down-button, QDoubleSpinBox::down-button {'
            'background-color: #1e2d50; border-left: 1px solid #444;'
            'width: 20px; border-bottom-right-radius: 3px; }'
            'QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,'
            'QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {'
            'background-color: #2a3f6a; }'
            'QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {'
            ' border-left: 4px solid transparent; border-right: 4px solid transparent;'
            ' border-bottom: 5px solid #aaaaaa; width: 0; height: 0; }'
            'QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {'
            ' border-left: 4px solid transparent; border-right: 4px solid transparent;'
            ' border-top: 5px solid #aaaaaa; width: 0; height: 0; }'
        )

        def _card_row():
            f = QFrame()
            f.setStyleSheet(_CARD)
            h = QHBoxLayout(f)
            h.setContentsMargins(10, 7, 10, 7)
            h.setSpacing(10)
            return f, h

        def _chk(text, color, val, callback):
            c = QCheckBox(text)
            c.setChecked(val)
            c.setStyleSheet(_CHK.format(color=color))
            c.toggled.connect(callback)
            return c

        def _m(text):
            l = QLabel(text)
            l.setStyleSheet(_MUTED_S)
            return l

        def _spin_int(lo, hi, val, callback, width=66):
            w = QSpinBox()
            w.setRange(lo, hi)
            w.setValue(val)
            w.setFixedWidth(width)
            w.setStyleSheet(_SPIN)
            w.valueChanged.connect(callback)
            return w

        def _spin_dbl(lo, hi, val, step, callback, width=66):
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setValue(val)
            w.setSingleStep(step)
            w.setDecimals(1)
            w.setFixedWidth(width)
            w.setStyleSheet(_SPIN)
            w.valueChanged.connect(callback)
            return w

        # ── 全局开关（单独一行，背景略深） ──────────────────────────────
        glob_card = QFrame()
        glob_card.setStyleSheet(
            "QFrame { background-color: #12203a; border: 1px solid #2a4070; border-radius: 6px; }"
        )
        glob_h = QHBoxLayout(glob_card)
        glob_h.setContentsMargins(12, 8, 12, 8)
        glob_h.setSpacing(14)

        self._chk_enabled = QCheckBox('全局提醒开关')
        self._chk_enabled.setChecked(s.get('enabled', True))
        self._chk_enabled.setStyleSheet(
            "QCheckBox { spacing: 7px; font-size: 13px; font-weight: bold; color: #fff; background: transparent; }"
            "QCheckBox::indicator { width: 17px; height: 17px; border: 2px solid #777; border-radius: 4px; background: #0d1525; }"
            "QCheckBox::indicator:checked { background-color: #e94560; border-color: #e94560; }"
        )
        self._chk_enabled.toggled.connect(lambda v: self._save('enabled', v))
        glob_h.addWidget(self._chk_enabled)
        glob_h.addWidget(_m('关闭后所有提醒停止'))
        glob_h.addStretch()
        glob_h.addWidget(_m('通知方式:'))
        self._chk_desktop = _chk('桌面弹窗', '#aaddff',
                                  s.get('desktop_notification', True),
                                  lambda v: self._save('desktop_notification', v))
        glob_h.addWidget(self._chk_desktop)
        self._chk_sound = _chk('声音', '#aaaaaa',
                                s.get('sound', False),
                                lambda v: self._save('sound', v))
        glob_h.addWidget(self._chk_sound)
        lay.addWidget(glob_card)

        # ── 自定义弹窗 ────────────────────────────────────────────────
        f, h = _card_row()
        self._chk_custom_popup = _chk('自定义弹窗', '#aaffcc',
                                      s.get('custom_popup_enabled', False),
                                      lambda v: self._save('custom_popup_enabled', v))
        self._chk_custom_popup.setToolTip(
            '启用后使用应用内自绘弹窗替代系统通知（可调整宽度和时长）；'
            'critical 级别通知始终使用自定义弹窗'
        )
        h.addWidget(self._chk_custom_popup)
        h.addSpacing(10)
        h.addWidget(_m('大小'))
        self._cbo_popup_size = QComboBox()
        for val, label in _POPUP_SIZE_LABELS:
            self._cbo_popup_size.addItem(label, val)
        cur_size = s.get('popup_size', 'medium')
        for i in range(self._cbo_popup_size.count()):
            if self._cbo_popup_size.itemData(i) == cur_size:
                self._cbo_popup_size.setCurrentIndex(i)
                break
        self._cbo_popup_size.setFixedWidth(110)
        self._cbo_popup_size.setStyleSheet(
            'QComboBox { background:#1a2540; color:#ddd; border:1px solid #444;'
            ' border-radius:3px; padding:1px 4px; font-size:12px; }'
            'QComboBox::drop-down { border:none; }'
        )
        self._cbo_popup_size.currentIndexChanged.connect(
            lambda _: self._save('popup_size', self._cbo_popup_size.currentData())
        )
        h.addWidget(self._cbo_popup_size)
        h.addSpacing(10)
        h.addWidget(_m('时长'))
        self._spn_popup_dur = _spin_int(3, 30,
                                        s.get('popup_duration', 5),
                                        lambda v: self._save('popup_duration', v),
                                        width=60)
        h.addWidget(self._spn_popup_dur)
        h.addWidget(_m('秒'))
        h.addStretch()
        lay.addWidget(f)

        # ── 自选股 ────────────────────────────────────────────────────
        f, h = _card_row()
        self._chk_wl = _chk('自选股涨跌幅', '#5dade2',
                              s.get('watchlist_pct_enabled', True),
                              lambda v: self._save('watchlist_pct_enabled', v))
        h.addWidget(self._chk_wl)
        _wl_ths = s.get('watchlist_pct_thresholds', [5, 8, 10])
        h.addWidget(_m('阈值 ±'))
        self._spn_wl_th1 = _spin_int(1, 30, _wl_ths[0] if len(_wl_ths) > 0 else 5,
                                      lambda v: self._save_thresholds())
        self._spn_wl_th2 = _spin_int(1, 30, _wl_ths[1] if len(_wl_ths) > 1 else 8,
                                      lambda v: self._save_thresholds())
        self._spn_wl_th3 = _spin_int(1, 30, _wl_ths[2] if len(_wl_ths) > 2 else 10,
                                      lambda v: self._save_thresholds())
        h.addWidget(self._spn_wl_th1)
        h.addWidget(_m('%  ±'))
        h.addWidget(self._spn_wl_th2)
        h.addWidget(_m('%  ±'))
        h.addWidget(self._spn_wl_th3)
        h.addWidget(_m('%'))
        h.addStretch()
        h.addWidget(_m('冷却'))
        self._spn_wl_cd = _spin_int(1, 120,
                                     s.get('watchlist_pct_cooldown', 600) // 60,
                                     lambda v: self._save('watchlist_pct_cooldown', v * 60))
        h.addWidget(self._spn_wl_cd)
        h.addWidget(_m('分钟'))
        lay.addWidget(f)

        # ── 新股 ──────────────────────────────────────────────────────
        f, h = _card_row()
        self._chk_explode = _chk('新股炸板', '#f0c040',
                                   s.get('ipo_explode_enabled', True),
                                   lambda v: self._save('ipo_explode_enabled', v))
        self._chk_reseal = _chk('回封提醒', '#f0c040',
                                  s.get('ipo_reseal_enabled', True),
                                  lambda v: self._save('ipo_reseal_enabled', v))
        h.addWidget(self._chk_explode)
        h.addWidget(self._chk_reseal)
        h.addStretch()
        h.addWidget(_m('冷却固定 5 分钟'))
        lay.addWidget(f)

        # ── 板块 TOP3 ─────────────────────────────────────────────────
        top3_card = QFrame()
        top3_card.setStyleSheet(_CARD)
        top3_v = QVBoxLayout(top3_card)
        top3_v.setContentsMargins(10, 7, 10, 7)
        top3_v.setSpacing(5)

        top3_row1 = QHBoxLayout()
        top3_row1.setSpacing(10)
        self._chk_top3 = _chk('板块TOP3突破', '#e94560',
                                s.get('sector_top3_enabled', True),
                                lambda v: self._save('sector_top3_enabled', v))
        top3_row1.addWidget(self._chk_top3)
        top3_row1.addStretch()
        top3_row1.addWidget(_m('净流入≥'))
        self._spn_top3_net = _spin_dbl(0.5, 50.0,
                                        s.get('sector_top3_min_net', 3.0),
                                        0.5,
                                        lambda v: self._save('sector_top3_min_net', v))
        top3_row1.addWidget(self._spn_top3_net)
        top3_row1.addWidget(_m('亿'))
        top3_row1.addSpacing(8)
        top3_row1.addWidget(_m('冷却'))
        self._spn_top3_cd = _spin_int(1, 120,
                                       s.get('sector_top3_cooldown', 900) // 60,
                                       lambda v: self._save('sector_top3_cooldown', v * 60))
        top3_row1.addWidget(self._spn_top3_cd)
        top3_row1.addWidget(_m('分钟'))
        top3_v.addLayout(top3_row1)

        top3_row2 = QHBoxLayout()
        top3_row2.setSpacing(10)
        top3_row2.addWidget(_m('开盘静默'))
        self._spn_silence = _spin_int(0, 15,
                                       int(s.get('open_silence_minutes', 5)),
                                       lambda v: self._save('open_silence_minutes', v),
                                       width=56)
        top3_row2.addWidget(self._spn_silence)
        top3_row2.addWidget(_m('分钟（开盘/午盘头N分钟跳过，防集中爆发）'))
        top3_row2.addStretch()
        top3_row2.addWidget(_m('每轮最多推1条，其余写日志'))
        top3_v.addLayout(top3_row2)
        lay.addWidget(top3_card)

        # ── 资金共振 ──────────────────────────────────────────────────
        f, h = _card_row()
        self._chk_resonance = _chk('资金共振', '#e94560',
                                    s.get('resonance_enabled', True),
                                    lambda v: self._save('resonance_enabled', v))
        h.addWidget(self._chk_resonance)
        h.addStretch()
        h.addWidget(_m('净流入≥'))
        self._spn_res_net = _spin_dbl(0.5, 50.0,
                                       s.get('resonance_min_net', 5.0),
                                       0.5,
                                       lambda v: self._save('resonance_min_net', v))
        h.addWidget(self._spn_res_net)
        h.addWidget(_m('亿'))
        h.addSpacing(8)
        h.addWidget(_m('涨停≥'))
        self._spn_res_zt = _spin_int(1, 30,
                                      s.get('resonance_min_zt', 5),
                                      lambda v: self._save('resonance_min_zt', v))
        h.addWidget(self._spn_res_zt)
        h.addWidget(_m('只'))
        h.addSpacing(8)
        h.addWidget(_m('冷却'))
        self._spn_res_cd = _spin_int(1, 60,
                                      s.get('resonance_cooldown', 300) // 60,
                                      lambda v: self._save('resonance_cooldown', v * 60))
        h.addWidget(self._spn_res_cd)
        h.addWidget(_m('分钟'))
        lay.addWidget(f)

        scroll.setWidget(container)
        return scroll

    def _save(self, key, value):
        self.ac.set_setting(key, value)

    def _save_thresholds(self):
        vals = sorted([
            self._spn_wl_th1.value(),
            self._spn_wl_th2.value(),
            self._spn_wl_th3.value(),
        ])
        self._save('watchlist_pct_thresholds', vals)

    # ---- 日志 ----
    def _on_new_alert(self, event):
        self._refresh_log()
        self.bottom_status_changed.emit(
            f"[{event.get('level', 'info').upper()}] {event.get('title', '')} — {event.get('message', '')}"
        )

    def _refresh_log(self):
        days = self._days_combo.currentData() or 1
        type_filter = self._type_filter.currentData() or ''
        entries = self.ac.get_log(days=days)
        if type_filter:
            entries = [e for e in entries if e.get('type') == type_filter]
        # 倒序（最新在前）
        entries = list(reversed(entries))

        self._log_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            ts = e.get('timestamp', '')
            if len(ts) > 10:
                ts = ts[11:]  # 只显示时间部分
            ts_item = QTableWidgetItem(ts)

            type_text = _TYPE_LABELS.get(e.get('type', ''), e.get('type', ''))
            type_item = QTableWidgetItem(type_text)

            level = e.get('level', 'info')
            level_item = QTableWidgetItem(_LEVEL_ZH.get(level, level))
            level_item.setForeground(QColor(_LEVEL_COLORS.get(level, MUTED)))

            title_text = e.get('title', '')
            title_item = QTableWidgetItem(title_text)
            title_item.setToolTip(title_text)

            msg_text = e.get('message', '')
            msg_item = QTableWidgetItem(msg_text)
            msg_item.setToolTip(msg_text)

            for item in (ts_item, type_item, level_item, title_item, msg_item):
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self._log_table.setItem(i, 0, ts_item)
            self._log_table.setItem(i, 1, type_item)
            self._log_table.setItem(i, 2, level_item)
            self._log_table.setItem(i, 3, title_item)
            self._log_table.setItem(i, 4, msg_item)

        # 更新今日摘要
        today_all = self.ac.get_log(days=1)
        cnt = {'resonance': 0, 'sector_top3': 0, 'ipo_explode': 0}
        for e in today_all:
            t = e.get('type', '')
            if t in cnt:
                cnt[t] += 1
        self._summary_lbl.setText(
            f'今日  共振 {cnt["resonance"]} 次   TOP3突破 {cnt["sector_top3"]} 次'
            f'   新股炸板 {cnt["ipo_explode"]} 次'
        )

        self.point_count_changed.emit(len(entries))

    def _test_notification(self):
        from datetime import datetime as _dt
        self.ac.try_trigger(
            event_type='resonance',
            title='🔔 测试通知',
            message='桌面弹窗功能正常，如看到此条日志则通知工作正常',
            level='warning',
            key=f'__test__{_dt.now().strftime("%H%M%S")}',
            cooldown=0,
        )

    def _clear_today(self):
        self.ac.clear_today_log()
        self._refresh_log()

    # ---- 公共 API（与其他面板一致） ----
    def refresh(self):
        self._refresh_log()

    def auto_refresh(self):
        self._refresh_log()
