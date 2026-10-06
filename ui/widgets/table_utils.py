from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QFontMetrics, QPalette, QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QHeaderView, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
)


class _WrapAnywhereDelegate(QStyledItemDelegate):
    """文本不省略、按列宽任意位置折行：长数字（如 18,468,460）也能折到下一行，
    行高随内容向下自适应。"""

    def _make_doc(self, opt: QStyleOptionViewItem, width: float) -> QTextDocument:
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        to = QTextOption()
        to.setWrapMode(QTextOption.WrapMode.WrapAnywhere)
        to.setAlignment(opt.displayAlignment)
        doc.setDefaultTextOption(to)
        doc.setDocumentMargin(2)
        doc.setPlainText(opt.text)
        if width > 0:
            doc.setTextWidth(width)
        return doc

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not opt.text:
            super().paint(painter, option, index)
            return

        style = opt.widget.style() if opt.widget else QApplication.style()
        text = opt.text
        opt.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        if opt.state & QStyle.StateFlag.State_Selected:
            color = opt.palette.color(QPalette.ColorRole.HighlightedText)
        else:
            color = opt.palette.color(QPalette.ColorRole.Text)

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        opt.text = text
        doc = self._make_doc(opt, text_rect.width())

        painter.save()
        painter.translate(text_rect.topLeft())
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.ColorRole.Text, color)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def sizeHint(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not opt.text:
            return super().sizeHint(option, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        fm = QFontMetrics(opt.font)
        text_w = fm.horizontalAdvance(opt.text)
        overhead = 8
        if opt.rect.width() > 0:
            text_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
            overhead = max(overhead, opt.rect.width() - text_rect.width())
        return QSize(text_w + overhead + 8, fm.height() + 10)


def configure_no_truncation(table: QTableWidget) -> None:
    """价格/数值不截断：关闭省略号 + 任意位置折行 + 行高随字体自适应。
    sizeHint 用 QFontMetrics 算单行宽高（不 new QTextDocument），
    ResizeToContents 列据此取单行自然宽，行高随字号同步增高。"""
    table.setWordWrap(True)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    table.setItemDelegate(_WrapAnywhereDelegate(table))
    table.verticalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents
    )
