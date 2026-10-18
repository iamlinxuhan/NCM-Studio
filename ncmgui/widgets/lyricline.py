"""播放页的一行歌词，当前行带逐字“幕布”。

**为什么不用 QLabel：** QLabel 只能整行一个颜色。逐字歌词要的是“同一时刻，唱过的
字深、没唱的字浅，幕布边缘落在字中间时字内还有连续渐变”。这必须画在字形本身
上，而不是在字后面铺一个矩形，所以这里用 ``QTextLayout`` 自己排版和绘制。

绘制分两层：

1. 整行先用浅蓝画一遍（未唱）；
2. 当前行把深蓝字形画到一张透明图上，用 ``DestinationIn`` 加一条横向 alpha
   渐变作为遮罩，再叠回浅蓝字上。遮罩左侧不透明、右侧透明，中间是一段软边，
   所以颜色变化发生在**字形内部**；软边随时间扫过每个字，就是幕布。

幕布位置由 :func:`ncmgui.lyrics.curtain_char` 根据字时间算出行内字符位置，这里
只负责把字符位置换算成横坐标。每 30ms 只更新当前行的遮罩并重绘，不重建排版。

普通 LRC 只有行级时间，没有逐字数据时由 :func:`ncmgui.lyrics.synthetic_words`
把整行时长按词/字均摊，因此两种歌词的表现一致，不再是「逐字有动画、整句只有
一种颜色」。
"""

from __future__ import annotations

import math
from dataclasses import replace

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QResizeEvent,
    QTextLayout,
    QTextOption,
)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .. import lyrics, theme

#: 与原来 QSS 里的 ``padding: 2px 6px`` 对齐
PAD_X = 6.0
PAD_Y = 2.0
#: 非当前行 / 当前行的字号，和原来 QSS 的 15px / 18px 一致
NORMAL_SIZE = 15
CURRENT_SIZE = 18


