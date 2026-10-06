"""主窗口框架：顶部工具栏 + 左侧导航 + 右侧 QStackedWidget。

只负责框架与导航，具体面板的业务逻辑在各 ui/*_panel.py 中实现。
通过信号 status_changed / point_count_changed / bottom_status_changed 接收面板状态。
"""
import os
import re as _re
import sys
import threading
from datetime import date, datetime
from pathlib import Path

from core.qt_runtime import configure_qt_runtime

configure_qt_runtime()


def _app_root() -> Path:
    """应用资源根目录。

    一文件模式下 __file__ 指向临时解压目录的 ui/，嵌入资源（app_icon.jpg、data/）
    也在临时解压根目录，因此统一用 __file__ 推算即可，不需要区分模式。
    """
    return Path(__file__).resolve().parent.parent

_FONT_SIZE_RE = _re.compile(r'font-size\s*:\s*(\d+(?:\.\d+)?)(px|pt)\b')
_FONT_BASE_PT = 10  # 所有样式表都是基于 10pt 设计的

from PySide6.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QComboBox, QSpinBox,
    QStackedWidget, QButtonGroup, QSystemTrayIcon, QMenu,
    QDialog, QTabWidget, QScrollArea, QFrame, QApplication,
    QTextBrowser, QFileDialog, QMessageBox,
)
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QIcon, QFont

from core.constants import MUTED, RED, GREEN
from core.data_source import fmt_price
from core.alert_center import AlertCenter
from core.cache import cleanup_stale_sector_cache, load_ui_config, update_ui_config
from ui.sector_panel import SectorPanel


