"""统计页用的轻量自绘图表。

不引入 matplotlib / pyqtgraph：三块图都很简单，QPainter 画出来体积和启动
开销都更小，颜色也能直接跟随主题切换。
"""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QToolTip, QWidget

from .. import theme
from ..library.stats import DayEntry, SliceEntry, format_duration

DAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


def _mix(start: QColor, end: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(start.red() + (end.red() - start.red()) * ratio),
        round(start.green() + (end.green() - start.green()) * ratio),
        round(start.blue() + (end.blue() - start.blue()) * ratio),
    )


class HeatmapWidget(QWidget):
    """最近 N 天的播放热力图，每列一周（周一在上）。"""

    CELL_GAP = 4
    LEFT = 30
    TOP = 22
    BOTTOM = 6
    MIN_CELL = 11
    MAX_CELL = 19

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._days: tuple[DayEntry, ...] = ()
        self._max = 1
        self._cells: list[tuple[QRect, DayEntry]] = []
        self._hover: date | None = None
        self.setMouseTracking(True)
        # 7 行格子 + 顶部月份标签；固定高度避免卡片压缩时最后一行被裁掉
        self.setFixedHeight(self.TOP + 7 * (self.MAX_CELL + self.CELL_GAP) + self.BOTTOM)

    def set_data(self, days: tuple[DayEntry, ...] | list[DayEntry]) -> None:
        self._days = tuple(days)
        self._max = max((item.plays for item in self._days), default=0) or 1
        self._hover = None
        self.update()

    def theme_changed(self) -> None:
        self.update()

    # ------------------------------------------------------------ 几何

    def _layout_metrics(self) -> tuple[int, int, int, int, int]:
        if not self._days:
            return 0, 0, 0, 0, 0
        first = self._days[0].day
        span = first.weekday() + len(self._days)
        columns = max(1, (span + 6) // 7)
        available = max(1, self.width() - self.LEFT - 8)
        cell = int(available / columns) - self.CELL_GAP
        cell = max(self.MIN_CELL, min(self.MAX_CELL, cell))
        grid_width = columns * (cell + self.CELL_GAP) - self.CELL_GAP
        # 列数少时把整块居中，避免右侧留下大片空白
        left = max(self.LEFT, (self.width() - grid_width) // 2)
        return columns, cell, first.weekday(), self.TOP, left

    def _cell_rect(self, index: int, metrics) -> QRect:
        _columns, cell, offset, top, left = metrics
        day = self._days[index].day
        position = offset + index
        column, row = divmod(position, 7)
        x = left + column * (cell + self.CELL_GAP)
        y = top + row * (cell + self.CELL_GAP)
        return QRect(x, y, cell, cell)

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        empty = QColor(theme.PANEL_ALT)
        border = QColor(theme.BORDER)
        accent = QColor(theme.CYAN)
        muted = QColor(theme.TEXT_DIM)
        text = QColor(theme.TEXT)

        font = self.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() - 2.0))
        painter.setFont(font)
        metrics = self._layout_metrics()

        self._cells = []
        if not self._days:
            painter.setPen(muted)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")
            return

        # 星期标签
        painter.setPen(muted)
        _columns, cell, _offset, top, left = metrics
        for row in (0, 2, 4):
            y = top + row * (cell + self.CELL_GAP) + cell
            painter.drawText(left - 28, y - 5, 24, 12,
                             int(Qt.AlignmentFlag.AlignRight), DAY_NAMES[row])

        # 月份标签：每一列第一次跨月时画一次
        last_month = ""
        for index, entry in enumerate(self._days):
            column = (metrics[2] + index) // 7
            label = f"{entry.day.month}月"
            if label == last_month:
                continue
            last_month = label
            x = left + column * (cell + self.CELL_GAP)
            painter.setPen(muted)
            painter.drawText(x, 0, 40, self.TOP - 4,
                             int(Qt.AlignmentFlag.AlignLeft), label)

        # 格子
        for index, entry in enumerate(self._days):
            rect = self._cell_rect(index, metrics)
            self._cells.append((rect, entry))
            if entry.plays <= 0:
                color = empty
            else:
                ratio = entry.plays / self._max
                # 1 次也至少看得出来；4 级足够区分
                level = 1 if ratio <= 0.25 else 2 if ratio <= 0.5 else 3 if ratio <= 0.75 else 4
                color = _mix(empty, accent, 0.30 + 0.18 * level)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(rect), 3, 3)

            if entry.day == self._hover:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(text, 1.4))
                painter.drawRoundedRect(QRectF(rect).adjusted(0.7, 0.7, -0.7, -0.7), 3, 3)

        # 今天的格子加一圈细描边，方便定位
        if self._days:
            today_rect = self._cells[-1][0]
            painter.setBrush(Qt.BrushStyle.NoBrush)
            today_rect = today_rect.adjusted(0, 0, -1, -1)
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(QRectF(today_rect), 3, 3)

    # ------------------------------------------------------------ 交互

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        position = event.position().toPoint()
        hit = None
        for rect, entry in self._cells:
            if rect.contains(position):
                hit = entry
                break
        if hit is None:
            if self._hover is not None:
                self._hover = None
                self.update()
            QToolTip.hideText()
            return
        if self._hover != hit.day:
            self._hover = hit.day
            self.update()
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{hit.day:%Y-%m-%d}（周{DAY_NAMES[hit.day.weekday()]}）\n"
                f"播放 {hit.plays} 次 · {format_duration(hit.listened_ms)}",
                self,
            )

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.update()
        QToolTip.hideText()
        super().leaveEvent(event)


