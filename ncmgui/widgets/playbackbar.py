"""全局底部播放条。

不管当前在哪一页，这一条都在：封面/歌名、上一首/播放/下一首、进度、音量和
播放顺序。左侧的歌曲信息可以点 —— 从播放页「返回」之后，这就是回到黑胶
唱片那一页的入口。

它只读 :class:`~ncmgui.playback.PlaybackController` 的状态，自己不留第二份 ——
否则暂停状态在两个地方各记一份，迟早对不上。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from .. import images, player, theme
from ..playback import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PLAYING,
    mode_label,
)
from .card import ghost_button
from ..workers import BackgroundTask

#: 已喜欢时红心的颜色；网易云的红色太扎眼，这里用略柔和的玫红
HEART_COLOR = "#ef476f"


class SeekSlider(QSlider):
    """点轨道任意处就跳过去的滑块。

    ``QSlider`` 默认点轨道是「翻一页」，对一首歌来说太钝。点在滑块本身上时
    仍然交给父类，拖动才不会被吃掉。
    """

    clickedValue = pyqtSignal(int)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            if not handle.contains(event.position().toPoint()):
                value = QStyle.sliderValueFromPosition(
                    self.minimum(),
                    self.maximum(),
                    int(event.position().x()),
                    max(1, self.width()),
                )
                self.setValue(value)
                self.clickedValue.emit(value)
                event.accept()
                return
        super().mousePressEvent(event)


def _speaker_pixmap(volume: int) -> QPixmap:
    """画一个喇叭图标，避免依赖 emoji 字体（彩色 emoji 改不了颜色和大小）。

    0 是带叉的静音图标，1–49 一道声波，50 以上两道。
    """
    volume = max(0, min(100, int(volume)))
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)

        color = QColor(theme.TEXT if volume > 0 else theme.TEXT_DIM)
        painter.setPen(
            QPen(
                color,
                1.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(color)

        body = QPainterPath()
        body.moveTo(2.8, 8.0)
        body.lineTo(6.6, 8.0)
        body.lineTo(11.2, 3.8)
        body.lineTo(11.2, 16.2)
        body.lineTo(6.6, 12.0)
        body.lineTo(2.8, 12.0)
        body.closeSubpath()
        painter.drawPath(body)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        if volume > 0:
            painter.drawArc(QRectF(9.8, 6.4, 6.4, 7.2), -55 * 16, 110 * 16)
            if volume >= 50:
                painter.drawArc(QRectF(9.0, 4.2, 11.0, 11.6), -55 * 16, 110 * 16)
        else:
            painter.drawLine(QPoint(14, 8), QPoint(18, 12))
            painter.drawLine(QPoint(18, 8), QPoint(14, 12))
    finally:
        painter.end()
    return pixmap


def _arrow_head(
    painter: QPainter, tip: QPointF, direction: QPointF, size: float = 2.5
) -> None:
    """在 ``tip`` 处画一个指向 ``direction`` 的实心小三角。"""
    length = math.hypot(direction.x(), direction.y()) or 1.0
    dx, dy = direction.x() / length, direction.y() / length
    nx, ny = -dy, dx
    base_x, base_y = tip.x() - dx * size, tip.y() - dy * size
    half = size * 0.62
    painter.drawPolygon(
        QPolygonF(
            [
                tip,
                QPointF(base_x + nx * half, base_y + ny * half),
                QPointF(base_x - nx * half, base_y - ny * half),
            ]
        )
    )


def _transport_pixmap(kind: str, color: QColor | None = None) -> QPixmap:
    """上一首 / 播放 / 暂停 / 下一首的图标，线条和三角都自己画。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)
        tint = QColor(color) if color is not None else QColor(theme.TEXT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)

        if kind == "play":
            triangle = QPainterPath()
            triangle.moveTo(6.6, 4.2)
            triangle.lineTo(16.0, 10.0)
            triangle.lineTo(6.6, 15.8)
            triangle.closeSubpath()
            painter.drawPath(triangle)
        elif kind == "pause":
            painter.drawRoundedRect(QRectF(6.6, 4.4, 2.8, 11.2), 1.2, 1.2)
            painter.drawRoundedRect(QRectF(11.2, 4.4, 2.8, 11.2), 1.2, 1.2)
        elif kind == "prev":
            painter.drawRoundedRect(QRectF(4.8, 4.6, 2.2, 10.8), 1.0, 1.0)
            triangle = QPainterPath()
            triangle.moveTo(15.4, 4.6)
            triangle.lineTo(15.4, 15.4)
            triangle.lineTo(7.4, 10.0)
            triangle.closeSubpath()
            painter.drawPath(triangle)
        elif kind == "next":
            painter.drawRoundedRect(QRectF(13.0, 4.6, 2.2, 10.8), 1.0, 1.0)
            triangle = QPainterPath()
            triangle.moveTo(4.6, 4.6)
            triangle.lineTo(4.6, 15.4)
            triangle.lineTo(12.6, 10.0)
            triangle.closeSubpath()
            painter.drawPath(triangle)
    finally:
        painter.end()
    return pixmap


