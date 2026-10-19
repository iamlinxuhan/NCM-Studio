"""收藏页：收藏的歌单、专辑、歌手。

三个列表共用同一套封面网格。取消收藏只做一次后台写请求，成功后才从当前列表
移除；失败保留原样并提示原因。
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..widgets.card import ghost_button
from ..widgets.covercard import CoverGrid
from ..workers import BackgroundTask
from .base import BasePage


class FavoritesPage(BasePage):
    PAGE_TITLE = "收藏"
    PAGE_SUBTITLE = "收藏的歌单、专辑和关注的歌手。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._loaded = False
        self._playlists = []
        self._albums = []
        self._artists = []
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.status = QLabel("登录后加载收藏。")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status, 1)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(self._reload)
        toolbar.addWidget(self.refresh_button)
        self.content.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.playlist_grid = self._add_tab("收藏歌单")
        self.album_grid = self._add_tab("收藏专辑")
        self.artist_grid = self._add_tab("收藏歌手", round_cover=True)
        self.content.addWidget(self.tabs, 1)

        self.playlist_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("playlist", int(payload))
        )
        self.album_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("album", int(payload))
        )
        self.artist_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("artist", int(payload))
        )
        self.playlist_grid.itemContextRequested.connect(
            lambda payload, pos: self._card_menu("playlist", int(payload), pos)
        )
        self.album_grid.itemContextRequested.connect(
            lambda payload, pos: self._card_menu("album", int(payload), pos)
        )
        self.artist_grid.itemContextRequested.connect(
            lambda payload, pos: self._card_menu("artist", int(payload), pos)
        )

    def _add_tab(self, title: str, round_cover: bool = False) -> CoverGrid:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 8, 0, 0)
        grid = CoverGrid(self.window.image_store, round_cover=round_cover)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(grid)
        layout.addWidget(scroll)
        self.tabs.addTab(widget, title)
        return grid

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        if not self.window.is_logged_in():
            self.status.setText("登录后加载收藏。")
            return
        if not self._loaded:
            self._reload()

    def _reload(self) -> None:
        if not self.window.is_logged_in():
            self.status.setText("登录后加载收藏。")
            return
        if self._task is not None and self._task.is_running:
            return
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载收藏…")
        client = self.window.client

        def work():
            playlists = [
                item for item in client.user_playlists()
                if item.special_type != 5
            ]
            return {
                "playlists": playlists,
                "albums": client.album_sublist(limit=200),
                "artists": client.artist_sublist(limit=200),
            }

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, data) -> None:
        self.refresh_button.setEnabled(True)
        self._playlists = list(data.get("playlists") or [])
        self._albums = list(data.get("albums") or [])
        self._artists = list(data.get("artists") or [])
        self._loaded = True

        self.playlist_grid.set_items([
            {
                "key": f"playlist:{item.id}",
                "title": item.name,
                "subtitle": item.subtitle,
                "payload": item.id,
                "url": item.cover_url,
            }
            for item in self._playlists
        ])
        self.album_grid.set_items([
            {
                "key": f"album:{item.id}",
                "title": item.name,
                "subtitle": item.artist_text,
                "payload": item.id,
                "url": item.pic_url,
            }
            for item in self._albums
        ])
        self.artist_grid.set_items([
            {
                "key": f"artist:{item.id}",
                "title": item.name,
                "subtitle": item.subtitle,
                "payload": item.id,
                "url": item.pic_url,
            }
            for item in self._artists
        ])

        self.tabs.setTabText(0, f"收藏歌单（{len(self._playlists)}）")
        self.tabs.setTabText(1, f"收藏专辑（{len(self._albums)}）")
        self.tabs.setTabText(2, f"收藏歌手（{len(self._artists)}）")
        self.status.setText("")

    def _on_failed(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    # ---------------------------------------------------------------- 取消收藏

    def _card_menu(self, kind: str, item_id: int, position) -> None:
        menu = QMenu(self)
        labels = {"playlist": "歌单", "album": "专辑", "artist": "歌手"}
        menu.addAction(f"取消收藏这个{labels.get(kind, '内容')}",
                       lambda: self._unsubscribe(kind, item_id))
        menu.exec(position)

    def _unsubscribe(self, kind: str, item_id: int) -> None:
        if not self.window.require_login_silent("取消收藏"):
            return
        client = self.window.client

        def work():
            if kind == "playlist":
                client.subscribe_playlist(item_id, False)
            elif kind == "album":
                client.subscribe_album(item_id, False)
            else:
                client.subscribe_artist(item_id, False)
            return kind

        task = BackgroundTask(work, parent=self)

        def done(result: str) -> None:
            if result == "playlist":
                self._playlists = [item for item in self._playlists
                                   if item.id != item_id]
            elif result == "album":
                self._albums = [item for item in self._albums if item.id != item_id]
            else:
                self._artists = [item for item in self._artists
                                 if item.id != item_id]
            self._on_loaded({
                "playlists": self._playlists,
                "albums": self._albums,
                "artists": self._artists,
            })
            self.window.toast("已取消收藏")

        task.done.connect(done)
        task.failed.connect(lambda message: self.window.toast(f"操作失败：{message}"))
        task.start()

    # ---------------------------------------------------------------- 登录

    def on_login_changed(self, logged_in: bool) -> None:
        self._loaded = False
        self._playlists = []
        self._albums = []
        self._artists = []
        for grid in (self.playlist_grid, self.album_grid, self.artist_grid):
            grid.clear()
        self.tabs.setTabText(0, "收藏歌单")
        self.tabs.setTabText(1, "收藏专辑")
        self.tabs.setTabText(2, "收藏歌手")
        if logged_in:
            self._reload()
        else:
            self.status.setText("登录后加载收藏。")
