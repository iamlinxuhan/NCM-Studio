"""系统托盘图标。

托盘不是所有桌面环境都可用（尤其是没装 StatusNotifier/AppIndicator 的
Wayland 会话）。这里只在 ``QSystemTrayIcon.isSystemTrayAvailable()`` 为真时
构造，主窗口的「关闭到托盘」也必须先问 ``available``，否则窗口藏起来就再也
找不回来。
"""

from __future__ import annotations

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .. import APP_NAME
from ..playback import STATE_PLAYING


class TrayIcon(QSystemTrayIcon):
    def __init__(self, window) -> None:
        icon = window.windowIcon()
        if icon.isNull():
            icon = QIcon.fromTheme("audio-x-generic")
        super().__init__(icon, window)
        self.window = window
        self.playback = window.playback
        self.setToolTip(APP_NAME)

        menu = QMenu()
        self.show_action = QAction("显示主窗口", menu)
        self.show_action.triggered.connect(self._show_window)
        self.play_action = QAction("播放", menu)
        self.play_action.triggered.connect(self.playback.toggle)
        self.prev_action = QAction("上一首", menu)
        self.prev_action.triggered.connect(self.playback.previous)
        self.next_action = QAction("下一首", menu)
        self.next_action.triggered.connect(self.playback.next)
        self.lyrics_action = QAction("桌面歌词", menu)
        self.lyrics_action.setCheckable(True)
        self.lyrics_action.triggered.connect(self._toggle_lyrics)
        self.quit_action = QAction("退出", menu)
        self.quit_action.triggered.connect(self._quit)

        menu.addAction(self.show_action)
        menu.addSeparator()
        menu.addAction(self.play_action)
        menu.addAction(self.prev_action)
        menu.addAction(self.next_action)
        menu.addSeparator()
        menu.addAction(self.lyrics_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.setContextMenu(menu)
        self._menu = menu

        self.activated.connect(self._on_activated)
        self.playback.stateChanged.connect(self._sync_state)
        self.playback.trackChanged.connect(self._sync_track)
        self._sync_state(self.playback.state)
        self._sync_track(self.playback.current)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def start(self) -> bool:
        if not self.available():
            return False
        self.show()
        return True

    def _show_window(self) -> None:
        self.window.show_normal()

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible() and not self.window.isMinimized():
                self.window.hide()
            else:
                self.window.show_normal()

    def _toggle_lyrics(self, checked: bool) -> None:
        hook = getattr(self.window, "toggle_desktop_lyrics", None)
        if callable(hook):
            hook(force=bool(checked))
        else:
            self.lyrics_action.setChecked(False)

    def _quit(self) -> None:
        self.window.quit_from_tray()

    def _sync_state(self, state: str) -> None:
        self.play_action.setText("暂停" if state == STATE_PLAYING else "播放")
        tooltip = APP_NAME
        song = self.playback.current
        if song is not None:
            tooltip = f"{song.display} — {APP_NAME}"
        self.setToolTip(tooltip)

    def _sync_track(self, song) -> None:
        self._sync_state(self.playback.state)

    def sync_lyrics(self, visible: bool) -> None:
        self.lyrics_action.setChecked(bool(visible))

    def shutdown(self) -> None:
        self.hide()
