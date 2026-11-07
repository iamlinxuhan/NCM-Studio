"""桌面歌词窗。

无边框、置顶、不抢焦点。Wayland（尤其 GNOME/Mutter）不保证
``WindowStaysOnTopHint`` 生效，因此设置里明确标成「可能不置顶」，而不是
假装所有合成器都支持。

**默认是一层淡灰色半透明底**（不是全透明，也不是黑框）：压在白壁纸或深色
代码窗口上都能一眼找到歌词层，又不刺眼。想要完全盖住背景时用右键菜单打开
「不透明背景」。

逐字效果与播放页一致：``QTextLayout`` 之外用一条横向 alpha 渐变做遮罩，
幕布边缘落在字内部时也是连续渐变；普通 LRC 没有字时间时用
:func:`ncmgui.lyrics.words_for` 均摊出伪逐字，动画不会因为换了歌单就消失。

位置、尺寸、字号、锁定状态写进 ``player.desktop_lyrics``。锁定只禁止拖动，
不做点击穿透 —— 真正的穿透需要 layer-shell，Qt 跨平台 API 给不了；与其让
窗口变成一块点不动的区域，不如保留右键菜单可以解锁。右上角常驻一个关闭按钮。
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QMenu, QWidget

from .. import lyrics, theme
from ..lyrics_service import song_key
from ..playback import STATE_PLAYING

#: 幕布推进间隔。和播放页的逐字定时器一致
TICK_MS = 33

#: 桌面歌词的配色**不跟着应用主题走**：背景是透明/壁纸，浅色主题的 TEXT
#: 是深灰，压上去几乎看不见。固定成“白字 + 青色幕布 + 黑色描边”在哪都清楚。
UNPLAYED_COLOR = QColor(255, 255, 255, 214)
PLAYED_COLOR = QColor(53, 214, 255)
OUTLINE_COLOR = QColor(0, 0, 0, 190)
TRANSLATION_COLOR = QColor(255, 255, 255, 170)
CLOSE_COLOR = QColor(255, 255, 255, 170)
CLOSE_HOVER_COLOR = QColor(255, 255, 255, 235)

#: 幕布软边宽度：字号倍数，和播放页 LyricLineWidget 保持一致
CURTAIN_BAND = 0.8


class DesktopLyricsWindow(QWidget):
    closed = pyqtSignal()

    def __init__(self, window) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.window = window
        self.cfg = window.cfg
        self.playback = window.playback
        self.service = window.lyrics
        self.setObjectName("DesktopLyrics")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 86)

        options = self.cfg.get("player.desktop_lyrics", {})
        if not isinstance(options, dict):
            options = {}
        self._locked = bool(options.get("locked", False))
        self._opaque = bool(options.get("opaque", False))
        self._auto_start = bool(options.get("auto_start", False))
        self._translation = bool(options.get("translation", True))
        try:
            self._font_size = max(14, min(64, int(options.get("font_size", 26))))
        except (TypeError, ValueError):
            self._font_size = 26
        try:
            width = max(360, int(options.get("width", 760)))
            height = max(86, int(options.get("height", 130)))
        except (TypeError, ValueError):
            width, height = 760, 130
        self.resize(width, height)
        self._restore_position(options)

        self._lines: list[lyrics.TimedLine] = []
        self._placeholder = "桌面歌词"
        self._index = -1
        self._position = 0.0
        self._anchor: tuple[float, float] | None = None
        self._drag_offset: QPoint | None = None
        self._drag_moved = False
        self._close_hover = False
        self._close_pressed = False

        self._ticker = QTimer(self)
        self._ticker.setInterval(TICK_MS)
        self._ticker.timeout.connect(self._tick)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._persist)

        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.positionChanged.connect(self._on_position)
        self.playback.stateChanged.connect(self._on_state)
        self.service.lyricsChanged.connect(self._on_lyrics)
        self.service.offsetChanged.connect(lambda _ms: self._reposition())

        self._on_track_changed(self.playback.current)
        self._on_state(self.playback.state)

    # ------------------------------------------------------------ 显示控制

    def open_lyrics(self) -> None:
        self.cfg.set("player.desktop_lyrics.enabled", True)
        self.show()
        self.raise_()
        self._on_track_changed(self.playback.current)
        self._update_ticker()
        self._schedule_persist()

    def hide_lyrics(self) -> None:
        self.cfg.set("player.desktop_lyrics.enabled", False)
        self._persist()
        self.hide()
        self._ticker.stop()
        self.closed.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_lyrics()
        else:
            self.open_lyrics()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._persist()
        self.closed.emit()
        super().closeEvent(event)

    def _restore_position(self, options: dict) -> None:
        try:
            x = int(options.get("x", -1))
            y = int(options.get("y", -1))
        except (TypeError, ValueError):
            x = y = -1
        screen = QGuiApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen is not None else None
        if x < 0 or y < 0:
            if geometry is not None:
                self.move(
                    geometry.x() + (geometry.width() - self.width()) // 2,
                    geometry.y() + geometry.height() - self.height() - 90,
                )
            return
        if geometry is not None and not geometry.contains(QPoint(x, y)):
            # 换了显示器/分辨率后不要把窗口留在屏幕外
            self.move(
                geometry.x() + (geometry.width() - self.width()) // 2,
                geometry.y() + geometry.height() - self.height() - 90,
            )
            return
        self.move(x, y)

    def _persist(self) -> None:
        options = {
            "enabled": self.isVisible(),
            "auto_start": self._auto_start,
            "locked": self._locked,
            "x": self.x(),
            "y": self.y(),
            "width": self.width(),
            "height": self.height(),
            "font_size": self._font_size,
            "translation": self._translation,
            "opaque": self._opaque,
        }
        self.cfg.set("player.desktop_lyrics", options)
        try:
            self.cfg.save()
        except (OSError, ValueError):
            pass

    def _schedule_persist(self) -> None:
        self._save_timer.start()

    # ------------------------------------------------------------ 播放事件

    def _on_track_changed(self, song) -> None:
        self._lines = []
        self._index = -1
        self._anchor = None
        self._position = 0.0
        if song is not None:
            self.service.request(song)
        self.update()
        self._update_ticker()

    def _on_lyrics(self, song, parsed, placeholder: str) -> None:
        current = self.playback.current
        if song_key(song) != song_key(current):
            return
        # 普通 LRC 只有行级时间。这里也补一份均摊的“伪逐字”，否则
        # lyrics.curtain_char() 因为没有 words 直接返回 0，幕布永远不动，
        # 表现就是“根本没有逐字歌词”。
        self._lines = [
            line if line.words else replace(line, words=lyrics.words_for(line))
            for line in (parsed.lines if parsed is not None else [])
        ]
        self._placeholder = placeholder or (current.name if current is not None else "桌面歌词")
        self._reposition()
        self.update()

    def _on_position(self, position: float, _duration: float) -> None:
        try:
            value = float(position)
        except (TypeError, ValueError):
            return
        display = self.service.display_position(value)
        self._position = display
        self._anchor = (display, self._monotonic())
        self._reposition()

    def _on_state(self, state: str) -> None:
        if state != STATE_PLAYING:
            self._anchor = None
        self._update_ticker()

    def _update_ticker(self) -> None:
        if self.isVisible() and self.playback.state == STATE_PLAYING and self._lines:
            if not self._ticker.isActive():
                self._ticker.start()
        else:
            self._ticker.stop()

    def _tick(self) -> None:
        if self._anchor is None:
            return
        anchor, stamp = self._anchor
        self._position = anchor + (self._monotonic() - stamp)
        self._reposition()

    @staticmethod
    def _monotonic() -> float:
        import time

        return time.monotonic()

    def _reposition(self) -> None:
        if not self._lines:
            self.update()
            return
        index = lyrics.line_index_at(self._lines, self._position)
        if index != self._index:
            self._index = index
        self.update()

    # ------------------------------------------------------------ 绘制

    def current_line(self) -> lyrics.TimedLine | None:
        if 0 <= self._index < len(self._lines):
            return self._lines[self._index]
        return None

    def _close_rect(self) -> QRectF:
        return QRectF(self.width() - 32.0, 6.0, 26.0, 26.0)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        # 默认是**淡灰色半透明底**，不是全透明：有一层很淡的浅灰才看得出
        # 歌词层的范围，压在白壁纸或深色代码窗口上都不刺眼。
        # 想要完全盖住背景时用右键菜单打开「不透明背景」。
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        if self._opaque:
            background = QColor(theme.PANEL)
            background.setAlpha(238)
            border = QColor(theme.BORDER)
        else:
            background = QColor(214, 219, 228, 96)
            border = QColor(255, 255, 255, 52)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 14.0, 14.0)

        line = self.current_line()
        text = line.text if line is not None else (
            self._placeholder or "桌面歌词"
        )
        translation = line.translation if line is not None and self._translation else ""

        font = QFont(self.font())
        font.setPixelSize(self._font_size)
        font.setBold(True)
        metrics = QFontMetrics(font)
        limit = max(40, self.width() - 52)
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, limit)

        main_height = self.height() - (34 if translation else 0)
        area = QRectF(26.0, 0.0, max(20.0, self.width() - 52.0), float(main_height))
        baseline = area.y() + (
            area.height() + metrics.ascent() - metrics.descent()) / 2.0
        text_width = float(metrics.horizontalAdvance(shown))
        x = area.x() + (area.width() - text_width) / 2.0

        path = QPainterPath()
        path.addText(QPointF(x, baseline), font, shown)
        # 描边先画：中心线描边会被随后的填充盖掉一半，剩下的正好是外轮廓
        pen = QPen(OUTLINE_COLOR, max(2.0, self._font_size * 0.10))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.fillPath(path, UNPLAYED_COLOR)

        if line is not None and text:
            curtain_x = self._curtain_x(line, x, text_width)
            if curtain_x is not None:
                self._paint_curtain(painter, path, curtain_x, font)

        if translation:
            small = QFont(font)
            small.setPixelSize(max(11, int(self._font_size * 0.62)))
            small.setBold(False)
            small_metrics = QFontMetrics(small)
            shown_translation = small_metrics.elidedText(
                translation, Qt.TextElideMode.ElideRight, limit)
            translation_rect = QRectF(
                26.0, float(main_height) - 2.0, max(20.0, self.width() - 52.0), 30.0)
            translation_path = QPainterPath()
            tx = translation_rect.x() + (
                translation_rect.width() - small_metrics.horizontalAdvance(shown_translation)
            ) / 2.0
            translation_baseline = translation_rect.y() + (
                translation_rect.height() + small_metrics.ascent()
                - small_metrics.descent()) / 2.0
            translation_path.addText(
                QPointF(tx, translation_baseline), small, shown_translation)
            translation_pen = QPen(OUTLINE_COLOR, 2.0)
            translation_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(translation_pen)
            painter.drawPath(translation_path)
            painter.fillPath(translation_path, TRANSLATION_COLOR)

        self._paint_close(painter)
        painter.end()

    def _curtain_x(self, line: lyrics.TimedLine, x: float,
                   width: float) -> float | None:
        """幕布在当前行的横坐标；还没开始唱时返回 ``None``。"""
        total = max(1, len(line.text))
        try:
            char = lyrics.curtain_char(line, self._position)
        except (TypeError, ValueError):
            return None
        ratio = max(0.0, min(1.0, float(char) / total))
        if ratio <= 0.0:
            return None
        return x + width * ratio

    def _paint_curtain(self, painter: QPainter, path: QPainterPath,
                       curtain_x: float, font: QFont) -> None:
        """把“已唱”颜色的字形按一条横向 alpha 渐变叠上去。

        渐变（软边）落在幕布位置两侧，颜色变化发生在**字形内部**，这就是
        逐字歌词的渐变效果；直接 setClipRect 只能得到一条硬边。
        """
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
        layer.fillPath(path, PLAYED_COLOR)

        band = max(10.0, font.pixelSize() * CURTAIN_BAND)
        left = curtain_x - band
        right = curtain_x + band
        mask = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        mask.setDevicePixelRatio(dpr)
        mask.fill(Qt.GlobalColor.transparent)
        shade = QPainter(mask)
        shade.setPen(Qt.PenStyle.NoPen)
        gradient = QLinearGradient(left, 0.0, right, 0.0)
        gradient.setColorAt(0.0, QColor(255, 255, 255, 255))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        shade.fillRect(
            QRectF(0.0, 0.0, float(self.width()), float(self.height())), gradient)
        shade.end()

        layer.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_DestinationIn)
        layer.drawImage(0, 0, mask)
        layer.end()
        painter.drawImage(0, 0, image)

    def _paint_close(self, painter: QPainter) -> None:
        rect = self._close_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 80 if self._close_hover else 55))
        painter.drawEllipse(rect)
        font = QFont(self.font())
        font.setPixelSize(15)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(CLOSE_HOVER_COLOR if self._close_hover else CLOSE_COLOR)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "×")

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._close_rect().contains(event.position()):
                self._close_pressed = True
                event.accept()
                return
            if not self._locked:
                # Wayland 不允许客户端自己 move() 顶层窗口（合成器说了算），
                # 只能请合成器执行一次交互式移动；X11 下也同样适用。
                handle = self.windowHandle()
                if handle is not None:
                    try:
                        if handle.startSystemMove():
                            # 移动期间收不到鼠标事件，结束后补一次位置保存
                            QTimer.singleShot(800, self._schedule_persist)
                            event.accept()
                            return
                    except (AttributeError, RuntimeError):
                        pass
                # 合成器不支持时退回手动拖动（X11 / 部分桌面）
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                self._drag_moved = False
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hover = self._close_rect().contains(event.position())
        if hover != self._close_hover:
            self._close_hover = hover
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hover
                else Qt.CursorShape.ArrowCursor)
            self.update()
        if self._drag_offset is not None and not self._locked:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._drag_moved = True
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._close_pressed:
            self._close_pressed = False
            if self._close_rect().contains(event.position()):
                self.hide_lyrics()
            event.accept()
            return
        if self._drag_offset is not None:
            self._drag_offset = None
            if self._drag_moved:
                self._schedule_persist()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._close_hover:
            self._close_hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def moveEvent(self, event) -> None:  # noqa: N802
        # startSystemMove() 的位移由合成器完成，Qt 侧只会收到新的坐标；
        # 在这里排一次保存，下次启动才能回到同一个位置
        super().moveEvent(event)
        if self.isVisible():
            self._schedule_persist()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_locked(not self._locked)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        lock = menu.addAction("解锁位置" if self._locked else "锁定位置")
        menu.addSeparator()
        bigger = menu.addAction("字号增大")
        smaller = menu.addAction("字号减小")
        menu.addSeparator()
        translation = menu.addAction("显示翻译")
        translation.setCheckable(True)
        translation.setChecked(self._translation)
        opaque = menu.addAction("不透明背景")
        opaque.setCheckable(True)
        opaque.setChecked(self._opaque)
        auto_start = menu.addAction("启动时显示")
        auto_start.setCheckable(True)
        auto_start.setChecked(self._auto_start)
        menu.addSeparator()
        close = menu.addAction("关闭桌面歌词")

        chosen = menu.exec(event.globalPos())
        if chosen is lock:
            self._set_locked(not self._locked)
        elif chosen is bigger:
            self._set_font_size(self._font_size + 2)
        elif chosen is smaller:
            self._set_font_size(self._font_size - 2)
        elif chosen is translation:
            self._translation = not self._translation
            self._schedule_persist()
            self.update()
        elif chosen is opaque:
            self._opaque = not self._opaque
            self._schedule_persist()
            self.update()
        elif chosen is auto_start:
            self._auto_start = auto_start.isChecked()
            self._schedule_persist()
        elif chosen is close:
            self.hide_lyrics()

    def _set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.setToolTip("桌面歌词已锁定：右键可解锁" if self._locked else "拖动可调整位置")
        self._schedule_persist()

    def _set_font_size(self, size: int) -> None:
        self._font_size = max(14, min(64, int(size)))
        self._schedule_persist()
        self.update()

    def theme_changed(self) -> None:
        self.update()

    def shutdown(self) -> None:
        self._save_timer.stop()
        self._persist()
        self._ticker.stop()

    # ------------------------------------------------------------ 截图辅助

    def set_demo(self, lines, position: float = 0.0, placeholder: str = "") -> None:
        self._lines = [
            line if line.words else replace(line, words=lyrics.words_for(line))
            for line in lines
        ]
        self._position = position
        self._placeholder = placeholder or "桌面歌词"
        self._index = lyrics.line_index_at(self._lines, position) if self._lines else -1
        self.update()
