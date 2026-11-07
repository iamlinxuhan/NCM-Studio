"""漫游：网页端「私人 FM / 漫游」的沉浸式连续播放。

漫游和普通歌单的区别在于队列没有尽头：快放完时要自动向服务端再要一批。
「不感兴趣」放在底部播放条（心形加斜杠，和网页播放器一致），这里负责
在选中理由后把歌从队列里拿掉并继续补货，让待播始终维持两三首。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel

from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

#: 一次向服务端要几首。网页端固定给 3 首左右，多要几次凑一批更稳
BATCH = 3
#: 队列里少于这么多待播就补货
LOW_WATER = 3


class RoamPage(BasePage):
    PAGE_TITLE = "漫游"
    PAGE_SUBTITLE = "根据你的口味连续推荐；不感兴趣的歌不会再出现。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._songs: list[Song] = []
        #: 当前播放队列是不是本页开的。用户从别的页面点歌后自动退出漫游
        self._active = False
        self._starting = False
        #: 下一次拿到歌时替换整个队列（刚点「开始漫游」），而不是追加
        self._replace_queue = False
        #: 「不感兴趣」过的歌，避免补货时又被推回来
        self._banned: set[int] = set()
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.status = QLabel("点「开始漫游」，网易云会按你的口味连续推歌。")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status, 1)

        self.start_button = accent_button("开始漫游")
        self.start_button.clicked.connect(lambda: self._start())
        toolbar.addWidget(self.start_button)

        self.like_button = ghost_button("喜欢", compact=True)
        self.like_button.clicked.connect(self._like_current)
        self.like_button.setEnabled(False)
        toolbar.addWidget(self.like_button)

        self.more_button = ghost_button("换一批", compact=True)
        self.more_button.clicked.connect(lambda: self._fetch(more=True))
        self.more_button.setEnabled(False)
        toolbar.addWidget(self.more_button)
        self.content.addLayout(toolbar)

        self.model = SongTableModel(self, extra_title="漫游")
        self.table = SongTableView()
        self.table.setModel(self.model)
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._show_menu)
        self.content.addWidget(self.table, 1)

        self.playback = self.window.playback
        self.actions = self.window.actions
        self.playback.trackEnded.connect(self._on_track_ended)
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)
        self.playback.stateChanged.connect(lambda _state: self._sync_buttons())

    # ---------------------------------------------------------------- 数据

    def on_show(self) -> None:
        self._sync_buttons()
        self._sync_table()
        # 网页端点「漫游」就直接开播、跳到唱片界面。这里同样不再要求
        # 二次点击；已经在漫游中（或正在取歌）时只显示队列。
        if not self._active and not self._starting:
            if self.window.is_logged_in():
                self._start()

    def _start(self) -> None:
        if not self.window.require_login_silent("漫游"):
            return
        self._active = True
        self._starting = True
        self._replace_queue = True
        self.status.setText("正在生成漫游队列…")
        self._fetch(more=False)

    def _fetch(self, *, more: bool) -> None:
        if self._task is not None and self._task.is_running:
            return
        client = self.window.client
        banned = set(self._banned)
        count = LOW_WATER if more else 3

        def work():
            songs: list[Song] = []
            seen: set[int] = set()
            # 每次只回 3 首；多要几次凑一批，顺手过滤重复和不感兴趣的
            for _ in range(count):
                for song in client.personal_fm():
                    if int(song.id) in banned or int(song.id) in seen:
                        continue
                    seen.add(int(song.id))
                    songs.append(song)
            return songs

        task = BackgroundTask(work, parent=self)
        self._task = task

        def done(songs) -> None:
            self._starting = False
            self.more_button.setEnabled(bool(self._songs))
            if not songs:
                self.status.setText("没拿到新的推荐，稍后再试。")
                return
            self._songs.extend(songs)
            self.model.set_songs(self._songs)
            if self._replace_queue:
                # 刚点「开始漫游」：旧队列（可能是别的页面留下的）整个换掉
                self._replace_queue = False
                self.actions.play_all(self._songs)
            elif self._active:
                self.playback.append(songs)
            self.status.setText(
                f"漫游中 · 队列 {len(self.playback.queue)} 首"
                if self._active else "已准备就绪。")

        def failed(message: str) -> None:
            self._starting = False
            self.status.setText(f"漫游暂时不可用：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 播放

    def _play_song(self, song: Song) -> None:
        self.actions.play(song, self._songs)

    def _show_menu(self, song: Song, position) -> None:
        self.actions.show_menu(self.table, song, self._songs, position)

    def _like_current(self) -> None:
        song = self.playback.current
        if song is not None and self._is_roam_song(song):
            self.actions.toggle_like(song)

    def discard_current(self, song) -> None:
        """「不感兴趣」选中理由后调用：从漫游队列拿掉并续播下一首。

        接口上报由 :class:`~ncmgui.actions.SongActions` 在后台做；这里只
        管界面，保证用户点完立刻听到下一首。
        """
        if song is None or not self._is_roam_song(song):
            return
        self._banned.add(int(song.id))

        index = self.playback.index
        self._songs = [item for item in self._songs if int(item.id) != int(song.id)]
        self.model.set_songs(self._songs)
        current = self.playback.current
        # 切歌很快时底栏可能还拿着上一首：别把正在放的那首从队列里删掉
        matches_current = (current is not None
                           and int(current.id) == int(song.id))
        if (matches_current and index is not None
                and index < len(self.playback.queue)):
            self.playback.remove_at(index)
            if self.playback.queue:
                self.playback.play_at(min(index, len(self.playback.queue) - 1))
        self._ensure_fill()
        self._sync_buttons()

    def _on_track_ended(self, _song) -> None:
        if self._active:
            self._ensure_fill()

    def _on_track_changed(self, _song) -> None:
        self._sync_buttons()
        self._sync_table()

    def _on_queue_changed(self) -> None:
        if not self._active:
            return
        # 用户从别的页面开了新队列：漫游自动结束，避免把别人的队列
        # 当成自己的继续补货
        current = self.playback.current
        if current is not None and not self._is_roam_song(current):
            self._active = False
            self.status.setText("漫游已暂停：当前播放的不是漫游队列。")
        self._sync_table()

    # ---------------------------------------------------------------- 队列

    def _ensure_fill(self) -> None:
        if not self._active:
            return
        queue = self.playback.queue
        index = self.playback.index if self.playback.index is not None else 0
        if len(queue) - index <= LOW_WATER:
            self._fetch(more=True)

    def _is_roam_song(self, song) -> bool:
        return any(int(item.id) == int(song.id) for item in self._songs)

    def is_current_song(self, song) -> bool:
        """底栏用它决定要不要显示「不感兴趣」。

        必须仍在漫游中：从别的页面点到一首曾经在漫游队列里的歌，漫游已经
        结束，不应该再给出「不感兴趣」。
        """
        return (self._active and song is not None
                and self._is_roam_song(song))

    def _sync_table(self) -> None:
        if not self._active:
            return
        current = self.playback.current
        self.model.set_playing(current.id if current else None)
        self.model.set_songs(self._songs)

    def _sync_buttons(self) -> None:
        song = self.playback.current
        on = song is not None and self._is_roam_song(song)
        self.like_button.setEnabled(on)
        self.more_button.setEnabled(bool(self._songs))
        self.status.setText(
            f"漫游中 · 队列 {len(self.playback.queue)} 首"
            if self._active and on else self.status.text())

    def shutdown(self) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
