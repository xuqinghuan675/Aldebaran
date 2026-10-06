"""自定义右下角浮动通知窗口（Toast）。

功能：
- 多条通知自动从右下向上叠加排列
- 按 level（info/warning/critical）显示不同颜色左边框
- 显示提醒类型、级别、标题、信息四层内容
- 三档尺寸（大/中/小），宽度按屏幕比例自动计算
  大 = 屏幕宽 1/6，中 = 1/8，小 = 1/12
- 字号随尺寸联动
- 自动消失时长可配置，点击 × 立即关闭
- 不抢夺输入焦点
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

_LEVEL_COLOR = {
    'info':     '#5dade2',
    'warning':  '#f0c040',
    'critical': '#e94560',
}

_LEVEL_LABEL = {
    'info': '信息',
    'warning': '警告',
    'critical': '严重',
}

_TYPE_LABEL = {
    'watchlist_pct': '自选股涨跌',
    'ipo_explode': '新股炸板',
    'ipo_reseal': '新股回封',
    'sector_top3': '板块TOP3',
    'resonance': '资金共振',
}

# 尺寸档位：屏幕宽分母 → (分母, title_pt, msg_pt, padding)
# 小 = 1/8 屏宽，中 = 1/5 屏宽，大 = 1/3 屏宽
_SIZE_SPEC: dict[str, tuple[int, int, int, int]] = {
    'large':  (3, 31, 27, 28),
    'medium': (5, 19, 16, 18),
    'small':  (8, 13, 11, 13),
}

_MARGIN = 16   # 距屏幕边缘像素
_GAP    = 8    # 相邻通知间距


def _resolve_width(size: str) -> int:
    """按屏幕宽度计算 toast 宽度像素。"""
    screen = QApplication.primaryScreen()
    sw = screen.availableGeometry().width() if screen else 1920
    divisor = _SIZE_SPEC.get(size, _SIZE_SPEC['medium'])[0]
    return max(200, sw // divisor)


class _ToastWidget(QWidget):
    """单条浮动通知卡片。"""

    closed = Signal(object)

    def __init__(self, title: str, message: str, level: str,
                 size: str, duration_ms: int, alert_type: str = '', parent=None):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        spec = _SIZE_SPEC.get(size, _SIZE_SPEC['medium'])
        width = _resolve_width(size)
        self._width = width
        color = _LEVEL_COLOR.get(level, _LEVEL_COLOR['info'])
        level_label = _LEVEL_LABEL.get(level, level or '信息')
        type_label = _TYPE_LABEL.get(alert_type, alert_type or '提醒')
        title_pt, msg_pt, pad = spec[1], spec[2], spec[3]
        meta_pt = max(8, msg_pt - 3)

        # ── 容器卡片 ─────────────────────────────────────────────────
        card = QWidget(self)
        card.setFixedSize(width, width)   # 正方形
        card.setStyleSheet(
            f'QWidget {{'
            f'  background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,'
            f'    stop:0 #15213a, stop:1 #10192d);'
            f'  border: 1px solid #263a60;'
            f'  border-left: 5px solid {color};'
            f'  border-radius: 8px;'
            f'}}'
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(pad, pad, pad - 4, pad)
        inner.setSpacing(max(8, pad // 2))

        # 类型 / 级别元信息 + 关闭按钮
        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)

        def _chip(text: str, fg: str, bg: str = '#18233c') -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f'color: {fg}; background-color: {bg}; border: 1px solid #263a60;'
                f' border-radius: 3px; padding: 3px 9px; font-size: {meta_pt}pt;'
                f' font-weight: bold;'
            )
            return lbl

        def _section_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f'color: #7f8aa3; font-size: {meta_pt}pt;'
                f' background: transparent; border: none; font-weight: bold;'
            )
            return lbl

        meta_row.addWidget(_chip(f'类型  {type_label}', '#c5d2ef'))
        meta_row.addWidget(_chip(f'级别  {level_label}', color, '#1a2335'))
        meta_row.addStretch(1)

        close_btn = QPushButton('×')
        btn_sz = title_pt + 8
        close_btn.setFixedSize(btn_sz, btn_sz)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            f'QPushButton {{ color: #888; background: transparent;'
            f' border: none; font-size: {title_pt + 2}pt; }}'
            f'QPushButton:hover {{ color: #fff; }}'
        )
        close_btn.clicked.connect(self._do_close)
        meta_row.addWidget(close_btn)
        inner.addLayout(meta_row)
        inner.addStretch(1)

        # 标题
        inner.addWidget(_section_label('标题'))

        title_lbl = QLabel(title)
        title_lbl.setObjectName('toastTitle')
        title_lbl.setWordWrap(True)
        title_lbl.setMaximumWidth(width - pad * 2 - 4)
        title_lbl.setStyleSheet(
            f'color: {color}; font-size: {title_pt}pt; font-weight: bold;'
            f' background: transparent; border: none;'
        )
        inner.addWidget(title_lbl)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet('border: none; border-top: 1px solid #263a60;')
        inner.addWidget(divider)
        inner.addStretch(1)  # after_title_stretch: 把信息区压到卡片下半部

        # 消息正文
        if message:
            message_box = QFrame()
            message_box.setObjectName('toastMessageBox')
            message_box.setStyleSheet(
                'QFrame { background-color: #101c32; border: 1px solid #203453;'
                ' border-radius: 5px; }'
            )
            message_lay = QVBoxLayout(message_box)
            message_lay.setContentsMargins(max(8, pad // 2), max(7, pad // 3),
                                            max(8, pad // 2), max(7, pad // 3))
            message_lay.setSpacing(max(8, pad // 2))
            message_lay.addWidget(_section_label('信息'))

            msg_lbl = QLabel(message)
            msg_lbl.setObjectName('toastMessage')
            msg_lbl.setWordWrap(True)
            msg_lbl.setMaximumWidth(width - pad * 3 - 4)
            msg_lbl.setStyleSheet(
                f'color: #cccccc; font-size: {msg_pt}pt;'
                f' background: transparent; border: none;'
            )
            message_lay.addWidget(msg_lbl)
            inner.addWidget(message_box)
        else:
            inner.addStretch(1)

        # 自动关闭
        if duration_ms > 0:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._do_close)
            t.start(duration_ms)

        self.setFixedSize(width, width)

    def _do_close(self):
        self.closed.emit(self)
        self.hide()
        self.deleteLater()


class ToastManager:
    """管理所有活跃 Toast，右下角向上叠加排列。"""

    def __init__(self):
        self._active: list[_ToastWidget] = []

    def show(self, title: str, message: str, level: str = 'info',
             size: str = 'medium', duration_ms: int = 5000, alert_type: str = ''):
        toast = _ToastWidget(title, message, level, size, duration_ms, alert_type)
        toast.closed.connect(self._on_closed)
        self._active.append(toast)
        self._reposition()
        toast.show()

    def _on_closed(self, toast: _ToastWidget):
        if toast in self._active:
            self._active.remove(toast)
        self._reposition()

    def _reposition(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        y = geo.bottom() - _MARGIN
        for toast in reversed(self._active):
            w = toast._width
            x = geo.right() - w - _MARGIN
            toast.move(x, y - w)   # 正方形：高 = 宽
            y -= w + _GAP


# ── 全局单例 ──────────────────────────────────────────────────────────────
_manager: ToastManager | None = None


def get_manager() -> ToastManager:
    global _manager
    if _manager is None:
        _manager = ToastManager()
    return _manager


def show_toast(title: str, message: str, level: str = 'info',
               size: str = 'medium', duration_ms: int = 5000, alert_type: str = ''):
    """全局入口：在右下角显示一条浮动通知。

    size: 'large'（屏幕宽 1/6）| 'medium'（1/8）| 'small'（1/12）
    """
    get_manager().show(title, message, level, size, duration_ms, alert_type)
