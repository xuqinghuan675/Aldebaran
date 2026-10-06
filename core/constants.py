from pathlib import Path

import matplotlib.pyplot as plt

from core.paths import CACHE_DIR  # noqa: F401  # 转发供全局 import
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------- 样式常量 ----------
DARK_BG = '#1a1a2e'
CHART_BG = '#16213e'
WIDGET_BG = '#0f3460'
ACCENT = '#e94560'
TEXT_COLOR = '#ffffff'
MUTED = '#aaaaaa'
RED = '#e84444'
GREEN = '#2ecc71'

LINE_COLORS = [
    '#e84444', '#ff7043', '#ffa726', '#ffee58', '#66bb6a',
    '#26c6da', '#42a5f5', '#7e57c2', '#ec407a', '#8d6e63',
    '#ef5350', '#ab47bc', '#5c6bc0', '#29b6f6', '#26a69a',
]

plt.rcParams['font.family'] = 'Microsoft YaHei'
plt.rcParams['axes.unicode_minus'] = False

STYLESHEET = """
    QMainWindow { background-color: #1a1a2e; }
    QWidget { background-color: #1a1a2e; color: #ffffff; font-family: "Microsoft YaHei"; }

    /* ---- Button ---- */
    QPushButton {
        background-color: #0f3460; color: #fff; border: 1px solid #3a4a6a;
        border-radius: 4px; padding: 5px 18px; font-size: 13px; min-height: 28px;
    }
    QPushButton:hover  { background-color: #e94560; border-color: #e94560; }
    QPushButton:pressed { background-color: #c73452; border-color: #c73452; }
    QPushButton:disabled { background-color: #1e1e2e; color: #555; border-color: #2a2a3a; }

    /* ---- ComboBox ---- */
    QComboBox {
        background-color: #0f3460; color: #fff; border: 1px solid #3a4a6a;
        border-radius: 4px; padding: 4px 10px; font-size: 13px;
        min-width: 80px; min-height: 26px;
    }
    QComboBox::drop-down { border: none; width: 18px; }
    QComboBox QAbstractItemView {
        background-color: #0d1a35; color: #fff; border: 1px solid #3a4a6a;
        selection-background-color: #e94560; outline: none;
    }

    /* ---- LineEdit ---- */
    QLineEdit {
        background-color: #0f3460; color: #fff; border: 1px solid #3a4a6a;
        border-radius: 4px; padding: 4px 10px; font-size: 13px; min-height: 26px;
    }
    QLineEdit:focus { border-color: #e94560; }

    /* ---- Labels ---- */
    QLabel { font-size: 13px; background: transparent; }

    /* ---- StatusBar ---- */
    QStatusBar {
        background-color: #0d1020; color: #888; font-size: 12px;
        border-top: 1px solid #1e2a3a;
    }

    /* ---- Table ---- */
    QTableWidget {
        background-color: #16213e; color: #fff;
        border: none; gridline-color: #1a2a40; font-size: 12px;
    }
    QTableWidget::item { padding: 4px 8px; }
    QTableWidget::item:selected { background-color: #1e3a6e; }
    QHeaderView { background-color: #111a30; }
    QHeaderView::section {
        background-color: #111a30; color: #aaa;
        border: none; border-right: 1px solid #1e2a40;
        padding: 6px 8px; font-size: 12px; font-weight: bold;
    }
    QHeaderView::section:last-child { border-right: none; }

    /* ---- Tabs ---- */
    QTabWidget::pane { border: 1px solid #1e2a40; background-color: #1a1a2e; border-top: none; }
    QTabBar::tab {
        background-color: #0d1525; color: #888; padding: 7px 20px;
        border: 1px solid #1e2a40; border-bottom: none;
        font-size: 13px; margin-right: 2px; border-radius: 4px 4px 0 0;
    }
    QTabBar::tab:selected {
        background-color: #1a1a2e; color: #fff; font-weight: bold;
        border-top: 2px solid #e94560;
    }
    QTabBar::tab:hover:!selected { background-color: #1a2a50; color: #ddd; }

    /* ---- Splitter ---- */
    QSplitter::handle { background-color: #1e2a40; }
    QSplitter::handle:horizontal { width: 1px; }
    QSplitter::handle:vertical   { height: 1px; }

    /* ---- ScrollBar ---- */
    QScrollBar:vertical {
        background-color: #0d1020; width: 8px; margin: 0; border-radius: 4px;
    }
    QScrollBar::handle:vertical {
        background-color: #2a3a5a; border-radius: 4px; min-height: 24px;
    }
    QScrollBar::handle:vertical:hover { background-color: #e94560; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar:horizontal {
        background-color: #0d1020; height: 8px; margin: 0; border-radius: 4px;
    }
    QScrollBar::handle:horizontal {
        background-color: #2a3a5a; border-radius: 4px; min-width: 24px;
    }
    QScrollBar::handle:horizontal:hover { background-color: #e94560; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

    /* ---- ToolTip ---- */
    QToolTip {
        background-color: #0d1525; color: #ddd;
        border: 1px solid #3a4a6a; border-radius: 4px;
        padding: 4px 8px; font-size: 12px;
    }
"""