class HourBars(QWidget):
    """24 小时播放次数柱状图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hours: tuple[int, ...] = (0,) * 24
        self.setMinimumHeight(150)

    def set_data(self, hours: tuple[int, ...]) -> None:
        self._hours = tuple(hours) + (0,) * max(0, 24 - len(hours))
        self.update()

    def theme_changed(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        muted = QColor(theme.TEXT_DIM)
        empty = QColor(theme.PANEL_ALT)
        accent = QColor(theme.CYAN)
        peak = QColor(theme.PURPLE)

        font = self.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() - 2.0))
        painter.setFont(font)
        font_metrics = QFontMetrics(font)

        left, right, top, bottom = 8, 8, 10, 22
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        slot = width / 24
        gap = max(2.0, slot * 0.22)
        bar_width = max(2.0, slot - gap)
        peak_value = max(self._hours) if self._hours else 0

        for hour, count in enumerate(self._hours):
            x = left + hour * slot + gap / 2
            if peak_value:
                bar_height = max(2.0, height * count / peak_value)
            else:
                bar_height = 2.0
            rect = QRectF(x, top + height - bar_height, bar_width, bar_height)
            if count <= 0:
                painter.setBrush(empty)
            else:
                painter.setBrush(peak if count == peak_value else accent)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, min(3.0, bar_width / 2), min(3.0, bar_width / 2))

        painter.setPen(muted)
        for hour in range(0, 24, 3):
            label = f"{hour:02d}"
            text_width = font_metrics.horizontalAdvance(label) + 4
            x = left + hour * slot + slot / 2 - text_width / 2
            painter.drawText(QRectF(x, self.height() - bottom + 3, text_width, 16),
                             int(Qt.AlignmentFlag.AlignCenter), label)


class SliceBars(QWidget):
    """标签 + 横条 + 次数，用来画音质构成。"""

    ROW_HEIGHT = 26
    LABEL_WIDTH = 92
    VALUE_WIDTH = 46

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[SliceEntry, ...] = ()
        self.setMinimumHeight(self.ROW_HEIGHT)

    def set_data(self, items: tuple[SliceEntry, ...] | list[SliceEntry]) -> None:
        self._items = tuple(items)
        self.setMinimumHeight(max(self.ROW_HEIGHT, len(self._items) * self.ROW_HEIGHT + 4))
        self.updateGeometry()
        self.update()

    def theme_changed(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        text = QColor(theme.TEXT)
        muted = QColor(theme.TEXT_DIM)
        track = QColor(theme.PANEL_ALT)
        accent = QColor(theme.CYAN)

        if not self._items:
            painter.setPen(muted)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无数据")
            return

        maximum = max(item.count for item in self._items)
        bar_left = self.LABEL_WIDTH
        bar_right = max(bar_left + 20, self.width() - self.VALUE_WIDTH)

        for index, item in enumerate(self._items):
            top = index * self.ROW_HEIGHT + 3
            middle = top + self.ROW_HEIGHT / 2
            painter.setPen(text)
            painter.drawText(0, top, self.LABEL_WIDTH - 10, self.ROW_HEIGHT,
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             item.label)

            bar_height = 9
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(
                QRectF(bar_left, middle - bar_height / 2,
                       bar_right - bar_left, bar_height), 4.5, 4.5)
            ratio = item.count / maximum
            painter.setBrush(accent)
            painter.drawRoundedRect(
                QRectF(bar_left, middle - bar_height / 2,
                       max(3.0, (bar_right - bar_left) * ratio), bar_height), 4.5, 4.5)

            painter.setPen(muted)
            painter.drawText(bar_right + 6, top, self.VALUE_WIDTH - 6, self.ROW_HEIGHT,
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             f"{item.count} 次")