class LyricLineWidget(QWidget):
    """一行原文歌词。当前行画幕布 —— 逐字数据缺失时由整行时长均摊得到。"""

    def __init__(self, line: lyrics.TimedLine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line = line
        #: 普通 LRC 没有字时间，按词/字均摊一份，渲染路径与逐字歌词保持一致
        self._words = lyrics.words_for(line)
        self._timed = replace(line, words=self._words) if self._words else line
        self._current = False
        self._position = 0.0

        self._layout: QTextLayout | None = None
        self._font: QFont | None = None
        self._layout_width = -1.0
        self._layout_height = 0.0
        self._resize_width = -1.0

        policy = QSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(1)
        # 歌词行不接收鼠标事件，滚轮交给 QScrollArea
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------ 外部状态

    def line(self) -> lyrics.TimedLine:
        return self._line

    def set_current(self, current: bool) -> None:
        """切换当前行：字号、粗细和底色都跟着变，必须重排版。"""
        if current == self._current:
            return
        self._current = current
        self._layout = None
        self.updateGeometry()
        self.update()

    def set_position(self, position: float | None) -> None:
        """播放位置（秒）。只影响幕布，不动文本排版。"""
        try:
            value = float(position) if position is not None else 0.0
        except (TypeError, ValueError):
            return
        if value != value:  # NaN
            return
        if value == self._position:
            return
        self._position = value
        if self._current and self._words:
            self.update()

    # ------------------------------------------------------------ 尺寸/排版

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 接口
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 接口
        self._ensure_layout(max(1, int(width)))
        return int(math.ceil(self._layout_height)) + int(PAD_Y * 2)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 接口
        width = self.width() if self.width() > 0 else 320
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 接口
        return QSize(40, self.heightForWidth(40))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        width = float(event.size().width())
        if abs(width - self._resize_width) > 0.5:
            self._resize_width = width
            # 宽度变了换行位置就变了，下一次绘制时重排
            self._layout = None
            self.updateGeometry()
            self.update()

    def _ensure_layout(self, width: int) -> None:
        content_width = max(1.0, float(width) - PAD_X * 2)
        if self._layout is not None and abs(self._layout_width - content_width) < 0.5:
            return

        font = QFont(theme.ui_family())
        font.setPixelSize(CURRENT_SIZE if self._current else NORMAL_SIZE)
        font.setBold(self._current)

        layout = QTextLayout(self._line.text, font)
        option = QTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignCenter)
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)

        # PyQt6 里 createLine() 不会自动给每行设置纵向位置，必须手动 setPosition，
        # 否则所有行会叠在 y=0 上。
        layout.beginLayout()
        y = 0.0
        while True:
            text_line = layout.createLine()
            if not text_line.isValid():
                break
            text_line.setLineWidth(content_width)
            text_line.setPosition(QPointF(0.0, y))
            y += text_line.height()
        layout.endLayout()

        self._font = font
        self._layout = layout
        self._layout_width = content_width
        self._layout_height = max(y, float(font.pixelSize()))

    # ------------------------------------------------------------ 幕布位置

    def _curtain(self) -> tuple[int, float]:
        """返回 ``(当前视觉行序号, 幕布在该行的 x)``。"""
        layout = self._layout
        if layout is None or layout.lineCount() == 0 or self._font is None:
            return 0, 0.0

        text_length = len(self._line.text)
        char_pos = lyrics.curtain_char(self._timed, self._position)
        char_pos = max(0.0, min(float(text_length), float(char_pos)))

        if text_length == 0 or char_pos >= text_length:
            index = layout.lineCount() - 1
            line = layout.lineAt(index)
            x = line.cursorToX(line.textStart() + line.textLength())[0]
            return index, float(x)

        position = int(char_pos)
        visual = layout.lineForTextPosition(position)
        if not visual.isValid():
            index = layout.lineCount() - 1
            line = layout.lineAt(index)
            return index, float(line.cursorToX(line.textStart() + line.textLength())[0])

        index = 0
        # 找 visual 在 layout 里的序号。lineForTextPosition 返回的是新包装，
        # 不能用对象同一性比较，只能用 textStart。
        for i in range(layout.lineCount()):
            if layout.lineAt(i).textStart() == visual.textStart():
                index = i
                break

        start = visual.textStart()
        end = start + visual.textLength()
        if char_pos <= start:
            return index, float(visual.cursorToX(start)[0])
        if char_pos >= end:
            return index, float(visual.cursorToX(end)[0])

        low = int(math.floor(char_pos))
        high = min(end, low + 1)
        x_low = visual.cursorToX(low)[0]
        x_high = visual.cursorToX(high)[0]
        fraction = char_pos - low
        return index, float(x_low + (x_high - x_low) * fraction)

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt 接口
        if not self._line.text:
            return
        self._ensure_layout(max(1, self.width()))
        if self._layout is None or self._font is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        origin = QPointF(PAD_X, PAD_Y)

        if self._current and not self._words:
            base_color = QColor(theme.CYAN)
        elif self._current:
            base_color = QColor(theme.LYRIC_UNSUNG)
        else:
            base_color = QColor(theme.TEXT_DIM)

        painter.setPen(base_color)
        painter.setFont(self._font)
        self._layout.draw(painter, origin)

        if self._current and self._words:
            self._paint_curtain(painter, origin)
        painter.end()

    def _paint_curtain(self, painter: QPainter, origin: QPointF) -> None:
        """把深蓝色字形按幕布遮罩叠到浅色底字上。"""
        layout = self._layout
        font = self._font
        if layout is None or font is None:
            return

        # 按屏幕缩放渲染，高分屏下不糊
        dpr = max(1.0, float(self.devicePixelRatioF()))
        image = QImage(
            max(1, int(round(self.width() * dpr))),
            max(1, int(round(self.height() * dpr))),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)

        layer = QPainter(image)
        layer.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        layer.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        layer.setPen(QColor(theme.LYRIC_SUNG))
        layer.setFont(font)
        layout.draw(layer, origin)

        visual_index, curtain_x = self._curtain()
        band = max(10.0, font.pixelSize() * float(theme.LYRIC_CURTAIN_BAND))
        left = PAD_X + curtain_x - band
        right = PAD_X + curtain_x + band

        # 遮罩单独画一张图，再整体乘到深色字上。
        #
        # 不能只对「该显示深色」的行做 DestinationIn：这个混合模式只处理
        # 被画到的像素，没画到的位置保留原样 —— 换行后的第二视觉行没人去
        # 碰，就会一直保持深色。之前单行歌词看不出问题，一旦挤压换行，
        # 整行未播放的部分全变深，就是这个原因。
        mask = QImage(
            image.size(),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        mask.setDevicePixelRatio(dpr)
        mask.fill(Qt.GlobalColor.transparent)

        shade = QPainter(mask)
        shade.setPen(Qt.PenStyle.NoPen)
        for index in range(layout.lineCount()):
            text_line = layout.lineAt(index)
            rect = QRectF(
                0.0,
                PAD_Y + text_line.y(),
                float(self.width()),
                text_line.height(),
            )
            if index < visual_index:
                # 幕布已经扫过去的视觉行：整行深色
                shade.fillRect(rect, QColor(255, 255, 255, 255))
            elif index == visual_index:
                gradient = QLinearGradient(left, 0.0, right, 0.0)
                gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
                gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
                shade.fillRect(rect, gradient)
            # 后面的视觉行留空（透明），乘上去就把深色字擦掉，露出浅色底字
        shade.end()

        layer.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_DestinationIn
        )
        layer.drawImage(0, 0, mask)
        layer.end()
        painter.drawImage(0, 0, image)
