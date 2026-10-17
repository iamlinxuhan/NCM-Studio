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


def _transport_pixmap(kind: str) -> QPixmap:
    """上一首 / 播放 / 暂停 / 下一首的图标，线条和三角都自己画。"""
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2.0, 2.0)
        color = QColor(theme.TEXT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)

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


def _control_pixmap(kind: str) -> QPixmap:
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

        color = QColor(theme.TEXT)
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
            triangle = QPainterPath()
            triangle.moveTo(3.6, 4.6)
            triangle.lineTo(3.6, 10.4)
            triangle.lineTo(8.8, 7.5)
            triangle.closeSubpath()
            painter.setBrush(color)
            painter.drawPath(triangle)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(10.2, 7.5), QPointF(17.0, 7.5))
            painter.drawLine(QPointF(3.6, 13.2), QPointF(17.0, 13.2))
            painter.drawLine(QPointF(3.6, 16.4), QPointF(13.6, 16.4))
    finally:
        painter.end()
    return pixmap


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
        self.slider.setToolTip("拖动调节音量")
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

    def set_cover(self, pixmap: QPixmap | None) -> None:
        """缩略图；``None`` 用渐变占位图。"""
        if pixmap is None or pixmap.isNull():
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

        # 三个都是扁平的小图标，播放键只比上一首/下一首略大一点
        self.prev_btn = self._transport_button("prev", "上一首", size=30)
        self.prev_btn.clicked.connect(self.playback.previous)
        self.play_btn = self._transport_button(
            "play", "播放 / 暂停", size=32, icon_size=20
        )
        self.play_btn.clicked.connect(self.playback.toggle)
        self.next_btn = self._transport_button("next", "下一首", size=30)
        self.next_btn.clicked.connect(lambda: self.playback.next(manual=True))
        layout.addWidget(self.prev_btn)
        layout.addWidget(self.play_btn)
        layout.addWidget(self.next_btn)
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
        self.volume_btn.setFixedSize(34, 30)
        self.volume_btn.setIconSize(QSize(20, 20))
        self.volume_btn.clicked.connect(lambda: self._toggle_volume_popup())
        layout.addWidget(self.volume_btn)

        self.queue_btn = self._icon_button("queue", "播放列表")
        self.queue_btn.clicked.connect(self.queueRequested.emit)
        layout.addWidget(self.queue_btn)

    def _transport_button(
        self, kind: str, tip: str, size: int, icon_size: int = 18
    ) -> QPushButton:
        button = ghost_button("", compact=True)
        button.setProperty("transport", "flat")
        button.setFixedSize(size, size)
        button.setIconSize(QSize(icon_size, icon_size))
        button.setIcon(QIcon(_transport_pixmap(kind)))
        button.setToolTip(tip)
        return button

    def _icon_button(self, kind: str, tip: str) -> QPushButton:
        """无边框的自绘图标按钮，悬停时才浮出底色。"""
        button = ghost_button("", compact=True)
        button.setProperty("transport", "flat")
        button.setFixedSize(34, 30)
        button.setIconSize(QSize(20, 20))
        button.setIcon(QIcon(_control_pixmap(kind)))
        button.setToolTip(tip)
        return button

    def _connect(self) -> None:
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.volumeChanged.connect(self._on_volume_changed)
        self.playback.modeChanged.connect(self._on_mode_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)
        if self.covers is not None:
            self.covers.coverReady.connect(self._on_cover_ready)

    # ---------------------------------------------------------------- 状态

    def _on_track_changed(self, song) -> None:
        self._song = song
        self.now_playing.set_song(song)

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

    def _on_cover_ready(self, song_id: int, pixmap: QPixmap) -> None:
        # 切歌很快时，上一首的封面可能后到
        if self._song is None or int(getattr(self._song, "id", 0)) != int(song_id):
            return
        self.now_playing.set_cover(pixmap)

    def _on_state_changed(self, state: str) -> None:
        playing = state == STATE_PLAYING
        self.play_btn.setIcon(
            QIcon(_transport_pixmap("pause" if playing else "play"))
        )
        self.play_btn.setToolTip("暂停" if playing else "播放")
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
        self.mode_btn.setIcon(QIcon(_control_pixmap(mode)))
        self.mode_btn.setToolTip(f"播放顺序：{label}（点击切换）")

    def _on_queue_changed(self) -> None:
        has_queue = bool(self.playback.queue)
        self.prev_btn.setEnabled(has_queue)
        self.next_btn.setEnabled(has_queue)
        self.play_btn.setEnabled(self._song is not None or has_queue)

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
