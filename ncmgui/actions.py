"""跨页面的歌曲动作：播放、加入队列、喜欢、评论、下载、右键菜单。

页面本身不该各自实现一遍「喜欢怎么调接口」。这里拿窗口的 ``client`` 和
``playback``，统一处理后台线程、失败回滚与提示。

线程边界：所有网络请求都在 :class:`~ncmgui.workers.BackgroundTask` 里；回到
主线程后才改界面状态。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget

from .api import ApiError, AuthRequired
from .dialogs.comments import CommentsDialog
from .dialogs.playlist import AddToPlaylistDialog
from .resolver import Song
from .widgets.songmenu import show_song_menu
from .workers import BackgroundTask


class SongActions(QObject):
    """一个窗口一份。页面通过 ``window.actions`` 调用。"""

    #: 喜欢状态批量加载完成；页面可据此刷新红心图标
    likedChanged = pyqtSignal()

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.playback = window.playback
        self._liked: set[int] = set()
        self._liked_loaded = False
        self._liked_task: BackgroundTask | None = None

    # ------------------------------------------------------------ 播放

    def play(self, song: Song, queue: Sequence[Song] | None = None) -> None:
        songs = list(queue or [song])
        index = next((row for row, item in enumerate(songs) if item.id == song.id), 0)
        self.playback.set_queue(songs, index)
        self.playback.play_at(index)
        self.window.open_player_page()

    def play_all(self, songs: Sequence[Song], shuffle: bool = False) -> None:
        songs = [song for song in songs if song is not None]
        if not songs:
            return
        self.playback.set_queue(songs, 0)
        if shuffle:
            self.playback.shuffle_remaining()
        self.playback.play_at(0)
        self.window.open_player_page()

    def play_next(self, song: Song) -> None:
        self.playback.insert_next(song)
        self.window.toast(f"下一首播放：{song.display}")

    def add_queue(self, song: Song) -> None:
        self.playback.append(song)
        self.window.toast(f"已加入队列：{song.display}")

    def add_queue_many(self, songs: Sequence[Song]) -> None:
        count = self.playback.append(list(songs))
        self.window.toast(f"已加入队列：{count} 首")

    # ------------------------------------------------------------ 喜欢

    @property
    def liked_loaded(self) -> bool:
        return self._liked_loaded

    def is_liked(self, song: Song) -> bool:
        return int(song.id) in self._liked

    def clear_liked(self) -> None:
        self._liked.clear()
        self._liked_loaded = False

    def ensure_liked(self) -> None:
        """登录后加载一次红心 ID 集合。失败静默，不影响浏览。"""
        if self._liked_loaded or not self.window.is_logged_in():
            return
        if self._liked_task is not None and self._liked_task.is_running:
            return
        client = self.window.client

        def work():
            return client.liked_ids()

        task = BackgroundTask(work, parent=self)
        self._liked_task = task

        def done(ids) -> None:
            self._liked = set(ids)
            self._liked_loaded = True
            self.likedChanged.emit()

        task.done.connect(done)
        # 网络失败不提示：右键菜单会先显示「喜欢」，用户点下去时再报错
        task.failed.connect(lambda _message: None)
        task.start()

    def toggle_like(self, song: Song, currently_liked: bool | None = None) -> None:
        if not self.window.require_login_silent("喜欢歌曲"):
            return
        liked = self.is_liked(song) if currently_liked is None else bool(currently_liked)
        target = not liked

        # 乐观更新：立刻改本地状态，失败再回滚
        if target:
            self._liked.add(int(song.id))
        else:
            self._liked.discard(int(song.id))
        self.likedChanged.emit()

        client = self.window.client

        def work():
            client.like(song.id, target)
            return target

        task = BackgroundTask(work, parent=self)

        def failed(message: str) -> None:
            if target:
                self._liked.discard(int(song.id))
            else:
                self._liked.add(int(song.id))
            self.likedChanged.emit()
            self.window.toast(f"操作失败：{message}")

        def done(value: bool) -> None:
            self.window.toast(
                f"已喜欢：{song.display}" if value else f"已取消喜欢：{song.display}"
            )

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ------------------------------------------------------------ 对话框

    def comments(self, song: Song) -> None:
        CommentsDialog(self.window, song, parent=self.window).exec()

    def add_to_playlist(self, songs: Song | Sequence[Song]) -> None:
        if not self.window.require_login_silent("加入歌单"):
            return
        items = [songs] if isinstance(songs, Song) else list(songs)
        items = [song for song in items if song is not None]
        if not items:
            return
        dialog = AddToPlaylistDialog(self.window, items, parent=self.window)
        dialog.exec()

    # ------------------------------------------------------------ 下载

    def download(self, songs: Song | Sequence[Song]) -> None:
        items = [songs] if isinstance(songs, Song) else list(songs)
        targets = [str(song.id) for song in items if song is not None]
        if not targets:
            return
        page = self.window.pages.get("download")
        if page is None:
            return
        self.window.show_page("download")
        hook = getattr(page, "add_targets", None)
        if callable(hook):
            hook(targets)
        self.window.toast(f"已加入下载目标：{len(targets)} 首")

    def download_url(self, url: str) -> None:
        page = self.window.pages.get("download")
        if page is None or not url:
            return
        self.window.show_page("download")
        hook = getattr(page, "add_targets", None)
        if callable(hook):
            hook([url])

    # ------------------------------------------------------------ 右键菜单

    def show_menu(self, parent: QWidget, song: Song, queue: Sequence[Song] | None,
                  position=None, *, extra=()) -> None:
        show_song_menu(
            parent, song, queue or [song],
            play=lambda item, songs: self.play(item, songs),
            play_next=self.play_next,
            add_queue=self.add_queue,
            is_liked=self.is_liked,
            toggle_like=self.toggle_like,
            add_playlist=lambda item: self.add_to_playlist(item),
            download=lambda item: self.download(item),
            comments=self.comments,
            search=self.search,
            extra=extra,
            position=position,
        )

    def search(self, text: str) -> None:
        page = self.window.pages.get("search")
        if page is None:
            return
        self.window.show_page("search")
        hook = getattr(page, "search_for", None)
        if callable(hook):
            hook(text)
