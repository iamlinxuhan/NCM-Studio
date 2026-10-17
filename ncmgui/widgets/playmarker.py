"""表格第 0 列的播放标记。

这里不用 ``⏸`` / ``❚❚`` 之类的字符：不同字体给这些符号的字面宽度差得很远，
同一个字号下有的挤成一团、有的中间空出一大块。改成和播放条同一种画法，
用两条紧凑的圆角竖条画暂停、一个实心三角画播放，间距和粗细都由代码控制。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .. import theme

#: 模型在第 0 列返回这两个字符串时，由本委托接管绘制
PAUSE_MARKER = "❚❚"
PLAY_MARKER = "▶"


class PlayMarkerDelegate(QStyledItemDelegate):
    """把第 0 列的 ▶ / ❚❚ 换成矢量图标。"""

    def paint(self, painter: QPainter, option, index) -> None:
        marker = index.data(Qt.ItemDataRole.DisplayRole)
        if marker not in (PAUSE_MARKER, PLAY_MARKER):
            super().paint(painter, option, index)
            return

        # 先用默认绘制把交替行底色 / 选中高亮画出来，只是不带文字
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        super().paint(painter, opt, index)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        color = QColor(theme.CYAN if selected else theme.TEXT)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)

        rect = option.rect
        cx = rect.center().x() + 0.5
        cy = rect.center().y() + 0.5
        height = min(14.0, max(8.0, float(rect.height()) - 14.0))

        if marker == PAUSE_MARKER:
            # 比例照搬播放条上的暂停键：竖条宽约高度的 1/4，缝约一多半条宽
            bar_w = height * 0.26
            gap = height * 0.17
            total = bar_w * 2 + gap
            left = cx - total / 2
            top = cy - height / 2
            painter.drawRoundedRect(QRectF(left, top, bar_w, height), 1.0, 1.0)
            painter.drawRoundedRect(
                QRectF(left + bar_w + gap, top, bar_w, height), 1.0, 1.0
            )
        else:
            # 播放三角：比暂停标记略宽一点，视觉重量才接近
            half = height * 0.45
            triangle = QPainterPath()
            triangle.moveTo(QPointF(cx - half, cy - height / 2))
            triangle.lineTo(QPointF(cx - half, cy + height / 2))
            triangle.lineTo(QPointF(cx + half, cy))
            triangle.closeSubpath()
            painter.drawPath(triangle)

        painter.restore()
