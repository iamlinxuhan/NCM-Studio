"""歌单 / 专辑 / 歌手详情页。

三种详情共用一个页面：头部信息、操作按钮、歌曲表格；歌手额外有「专辑」Tab。
歌单曲目按 500 首分页，避免几千首的收藏歌单一次把界面卡住。
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError, AuthRequired
from ..images import placeholder
from ..resolver import PLAYLIST_URL, Song
from ..widgets.card import accent_button, danger_button, ghost_button
from ..widgets.covercard import CoverGrid
from ..widgets.pager import Pager
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

PLAYLIST_PAGE_SIZE = 500


class CollectionPage(BasePage):
    """歌单/专辑/歌手详情。由 :class:`DetailHost` 创建和销毁。"""

    backRequested = pyqtSignal()
    TAB_SONGS = 0
    TAB_ALBUMS = 1

    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window, kind: str = "", item_id: int = 0) -> None:
        self.kind = kind
        self.item_id = int(item_id or 0)
        self._task: BackgroundTask | None = None
        self._generation = 0
        self._entity = None
        self._songs: list[Song] = []
        self._albums = []
        self._total = 0
        self._page = 1
        self._page_size = PLAYLIST_PAGE_SIZE
        self._has_more = False
        self._cover_key = ""
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        self.actions = self.window.actions
        self._build_header()
        self._build_tabs()
        self.window.playback.trackChanged.connect(self._on_track_changed)
        self.window.image_store.imageReady.connect(self._on_image)
        self.actions.likedChanged.connect(self._refresh_marker)

    def _build_header(self) -> None:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)

        self.cover = QLabel()
        self.cover.setFixedSize(168, 168)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setPixmap(placeholder(168))
        row.addWidget(self.cover)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.title = QLabel("加载中…")
        self.title.setObjectName("PageTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("Hint")
        self.meta.setWordWrap(True)
        self.desc = QLabel("")
        self.desc.setObjectName("Hint")
        self.desc.setWordWrap(True)
        self.desc.setMaximumHeight(72)
        right.addWidget(self.title)
        right.addWidget(self.meta)
        right.addWidget(self.desc)
        right.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.play_button = accent_button("播放全部", glow=False)
        self.play_button.clicked.connect(self._play_all)
        self.next_button = ghost_button("下一首播放", compact=True)
        self.next_button.clicked.connect(self._play_next_all)
        self.queue_button = ghost_button("加入队列", compact=True)
        self.queue_button.clicked.connect(self._queue_all)
        self.download_button = ghost_button("下载", compact=True)
        self.download_button.clicked.connect(self._download_all)
        self.subscribe_button = ghost_button("收藏", compact=True)
        self.subscribe_button.clicked.connect(self._toggle_subscribe)
        self.edit_button = ghost_button("编辑", compact=True)
        self.edit_button.clicked.connect(self._edit_menu)
        self.delete_button = danger_button("删除歌单")
        self.delete_button.clicked.connect(self._delete_playlist)
        for button in (self.play_button, self.next_button, self.queue_button,
                       self.download_button, self.subscribe_button,
                       self.edit_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        right.addLayout(buttons)
        row.addLayout(right, 1)
        self.content.addWidget(header)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

    def _build_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        songs_page = QWidget()
        songs_layout = QVBoxLayout(songs_page)
        songs_layout.setContentsMargins(0, 8, 0, 0)
        songs_layout.setSpacing(6)
        self.model = SongTableModel(self)
        self.table = SongTableView()
        self.table.setModel(self.model)
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._show_menu)
        songs_layout.addWidget(self.table, 1)
        self.pager = Pager()
        self.pager.pageChanged.connect(self._load_song_page)
        songs_layout.addWidget(self.pager)
        self.tabs.addTab(songs_page, "歌曲")

        self.albums_page = QWidget()
        albums_layout = QVBoxLayout(self.albums_page)
        albums_layout.setContentsMargins(0, 8, 0, 0)
        self.album_grid = CoverGrid(self.window.image_store)
        self.album_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("album", int(payload))
        )
        albums_scroll = QScrollArea()
        albums_scroll.setWidgetResizable(True)
        albums_scroll.setFrameShape(QFrame.Shape.NoFrame)
        albums_scroll.setWidget(self.album_grid)
        albums_layout.addWidget(albums_scroll)
        # 歌手详情才加这一页；其他类型在 load() 里移除
        self.content.addWidget(self.tabs, 1)

    # ---------------------------------------------------------------- 加载

    def load(self, kind: str, item_id: int) -> None:
        self.kind = kind
        self.item_id = int(item_id or 0)
        self._page = 1
        self._songs = []
        self._albums = []
        self._total = 0
        self._has_more = False
        self._entity = None
        self.model.set_songs([])
        self.album_grid.clear()
        self.pager.setVisible(False)
        self._set_actions_enabled(False)
        self._set_albums_tab(False)
        self.title.setText("加载中…")
        self.meta.setText("")
        self.desc.setText("")
        self.status.setText("正在加载…")
        self.cover.setPixmap(placeholder(168))

        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        client = self.window.client
        page_size = self._page_size
        logged_in = self.window.is_logged_in()

        def work():
            if logged_in:
                try:
                    client.account()
                except ApiError:
                    pass
            if kind == "playlist":
                playlist, songs, total = client.playlist(item_id, track_limit=page_size)
                return {"entity": playlist, "songs": songs, "total": total,
                        "albums": [], "has_more": total > len(songs)}
            if kind == "album":
                album, songs = client.album(item_id)
                return {"entity": album, "songs": songs, "total": len(songs),
                        "albums": [], "has_more": False}
            artist = client.artist(item_id)
            songs, more = client.artist_songs(item_id, page=1, limit=50)
            albums, _album_more = client.artist_albums(item_id, page=1, limit=100)
            return {"entity": artist, "songs": songs, "total": len(songs),
                    "albums": albums, "has_more": more}

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(lambda data: self._on_loaded(data, generation))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _on_loaded(self, data, generation: int) -> None:
        if generation != self._generation:
            return
        self._entity = data.get("entity")
        self._songs = list(data.get("songs") or [])
        self._albums = list(data.get("albums") or [])
        self._total = int(data.get("total") or len(self._songs))
        self._has_more = bool(data.get("has_more"))
        self.status.setText("")
        self._apply_entity()
        self.model.set_songs(self._songs)
        self._set_actions_enabled(bool(self._songs))
        self._sync_pager()
        self._refresh_marker()

        if self.kind == "artist":
            self._set_albums_tab(True)
            self.album_grid.set_items([
                {
                    "key": f"album:{album.id}",
                    "title": album.name,
                    "subtitle": album.artist_text,
                    "payload": album.id,
                    "url": album.pic_url,
                }
                for album in self._albums
            ])
        else:
            self._set_albums_tab(False)

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.status.setText(f"加载失败：{message}")
        self.title.setText("加载失败")
        self._set_actions_enabled(False)

    def _apply_entity(self) -> None:
        entity = self._entity
        if entity is None:
            return
        self.title.setText(entity.name)
        url = ""
        if self.kind == "playlist":
            url = entity.cover_url
            meta = entity.subtitle
            if entity.creator:
                meta += f" · 创建者 {entity.creator}"
            self.desc.setText(entity.description or "")
            self.subscribe_button.setVisible(
                not entity.own and entity.creator_id != self.window.client.uid
            )
            self.subscribe_button.setText("取消收藏" if entity.subscribed else "收藏")
            own = entity.creator_id == self.window.client.uid and not entity.own
            self.edit_button.setVisible(own)
            self.delete_button.setVisible(own)
        elif self.kind == "album":
            url = entity.pic_url
            meta = entity.artist_text
            if entity.company:
                meta += f" · {entity.company}"
            self.desc.setText(entity.description or "")
            self.subscribe_button.setVisible(True)
            self.subscribe_button.setText("取消收藏" if entity.subscribed else "收藏")
            self.edit_button.setVisible(False)
            self.delete_button.setVisible(False)
        else:
            url = entity.pic_url
            meta = entity.subtitle
            self.desc.setText(entity.brief or "")
            self.subscribe_button.setVisible(True)
            self.subscribe_button.setText("取消关注" if entity.followed else "关注")
            self.edit_button.setVisible(False)
            self.delete_button.setVisible(False)
        self.meta.setText(meta)
        self._cover_key = f"{self.kind}:{entity.id}"
        self.cover.setPixmap(placeholder(168))
        if url:
            cached = self.window.image_store.get(self._cover_key)
            if cached is not None:
                self._set_cover(cached)
            else:
                self.window.image_store.request(self._cover_key, url)

    def _set_cover(self, pixmap) -> None:
        self.cover.setPixmap(
            pixmap.scaled(168, 168, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
        )

    def _on_image(self, key: str, pixmap) -> None:
        if key == self._cover_key:
            self._set_cover(pixmap)

    def _set_albums_tab(self, visible: bool) -> None:
        index = self.tabs.indexOf(self.albums_page)
        if visible and index < 0:
            self.tabs.addTab(self.albums_page, "专辑")
        elif not visible and index >= 0:
            self.tabs.removeTab(index)

    def _set_actions_enabled(self, on: bool) -> None:
        for button in (self.play_button, self.next_button, self.queue_button,
                       self.download_button):
            button.setEnabled(on)
        self.subscribe_button.setEnabled(on and self.window.is_logged_in())
        self.edit_button.setEnabled(on)
        self.delete_button.setEnabled(on)

    def _sync_pager(self) -> None:
        if self.kind == "playlist" and self._total > self._page_size:
            pages = max(1, math.ceil(self._total / self._page_size))
            self.pager.set_page(self._page, self._page < pages, self._total)
            self.pager.setVisible(True)
        elif self.kind == "artist" and (self._page > 1 or self._has_more):
            self.pager.set_page(self._page, self._has_more, 0)
            self.pager.setVisible(True)
        else:
            self.pager.setVisible(False)

    # ---------------------------------------------------------------- 分页

    def _load_song_page(self, page: int) -> None:
        if self.kind not in ("playlist", "artist") or not self.item_id:
            return
        self._page = max(1, int(page))
        self.status.setText(f"正在加载第 {self._page} 页…")
        client = self.window.client
        offset = (self._page - 1) * self._page_size
        item_id = self.item_id
        page_size = self._page_size
        kind = self.kind
        self._generation += 1
        generation = self._generation

        if kind == "playlist":
            def work():
                return client.playlist_tracks(item_id, page_size, offset), False
        else:
            def work():
                return client.artist_songs(item_id, page=self._page, limit=50)

        task = BackgroundTask(work, parent=self)
        self._task = task

        def done(result) -> None:
            if generation != self._generation:
                return
            songs, more = result if isinstance(result, tuple) else (result, False)
            self._songs = list(songs)
            self._has_more = bool(more)
            self.model.set_songs(self._songs)
            self.status.setText("")
            self._sync_pager()
            self._refresh_marker()

        task.done.connect(done)
        task.failed.connect(lambda message: self._page_failed(message, generation))
        task.start()

    def _page_failed(self, message: str, generation: int) -> None:
        if generation == self._generation:
            self.status.setText(f"加载失败：{message}")

    # ---------------------------------------------------------------- 播放

    def _play_song(self, song: Song) -> None:
        self.actions.play(song, self._songs)

    def _play_all(self) -> None:
        if self.kind == "playlist" and self._total > len(self._songs):
            self._load_all_then(lambda songs: self.actions.play_all(songs))
        elif self._songs:
            self.actions.play_all(self._songs)

    def _play_next_all(self) -> None:
        if not self._songs:
            return
        self.window.playback.insert_next(list(self._songs))
        self.window.toast(f"已加入下一首播放：{len(self._songs)} 首")

    def _queue_all(self) -> None:
        if self._songs:
            self.actions.add_queue_many(self._songs)

    def _download_all(self) -> None:
        if self.kind == "playlist":
            self.actions.download_url(PLAYLIST_URL.format(self.item_id))
        elif self.kind == "album":
            self.actions.download_url(
                f"https://music.163.com/album?id={self.item_id}"
            )
        elif self.kind == "artist":
            self.actions.download_url(
                f"https://music.163.com/artist?id={self.item_id}"
            )

    def _load_all_then(self, callback) -> None:
        client = self.window.client
        item_id = self.item_id
        self.status.setText("正在读取完整歌单…")

        def work():
            return client.playlist_tracks_all(item_id)

        task = BackgroundTask(work, parent=self)

        def done(songs) -> None:
            self.status.setText("")
            songs = list(songs or [])
            if songs:
                callback(songs)
            else:
                self.status.setText("这个歌单还没有歌曲。")

        task.done.connect(done)
        task.failed.connect(lambda message: self.status.setText(f"读取失败：{message}"))
        task.start()

    # ---------------------------------------------------------------- 写操作

    def _toggle_subscribe(self) -> None:
        if not self.window.require_login_silent("收藏"):
            return
        client = self.window.client
        entity = self._entity
        if entity is None:
            return

        def work():
            if self.kind == "playlist":
                target = not entity.subscribed
                client.subscribe_playlist(entity.id, target)
                return target
            if self.kind == "album":
                target = not entity.subscribed
                client.subscribe_album(entity.id, target)
                return target
            target = not entity.followed
            client.subscribe_artist(entity.id, target)
            return target

        self.subscribe_button.setEnabled(False)

        def done(target) -> None:
            self.subscribe_button.setEnabled(True)
            if self.kind == "playlist":
                self._entity = replace(entity, subscribed=bool(target))
            elif self.kind == "album":
                self._entity = replace(entity, subscribed=bool(target))
            else:
                self._entity = replace(entity, followed=bool(target))
            self._apply_entity()
            self.window.toast("操作成功")

        def failed(message: str) -> None:
            self.subscribe_button.setEnabled(True)
            self.window.toast(f"操作失败：{message}")

        task = BackgroundTask(work, parent=self)
        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _edit_menu(self) -> None:
        entity = self._entity
        if self.kind != "playlist" or entity is None:
            return
        menu = QMenu(self)
        menu.addAction("重命名", self._rename_playlist)
        menu.addAction("修改简介", self._edit_description)
        menu.exec(self.edit_button.mapToGlobal(self.edit_button.rect().bottomLeft()))

    def _rename_playlist(self) -> None:
        entity = self._entity
        if entity is None:
            return
        name, ok = QInputDialog.getText(self, "重命名歌单", "歌单名称：", text=entity.name)
        name = (name or "").strip()
        if not ok or not name or name == entity.name:
            return
        self._write(lambda client: client.playlist_rename(entity.id, name),
                    "已重命名")

    def _edit_description(self) -> None:
        entity = self._entity
        if entity is None:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "修改简介", "歌单简介：", entity.description or ""
        )
        if not ok or text == (entity.description or ""):
            return
        self._write(lambda client: client.playlist_desc(entity.id, text),
                    "简介已更新")

    def _write(self, action, success: str) -> None:
        self.status.setText("正在提交…")

        def work():
            action(self.window.client)
            return True

        task = BackgroundTask(work, parent=self)
        task.done.connect(lambda _result: (
            self.status.setText(""), self.window.toast(success), self.load(
                self.kind, self.item_id)
        ))
        task.failed.connect(lambda message: self.status.setText(f"操作失败：{message}"))
        task.start()

    def _delete_playlist(self) -> None:
        entity = self._entity
        if self.kind != "playlist" or entity is None:
            return
        answer = QMessageBox.question(
            self, "删除歌单",
            f"确定删除歌单「{entity.name}」吗？<br>这个操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status.setText("正在删除…")

        def work():
            self.window.client.playlist_delete(entity.id)
            return True

        task = BackgroundTask(work, parent=self)
        task.done.connect(lambda _result: self._deleted())
        task.failed.connect(lambda message: self.status.setText(f"删除失败：{message}"))
        task.start()

    def _deleted(self) -> None:
        self.window.toast("歌单已删除")
        self.backRequested.emit()

    # ---------------------------------------------------------------- 菜单

    def _show_menu(self, song: Song, position) -> None:
        extra = []
        entity = self._entity
        if (self.kind == "playlist" and entity is not None
                and entity.creator_id == self.window.client.uid and not entity.own):
            # 网易云网页端没有公开的曲目重排接口（playlist/order/update 只调整
            # 歌单列表顺序），SPlayer 也不提供这项；这里只保留移除。
            extra.append(("从歌单移除", self._remove_song))
        self.actions.show_menu(self.table, song, self._songs, position, extra=extra)

    def _remove_song(self, song: Song) -> None:
        entity = self._entity
        if entity is None:
            return
        answer = QMessageBox.question(
            self, "从歌单移除",
            f"把《{song.name}》从「{entity.name}」移除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        client = self.window.client
        playlist_id = entity.id

        def work():
            client.playlist_remove_tracks(playlist_id, [song.id])
            return True

        task = BackgroundTask(work, parent=self)

        def done(_result) -> None:
            self._songs = [item for item in self._songs if item.id != song.id]
            self._total = max(0, self._total - 1)
            self.model.set_songs(self._songs)
            self._sync_pager()
            self.window.toast("已从歌单移除")

        task.done.connect(done)
        task.failed.connect(lambda message: self.window.toast(f"移除失败：{message}"))
        task.start()

    # ---------------------------------------------------------------- 状态

    def _on_track_changed(self, song) -> None:
        self.model.set_playing(getattr(song, "id", None) if song else None)

    def _refresh_marker(self) -> None:
        current = self.window.playback.current
        self.model.set_playing(current.id if current else None)

    def on_show(self) -> None:
        self.actions.ensure_liked()
        self._refresh_marker()

    def shutdown(self) -> None:
        self._generation += 1
        if self._task is not None and self._task.is_running:
            self._task.cancel()