# ---------- 面板占位符 ----------
class _PanelPlaceholder(QWidget):
    """启动期间尚未构建真实面板时的轻量占位符。"""
    def __init__(self, name='', parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(f'{name}\n加载中…')
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet('color: #444; font-size: 14px;')
        lay.addWidget(lbl)


# ---------- 主窗口 ----------
class MainWindow(QMainWindow):
    _portfolio_prices_ready = Signal(dict)
    _tracking_prices_ready = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aldebaran V3.0")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 900)

        # 窗口图标（绝对路径，独立于 QApplication 级别；Nuitka onefile 安全）
        icon_path = str(_app_root() / 'app_icon.jpg')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- 顶部工具栏 ----
        toolbar_widget = QWidget()
        toolbar_widget.setStyleSheet("background-color: #111122; border-bottom: 1px solid #333;")
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(12, 6, 12, 6)
        toolbar.setSpacing(8)

        title_lbl = QLabel("Aldebaran V3.0")
        title_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #e94560;")
        toolbar.addWidget(title_lbl)
        toolbar.addSpacing(16)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setFixedWidth(70)
        self.refresh_btn.clicked.connect(self.fetch_data)
        toolbar.addWidget(self.refresh_btn)

        toolbar.addWidget(QLabel("自动刷新:"))
        self.interval_combo = QComboBox()
        self.interval_combo.addItems(["关闭", "5秒", "15秒", "30秒", "1分钟", "3分钟"])
        self.interval_combo.setCurrentIndex(2)
        self.interval_combo.currentIndexChanged.connect(self.update_timer)
        toolbar.addWidget(self.interval_combo)

        toolbar.addSpacing(10)
        self.point_label = QLabel("数据点: 0")
        self.point_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        toolbar.addWidget(self.point_label)

        # P1-G 字体大小
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("字号:"))
        self.font_combo = QComboBox()
        self.font_combo.addItems(["10pt", "13pt"])
        _cfg = load_ui_config()
        _saved_font = _cfg.get('font_size_pt', 10)
        _fi = {10: 0, 13: 1}.get(_saved_font, 0)
        self.font_combo.setCurrentIndex(_fi)
        self.font_combo.currentIndexChanged.connect(self._on_font_size_changed)
        toolbar.addWidget(self.font_combo)
        self._font_pt = _saved_font          # 当前字号状态，延迟面板构建时需要此字段
        self._apply_font_size(_saved_font)

        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("默认市值:"))
        self.pos_value_spin = QSpinBox()
        self.pos_value_spin.setRange(1000, 100_000_000)
        self.pos_value_spin.setSingleStep(10000)
        self.pos_value_spin.setSuffix(" 元")
        self.pos_value_spin.setGroupSeparatorShown(True)
        self.pos_value_spin.setValue(int(_cfg.get('default_position_value', 80000)))
        self.pos_value_spin.setToolTip("自选/持仓添加股票时，数量留空按此市值÷现价四舍五入到100股估算")
        self.pos_value_spin.editingFinished.connect(self._on_default_pos_value_changed)
        toolbar.addWidget(self.pos_value_spin)

        # P1-F API 费用说明按钮
        toolbar.addSpacing(6)
        api_cost_btn = QPushButton("💲 API")
        api_cost_btn.setFixedWidth(60)
        api_cost_btn.setToolTip(
            "DeepSeek API 费用参考\n"
            "• deepseek-flash（DeepSeek-V4.1-Flash）：情报自动分类、宏观状态机等轻量任务\n"
            "  官方当前价格（低峰/高峰）：缓存命中 $0.003/$0.006/M，缓存未命中 $0.15/$0.30/M，输出 $0.60/$1.20/M\n"
            "• 同一 deepseek-flash thinking：个股 AI 分析 5 Agent + F/G、板块/主线深度分析、自动反思\n"
            "每次分类批次约 0.5–2k tokens\n"
            "每次完整个股 AI 分析约 8–35k tokens（含 5 Agent + F/G thinking）\n"
            "按 $1≈¥6.76、每天分析 10 次估算：约 ¥14–84 / 月（按 30 天）"
        )
        api_cost_btn.setStyleSheet(
            'QPushButton { background-color: #1a2a3a; color: #aaa; border: 1px solid #2f4b7c;'
            '  border-radius: 4px; font-size: 11px; padding: 3px 6px; }'
            'QPushButton:hover { background-color: #2a3a5a; color: #fff; }'
        )
        api_cost_btn.clicked.connect(self._show_api_cost_detail)
        toolbar.addWidget(api_cost_btn)
        toolbar.addSpacing(18)
        self._install_watermark(toolbar)

        toolbar.addStretch()

        self.market_label = QLabel()
        self.market_label.setStyleSheet("font-size: 13px;")
        toolbar.addWidget(self.market_label)

        self.status_label = QLabel("启动中...")
        self.status_label.setStyleSheet(f"color: {MUTED};")
        toolbar.addWidget(self.status_label)

        main_layout.addWidget(toolbar_widget)

        # ---- 主体：侧边栏 + 内容堆叠区 ----
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # 侧边栏
        sidebar = QWidget()
        sidebar.setFixedWidth(88)
        sidebar.setStyleSheet("background-color: #0d0d1f; border-right: 1px solid #333;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 16, 0, 16)
        sidebar_layout.setSpacing(4)

        self._nav_btns = []
        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        nav_items = [
            ("📊", "大盘", 0),
            ("🌍", "全球", 1),
            ("💰", "板块", 2),
            ("📈", "个股", 3),
            ("🔔", "提醒", 4),
            ("🔍", "情报", 5),
            ("💼", "持仓", 6),
            ("🎯", "追踪", 7),
        ]
        for icon, label, idx in nav_items:
            btn = QPushButton(f"{icon}\n{label}")
            btn.setCheckable(True)
            btn.setFixedHeight(56)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #777777;
                    border: none;
                    border-left: 3px solid transparent;
                    border-radius: 0px;
                    font-size: 12px;
                    font-weight: bold;
                    margin: 1px 0px;
                    padding: 8px 4px;
                }
                QPushButton:checked {
                    background-color: #111a30;
                    color: #ffffff;
                    border-left: 3px solid #e94560;
                }
                QPushButton:hover:!checked {
                    background-color: #0d1525;
                    color: #cccccc;
                    border-left: 3px solid #3a4a6a;
                }
            """)
            btn.clicked.connect(lambda checked, i=idx: self._switch_panel(i))
            self._btn_group.addButton(btn, idx)
            self._nav_btns.append(btn)
            sidebar_layout.addWidget(btn)

        sidebar_layout.addStretch()

        _icon_btn_style = """
            QPushButton {
                background-color: transparent;
                color: #ccc;
                border: 1px solid #3a4a6a;
                border-radius: 18px;
                font-size: 15px;
                font-weight: bold;
                margin: 0px;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: #e94560;
                color: #fff;
                border-color: #e94560;
            }
        """

        data_btn = QPushButton('⚙')
        data_btn.setFixedSize(36, 36)
        data_btn.setToolTip('导出 / 导入 / 重置用户数据')
        data_btn.setStyleSheet(_icon_btn_style)
        data_btn.clicked.connect(self._show_data_manager)

        help_btn = QPushButton('?')
        help_btn.setFixedSize(36, 36)
        help_btn.setToolTip('功能说明与术语解释')
        help_btn.setStyleSheet(_icon_btn_style)
        help_btn.clicked.connect(self._show_help)

        help_row = QHBoxLayout()
        help_row.setContentsMargins(0, 0, 0, 8)
        help_row.setSpacing(4)
        help_row.addStretch()
        help_row.addWidget(data_btn)
        help_row.addWidget(help_btn)
        help_row.addStretch()
        sidebar_layout.addLayout(help_row)

        body_layout.addWidget(sidebar)

        # 右侧内容堆叠区
        self.stack = QStackedWidget()

        # AlertCenter 必须立即创建（各面板通过 alert_center 属性注入）
        self.alert_center = AlertCenter(self)

        # 只有默认视图（板块，idx=2）立即构建，其余面板延迟创建
        self.market_panel = None
        self.global_panel = None
        self.stock_panel  = None
        self.alert_panel  = None
        self.intel_panel  = None
        self.portfolio_panel = None
        self.tracking_panel = None

        self.sector_panel = SectorPanel()
        self.sector_panel.alert_center = self.alert_center
        self._attach_panel_signals(self.sector_panel, 2)

        _names = ['大盘', '全球', '板块', '个股', '提醒', '情报', '持仓', '追踪']
        for i in range(8):
            if i == 2:
                self.stack.addWidget(self.sector_panel)
            else:
                self.stack.addWidget(_PanelPlaceholder(_names[i]))

        body_layout.addWidget(self.stack, stretch=1)
        main_layout.addWidget(body, stretch=1)

        # ---- 状态栏 ----
        self.statusBar().showMessage("就绪 - 等待数据...")

        # 初始化已有数据点（缓存可能已加载）
        self.point_label.setText(f"数据点: {self.sector_panel.current_point_count()}")

        # ---- 定时器（自动刷新） ----
        self.timer = QTimer()
        self.timer.timeout.connect(self._on_timer_tick)
        self.update_timer()

        self._post_close_done = None  # update_market_status 盘后分支会读它，须先初始化
        self.update_market_status()

        # 托盘通知
        self._setup_tray_icon()
        self.alert_center.alert_triggered.connect(self._on_alert_triggered)
        self._portfolio_prices_ready.connect(self._apply_portfolio_prices)
        self._tracking_prices_ready.connect(self._apply_tracking_prices)

        # 默认显示板块面板
        self._switch_panel(2, refresh=False)
        # 缓存清理放后台线程，不阻塞主线程
        threading.Thread(
            target=cleanup_stale_sector_cache, args=(7,), daemon=True
        ).start()
        self._startup_refresh_sequence()

        # 每分钟更新盘面状态 + 持仓监控 + 追踪面板价格推送
        self.market_timer = QTimer()
        self.market_timer.timeout.connect(self.update_market_status)
        self.market_timer.timeout.connect(self._monitor_positions_async)
        self.market_timer.start(60000)
        self._closing = False

    def _install_watermark(self, toolbar):
        self._watermark = QLabel('Aldebaran')
        self._watermark.setObjectName('toolbarWatermark')
        self._watermark.setMaximumHeight(34)
        self._watermark.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._watermark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._watermark.setStyleSheet(
            'QLabel#toolbarWatermark {'
            ' color: rgba(233, 69, 96, 72);'
            ' font-size: 22px;'
            ' font-weight: 800;'
            ' padding: 0px 4px;'
            '}'
        )
        toolbar.addWidget(self._watermark)

    def closeEvent(self, event):
        event.accept()
        if self._closing:
            return
        self._closing = True
        self._shutdown_for_window_close()

    def _shutdown_for_window_close(self):
        try:
            self.timer.stop()
        except Exception:
            pass
        try:
            self.market_timer.stop()
        except Exception:
            pass
        try:
            if self._tray:
                self._tray.hide()
        except Exception:
            pass
        guard = threading.Timer(1.5, lambda: os._exit(0))
        guard.daemon = True
        guard.start()
        if QApplication.instance() is not None:
            QApplication.instance().quit()

    # ---- 面板延迟构建 ----
    _PANEL_ATTRS = {0: 'market_panel', 1: 'global_panel', 3: 'stock_panel',
                   4: 'alert_panel',  5: 'intel_panel',
                   6: 'portfolio_panel', 7: 'tracking_panel'}

    def _attach_panel_signals(self, panel, idx):
        panel.status_changed.connect(self._on_panel_status)
        if idx == 2:
            panel.request_add_watchlist.connect(self._on_sector_add_watchlist)
        if idx == 4:
            panel.point_count_changed.connect(
                lambda n: self.point_label.setText(f'提醒: {n} 条')
            )
        elif idx == 5:
            panel.point_count_changed.connect(
                lambda n, p=panel: self.point_label.setText(f'情报: {n} 条')
                if self.stack.currentWidget() is p else None
            )
        else:
            panel.point_count_changed.connect(
                lambda n: self.point_label.setText(f'数据点: {n}')
            )
        panel.bottom_status_changed.connect(
            lambda msg: self.statusBar().showMessage(msg)
        )
        # 情报/大盘/全球面板成功刷新后，推送最新数据到持仓面板
        if idx in (0, 1, 5):
            panel.status_changed.connect(
                lambda state, _msg, _i=idx: self._push_to_portfolio() if state == 'success' else None
            )

    def _build_deferred_panel(self, idx):
        """构建延迟面板并替换占位符；若已构建则直接返回。"""
        if not isinstance(self.stack.widget(idx), _PanelPlaceholder):
            return  # already built
        if idx == 0:
            from ui.market_panel import MarketPanel
            panel = MarketPanel(self)
        elif idx == 1:
            from ui.global_panel import GlobalPanel
            panel = GlobalPanel(self)
        elif idx == 3:
            from ui.stock_panel import StockPanel
            try:
                panel = StockPanel(self)
            except Exception:
                import traceback
                traceback.print_exc()
                msg = '个股页面加载失败，请尝试清除缓存后重启'
                self.statusBar().showMessage(msg, 8000)
                QMessageBox.warning(self, '个股页面加载失败', msg)
                return
        elif idx == 4:
            from ui.alert_panel import AlertPanel
            panel = AlertPanel(self.alert_center, self)
        elif idx == 5:
            from ui.intelligence_network_panel import IntelligenceNetworkPanel
            panel = IntelligenceNetworkPanel(self)
        elif idx == 6:
            from ui.portfolio_panel import PortfolioPanel
            panel = PortfolioPanel(self)
        elif idx == 7:
            from ui.tracking_panel import TrackingPanel
            panel = TrackingPanel(self)
        else:
            return
        self._attach_panel_signals(panel, idx)
        if idx != 5:
            panel.alert_center = self.alert_center
        if idx == 5:
            panel._sector_panel_ref = self.sector_panel
        setattr(self, self._PANEL_ATTRS[idx], panel)
        if idx == 6:
            self._push_to_portfolio()
            panel.tracking_task_created.connect(self._refresh_tracking_panel)
            panel.tracking_tasks_settled.connect(self._refresh_tracking_panel)
            av = getattr(panel, '_analysis_view', None)
            if av and hasattr(av, 'intel_anchor_clicked'):
                av.intel_anchor_clicked.connect(self._on_intel_anchor)
        # 替换 stack 中的占位符，保持索引不变
        was_current = self.stack.currentIndex() == idx
        # 将新面板同步到当前字号（仅作用于新面板，不遍历全局控件）
        _cur_pt = getattr(self, '_font_pt', _FONT_BASE_PT)
        if _cur_pt != _FONT_BASE_PT:
            self._apply_font_to_panel(panel, _cur_pt)
        old = self.stack.widget(idx)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(idx, panel)
        if was_current:
            self.stack.setCurrentIndex(idx)

    def _on_sector_add_watchlist(self, code, name, price):
        """板块加自选 → 确保个股页已构建，弹与自选页一致的「添加自选股」对话框。"""
        self._ensure_panel(3)
        sp = getattr(self, 'stock_panel', None)
        if sp is not None and hasattr(sp, '_quick_add_to_watchlist'):
            sp._quick_add_to_watchlist(code, name, price)

    def _refresh_tracking_panel(self):
        if isinstance(self.stack.widget(7), _PanelPlaceholder):
            self._build_deferred_panel(7)
        tp = getattr(self, 'tracking_panel', None)
        if tp is not None and hasattr(tp, 'refresh'):
            tp.refresh()
            if hasattr(tp, 'request_maintenance'):
                tp.request_maintenance()

    def _ensure_panel(self, idx):
        """导航到某面板时，若尚未构建则立即同步构建。"""
        if isinstance(self.stack.widget(idx), _PanelPlaceholder):
            self._build_deferred_panel(idx)

    # ---- 启动刷新序列 ----
    def _startup_refresh_sequence(self):
        # Phase 1: 轻量面板（无 pandas/matplotlib 依赖，构建极快）
        build_light = [(100, 4), (200, 5), (300, 7)]
        for ms, i in build_light:
            QTimer.singleShot(ms, lambda idx=i: self._build_deferred_panel(idx))

        # Phase 2: 重量面板（各自 lazy import core 模块，错开避免帧卡顿）
        build_heavy = [(600, 0), (1000, 1), (1400, 3), (1800, 6)]
        for ms, i in build_heavy:
            QTimer.singleShot(ms, lambda idx=i: self._build_deferred_panel(idx))

        # Phase 3: 网络刷新（与构建错开，间隔 3.5s 避免踩踏）
        QTimer.singleShot(500, self.sector_panel.refresh)
        # 情报读本地 JSON，等面板构建完再加载
        QTimer.singleShot(600, lambda: self.intel_panel.refresh() if self.intel_panel else None)
        refresh_plan = [(4000, 0), (7500, 1), (11000, 3)]
        for ms, i in refresh_plan:
            QTimer.singleShot(ms, lambda idx=i: self._deferred_refresh(idx))
        # A 股交易日历预热
        QTimer.singleShot(2500, self._warm_trade_calendar)

    def _warm_trade_calendar(self):
        """后台预热 A 股交易日历（daemon 线程，不阻塞主线程）。"""
        def _run():
            try:
                from core.trade_calendar import _ensure_loaded
                dates = _ensure_loaded()
                if dates:
                    import logging
                    logging.getLogger(__name__).info(
                        '[trade_calendar] warmed up, %d trade days loaded', len(dates)
                    )
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _deferred_refresh(self, idx):
        """确保面板已构建后再 refresh。"""
        self._ensure_panel(idx)
        panel = self.stack.widget(idx)
        if hasattr(panel, 'refresh'):
            panel.refresh()

    def _should_refresh_on_switch(self, idx: int) -> bool:
        return True

    # ---- 面板切换 ----
    def _switch_panel(self, idx, refresh=True):
        self._ensure_panel(idx)
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_btns):
            btn.setChecked(i == idx)
        if idx == 6:   # 持仓：推送数据
            self._push_to_portfolio()
            pp = getattr(self, 'portfolio_panel', None)
            if pp is not None and hasattr(pp, 'enable_intraday_pnl_calendar'):
                pp.enable_intraday_pnl_calendar()
        if idx == 7:   # 追踪：检查 API Key（反思要用）
            self._ensure_apikey('追踪反思生成')
        if idx == 5:   # 情报：检查 API Key
            self._ensure_apikey('情报自动分类')
        if refresh and self._should_refresh_on_switch(idx):
            self.fetch_data()

    # ---- 刷新分发 ----
    def fetch_data(self):
        """手动刷新：调用当前面板的 refresh()。"""
        panel = self.stack.currentWidget()
        if hasattr(panel, 'refresh'):
            panel.refresh()

    def _push_to_portfolio(self):
        """将情报/板块/情绪/全球快照推送到持仓面板。

        在三个时机调用：
        1. 切换到持仓面板 (_switch_panel 6)
        2. 持仓面板首次构建 (_build_deferred_panel 6)
        3. 情报/大盘/全球面板 status_changed('success')
        """
        pp = self.portfolio_panel
        if pp is None or not hasattr(pp, 'set_intel_events'):
            return

        # 情报事件
        intel = getattr(self, 'intel_panel', None)
        events: list = []
        if intel is not None:
            events = list(getattr(intel, '_events', []))
        if not events:
            # 兜底：直接从 intel_feed.json 加载（intel_panel 未构造或未刷新）
            try:
                from ui.intel_panel import _load_feed
                events = _load_feed()
            except Exception:
                events = []
        pp.set_intel_events(events)

        # 板块资金快照
        pp.set_sector_context(self.sector_panel.get_market_context())

        # 大盘情绪 snapshot（映射到 market_context_provider 格式）
        mp = getattr(self, 'market_panel', None)
        if mp is not None:
            snap = getattr(mp, '_last_snap', None)
            if snap:
                emotion = {
                    'zt':      snap.get('zt_count', 0),
                    'dt':      snap.get('dt_count', 0),
                    'zb':      snap.get('zb_count', 0),
                    'real_zt': snap.get('real_zt', 0),
                    'real_dt': snap.get('real_dt', 0),
                    'up':      snap.get('up_count', 0),
                    'down':    snap.get('down_count', 0),
                    'amount':  snap.get('total_amount'),
                }
                pp.set_emotion_context(emotion)

        # 全球指数 snapshot（映射到 market_context_provider 格式）
        gp = getattr(self, 'global_panel', None)
        if gp is not None:
            items = getattr(gp, '_last_items', None)
            if items:
                indices = [
                    {'name': n, 'price': float(p or 0), 'pct': float(pct or 0)}
                    for n, p, pct, *_ in items[:4]
                ]
                pp.set_global_context({'indices': indices})
                return
        # 全球面板尚未加载，保持 None（worker 线稏会自行投分泛拉取）

    def _on_intel_anchor(self, event_id: str):
        """情报锚点被点击：跳转到情报面板并定位到对应事件。

        三分支策略：
          A. 当前已渲染 → 直接定位
          B. 在 _events 但被分类过滤挡掉 → 清过滤重建后再试
          C. 真不在库 → 弹窗告知，引导查看追踪详情中的快照
        """
        self._switch_panel(5, refresh=False)
        intel = getattr(self, 'intel_panel', None)
        if not (intel and event_id):
            return

        # 冷启动兜底：_events 空时直接同步加载（_load_feed 是同步文件读）
        if not intel._events:
            try:
                from ui.intel_panel import _load_feed
                intel._events = _load_feed()
            except Exception:
                pass

        # 清除旧选中状态
        if intel._selected_id and intel._selected_id in intel._item_widgets:
            intel._item_widgets[intel._selected_id].set_selected(False)

        def _select_and_scroll(eid: str) -> bool:
            if eid not in intel._item_widgets:
                return False
            intel._selected_id = eid
            intel._show_detail(eid)
            w = intel._item_widgets[eid]
            w.set_selected(True)
            try:
                intel._list_scroll.ensureWidgetVisible(w)
            except Exception:
                pass
            return True

        # 分支 A：当前已渲染 → 直接定位
        if _select_and_scroll(event_id):
            return

        # 分支 B：在 _events 里但被当前分类过滤挡掉 → 清过滤重建后再试
        def _get_id(e):
            return e.id if hasattr(e, 'id') else str(e.get('id', ''))
        in_feed = any(_get_id(e) == event_id for e in (intel._events or []))
        if in_feed:
            try:
                intel._on_filter('')   # 清分类过滤，触发 _rebuild_list
            except Exception:
                pass
            if _select_and_scroll(event_id):
                return
            # 清过滤后仍找不到 → 穿透到分支 C

        # 分支 C：真不在库（超保留期被裁剪 / 源文件被删 / 过滤实现异常）
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, '原情报不可用',
            '原情报已不在当前情报库中（可能超出保留期被清理）。\n'
            '你可以在追踪详情"📰 情报快照"中查看当时的快照副本。'
        )

    def _monitor_positions_async(self):
        """每分钟后台监控：真实持仓止损/目标价 + 推送价格到持仓/追踪面板。"""
        if getattr(self, '_monitor_running', False):
            return
        self._monitor_running = True

        def _run():
            try:
                self._monitor_positions()
            finally:
                self._monitor_running = False

        threading.Thread(target=_run, daemon=True).start()

    def _apply_portfolio_prices(self, prices: dict):
        pp = getattr(self, 'portfolio_panel', None)
        if pp is not None:
            pp.set_current_prices(prices)

    def _apply_tracking_prices(self, prices: dict):
        tp = getattr(self, 'tracking_panel', None)
        if tp is not None:
            tp.set_current_prices(prices)

    def _monitor_positions(self):
        try:
            from core.data_source import quotes_routed

            # ── 真实持仓止损/目标价监控 ──
            try:
                from core.portfolio_data import load_holdings, compute_kpis
                holdings = load_holdings()
                if holdings:
                    real_codes = list({h['code'] for h in holdings})
                    try:
                        real_prices = quotes_routed(real_codes)
                    except Exception:
                        real_prices = {}
                    for h in holdings:
                        code = h.get('code', '')
                        cur_q = real_prices.get(code, {})
                        cur_p = cur_q.get('price') if cur_q else None
                        kpi = compute_kpis(h, cur_p)
                        name = h.get('name', code)
                        if kpi.get('stop_triggered') and self.alert_center:
                            self.alert_center.try_trigger(
                                event_type='real_stop_loss',
                                title=f'⚠️ 真实持仓止损警告：{name}',
                                message=(
                                    f'{name}（{code}）当前价 {fmt_price(kpi["current_price"], code)}，'
                                    f'盈亏 {kpi["pnl_pct"]:+.2f}%，已触及止损线'
                                ),
                                level='warning',
                                key=f'real_stop_{code}_{date.today().isoformat()}',
                                cooldown=3600,
                            )
                        elif kpi.get('target_reached') and self.alert_center:
                            self.alert_center.try_trigger(
                                event_type='real_target_hit',
                                title=f'🎯 真实持仓目标达成：{name}',
                                message=(
                                    f'{name}（{code}）当前价 {fmt_price(kpi["current_price"], code)}，'
                                    f'已达到目标价 {h.get("target_price")} 元'
                                ),
                                level='info',
                                key=f'real_target_{code}_{date.today().isoformat()}',
                                cooldown=3600,
                            )
                    pp = getattr(self, 'portfolio_panel', None)
                    if pp is not None:
                        self._portfolio_prices_ready.emit(real_prices)
            except Exception:
                pass

            # ── 推送价格给追踪面板（驱动浮盈刷新 + 到期结算）──
            try:
                tp = getattr(self, 'tracking_panel', None)
                if tp is not None:
                    from core.tracking import load_tasks
                    open_codes = list({t['code'] for t in load_tasks()
                                       if t.get('status') == 'open'})
                    if open_codes:
                        try:
                            track_prices = quotes_routed(open_codes)
                        except Exception:
                            track_prices = {}
                        self._tracking_prices_ready.emit(track_prices)
            except Exception:
                pass
        except Exception:
            pass

    # ---- API Key ----
    def _ensure_apikey(self, feature_name: str) -> bool:
        from core.credentials import load_api_key, save_api_key
        if load_api_key():
            return True
        from PySide6.QtWidgets import QMessageBox, QInputDialog, QLineEdit
        ret = QMessageBox.question(
            self, '需要 API Key',
            f'「{feature_name}」需要 DeepSeek API Key。\n现在填写吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            key, ok = QInputDialog.getText(
                self, '填写 API Key',
                'DeepSeek API Key（仅保存本地）：',
                QLineEdit.EchoMode.Password,
            )
            if ok and key.strip():
                save_api_key(key.strip())
                return True
        return False

    def _show_help(self):
        dlg = _HelpDialog(self)
        dlg.exec()

    def _show_data_manager(self):
        dlg = _DataManagerDialog(self)
        dlg.exec()
        if getattr(dlg, '_reset_performed', False):
            self._refresh_after_user_data_reset()
        elif getattr(dlg, '_import_performed', False):
            self._refresh_after_user_data_import()

    def _refresh_loaded_user_data_panels(self):
        for attr in ('portfolio_panel', 'tracking_panel'):
            panel = getattr(self, attr, None)
            if panel is not None and hasattr(panel, 'refresh'):
                try:
                    panel.refresh()
                except Exception:
                    pass

    def _refresh_after_user_data_reset(self):
        stock = getattr(self, 'stock_panel', None)
        if stock is not None:
            try:
                stock._watchlist_codes = []
                stock._last_watchlist_snap = None
                stock._wl_active_group = None
                if hasattr(stock, '_reload_group_combo'):
                    stock._reload_group_combo()
                if hasattr(stock, '_fill_watchlist_table'):
                    stock._fill_watchlist_table([])
                if hasattr(stock, '_refresh_option_table'):
                    stock._refresh_option_table()
            except Exception:
                pass
        self._refresh_loaded_user_data_panels()

    def _refresh_after_user_data_import(self):
        stock = getattr(self, 'stock_panel', None)
        if stock is not None:
            try:
                from core.cache import load_watchlist
                stock._watchlist_codes = load_watchlist()
                stock._last_watchlist_snap = None
                stock._wl_active_group = None
                if hasattr(stock, '_ensure_watchlist_groups'):
                    stock._ensure_watchlist_groups()
                if hasattr(stock, '_reload_group_combo'):
                    stock._reload_group_combo()
                if hasattr(stock, '_fetch_watchlist'):
                    stock._fetch_watchlist()
                if hasattr(stock, '_refresh_option_table'):
                    stock._refresh_option_table()
            except Exception:
                pass
        self._refresh_loaded_user_data_panels()

    def _on_font_size_changed(self, idx):
        pt = [10, 13][idx]
        self._font_pt = pt
        update_ui_config(font_size_pt=pt)
        self._apply_font_size(pt)

    def _on_default_pos_value_changed(self):
        update_ui_config(default_position_value=int(self.pos_value_spin.value()))

    def _apply_font_size(self, pt):
        app = QApplication.instance()
        if app is None:
            return
        scale = pt / _FONT_BASE_PT

        f = QFont()
        f.setPointSize(pt)
        app.setFont(f)

        def _scale_css(css):
            def _repl(m):
                val = float(m.group(1))
                unit = m.group(2)
                if unit == 'pt':
                    return f'font-size: {max(7, round(val * scale, 1))}pt'
                else:
                    return f'font-size: {max(8, int(round(val * scale)))}px'
            return _FONT_SIZE_RE.sub(_repl, css)

        for w in app.allWidgets():
            orig = w.property('_orig_ss')
            if orig is None:
                orig = w.styleSheet() or ''
                w.setProperty('_orig_ss', orig)
            if not orig or 'font-size' not in orig:
                w.setFont(f)
            else:
                w.setStyleSheet(_scale_css(orig))

        # 情报面板动态内容（每次 auto_refresh 重建）需立即重绘以匹配新字号
        intel = getattr(self, 'intel_panel', None)
        if intel is not None and getattr(intel, '_events', None):
            try:
                intel.reapply_font()
            except Exception:
                pass
        portfolio = getattr(self, 'portfolio_panel', None)
        if portfolio is not None and hasattr(portfolio, 'reapply_font'):
            try:
                portfolio.reapply_font()
            except Exception:
                pass

        # 同步 matplotlib 字号
        try:
            import matplotlib as _mpl
            _tick_sz = max(8, pt - 1)
            _mpl.rcParams.update({
                'font.size':        pt,
                'axes.titlesize':   pt + 1,
                'axes.labelsize':   pt,
                'xtick.labelsize':  _tick_sz,
                'ytick.labelsize':  _tick_sz,
                'legend.fontsize':  _tick_sz,
            })
            # 对已渲染的 Figure 立即生效（刻度 + 注释文字 + 标题）
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as _FC
            for w in app.allWidgets():
                if not isinstance(w, _FC):
                    continue
                fig = w.figure
                for txt in fig.texts:
                    txt.set_fontsize(pt)
                for ax in fig.get_axes():
                    ax.tick_params(axis='both', labelsize=_tick_sz)
                    for txt in ax.texts:
                        txt.set_fontsize(pt)
                    if ax.title.get_text():
                        ax.title.set_fontsize(pt + 1)
                    if ax.xaxis.label.get_text():
                        ax.xaxis.label.set_fontsize(pt)
                    if ax.yaxis.label.get_text():
                        ax.yaxis.label.set_fontsize(pt)
                w.draw_idle()
        except Exception:
            pass

    def _apply_font_to_panel(self, panel, pt):
        """仅对新建面板及其子控件同步字号，避免遍历全局 allWidgets()。"""
        scale = pt / _FONT_BASE_PT
        f = QFont()
        f.setPointSize(pt)
        for w in [panel] + panel.findChildren(QWidget):
            orig = w.styleSheet() or ''
            if not orig or 'font-size' not in orig:
                w.setFont(f)
            else:
                w.setProperty('_orig_ss', orig)
                w.setStyleSheet(_FONT_SIZE_RE.sub(
                    lambda m: (f'font-size: {max(7, round(float(m.group(1)) * scale, 1))}pt'
                               if m.group(2) == 'pt'
                               else f'font-size: {max(8, int(round(float(m.group(1)) * scale)))}px'),
                    orig,
                ))

    def _show_api_cost_detail(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self,
            'DeepSeek API 费用说明',
            'DeepSeek API 费用（官方按美元/百万 tokens 计费；下方人民币按 $1≈¥6.76 粗略折算）\n\n'
            '▸ deepseek-flash（DeepSeek-V4.1-Flash；轻量任务 + 全部 AI 深度分析）\n'
            '  • 输入（缓存命中）: $0.003 / $0.006 / M tokens（低峰 / 高峰）\n'
            '  • 输入（缓存未命中）: $0.15 / $0.30 / M tokens（低峰 / 高峰）\n'
            '  • 输出: $0.60 / $1.20 / M tokens（低峰 / 高峰）\n\n'
            '典型消耗估算：\n'
            '  • 每次情报分类批次 ≈ 0.5–2k tokens → 约 ¥0.0005–0.002\n'
            '  • 一次完整的个股 AI 分析约 ¥0.05 ~ 0.28 元\n'
            '    （约 8–35k tokens；含 5 Agent + 多空辩论 + 最终决策 thinking）\n'
            '  • 按每天分析 10 次估算：约 ¥0.5–2.8 / 天，约 ¥14–84 / 月（按 30 天）\n'
            '  • 首次充值 10 元 ≈ 可完成约 35–200 次完整个股分析\n'
            '  • 同一股票 4h 内重复分析命中推理缓存时，不再调用 API\n\n'
            'API Key 仅存储在本机 AldebaranData/ 目录下，\n'
            '不上传任何服务器，不做任何其他用途。\n\n'
            '价格以 DeepSeek 官方 pricing 页面为准，可能随平台调整而变化。',
        )

    def _on_timer_tick(self):
        """定时器自动刷新：调用当前面板的 auto_refresh()。"""
        panel = self.stack.currentWidget()
        # 最小化/失焦时暂停自动刷新省电；板块资金折线图需实时，放行
        foreground = self.isActiveWindow() and not self.isMinimized()
        if hasattr(panel, 'auto_refresh') and (foreground or panel is self.sector_panel):
            panel.auto_refresh()
        self._auto_refresh_background_quote_panels(panel)

    def _auto_refresh_background_quote_panels(self, _current_panel):
        """Keep quote-dependent pages warm even when their tab is not selected."""
        stock = getattr(self, 'stock_panel', None)
        if stock is not None and hasattr(stock, 'background_auto_refresh'):
            stock.background_auto_refresh()

        portfolio = getattr(self, 'portfolio_panel', None)
        if portfolio is not None and hasattr(portfolio, 'auto_refresh'):
            portfolio.auto_refresh()

    # ---- 面板状态汇总到工具栏 ----
    def _on_panel_status(self, level, text):
        color_map = {
            'loading': '#f0c040',
            'success': GREEN,
            'error': RED,
        }
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color_map.get(level, MUTED)};")
        self.refresh_btn.setEnabled(level != 'loading')

    # ---- 盘面/定时器 ----
    def update_market_status(self):
        now = datetime.now()
        weekday = now.weekday()
        hour_min = now.hour * 100 + now.minute

        if weekday >= 5:
            self.market_label.setText("休市（周末）")
            self.market_label.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        elif 930 <= hour_min <= 1130 or 1300 <= hour_min <= 1500:
            self.market_label.setText("交易中")
            self.market_label.setStyleSheet(f"color: {RED}; font-size: 13px; font-weight: bold;")
        elif hour_min < 930:
            self.market_label.setText("盘前")
            self.market_label.setStyleSheet("color: #f0c040; font-size: 13px;")
        elif 1130 < hour_min < 1300:
            self.market_label.setText("午间休市")
            self.market_label.setStyleSheet("color: #f0c040; font-size: 13px;")
        else:
            self.market_label.setText("已收盘")
            self.market_label.setStyleSheet(f"color: {MUTED}; font-size: 13px;")

        self._maybe_post_close_freeze(weekday, hour_min)

    def _maybe_post_close_freeze(self, weekday, hour_min):
        """收盘后（交易日 ≥15:00）触发一次自选/期权冻结刷新，每天仅一次。

        持仓月历的盘后冻结由 60 秒后台监控独立完成；此处只补自选与期权，
        且 post_close_refresh 内部已错开两者，避免同时拉取过载。
        """
        if weekday >= 5 or hour_min < 1500:
            return
        today = date.today().isoformat()
        if self._post_close_done == today:
            return
        sp = getattr(self, 'stock_panel', None)
        self._post_close_done = today
        if sp is not None and hasattr(sp, 'post_close_refresh'):
            sp.post_close_refresh()
        if isinstance(self.stack.widget(7), _PanelPlaceholder):
            self._build_deferred_panel(7)
        tp = getattr(self, 'tracking_panel', None)
        if tp is not None and hasattr(tp, 'post_close_refresh'):
            tp.post_close_refresh()

    def update_timer(self):
        intervals = [0, 5000, 15000, 30000, 60000, 180000]
        idx = self.interval_combo.currentIndex()
        if intervals[idx] == 0:
            self.timer.stop()
        else:
            self.timer.start(intervals[idx])

    # ---- 托盘通知 ----
    def _setup_tray_icon(self):
        self._tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon_path = str(_app_root() / 'app_icon.ico')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else self.windowIcon()
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip('Aldebaran V3.0')
        menu = QMenu()
        menu.addAction('显示主窗口', self._show_from_tray)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def _show_from_tray(self):
        self.showNormal()
        self.activateWindow()

    def _on_alert_triggered(self, event):
        """收到提醒事件 → 路由到自定义弹窗或系统托盘通知。

        路由规则：
          1. critical 级别 → 始终使用自定义弹窗
          2. custom_popup_enabled = True → 自定义弹窗
          3. 否则 → 系统托盘通知（降级：状态栏）
        """
        if not self.alert_center.get_setting('desktop_notification', True):
            return

        level = event.get('level', 'info')
        alert_type = event.get('type', '')
        title = event.get('title', '提醒')
        message = event.get('message', '')

        use_custom = (
            level == 'critical'
            or self.alert_center.get_setting('custom_popup_enabled', False)
        )

        if use_custom:
            from ui.widgets.toast_notification import show_toast
            size = self.alert_center.get_setting('popup_size', 'medium')
            duration_ms = int(self.alert_center.get_setting('popup_duration', 5)) * 1000
            show_toast(title, message, level, size, duration_ms, alert_type)
            return

        if self._tray and self._tray.isVisible():
            icon_map = {
                'info': QSystemTrayIcon.MessageIcon.Information,
                'warning': QSystemTrayIcon.MessageIcon.Warning,
                'critical': QSystemTrayIcon.MessageIcon.Critical,
            }
            self._tray.showMessage(
                title, message,
                icon_map.get(level, QSystemTrayIcon.MessageIcon.Information),
                5000,
            )
        else:
            self.statusBar().showMessage(
                f"[{level.upper()}] {title} — {message}",
                8000,
            )


# ---------- 帮助弹窗 ----------
_HELP_STYLE = """
QDialog { background-color: #0d1525; }
QTabWidget::pane { border: 1px solid #333; background-color: #0d1525; }
QTabBar::tab {
    background-color: #111a30; color: #888; padding: 6px 16px;
    border: 1px solid #333; border-bottom: none; border-radius: 4px 4px 0 0;
    font-size: 12px;
}
QTabBar::tab:selected { background-color: #e94560; color: #fff; }
QTabBar::tab:hover:!selected { background-color: #1a2a50; color: #ccc; }
QScrollArea { border: none; background-color: transparent; }
QWidget#scroll_inner { background-color: transparent; }
"""

_SECTION_H = "color: #e94560; font-size: 13px; font-weight: bold; margin-top: 10px;"
_TERM_KEY   = "color: #f0c040; font-size: 12px; font-weight: bold;"
_TERM_VAL   = "color: #cccccc; font-size: 12px;"
_DIVIDER    = "color: #333333;"


def _make_scroll_tab(items):
    """items: list of ('section'|'term'|'divider', text, desc?)"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    inner = QWidget()
    inner.setObjectName('scroll_inner')
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(16, 10, 16, 16)
    layout.setSpacing(4)

    for kind, *args in items:
        if kind == 'section':
            lbl = QLabel(args[0])
            lbl.setStyleSheet(_SECTION_H)
            layout.addWidget(lbl)
        elif kind == 'term':
            row = QHBoxLayout()
            row.setSpacing(8)
            key_lbl = QLabel(f'• {args[0]}')
            key_lbl.setStyleSheet(_TERM_KEY)
            key_lbl.setFixedWidth(160)
            key_lbl.setWordWrap(False)
            val_lbl = QLabel(args[1])
            val_lbl.setStyleSheet(_TERM_VAL)
            val_lbl.setWordWrap(True)
            row.addWidget(key_lbl)
            row.addWidget(val_lbl, stretch=1)
            w = QWidget()
            w.setLayout(row)
            layout.addWidget(w)
        elif kind == 'divider':
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet(_DIVIDER)
            layout.addWidget(line)

    layout.addStretch()
    scroll.setWidget(inner)
    return scroll


class _DataManagerDialog(QDialog):
    """用户数据导出 / 导入 / 重置 对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reset_performed = False
        self._import_performed = False
        self.setWindowTitle('数据管理')
        self.setFixedSize(480, 340)
        self.setStyleSheet('background:#0d0d1f; color:#e0e0e0;')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)

        title = QLabel('用户数据管理')
        title.setStyleSheet('font-size:16px; font-weight:bold; color:#c8d8f8;')
        lay.addWidget(title)

        desc = QLabel('导出包含：自选股/分组、期权台账、持仓、收益月历、追踪任务、分析记忆。\n不含 API Key 及情报缓存。')
        desc.setStyleSheet('font-size:12px; color:#8899aa;')
        desc.setWordWrap(True)
        lay.addWidget(desc)

        _btn_style = (
            'QPushButton { background:#1a2a4a; color:#c8d8f8; border:1px solid #2a4a6a;'
            ' border-radius:4px; font-size:13px; padding:6px 18px; }'
            'QPushButton:hover { background:#2a3a6a; }'
        )

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        export_btn = QPushButton('📤 导出数据')
        export_btn.setStyleSheet(_btn_style)
        export_btn.clicked.connect(self._on_export)
        btn_row.addWidget(export_btn)

        import_btn = QPushButton('📥 导入数据')
        import_btn.setStyleSheet(_btn_style)
        import_btn.clicked.connect(self._on_import)
        btn_row.addWidget(import_btn)

        reset_btn = QPushButton('🗑️ 一键重置')
        reset_btn.setStyleSheet(
            'QPushButton { background:#3a1010; color:#ff8888; border:1px solid #6a2020;'
            ' border-radius:4px; font-size:13px; padding:6px 18px; }'
            'QPushButton:hover { background:#5a1010; }'
        )
        reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(reset_btn)

        lay.addLayout(btn_row)

        # ---- TickFlow 行情源 ----
        tf_title = QLabel('行情数据源')
        tf_title.setStyleSheet('font-size:13px; font-weight:bold; color:#c8d8f8;')
        lay.addWidget(tf_title)

        tf_row = QHBoxLayout()
        tf_row.setSpacing(10)
        self._tf_status = QLabel()
        self._tf_status.setStyleSheet('font-size:12px; color:#8899aa;')
        self._tf_status.setWordWrap(True)
        tf_row.addWidget(self._tf_status, stretch=1)

        tf_set_btn = QPushButton('设置')
        tf_set_btn.setStyleSheet(_btn_style)
        tf_set_btn.clicked.connect(self._on_set_tickflow)
        tf_row.addWidget(tf_set_btn)

        tf_clear_btn = QPushButton('清除')
        tf_clear_btn.setStyleSheet(_btn_style)
        tf_clear_btn.clicked.connect(self._on_clear_tickflow)
        tf_row.addWidget(tf_clear_btn)
        lay.addLayout(tf_row)
        self._refresh_tf_status()

        # ---- DeepSeek API Key ----
        ds_title = QLabel('DeepSeek API Key')
        ds_title.setStyleSheet('font-size:13px; font-weight:bold; color:#c8d8f8;')
        lay.addWidget(ds_title)

        ds_row = QHBoxLayout()
        ds_row.setSpacing(10)
        self._ds_status = QLabel()
        self._ds_status.setStyleSheet('font-size:12px; color:#8899aa;')
        self._ds_status.setWordWrap(True)
        ds_row.addWidget(self._ds_status, stretch=1)

        ds_set_btn = QPushButton('填充')
        ds_set_btn.setStyleSheet(_btn_style)
        ds_set_btn.clicked.connect(self._on_set_deepseek)
        ds_row.addWidget(ds_set_btn)

        ds_clear_btn = QPushButton('撤销')
        ds_clear_btn.setStyleSheet(_btn_style)
        ds_clear_btn.clicked.connect(self._on_clear_deepseek)
        ds_row.addWidget(ds_clear_btn)
        lay.addLayout(ds_row)
        self._refresh_ds_status()

        lay.addStretch()

        close_btn = QPushButton('关闭')
        close_btn.setStyleSheet(
            'QPushButton { background:#2a3a5a; color:#aaa; border:none;'
            ' border-radius:4px; padding:6px 18px; }'
        )
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def _on_export(self):
        from core.user_data_io import export_user_data
        from datetime import date as _date
        default = f'aldebaran_userdata_{_date.today().strftime("%Y%m%d")}.json'
        path, _ = QFileDialog.getSaveFileName(
            self, '选择保存位置', default, 'JSON 文件 (*.json)'
        )
        if not path:
            return
        status = export_user_data(Path(path))
        ok = status.get('_write') == 'ok'
        if ok:
            QMessageBox.information(self, '导出完成', f'已保存到：\n{path}')
        else:
            QMessageBox.warning(self, '导出失败', str(status))

    def _on_import(self):
        from core.user_data_io import import_user_data
        path, _ = QFileDialog.getOpenFileName(
            self, '选择数据文件', '', 'JSON 文件 (*.json)'
        )
        if not path:
            return
        result = import_user_data(Path(path))
        if not result.success:
            QMessageBox.warning(self, '导入失败', '\n'.join(result.errors))
            return
        backup = result.backup_path
        msg = (
            f'导入成功！\n'
            f'已导入：{", ".join(result.imported)}\n'
        )
        if backup:
            msg += f'\n原数据已备份到：\n{backup}'
        self._import_performed = True
        QMessageBox.information(self, '导入完成', msg)

    def _on_reset(self):
        from core.user_data_io import reset_user_data
        ret = QMessageBox.warning(
            self, '⚠️ 确认重置',
            '将删除所有用户数据（持仓、自选、期权、追踪、分析记忆）。\n'
            '此操作不可撤销，确定吗？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        deleted = reset_user_data()
        self._reset_performed = True
        QMessageBox.information(
            self, '重置完成',
            f'已删除 {len(deleted)} 个数据文件。\n当前界面将同步刷新。'
        )
        self.accept()

    def _refresh_tf_status(self):
        from core.credentials import load_tickflow_token
        tok = load_tickflow_token()
        if tok:
            masked = f'{tok[:3]}****{tok[-3:]}' if len(tok) > 8 else '*' * len(tok)
            self._tf_status.setText(
                f'TickFlow API Key：已启用（{masked}）\n'
                '非期权价格 / K 线可优先走 TickFlow，失败自动用免费公开源'
            )
        else:
            self._tf_status.setText(
                'TickFlow API Key：未启用\n'
                '不填时使用免费公开源'
            )

    def _on_set_tickflow(self):
        from core.credentials import save_tickflow_token, load_tickflow_token
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        key, ok = QInputDialog.getText(
            self, 'TickFlow API Key',
            'TickFlow API Key（可选，仅保存本地）：',
            QLineEdit.EchoMode.Password,
            load_tickflow_token(),
        )
        if ok:
            save_tickflow_token(key.strip())
            self._refresh_tf_status()

    def _on_clear_tickflow(self):
        from core.credentials import save_tickflow_token
        save_tickflow_token('')
        self._refresh_tf_status()

    def _refresh_ds_status(self):
        from core.credentials import load_api_key
        key = load_api_key()
        if key:
            masked = f'{key[:3]}****{key[-4:]}' if len(key) > 10 else '*' * len(key)
            self._ds_status.setText(
                f'DeepSeek API Key：已填充（{masked}）\n情报分类 / AI 分析 / 反思可用'
            )
        else:
            self._ds_status.setText(
                'DeepSeek API Key：未填充\n填入后情报自动分类、AI 深度分析、追踪反思才可用'
            )

    def _on_set_deepseek(self):
        from core.credentials import save_api_key, load_api_key
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        key, ok = QInputDialog.getText(
            self, 'DeepSeek API Key',
            'DeepSeek API Key（仅保存本地）：',
            QLineEdit.EchoMode.Password,
            load_api_key(),
        )
        if ok:
            save_api_key(key.strip())
            self._refresh_ds_status()

    def _on_clear_deepseek(self):
        from core.credentials import save_api_key
        save_api_key('')
        self._refresh_ds_status()


class _HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Aldebaran 功能说明 & 术语解释')
        self.resize(700, 540)
        self.setStyleSheet(_HELP_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.addTab(_make_scroll_tab(_TAB_FEATURES),    '📊 功能一览')
        tabs.addTab(_make_scroll_tab(_TAB_FUND),        '💰 资金术语')
        tabs.addTab(_make_scroll_tab(_TAB_ALERT),       '🔔 提醒术语')
        tabs.addTab(_make_scroll_tab(_TAB_DATASOURCE),  '🔗 数据来源')
        layout.addWidget(tabs)

        close_btn = QPushButton('关闭')
        close_btn.setFixedWidth(80)
        close_btn.setStyleSheet(
            'QPushButton { background-color: #e94560; color: #fff; border: none;'
            'border-radius: 4px; padding: 6px 0; font-size: 12px; }'
            'QPushButton:hover { background-color: #ff6080; }'
        )
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        btn_row.setContentsMargins(0, 0, 16, 0)
        layout.addLayout(btn_row)


_TAB_FEATURES = [
    ('section', '八个面板'),
    ('term', '📊 大盘', 'A股9大指数实时涨跌 + 涨停/跌停/炸板情绪三数 + 近60日情绪历史折线（点击”情绪趋势”按钮展开）'),
    ('term', '🌍 全球', '美股三大指数、港股恒指/国企/科技、英国/德国/日本等主要市场，显示最新价、涨跌幅、报价时间'),
    ('term', '💰 板块', '实时分时折线 + 资金净额柱状图 + 右侧排行表（可按净额/涨跌幅排序）；双击板块名查看成分强势股TOP5；🔥标记表示共振；网络异常时折线自动切换昨日分时回放，标题显示「[昨日]」'),
    ('term', '📈 个股', '6 个标签页：主力净流入/流出/涨跌幅 TOP30 + 自选股 + 新股 + 期权；\n'
     '  · 自选股 Tab：表格含数量/加入价/最新价/昨收价/累计盈亏/盈亏比例/当日收益，累计盈亏次日起按 (最新价−加入价)×数量 计；\n'
     '  · 分组管理 + 右键编辑，支持 5 列排序（涨跌幅/主力净额/累计盈亏/盈亏比例/当日收益）；\n'
     '  · 「📥 批量导入」：通达信/东财 Excel（智能列头识别）+ .txt（Tab 分隔/纯代码）；\n'
     '  · 网络异常时涨跌幅榜自动切换新浪实时数据'),
    ('term', '🔍 情报', '事件情报库（AI自动获取 + 手动维护）；左侧按分类过滤，色条显示资金共振/背离/待验证；右侧默认主线聚合视图，出现全场背离时结论栏自动展开推理链；详情页含操作提示；支持 AI 板块/指数/主线深度分析；情报假连接可展开持仓面板相关详情'),
    ('term', '🎯 追踪', '独立顶级面板，全屏战绩视图；三行 KPI 看板（① 核心胜率：看多兑现/回避有效/观察合理/候选转换/板块胜率 ② 评级·路径分布 ③ 系统判断力＝看多组超额−回避组超额）+ 完整任务列表 + 反思详情；进行中/已完成分区；点「刷新」对所有看多观察/看多单全扫跟踪周期 K 线重算（开机后/收盘后自动同样全扫；盘中自动更新才按现价闸门只扫到价的）；进行中买入单盘中触及目标价即提前止盈、审判日按持有期最高价复核；双击行展开 AI 反思与原始预测快照'),
    ('term', '🔔 提醒', '四类异动提醒实时记录；日志支持按类型和天数筛选；设置区可直接修改阈值和冷却时间；追踪任务到期/目标达成/止损触发时自动弹窗'),
    ('term', '💼 持仓', 'V3.0 三段式布局：\n'
     '  · 顶部汇总：真实持仓板块占比 + 追踪任务评级分布，自动汇总总市值/总盈亏/胜率；\n'
     '  · K 线支持滚轮缩放 + 拖拽平移 + 30/60/120/250 日周期切换；\n'
     '  · 点击「分析该股」启动 5 Agent 引擎：Agent1 市场环境 + Agent3 基本面 + Agent4 技术面 + Agent5 资金流向 + Agent6 事件催化并行（个股 AI 分析链路统一使用 deepseek-flash (V4.1 Flash) thinking）→ F 层多空辩论裁决（V4.1 Flash thinking）→ Z 层时间维度 → G 层最终决策（V4.1 Flash thinking）；\n'
     '  · 输出看多/看跌回避/中性观望 + 观察基准、失效条件、判定日和完整推理链；\n'
     '  · 板块景气度深度分析在「情报」页「📊 分析板块并追踪」按钮（另起，不在本页）；\n'
     '  · 追踪任务到期后按看多/回避/观望/候选触发四类契约口径自动评级，并在结算后自动生成单条 AI 反思留档（仅供复盘查看，不回灌后续分析）；\n'
     '  · 全部分析 4h 缓存复用，DeepSeek prompt caching 自动命中省 60-90% token'),
    ('divider',),
    ('section', '持仓面板操作'),
    ('term', '分析该股按钮', '自动获取当前价格，拉取 K 线/基本面/资金流/个股专属新闻，并行调用 5 个 V4.1 Flash thinking Agent，随后串行跑 F 层多空辩论 + G 层决策；分析层只能基于已取得资料和确定性画像输出，不允许补充未提供事实。'),
    ('term', '建立追踪', '预测完成后系统会自动建立追踪任务；若交易参数不完整或仅建议观望，则只作为观察样本，不给买入提示'),
    ('term', 'K 线图工具栏', '持仓面板 K 线图支持 Home（复位）/ Pan（平移）/ Zoom（缩放）；鼠标悬停显示 OHLC + 十字光标'),
    ('term', '4h 缓存', '同一股票同一交易日已推理的结果直接读缓存，点击「重新推理」强制刷新'),
    ('divider',),
    ('section', '自选股功能（v3.0 新增）'),
    ('term', '添加自选', '输入框回车弹出对话框：代码（必填）+ 数量/加入价/分组（可选）。数量留空按「默认市值」（工具栏可改，默认 8 万）÷现价四舍五入到 100 股估算；指数需带后缀，如中证全指输 000985.CSI。同组内去重、跨组可重复，文件导入则全局去重'),
    ('term', '加入价', '加入自选时的现价（留空默认加入时现价，也可手填）；累计盈亏与盈亏比例均以此为基准'),
    ('term', '当日收益', '(最新价 − 昨收) × 数量，逐只显示；右上角「当日总收益」= 当前分组各股当日收益合计 + 期权当日收益合计'),
    ('term', '累计盈亏', '所有记录（含默认市值估算股数）按 (最新价 − 加入价) × 数量 计；加入当天不计、次日起生效'),
    ('term', '盈亏比例', '(最新价 − 加入价) ÷ 加入价 × 100%，可点击列头排序'),
    ('term', '编辑/排序', '每行「编辑」按钮可修改数量/加入价/迁移分组；涨跌幅/主力净额/累计盈亏/盈亏比例/当日收益 5 列支持降序↔升序切换'),
    ('term', '批量导入', '通达信/东财 .xlsx（前 5 行智能识别「代码」列头）+ .txt（Tab 分隔/纯代码），A 股代码区间自动过滤无效数字'),
    ('term', '期权台账', '「期权」Tab 维护合约代码/标的/到期日/方向/张数/开仓价，AKShare 免费行情刷新；切到期权页立即刷新一次，并在 9:25-11:35、12:55-15:05 固定每 8 秒自动刷新；当日收益 = 方向 × 张数 × 合约单位(10000) × (最新价 − 昨收)，买方为正、卖方为负，计入右上角当日总收益'),
    ('divider',),
    ('section', '常用操作'),
    ('term', '手动刷新', '顶部工具栏”刷新”按钮，任何时间均可触发当前面板数据更新'),
    ('term', '自动刷新', '下拉框选择股票等常规面板间隔（5秒/15秒/30秒/1分钟/3分钟），交易时段自动刷新，非交易时段暂停；期权不跟随下拉框，打开期权页时固定 8 秒刷新'),
    ('term', '板块折线图', '支持鼠标滚轮缩放Y轴、左键拖动平移、双击复位'),
    ('term', '双击自选股行', '弹窗显示该股近10日主力净额柱状图（红柱=净流入，绿柱=净流出）'),
    ('divider',),
    ('section', '情报面板操作'),
    ('term', '🤖 自动获取', '点击后先设目标条数（10 的倍数），按现有逻辑一批批排队抓取直到达标：每批并行拉取 8 个来源（财联社/华尔街见闻/新浪/东财/Reuters/CNBC/龙虎榜/大宗交易），去重后调用 DeepSeek flash 分类并入库；龙虎榜/大宗交易走本地结构化分类器直通，不消耗 LLM token；某一批新增不足 5 条即自动停止并提示「难以达到目标数量」；首次使用弹对话框输入 API Key（Key 仅存本地）'),
    ('term', '操作提示', '详情页显示 DeepSeek 生成的单句操作建议（关注/观望/介入/回避），带颜色边框（利好绿/利空红/中性橙）'),
    ('term', '板块聚合卡', '右侧默认视图按板块归并情报，显示信号/条数/净流入；点击卡片跳转到该板块热度最高的情报详情'),
    ('term', '背离推理链', '全场/普遍背离时，底部结论栏自动展开：① 触发情报 → ② 关联板块实时资金 → ③ 背离结论与建议'),
    ('term', '热度排序', '热度 = 情报权重 × 板块资金共振强度 × 时间衰减；热度高的事件标题加粗并显示分值'),
]

_TAB_FUND = [
    ('section', '资金流核心指标'),
    ('term', '主力净额', '超大单净买入 + 大单净买入，反映机构/游资主导的资金方向；正值=净流入，负值=净流出'),
    ('term', '主力占比', '主力净额 / 总成交额 × 100%，反映机构参与程度；绝对值越大说明主力力度越强'),
    ('term', '超大单', '单笔成交额 > 100万（大型机构行为，如基金/社保/外资）'),
    ('term', '大单', '单笔成交额 50–100万（中型机构/大户行为）'),
    ('term', '中单', '单笔成交额 10–50万（普通投资者）'),
    ('term', '小单', '单笔成交额 < 10万（散户行为）'),
    ('divider',),
    ('section', '其他行情指标'),
    ('term', '换手率', '当日成交量 / 流通股本 × 100%；越高说明当日筹码换手越活跃，通常伴随主力建仓或出货'),
    ('term', '封单量', '涨停价位挂单等待买入的手数（1手=100股）；越大说明封板越牢固，越难被大卖单打开'),
    ('term', '净流入', '主动买入金额 − 主动卖出金额；正值代表资金积极买入该标的'),
    ('term', '板块净额', '板块内所有个股的主力净额总和；反映整个板块的资金聚集程度（单位：亿元）'),
    ('divider',),
    ('section', '资金共振信号'),
    ('term', '资金共振', '板块净流入和板块内涨停股数量同时达到设定阈值时触发，是多维度资金汇聚的强势信号'),
    ('term', '🔥 标记', '排行表中带🔥的板块表示当前满足共振条件（默认：净流入≥5亿 + 涨停股≥5只）'),
]

_TAB_DATASOURCE = [
    ('section', '免费/公开数据'),
    ('term', '东方财富 push2', '板块资金流、分时折线；通过 JS 逆向 push2.eastmoney.com 接口获取，无需注册，免费访问'),
    ('term', '腾讯行情 qtimg', '个股实时报价（含封单量/换手率）；通过 qt.gtimg.cn 接口，免费公开'),
    ('term', '新浪行情 hq.sinajs', '个股涨跌幅榜备用数据源（push2 不可用时自动切换）；免费公开'),
    ('term', 'akshare', '新股申购列表、个股详情、K 线数据、个股资金流、大盘情绪、全年交易日历（tool_trade_date_hist_sina）；开源库，调用东财/巨潮等公开接口，免费使用'),
    ('term', '财联社/华尔街/新浪/东财/Reuters/CNBC', '情报面板「自动获取」爬取的 6 个公开新闻页面，无鉴权，免费'),
    ('term', '东方财富龙虎榜 / 大宗交易', '情报面板本地结构化分类器直通，不消耗 LLM token；龙虎榜含 348 条代码→板块预映射，大宗交易含折溢价过滤'),
    ('term', 'GitHub REST API', '持仓面板获取相关板块的开源仓库 Release/Push 信号（如 llama.cpp/AKShare 等）；无 Token 时每小时60次免费；可在设置中配置 GitHub PAT 提至 5000次/小时'),
    ('divider',),
    ('section', '付费 API（需自行申请）'),
    ('term', 'DeepSeek API',
     '计费以 DeepSeek 官方 pricing 页面为准，单位为每百万 tokens；'
     'deepseek-flash（DeepSeek-V4.1-Flash）用于情报自动分类、宏观状态机等非个股分析轻量任务；'
     '个股 AI 分析链路统一使用 deepseek-flash (V4.1 Flash) thinking，包含 5 Agent、F 层多空辩论和 G 层最终决策；同一模型也用于板块/主线深度分析与自动反思。'
     '当前官方价格按低峰/高峰计：输入缓存命中 $0.003/$0.006/M、输入缓存未命中 $0.15/$0.30/M、输出 $0.60/$1.20/M；'
     '人民币按 $1≈¥6.76 粗略折算：一次完整的个股 AI 分析约 ¥0.05 ~ 0.28 元，按每天 10 次约 ¥14 ~ 84/月；'
     'prompt cache 和 4h 推理缓存会降低实际费用。需在 platform.deepseek.com 注册并充值；Key 仅存本机 AldebaranData/intel_config.json'),
    ('term', 'TickFlow API Key',
     '可选行情增强源。注册后在「设置 → 行情数据源」填入 TickFlow API Key，非期权价格 / K 线可优先走 TickFlow，失败自动回退免费源；'
     '如需使用，请从 TickFlow 官方渠道自行申请 API Key；未配置时自动回退免费公开源。'),
    ('divider',),
    ('section', '本地缓存位置'),
    ('term', '缓存目录', 'AldebaranData/cache/  （各面板数据短期缓存，板块分时最长缓存1天；实时报价/分析上下文盘中最多5分钟；日K按最新交易日校验）'),
    ('term', 'UI 配置', 'AldebaranData/ui_config.json  （自选板块列表、显示模式、字体大小等用户偏好）'),
    ('term', '情报数据库', 'AldebaranData/data/intel_feed.json  （情报条目 JSON，30 天滚动保留）'),
    ('term', 'API Key 存储', 'AldebaranData/intel_config.json  （DeepSeek Key + GitHub Token + TickFlow Key 明文存储，不上传）'),
    ('term', 'AI 预测缓存', 'AldebaranData/cache/ai_predictions.json（预测结果 4h TTL）；AldebaranData/cache/reasoning/（思考过程 4h TTL）'),
    ('term', '交易日历缓存', 'AldebaranData/trade_calendar.json  （A 股全年交易日，年度更新）'),
    ('term', 'GitHub 情报缓存', 'AldebaranData/cache/github/  （各仓库 Release/Push 信号，1h TTL）'),
]

_TAB_ALERT = [
    ('section', '提醒类型'),
    ('term', '自选股涨跌', '自选股涨跌幅触及 ±5% / ±8% / ±10% 档位时分级提醒；同股票同方向同档位当日只提醒一次'),
    ('term', '新股炸板', '新股涨停后被大量卖单打开（涨幅从≥涨停线跌破涨停线），提醒关注首日博弈'),
    ('term', '新股回封', '炸板后重新封回涨停价（再次获得大量买盘支撑），往往是短线机会信号'),
    ('term', '板块TOP3突破', '某板块净流入从TOP3之外新进入前三，且净流入达到设定最低门槛时触发'),
    ('term', '资金共振', '板块TOP3中同时满足净流入和涨停数量双阈值，是最强的资金聚焦信号（critical级）'),
    ('term', '全场背离预警', '情报面板：≥3个主线板块的资金方向全部与情报消息面相反，判定为全场派发状态（critical级，冷却30分钟）'),
    ('term', '普遍背离预警', '情报面板：≥75%主线板块出现资金背离，判定为普遍派发（warning级，冷却30分钟）'),
    ('divider',),
    ('section', '通用概念'),
    ('term', '涨停 / 跌停', '股价达到当日最大涨跌幅限制：主板±10%，创业板/科创板±20%，北交所±30%，ST股±5%'),
    ('term', '炸板', '涨停板被大量卖出订单打开，说明获利盘出逃或主力出货意愿明确'),
    ('term', '回封', '炸板后重新封回涨停，说明买盘意愿依然强烈，有机会继续强势'),
    ('term', '冷却时间', '同类提醒的最小触发间隔，防止每次自动刷新都重复弹窗干扰操作'),
    ('term', 'INFO级', '普通信息提醒（蓝色），仅记录日志，不弹桌面通知'),
    ('term', 'WARNING级', '中等重要提醒（黄色），记录日志 + 弹桌面气泡通知'),
    ('term', 'CRITICAL级', '高度重要提醒（红色），记录日志 + 弹桌面气泡通知，通常为资金共振信号'),
]