def _control_pixmap(kind: str, color: QColor | None = None) -> QPixmap:
    """播放条上的模式/队列图标。

    和喇叭一样用 QPainter 画：彩色 emoji 字体改不了颜色，纯文字又太占宽度。
    所有图形都按 20×20 的逻辑坐标设计，实际画在 40×40 上缩小显示。
    """
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)

        color = QColor(color) if color is not None else QColor(theme.TEXT)
        painter.setPen(
            QPen(
                color,
                1.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if kind in {"loop", "one"}:
            # 缺口留在右上角，箭头顺着顺时针方向收尾
            painter.drawArc(QRectF(3.4, 3.4, 13.2, 13.2), 15 * 16, -330 * 16)
            theta = math.radians(45)
            radius = 6.6
            tip = QPointF(
                10 + radius * math.cos(theta),
                10 - radius * math.sin(theta),
            )
            painter.setBrush(color)
            _arrow_head(
                painter,
                tip,
                QPointF(math.sin(theta), math.cos(theta)),
                2.6,
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if kind == "one":
                font = QFont()
                font.setPixelSize(8)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(
                    QRectF(0, 0, 20, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    "1",
                )

        elif kind == "shuffle":
            upper = QPainterPath(QPointF(3.0, 13.4))
            upper.cubicTo(QPointF(7.0, 13.4), QPointF(8.0, 6.6), QPointF(13.2, 6.6))
            painter.drawPath(upper)
            lower = QPainterPath(QPointF(3.0, 6.6))
            lower.cubicTo(QPointF(7.0, 6.6), QPointF(8.0, 13.4), QPointF(13.2, 13.4))
            painter.drawPath(lower)
            painter.setBrush(color)
            _arrow_head(painter, QPointF(15.8, 6.6), QPointF(1, 0), 2.4)
            _arrow_head(painter, QPointF(15.8, 13.4), QPointF(1, 0), 2.4)

        elif kind == "order":
            for y in (6.8, 13.2):
                painter.drawLine(QPointF(3.0, y), QPointF(14.4, y))
                painter.setBrush(color)
                _arrow_head(painter, QPointF(17.0, y), QPointF(1, 0), 2.4)
                painter.setBrush(Qt.BrushStyle.NoBrush)

        elif kind == "queue":
            # 三行等距（原来 7.5/13.2/16.4 间距不匀，第一行单独飘在上面，
            # 缩到 20px 后糊成一团）。三角高度对齐一行间距，三条线右端
            # 对齐，第一行让开三角，整体就是「正在播放的列表」。
            rows = (5.0, 10.0, 15.0)
            triangle = QPainterPath()
            triangle.moveTo(3.2, 2.5)
            triangle.lineTo(3.2, 7.5)
            triangle.lineTo(7.6, 5.0)
            triangle.closeSubpath()
            painter.setBrush(color)
            painter.drawPath(triangle)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # 三条线右端对齐；第一行让开三角，后两行顶到左边
            painter.drawLine(QPointF(9.4, rows[0]), QPointF(16.8, rows[0]))
            painter.drawLine(QPointF(3.2, rows[1]), QPointF(16.8, rows[1]))
            painter.drawLine(QPointF(3.2, rows[2]), QPointF(16.8, rows[2]))
    finally:
        painter.end()
    return pixmap


def _heart_pixmap(filled: bool, color: QColor | None = None) -> QPixmap:
    """红心图标：喜欢时实心涂红，未喜欢时只画轮廓。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)

        path = QPainterPath(QPointF(10.0, 16.4))
        path.cubicTo(QPointF(8.6, 15.1), QPointF(3.4, 11.7), QPointF(3.4, 7.7))
        path.cubicTo(QPointF(3.4, 5.0), QPointF(5.4, 3.3), QPointF(7.5, 3.3))
        path.cubicTo(QPointF(8.8, 3.3), QPointF(9.7, 4.1), QPointF(10.0, 4.8))
        path.cubicTo(QPointF(10.3, 4.1), QPointF(11.2, 3.3), QPointF(12.5, 3.3))
        path.cubicTo(QPointF(14.6, 3.3), QPointF(16.6, 5.0), QPointF(16.6, 7.7))
        path.cubicTo(QPointF(16.6, 11.7), QPointF(11.4, 15.1), QPointF(10.0, 16.4))

        if filled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(HEART_COLOR) if color is None else QColor(color))
            painter.drawPath(path)
        else:
            tint = QColor(theme.TEXT) if color is None else QColor(color)
            painter.setPen(
                QPen(
                    tint,
                    1.7,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
    finally:
        painter.end()
    return pixmap


def _heart_icon(liked: bool, enabled: bool = True) -> QIcon:
    """喜欢/未喜欢、可用/禁用四种组合的图标。"""
    icon = QIcon()
    icon.addPixmap(
        _heart_pixmap(liked and enabled),
        QIcon.Mode.Normal,
    )
    icon.addPixmap(
        _heart_pixmap(False, _disabled_tint()),
        QIcon.Mode.Disabled,
    )
    return icon


def _dislike_pixmap(color: QColor | None = None) -> QPixmap:
    """不感兴趣：一颗带斜杠的心，和网页播放器底栏一致。

    斜杠穿过心形时要把底下那一段心先擦成透明（留出缺口），否则两条线
    糊在一起，缩到 26px 就看不出是一颗「被划掉的心」。
    """
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)

        tint = QColor(theme.TEXT) if color is None else QColor(color)
        path = QPainterPath(QPointF(10.0, 16.4))
        path.cubicTo(QPointF(8.6, 15.1), QPointF(3.4, 11.7), QPointF(3.4, 7.7))
        path.cubicTo(QPointF(3.4, 5.0), QPointF(5.4, 3.3), QPointF(7.5, 3.3))
        path.cubicTo(QPointF(8.8, 3.3), QPointF(9.7, 4.1), QPointF(10.0, 4.8))
        path.cubicTo(QPointF(10.3, 4.1), QPointF(11.2, 3.3), QPointF(12.5, 3.3))
        path.cubicTo(QPointF(14.6, 3.3), QPointF(16.6, 5.0), QPointF(16.6, 7.7))
        path.cubicTo(QPointF(16.6, 11.7), QPointF(11.4, 15.1), QPointF(10.0, 16.4))

        painter.setPen(QPen(tint, 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        start, end = QPointF(3.8, 16.2), QPointF(16.2, 3.8)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(QPen(Qt.GlobalColor.transparent, 4.8,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, end)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QPen(tint, 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, end)
    finally:
        painter.end()
    return pixmap


def _dislike_icon() -> QIcon:
    icon = QIcon()
    icon.addPixmap(_dislike_pixmap(), QIcon.Mode.Normal)
    icon.addPixmap(_dislike_pixmap(_disabled_tint()), QIcon.Mode.Disabled)
    return icon


def _collect_pixmap(color: QColor | None = None) -> QPixmap:
    """收藏到歌单：一个带加号的方框，和网页端的收藏图标一致。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)
        tint = QColor(theme.TEXT) if color is None else QColor(color)
        painter.setPen(QPen(tint, 1.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(3.6, 4.4, 12.8, 11.4), 2.2, 2.2)
        painter.drawLine(QPointF(7.4, 10.1), QPointF(12.6, 10.1))
        painter.drawLine(QPointF(10.0, 7.5), QPointF(10.0, 12.7))
    finally:
        painter.end()
    return pixmap


def _collect_icon(enabled: bool = True) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_collect_pixmap(), QIcon.Mode.Normal)
    icon.addPixmap(_collect_pixmap(_disabled_tint()), QIcon.Mode.Disabled)
    return icon


def _draw_comment_bubble(painter: QPainter, color: QColor | None = None) -> None:
    """在左上角 21×21 的范围内画评论气泡；调用方负责缩放和坐标原点。

    形状跟网页播放器一致：圆气泡 + 左下角小尾巴，里面两道横线代表文字。
    """
    tint = QColor(theme.TEXT) if color is None else QColor(color)
    painter.setPen(QPen(tint, 1.6, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(3.4, 2.8, 14.0, 13.2))
    # 左下角的尖尾巴；两端落在圆上
    tail = QPainterPath()
    tail.moveTo(4.9, 13.5)
    tail.lineTo(4.0, 17.4)
    tail.lineTo(7.4, 15.1)
    painter.drawPath(tail)
    # 两道横线：上面短、下面长
    painter.drawLine(QPointF(7.6, 8.3), QPointF(9.7, 8.3))
    painter.drawLine(QPointF(7.6, 11.1), QPointF(12.4, 11.1))


def _comment_pixmap(color: QColor | None = None) -> QPixmap:
    """评论：一个带小尾巴的气泡，里面三个点。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)
        _draw_comment_bubble(painter, color)
    finally:
        painter.end()
    return pixmap


def _comment_count_text(total: int) -> str:
    """评论数按网页端的写法显示：一万以上缩成「4w+」。"""
    total = int(total or 0)
    if total >= 10000:
        return f"{total // 10000}w+"
    return str(total)


#: 评论按钮里数字的字号（逻辑像素）
_COMMENT_COUNT_FONT = 10
#: 带上数字后气泡稍微放大一点，图标和角标都看得清
_COMMENT_BUBBLE = 26


def _comment_button_pixmap(text: str = "", color: QColor | None = None) -> QPixmap:
    """气泡和评论数画在同一张图里，数字压在气泡右上角。

    网页播放器里数字不是单独一项，而是像角标一样嵌在气泡右上方；分开成
    两个控件时字号、基线很难对齐，画在一起后点击区域也只有一个。
    """
    font = QFont()
    font.setPixelSize(_COMMENT_COUNT_FONT)
    metrics = QFontMetrics(font)
    text_width = metrics.horizontalAdvance(text) if text else 0
    text_height = metrics.height() if text else 0
    bubble = _COMMENT_BUBBLE
    if text:
        # 数字从气泡中段起笔，压在右上角；底部留同样的透明边，气泡才会
        # 和旁边的心形、收藏图标在同一条水平线上
        text_x = bubble // 2
        text_dy = 3
        width = max(bubble, text_x + text_width + 1)
        bubble_y = max(0, text_height - 8)
        height = bubble_y * 2 + bubble
    else:
        width, height, bubble_y, text_dy = bubble, bubble, 0, 0

    pixmap = QPixmap(width * 2, height * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)
        painter.save()
        painter.translate(0, bubble_y)
        painter.scale(bubble / 21.0, bubble / 21.0)
        _draw_comment_bubble(painter, color)
        painter.restore()
        if text:
            # 和网页端一样，数字盖住气泡右上角：先把文字这一小块擦成透明，
            # 再写字。用透明擦除而不是填底色，鼠标悬停变底色时也不会露馅。
            ink = metrics.tightBoundingRect(text)
            ink_top = metrics.ascent() + ink.top() + text_dy
            ink_bottom = metrics.ascent() + ink.bottom() + text_dy
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(
                QRectF(text_x - 1, ink_top - 0.5,
                       width - text_x + 2, ink_bottom - ink_top + 1.5),
                Qt.GlobalColor.transparent,
            )
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT_DIM) if color is None else QColor(color))
            painter.drawText(
                QRectF(text_x, text_dy, text_width + 1, text_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
    finally:
        painter.end()
    return pixmap


def _comment_button_size(text: str = "") -> QSize:
    pixmap = _comment_button_pixmap(text)
    return QSize(pixmap.width() // 2, pixmap.height() // 2)


def _comment_icon(text: str = "", enabled: bool = True) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_comment_button_pixmap(text), QIcon.Mode.Normal)
    icon.addPixmap(
        _comment_button_pixmap(text, _disabled_tint()), QIcon.Mode.Disabled)
    return icon


def _disabled_tint() -> QColor:
    """禁用图标用的颜色。

    自绘 pixmap 交给平台样式去淡化并不靠谱：Breeze 会把 40×40 的图标淡到
    只剩一层影子，浅色主题下几乎看不见形状。这里自己把正文色和底色按
    55 : 45 混一档 —— 一眼能看出「灰了」，但轮廓仍然清楚，两个主题都成立。
    """
    tint = QColor(theme.TEXT)
    base = QColor(theme.PANEL)
    return QColor(
        round(tint.red() * 0.55 + base.red() * 0.45),
        round(tint.green() * 0.55 + base.green() * 0.45),
        round(tint.blue() * 0.55 + base.blue() * 0.45),
    )


def _transport_icon(kind: str) -> QIcon:
    """上一首/播放/下一首的图标，含自己画的禁用态。

    只给 Normal 的话，Qt 会用平台样式给禁用按钮生成一个淡化版本；各平台
    淡得不一样，Breeze 上尤其淡。显式提供 Disabled 才能保证不管用什么
    桌面主题，禁用时都还看得见。
    """
    icon = QIcon()
    icon.addPixmap(_transport_pixmap(kind), QIcon.Mode.Normal)
    icon.addPixmap(_transport_pixmap(kind, _disabled_tint()), QIcon.Mode.Disabled)
    return icon


def _control_icon(kind: str) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_control_pixmap(kind), QIcon.Mode.Normal)
    icon.addPixmap(_control_pixmap(kind, _disabled_tint()), QIcon.Mode.Disabled)
    return icon


class VolumePopup(QWidget):
    """点喇叭后在按钮上方弹出的小面板：竖直音量条 + 百分比。

    用 ``Qt.WindowType.Popup``：点面板外面任意处会自动收起，不需要自己
    装事件过滤器。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("VolumePopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 9)
        layout.setSpacing(7)

        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setObjectName("VolumeSlider")
        self.slider.setRange(0, 100)
        self.slider.setFixedSize(24, 118)
        layout.addWidget(self.slider, 0, Qt.AlignmentFlag.AlignHCenter)

        self.value_label = QLabel("0%")
        self.value_label.setObjectName("VolumeValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)

        self.setMinimumWidth(62)

    def show_above(self, anchor: QWidget) -> None:
        """停在 ``anchor`` 正上方；超出屏幕时往回收。"""
        self.adjustSize()
        origin = anchor.mapToGlobal(QPoint(0, 0))
        x = origin.x() + (anchor.width() - self.width()) // 2
        y = origin.y() - self.height() - 8

        screen = anchor.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 4, min(x, area.right() - self.width() - 3))
            y = max(area.top() + 4, y)

        self.move(x, y)
        self.show()
        self.slider.setFocus()


class NowPlaying(QWidget):
    """播放条左侧的「正在播放」：封面 + 歌名/歌手，点一下打开播放页。

    播放页不是导航项，离开之后侧栏里没有入口；没有这一块，用户从播放页
    返回就只能靠播放列表按钮 —— 不知道该点哪儿的人会被困在别的页面。
    """

    clicked = pyqtSignal()

    #: 封面缩略图边长
    COVER_SIZE = 46

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NowPlaying")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("打开播放页")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(216)

        self._placeholder = images.placeholder(self.COVER_SIZE)
        #: 当前真实封面；None 表示显示的是占位图
        self._source: QPixmap | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        self.cover = QLabel()
        self.cover.setObjectName("NowPlayingCover")
        self.cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.cover)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        self.title = QLabel("未在播放")
        self.title.setObjectName("NowPlayingTitle")
        self.artist = QLabel("")
        self.artist.setObjectName("NowPlayingArtist")
        text.addWidget(self.title)
        text.addWidget(self.artist)
        layout.addLayout(text, 1)

        # 事件统一交给这一层处理：点在封面或文字上也要能打开播放页
        for widget in (self.cover, self.title, self.artist):
            widget.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )

        self.set_cover(None)

    # ------------------------------------------------------------ 状态

    def set_song(self, song) -> None:
        if song is None:
            self.title.setText("未在播放")
            self.artist.setText("")
            self.setToolTip("打开播放页")
            return

        name = getattr(song, "name", "") or "未知曲目"
        artist = ", ".join(getattr(song, "artists", ()) or ()) or "未知歌手"
        metrics = self.title.fontMetrics()
        self.title.setText(
            metrics.elidedText(name, Qt.TextElideMode.ElideRight, 152)
        )
        self.artist.setText(
            self.artist.fontMetrics().elidedText(
                artist, Qt.TextElideMode.ElideRight, 152
            )
        )
        self.setToolTip(f"{name}\n{artist}\n点击打开播放页")

    def theme_changed(self) -> None:
        """占位图是构造时画好的 pixmap，换主题后重画；真实封面不动。"""
        self._placeholder = images.placeholder(self.COVER_SIZE)
        if self._source is None:
            self.set_cover(None)

    def set_cover(self, pixmap: QPixmap | None) -> None:
        """缩略图；``None`` 用渐变占位图。"""
        self._source = None if pixmap is None or pixmap.isNull() else pixmap
        if self._source is None:
            pixmap = self._placeholder
        scaled = pixmap.scaled(
            self.COVER_SIZE,
            self.COVER_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover.setPixmap(scaled)

    # ------------------------------------------------------------ 点击

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PlaybackBar(QWidget):
    """窗口底部的常驻播放条。"""

    #: 用户点了「播放列表」：打开播放页并展开队列
    queueRequested = pyqtSignal()
    #: 用户点了左侧歌曲信息：打开播放页（不展开队列）
    playerRequested = pyqtSignal()

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.playback = window.playback
        self.covers = getattr(window, "covers", None)

        self._song = None
        self._scrubbing = False
        #: 当前歌曲的评论数（懒加载，只用于按钮提示）
        self._comment_song_id = 0
        self._comment_count = 0
        self._comment_task = None

        self.setObjectName("PlaybackBar")
        self.setFixedHeight(70)
        self._build()
        self._connect()

        self._on_track_changed(self.playback.current)
        self._on_state_changed(self.playback.state)
        self._on_mode_changed(self.playback.mode)
        self._on_volume_changed(self.playback.volume)
        self._on_position_changed(self.playback.position, self.playback.duration)
        self._on_queue_changed()

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(10)

        # 左侧「正在播放」同时充当回到播放页的入口
        self.now_playing = NowPlaying(self)
        self.now_playing.clicked.connect(self.playerRequested.emit)
        layout.addWidget(self.now_playing)
        layout.addSpacing(2)

        # 歌曲动作图标排成一组，组内只留 2px：按钮本身已经是 32px 的点击
        # 区域，再吃主布局的 10px 间距，看上去就像四处漏风。
        icon_row = QHBoxLayout()
        icon_row.setContentsMargins(0, 0, 0, 0)
        icon_row.setSpacing(2)

        # 红心：喜欢时涂红，未喜欢只有轮廓；本地/流媒体歌曲没有这个概念，
        # 直接隐藏。保留占位，否则切歌时播放键会左右跳。
        self.like_btn = ghost_button("", compact=True)
        self.like_btn.setProperty("transport", "flat")
        self.like_btn.setFixedSize(32, 32)
        self.like_btn.setIconSize(QSize(26, 26))
        self.like_btn.setIcon(_heart_icon(False, False))
        self.like_btn.clicked.connect(self._toggle_like)
        like_policy = self.like_btn.sizePolicy()
        like_policy.setRetainSizeWhenHidden(True)
        self.like_btn.setSizePolicy(like_policy)
        icon_row.addWidget(self.like_btn)

        # 收藏 = 收藏到歌单，和红心（喜欢）是两件事。网页播放器里也是
        # 心形旁边一个方框加号。
        self.collect_btn = ghost_button("", compact=True)
        self.collect_btn.setProperty("transport", "flat")
        self.collect_btn.setFixedSize(32, 32)
        self.collect_btn.setIconSize(QSize(26, 26))
        self.collect_btn.setIcon(_collect_icon())
        self.collect_btn.clicked.connect(self._collect)
        collect_policy = self.collect_btn.sizePolicy()
        collect_policy.setRetainSizeWhenHidden(True)
        self.collect_btn.setSizePolicy(collect_policy)
        icon_row.addWidget(self.collect_btn)

        # 评论入口放在收藏旁边，和网页播放器一致。气泡和评论数画在同一张
        # 图里（一万以上缩写为「4w+」），整个按钮只有一处点击区域。
        self.comment_btn = ghost_button("", compact=True)
        self.comment_btn.setProperty("transport", "flat")
        self.comment_btn.setIcon(_comment_icon())
        self.comment_btn.setToolTip("查看评论")
        self.comment_btn.clicked.connect(self._open_comments)
        comment_policy = self.comment_btn.sizePolicy()
        comment_policy.setRetainSizeWhenHidden(True)
        self.comment_btn.setSizePolicy(comment_policy)
        icon_row.addWidget(self.comment_btn)
        self._apply_comment_count()

        # 不感兴趣：只在漫游时出现，心形加一道斜杠。点开由服务端给菜单
        # （推荐重复 / 推荐不感兴趣 / 屏蔽歌手），和网页播放器同一位置。
        self.dislike_btn = ghost_button("", compact=True)
        self.dislike_btn.setProperty("transport", "flat")
        self.dislike_btn.setFixedSize(32, 32)
        self.dislike_btn.setIconSize(QSize(26, 26))
        self.dislike_btn.setIcon(_dislike_icon())
        self.dislike_btn.setToolTip("不感兴趣")
        self.dislike_btn.clicked.connect(self._dislike)
        # 不保留隐藏占位：它只在漫游里出现，普通歌单也占 32px 的话，评论
        # 和上一首之间会空出一大块。进出漫游时让播放键挪一下可以接受。
        icon_row.addWidget(self.dislike_btn)
        layout.addLayout(icon_row)
        layout.addSpacing(2)

        # 三个都是扁平的小图标，播放键只比上一首/下一首略大一点
        self.prev_btn = self._transport_button("prev", size=32, icon_size=26)
        self.prev_btn.clicked.connect(self.playback.previous)
        self.play_btn = self._transport_button("play", size=34, icon_size=26)
        self.play_btn.clicked.connect(self.playback.toggle)
        self.next_btn = self._transport_button("next", size=32, icon_size=26)
        self.next_btn.clicked.connect(lambda: self.playback.next(manual=True))
        transport_row = QHBoxLayout()
        transport_row.setContentsMargins(0, 0, 0, 0)
        transport_row.setSpacing(2)
        transport_row.addWidget(self.prev_btn)
        transport_row.addWidget(self.play_btn)
        transport_row.addWidget(self.next_btn)
        layout.addLayout(transport_row)
        layout.addSpacing(4)

        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("PlaybackTime")
        # 「正在缓冲…」比时间串短，固定宽度就不会带着进度条一起动
        self.time_label.setFixedWidth(96)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.time_label)

        self.progress = SeekSlider(Qt.Orientation.Horizontal)
        self.progress.setObjectName("PlaybackProgress")
        self.progress.setRange(0, 1000)
        self.progress.setEnabled(False)
        self.progress.sliderPressed.connect(self._on_scrub_start)
        self.progress.sliderReleased.connect(self._on_scrub_end)
        self.progress.sliderMoved.connect(self._on_scrub_move)
        self.progress.clickedValue.connect(self._seek_to_value)
        layout.addWidget(self.progress, 1)

        self.mode_btn = self._icon_button(
            self.playback.mode,
            f"播放顺序：{mode_label(self.playback.mode)}（点击切换）",
        )
        self.mode_btn.clicked.connect(self.playback.cycle_mode)
        layout.addWidget(self.mode_btn)

        # 音量收成一个喇叭按钮，点开才显示竖直滑条 —— 常驻的横条太占地方
        self.volume_popup = VolumePopup(self)
        self.volume_popup.slider.valueChanged.connect(self.playback.set_volume)

        self.volume_btn = ghost_button("", compact=True)
        self.volume_btn.setProperty("transport", "flat")
        self.volume_btn.setFixedSize(34, 32)
        self.volume_btn.setIconSize(QSize(26, 26))
        self.volume_btn.clicked.connect(lambda: self._toggle_volume_popup())
        layout.addWidget(self.volume_btn)

        self.queue_btn = self._icon_button("queue", "播放列表")
        self.queue_btn.clicked.connect(self.queueRequested.emit)
        layout.addWidget(self.queue_btn)

    def _transport_button(
        self, kind: str, size: int, icon_size: int = 23
    ) -> QPushButton:
        button = ghost_button("", compact=True)
        button.setProperty("transport", "flat")
        button.setFixedSize(size, size)
        button.setIconSize(QSize(icon_size, icon_size))
        button.setIcon(_transport_icon(kind))
        return button

    def _icon_button(self, kind: str, tip: str) -> QPushButton:
        """无边框的自绘图标按钮，悬停时才浮出底色。"""
        button = ghost_button("", compact=True)
        button.setProperty("transport", "flat")
        button.setFixedSize(34, 32)
        button.setIconSize(QSize(26, 26))
        button.setIcon(_control_icon(kind))
        button.setToolTip(tip)
        return button

    def _connect(self) -> None:
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.volumeChanged.connect(self._on_volume_changed)
        self.playback.modeChanged.connect(self._on_mode_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)
        self.window.actions.likedChanged.connect(self._sync_like)
        self.window.loginChanged.connect(lambda _logged_in: self._sync_like())
        if self.covers is not None:
            self.covers.coverReady.connect(self._on_cover_ready)

    def theme_changed(self) -> None:
        """按新的 ``theme.TEXT`` 重画所有自绘图标。

        这些 pixmap 是构造时画好、之后一直复用的；不重画就会带着旧主题的
        颜色留在界面上 —— 暗色启动切到亮色时，上一首/下一首/播放顺序/播放
        列表全是近白色，落在浅底上几乎看不见。播放/暂停和音量因为状态一变
        就重画，反而没这个问题，所以故障看起来只出在某几个按钮上。
        """
        self.now_playing.theme_changed()
        self.prev_btn.setIcon(_transport_icon("prev"))
        playing = self.playback.state == STATE_PLAYING
        self.play_btn.setIcon(_transport_icon("pause" if playing else "play"))
        self.next_btn.setIcon(_transport_icon("next"))
        self.mode_btn.setIcon(_control_icon(self.playback.mode))
        self.volume_btn.setIcon(QIcon(_speaker_pixmap(self.playback.volume)))
        self.queue_btn.setIcon(_control_icon("queue"))
        self._apply_comment_count()
        self._sync_like()

    # ---------------------------------------------------------------- 状态

    def _on_track_changed(self, song) -> None:
        self._song = song
        self.now_playing.set_song(song)
        self._sync_like()
        self._load_comment_count(song)

        cached = None
        if song is not None and self.covers is not None:
            cached = self.covers.get(getattr(song, "id", None))
        self.now_playing.set_cover(cached)
        if song is not None and cached is None and self.covers is not None:
            # 和播放页共用同一个缓存/去重，不会下两次
            self.covers.request(song)

        self.progress.setEnabled(
            song is not None and self.playback.duration > 0
        )
        self._on_state_changed(self.playback.state)

    @staticmethod
    def _is_netease_song(song) -> bool:
        """只有网易云在线歌曲有「红心」；本地与流媒体歌曲没有这个概念。"""
        if song is None or getattr(song, "source", ""):
            return False
        return int(getattr(song, "id", 0)) > 0

    def _sync_like(self) -> None:
        song = self._song
        if not self._is_netease_song(song):
            liked = False
            visible = enabled = False
            tip = "喜欢"
        elif not self.window.is_logged_in():
            liked = False
            visible, enabled = True, False
            tip = "登录网易云后可喜欢"
        else:
            liked = self.window.actions.is_liked(song)
            visible = enabled = True
            tip = "取消喜欢" if liked else "喜欢"
        self.like_btn.setVisible(visible)
        self.like_btn.setEnabled(enabled)
        self.like_btn.setIcon(_heart_icon(liked, enabled))
        self.like_btn.setToolTip(tip)

        if hasattr(self, "collect_btn"):
            can_collect = self._is_netease_song(song)
            self.collect_btn.setVisible(can_collect)
            self.collect_btn.setEnabled(can_collect
                                        and self.window.is_logged_in())
            self.collect_btn.setToolTip(
                "收藏到歌单" if self.window.is_logged_in()
                else "登录后可收藏到歌单")

        if hasattr(self, "comment_btn"):
            # 看评论不需要登录，公开接口就能读
            can_comment = self._is_netease_song(song)
            self.comment_btn.setVisible(can_comment)
            self.comment_btn.setEnabled(can_comment)

        self._sync_dislike()

    def _sync_dislike(self) -> None:
        """「不感兴趣」只属于漫游：当前歌不在漫游队列里就隐藏。"""
        if not hasattr(self, "dislike_btn"):
            return
        roam = getattr(self.window, "pages", {}).get("roam")
        is_roam = getattr(roam, "is_current_song", None)
        in_roam = bool(
            self._is_netease_song(self._song)
            and callable(is_roam) and is_roam(self._song))
        logged_in = self.window.is_logged_in()
        self.dislike_btn.setVisible(in_roam)
        self.dislike_btn.setEnabled(in_roam and logged_in)
        self.dislike_btn.setToolTip(
            "不感兴趣" if logged_in else "登录网易云后可不感兴趣")

    def _dislike(self) -> None:
        if self._song is not None and self.dislike_btn.isEnabled():
            self.window.actions.dislike(self._song, self.dislike_btn)

    def _toggle_like(self) -> None:
        if self._is_netease_song(self._song):
            self.window.actions.toggle_like(self._song)

    def _collect(self) -> None:
        if self._is_netease_song(self._song):
            self.window.actions.add_to_playlist(self._song)

    def _open_comments(self) -> None:
        if self._is_netease_song(self._song):
            self.window.actions.comments(self._song)

    def _load_comment_count(self, song) -> None:
        """取当前歌曲的评论数，显示在评论图标右侧。

        网页播放器每次切歌都会请求一次评论计数；这里同样异步做，失败就
        不显示数字，不打扰播放。
        """
        self._comment_song_id = 0
        self._comment_count = 0
        self._apply_comment_count()
        if not self._is_netease_song(song):
            return
        song_id = int(song.id)
        self._comment_song_id = song_id
        client = self.window.client
        if self._comment_task is not None and self._comment_task.is_running:
            self._comment_task.cancel()

        def work():
            _comments, total, _more = client.comments(song_id, page=1, limit=1)
            return total

        task = BackgroundTask(work, parent=self)
        self._comment_task = task

        def done(total) -> None:
            if self._comment_song_id != song_id:
                return
            self._comment_count = int(total or 0)
            self._apply_comment_count()

        task.done.connect(done)
        task.failed.connect(lambda _message: None)
        task.start()

    def _apply_comment_count(self) -> None:
        """把评论数画进评论按钮；没有数字时只显示气泡。"""
        text = _comment_count_text(self._comment_count) if self._comment_count else ""
        size = _comment_button_size(text)
        self.comment_btn.setIconSize(size)
        # 左右各留 4px，悬停底色不会贴着气泡
        self.comment_btn.setFixedSize(size.width() + 8, max(30, size.height() + 2))
        self.comment_btn.setIcon(_comment_icon(text))
        self.comment_btn.setToolTip(
            f"查看评论（{self._comment_count} 条）"
            if self._comment_count else "查看评论")

    def _on_cover_ready(self, song_id: int, pixmap: QPixmap) -> None:
        # 切歌很快时，上一首的封面可能后到
        if self._song is None or int(getattr(self._song, "id", 0)) != int(song_id):
            return
        self.now_playing.set_cover(pixmap)

    def _on_state_changed(self, state: str) -> None:
        playing = state == STATE_PLAYING
        self.play_btn.setIcon(
            _transport_icon("pause" if playing else "play")
        )
        self.progress.setEnabled(
            self._song is not None and self.playback.duration > 0
            and state not in (STATE_IDLE, STATE_ERROR)
        )
        if state == STATE_LOADING:
            self.time_label.setText("正在缓冲…")
        elif state == STATE_ERROR:
            self.time_label.setText("播放失败")
        elif state == STATE_IDLE and self._song is None:
            self.time_label.setText("--:-- / --:--")
        self.play_btn.setEnabled(self._song is not None or bool(self.playback.queue))
        if state in (STATE_PAUSED, STATE_PLAYING) and self.playback.duration > 0:
            self._on_position_changed(self.playback.position, self.playback.duration)

    def _on_position_changed(self, position: float, duration: float) -> None:
        if duration > 0 and not self.progress.isEnabled():
            # 本地文件一开始没有时长信息，mpv 报上来之后才能拖动
            self.progress.setEnabled(True)
        if self._scrubbing:
            return
        if duration > 0:
            self.progress.setValue(player.progress_value(position, duration))
            self.time_label.setText(
                f"{player.format_clock(position)} / {player.format_clock(duration)}"
            )
        elif self._song is None:
            self.progress.setValue(0)
            self.time_label.setText("--:-- / --:--")
        else:
            self.progress.setValue(0)
            self.time_label.setText(f"{player.format_clock(position)} / --:--")

    def _on_volume_changed(self, value: int) -> None:
        value = player.clamp_volume(value, self.playback.volume)
        self.volume_btn.setIcon(QIcon(_speaker_pixmap(value)))
        self.volume_btn.setToolTip(f"音量 {value}%（点击调节）")
        # 面板里的滑条跟着外部音量走，但别再回发一次 set_volume
        self.volume_popup.slider.blockSignals(True)
        self.volume_popup.slider.setValue(value)
        self.volume_popup.slider.blockSignals(False)
        self.volume_popup.value_label.setText(f"{value}%")

    def _toggle_volume_popup(self) -> None:
        if self.volume_popup.isVisible():
            self.volume_popup.hide()
        else:
            self.volume_popup.show_above(self.volume_btn)

    def _on_mode_changed(self, mode: str) -> None:
        label = mode_label(mode)
        self.mode_btn.setIcon(_control_icon(mode))
        self.mode_btn.setToolTip(f"播放顺序：{label}（点击切换）")

    def _on_queue_changed(self) -> None:
        has_queue = bool(self.playback.queue)
        self.prev_btn.setEnabled(has_queue)
        self.next_btn.setEnabled(has_queue)
        self.play_btn.setEnabled(self._song is not None or has_queue)
        # 漫游队列换了（补货 / 不感兴趣）后按钮可能该出现或消失
        self._sync_dislike()

    # ---------------------------------------------------------------- 演示

    def set_demo(
        self,
        song,
        *,
        position: float = 0.0,
        duration: float = 0.0,
        volume: int = 70,
        mode: str = "loop",
    ) -> None:
        """截图用：不经过播放控制器，把播放条摆成正在播放的样子。"""
        self._song = song
        self.now_playing.set_song(song)
        self.now_playing.set_cover(None)
        self._on_state_changed(STATE_PLAYING)
        self.progress.setEnabled(duration > 0)
        if duration > 0:
            self.progress.setValue(player.progress_value(position, duration))
            self.time_label.setText(
                f"{player.format_clock(position)} / {player.format_clock(duration)}"
            )
        self._on_volume_changed(volume)
        self._on_mode_changed(mode)
        self._sync_like()
        self.prev_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self.play_btn.setEnabled(True)

    # ---------------------------------------------------------------- 拖动

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_move(self, value: int) -> None:
        duration = self.playback.duration
        if duration > 0:
            seconds = player.seek_seconds(value, duration)
            self.time_label.setText(
                f"{player.format_clock(seconds)} / {player.format_clock(duration)}"
            )

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        self._seek_to_value(self.progress.value())

    def _seek_to_value(self, value: int) -> None:
        duration = self.playback.duration
        if duration > 0:
            self.playback.seek(player.seek_seconds(value, duration))
