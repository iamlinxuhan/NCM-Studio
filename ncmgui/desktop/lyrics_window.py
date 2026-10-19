"""桌面歌词窗。

无边框、置顶、不抢焦点。Wayland（尤其 GNOME/Mutter）不保证
``WindowStaysOnTopHint`` 生效，因此设置里明确标成「可能不置顶」，而不是
假装所有合成器都支持。

位置、尺寸、字号、锁定状态写进 ``player.desktop_lyrics``。锁定只禁止拖动，
不做点击穿透 —— 真正的穿透需要 layer-shell，Qt 跨平台 API 给不了；与其让
窗口变成一块点不动的区域，不如保留右键菜单可以解锁。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import QMenu, QWidget

from .. import lyrics, theme
from ..lyrics_service import song_key
from ..playback import STATE_PLAYING

#: 幕布推进间隔。和播放页的逐字定时器一致
TICK_MS = 33


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
        self._lines = list(parsed.lines) if parsed is not None else []
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

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        dark = theme.is_dark()
        if self._opaque:
            background = QColor(theme.PANEL)
            background.setAlpha(238)
            border = QColor(theme.BORDER)
        else:
            background = QColor(8, 12, 20, 150 if dark else 205)
            border = QColor(255, 255, 255, 40 if dark else 120)
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
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(20, self.width() - 44))
        text_rect = QRectF(22, 14 if translation else 0, self.width() - 44, self.height())
        if not translation:
            text_rect.setHeight(self.height())
        else:
            text_rect.setHeight(self.height() - 32)

        ink = QColor(theme.TEXT)
        played = QColor(theme.CYAN)
        unplayed = QColor(theme.TEXT)
        unplayed.setAlpha(135)

        # 文字阴影：透明背景上只靠颜色对比，压不住复杂壁纸
        shadow = QColor(0, 0, 0, 160 if dark else 70)
        for dx, dy in ((1, 1), (-1, 1), (0, 2)):
            painter.setPen(shadow)
            painter.drawText(text_rect.translated(dx, dy), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, text)

        painter.setPen(unplayed)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            text,
        )

        if line is not None:
            fraction = self._played_fraction(line)
            if fraction > 0:
                painter.save()
                clip = QRectF(text_rect)
                clip.setWidth(text_rect.width() * fraction)
                painter.setClipRect(clip)
                painter.setPen(played)
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                    text,
                )
                painter.restore()

        if translation:
            small = QFont(font)
            small.setPixelSize(max(11, int(self._font_size * 0.62)))
            small.setBold(False)
            painter.setFont(small)
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(
                QRectF(22, text_rect.bottom() - 2, self.width() - 44, 30),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                QFontMetrics(small).elidedText(
                    translation, Qt.TextElideMode.ElideRight, max(20, self.width() - 44)
                ),
            )

    def _played_fraction(self, line: lyrics.TimedLine) -> float:
        total = max(1, len(line.text))
        try:
            char = lyrics.curtain_char(line, self._position)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, float(char) / total))

    # ------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._locked:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            self._drag_moved = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and not self._locked:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._drag_moved = True
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            if self._drag_moved:
                self._schedule_persist()
            event.accept()
            return
        super().mouseReleaseEvent(event)

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
        self._lines = list(lines)
        self._position = position
        self._placeholder = placeholder or "桌面歌词"
        self._index = lyrics.line_index_at(self._lines, position) if self._lines else -1
        self.update()
