"""流媒体服务器页：Subsonic 系与 Jellyfin/Emby。

自建服务器的曲库通常不大，这里采用「选中服务器后一次拉三张表」的做法：
歌曲（Subsonic 用随机歌曲，见适配器说明）、专辑、歌手。专辑与歌手双击后
再按需取详情，避免首屏发几十个请求。

密码/API Key 只保存在配置里，界面不回显、日志不打印。
"""

from __future__ import annotations

from typing import Sequence

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..resolver import Song
from ..streaming import StreamAlbum, StreamArtist, StreamPlaylist, StreamTrack
from ..streaming.registry import KIND_CHOICES
from ..widgets.card import accent_button, danger_button, ghost_button
from ..widgets.songmenu import show_song_menu
from ..widgets.songtable import SongFilterProxy, SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

SONG_LIMIT = 200
ALBUM_LIMIT = 200
ARTIST_LIMIT = 500
PLAYLIST_LIMIT = 200


def _duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}" if seconds else "--:--"


def _make_table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(32)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


class ServerDialog(QDialog):
    """新增/编辑服务器。"""

    def __init__(self, parent: QWidget | None, server: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑服务器" if server else "添加服务器")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        form.setSpacing(10)

        self.kind = QComboBox()
        for label, value in KIND_CHOICES:
            self.kind.addItem(label, value)
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如：家里的 Navidrome")
        self.url = QLineEdit()
        self.url.setPlaceholderText("http://192.168.1.2:4533")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.token = QLineEdit()
        # API Key 与密码等权：都只用于本机连接，录入时一并不回显
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Jellyfin API Key；留空则用用户名密码登录")

        form.addRow("类型", self.kind)
        form.addRow("名称", self.name)
        form.addRow("地址", self.url)
        form.addRow("用户名", self.username)
        form.addRow("密码", self.password)
        form.addRow("API Key", self.token)

        hint = QLabel(
            "Subsonic 系填用户名与密码；Jellyfin/Emby 可只填 API Key，"
            "也可以填用户名密码，登录后会自动换成 AccessToken。"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        form.addRow(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if server:
            index = self.kind.findData(server.get("kind"))
            if index >= 0:
                self.kind.setCurrentIndex(index)
            self.name.setText(str(server.get("name") or ""))
            self.url.setText(str(server.get("url") or ""))
            self.username.setText(str(server.get("username") or ""))
            self.password.setText(str(server.get("password") or ""))
            self.token.setText(str(server.get("token") or ""))

    def _accept(self) -> None:
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "地址不完整", "服务器地址必须以 http:// 或 https:// 开头。")
            return
        if not self.name.text().strip():
            self.name.setText(url)
        self.accept()

    def data(self) -> dict:
        return {
            "kind": self.kind.currentData() or "subsonic",
            "name": self.name.text().strip(),
            "url": self.url.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "token": self.token.text().strip(),
        }


class TrackListDialog(QDialog):
    """专辑/歌单共用的曲目窗：标题、副标题、曲目表、播放/入队。"""

    def __init__(
        self,
        parent: QWidget,
        window,
        server_id: str,
        heading: str,
        meta_text: str,
        loader,
        empty_text: str = "这里还没有曲目",
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.server_id = server_id
        self._loader = loader
        self._empty_text = empty_text
        self._songs: list[Song] = []
        self._task: BackgroundTask | None = None
        self.setWindowTitle(f"{heading} - {window.streaming.name_for(server_id)}")
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        title = QLabel(heading)
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        meta = QLabel(meta_text)
        meta.setObjectName("Hint")
        layout.addWidget(title)
        layout.addWidget(meta)

        self.table = _make_table(("歌曲", "歌手", "时长"))
        self.table.doubleClicked.connect(self._play_index)
        layout.addWidget(self.table, 1)

        self.status = QLabel("正在读取曲目…")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.play_button = accent_button("播放全部", glow=False)
        self.play_button.clicked.connect(lambda: self._play_all())
        self.queue_button = ghost_button("加入队列", compact=True)
        self.queue_button.clicked.connect(self._queue_all)
        close = ghost_button("关闭", compact=True)
        close.clicked.connect(self.reject)
        row.addWidget(self.play_button)
        row.addWidget(self.queue_button)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        self.play_button.setEnabled(False)
        self.queue_button.setEnabled(False)
        self._load()

    def _load(self) -> None:
        loader = self._loader

        def work():
            return loader()

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, tracks) -> None:
        tracks = [track for track in tracks if isinstance(track, StreamTrack)]
        self._songs = self.window.streaming.songs_for(self.server_id, tracks)
        self.table.setRowCount(len(self._songs))
        for row, track in enumerate(tracks):
            self.table.setItem(row, 0, QTableWidgetItem(track.name))
            self.table.setItem(row, 1, QTableWidgetItem(", ".join(track.artists) or "—"))
            self.table.setItem(row, 2, QTableWidgetItem(_duration_text(track.duration)))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.status.setText(
            f"{len(self._songs)} 首" if self._songs else self._empty_text
        )
        self.play_button.setEnabled(bool(self._songs))
        self.queue_button.setEnabled(bool(self._songs))

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"读取失败：{message}")

    def _play_all(self) -> None:
        if self._songs:
            self.window.actions.play_all(self._songs)
            self.accept()

    def _play_index(self, index) -> None:
        if 0 <= index.row() < len(self._songs):
            self.window.actions.play(self._songs[index.row()], self._songs)
            self.accept()

    def _queue_all(self) -> None:
        if self._songs:
            self.window.actions.add_queue_many(self._songs)
            self.status.setText(f"已加入队列：{len(self._songs)} 首")


class AlbumDialog(TrackListDialog):
    """一张流媒体专辑的曲目。"""

    def __init__(
        self,
        parent: QWidget,
        window,
        server_id: str,
        album: StreamAlbum,
    ) -> None:
        registry = window.streaming
        super().__init__(
            parent,
            window,
            server_id,
            album.name,
            f"{', '.join(album.artists) or '未知歌手'}"
            + (f" · {album.year}" if album.year else ""),
            lambda: registry.album_songs(server_id, album.id),
            "这张专辑是空的",
        )


class PlaylistDialog(TrackListDialog):
    """一个流媒体歌单的曲目。"""

    def __init__(
        self,
        parent: QWidget,
        window,
        server_id: str,
        playlist: StreamPlaylist,
    ) -> None:
        registry = window.streaming
        meta = playlist.owner or "流媒体歌单"
        if playlist.song_count:
            meta += f" · {playlist.song_count} 首"
        super().__init__(
            parent,
            window,
            server_id,
            playlist.name,
            meta,
            lambda: registry.playlist_songs(server_id, playlist.id),
            "这个歌单是空的",
        )


class ArtistDialog(QDialog):
    """一位流媒体歌手的专辑列表。"""

    def __init__(
        self,
        parent: QWidget,
        window,
        server_id: str,
        artist: StreamArtist,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.server_id = server_id
        self.artist = artist
        self._albums: list[StreamAlbum] = []
        self._task: BackgroundTask | None = None
        self.setWindowTitle(f"{artist.name} - {window.streaming.name_for(server_id)}")
        self.resize(680, 480)

        layout = QVBoxLayout(self)
        title = QLabel(artist.name)
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        self.table = _make_table(("专辑", "年份"))
        self.table.doubleClicked.connect(self._open_album)
        layout.addWidget(self.table, 1)

        self.status = QLabel("正在读取专辑…")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        hint = QLabel("双击专辑查看曲目")
        hint.setObjectName("Hint")
        close = ghost_button("关闭", compact=True)
        close.clicked.connect(self.reject)
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        self._load()

    def _load(self) -> None:
        server_id = self.server_id
        artist_id = self.artist.id
        registry = self.window.streaming

        def work():
            return registry.artist_albums(server_id, artist_id)

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, albums) -> None:
        self._albums = [album for album in albums if isinstance(album, StreamAlbum)]
        self.table.setRowCount(len(self._albums))
        for row, album in enumerate(self._albums):
            self.table.setItem(row, 0, QTableWidgetItem(album.name))
            self.table.setItem(row, 1, QTableWidgetItem(str(album.year or "—")))
        self.status.setText(f"{len(self._albums)} 张专辑" if self._albums else "没有找到专辑")

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"读取失败：{message}")

    def _open_album(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._albums):
            AlbumDialog(self, self.window, self.server_id, self._albums[row]).exec()


class StreamingPage(BasePage):
    PAGE_TITLE = "流媒体"
    PAGE_SUBTITLE = "连接自建的 Subsonic / Navidrome、Jellyfin 或 Emby"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._generation = 0
        self._loaded_key: tuple[str, int] | None = None
        self._songs: list[Song] = []
        self._albums: list[StreamAlbum] = []
        self._artists: list[StreamArtist] = []
        self._playlists: list[StreamPlaylist] = []
        super().__init__(window)

    # ------------------------------------------------------------ 构建

    def build(self) -> None:
        self.registry = self.window.streaming

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        label = QLabel("服务器")
        label.setObjectName("Hint")
        self.server_combo = QComboBox()
        self.server_combo.setMinimumWidth(220)
        self.server_combo.currentIndexChanged.connect(self._on_server_changed)
        toolbar.addWidget(label)
        toolbar.addWidget(self.server_combo, 1)

        self.add_button = ghost_button("添加", compact=True)
        self.add_button.clicked.connect(self._add_server)
        self.edit_button = ghost_button("编辑", compact=True)
        self.edit_button.clicked.connect(self._edit_server)
        self.delete_button = danger_button("删除")
        self.delete_button.setMinimumHeight(26)
        self.delete_button.clicked.connect(self._delete_server)
        self.test_button = ghost_button("测试连接", compact=True)
        self.test_button.clicked.connect(self._test_server)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(lambda: self._reload(force=True))
        for button in (
            self.add_button, self.edit_button, self.delete_button,
            self.test_button, self.refresh_button,
        ):
            toolbar.addWidget(button)
        self.content.addLayout(toolbar)

        self.status = QLabel("还没有配置流媒体服务器。点「添加」连接 Navidrome、Jellyfin 等。")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_songs_tab(), "歌曲")
        self.tabs.addTab(self._build_albums_tab(), "专辑")
        self.tabs.addTab(self._build_artists_tab(), "歌手")
        self.tabs.addTab(self._build_playlists_tab(), "歌单")
        self.content.addWidget(self.tabs, 1)

        self.window.playback.trackChanged.connect(self._on_track_changed)
        self._refresh_servers()

    def _build_songs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("在当前列表里过滤")
        self.filter.textChanged.connect(self._on_filter)
        layout.addWidget(self.filter)

        self.model = SongTableModel(
            self,
            extra_title="来源",
            extra_value=lambda song: (
                "Jellyfin" if getattr(song, "source", "") == "jellyfin" else "Subsonic"
            ),
        )
        self.proxy = SongFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = SongTableView()
        self.table.setModel(self.proxy)
        self.table.setColumnWidth(SongTableModel.COL_EXTRA, 92)
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.table, 1)
        return page

    def _build_albums_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.album_table = _make_table(("专辑", "歌手", "年份"))
        self.album_table.doubleClicked.connect(self._open_album)
        layout.addWidget(self.album_table, 1)
        hint = QLabel("双击专辑查看曲目并播放")
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        return page

    def _build_artists_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.artist_table = _make_table(("歌手",))
        self.artist_table.doubleClicked.connect(self._open_artist)
        layout.addWidget(self.artist_table, 1)
        hint = QLabel("双击歌手查看专辑")
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        return page

    def _build_playlists_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.playlist_table = _make_table(("歌单", "创建者", "歌曲数"))
        self.playlist_table.doubleClicked.connect(self._open_playlist)
        layout.addWidget(self.playlist_table, 1)
        hint = QLabel("双击歌单查看曲目并播放")
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        return page

    # ------------------------------------------------------------ 服务器列表

    def _refresh_servers(self, select: str = "") -> None:
        servers = self.registry.servers()
        current = select or self.server_combo.currentData() or self.registry.last_server
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        for server in servers:
            self.server_combo.addItem(
                f"{server['name']}  ·  {server['url']}", server["id"]
            )
        if servers:
            index = self.server_combo.findData(current)
            self.server_combo.setCurrentIndex(index if index >= 0 else 0)
        self.server_combo.blockSignals(False)

        has_server = bool(servers)
        for button in (self.edit_button, self.delete_button, self.test_button,
                       self.refresh_button):
            button.setEnabled(has_server)
        self.tabs.setEnabled(has_server)
        self._loaded_key = None
        if not has_server:
            self._clear_data()
            self.status.setText(
                "还没有配置流媒体服务器。点「添加」连接 Navidrome、Jellyfin 等。"
            )
            return
        # 建页阶段不联网：切到本页时 on_show 会读一次，用户也可以点「刷新」
        self.status.setText("已选择服务器。切换到本页或点「刷新」开始读取。")

    def _current_server_id(self) -> str:
        return str(self.server_combo.currentData() or "")

    def _on_server_changed(self, _index: int = -1) -> None:
        server_id = self._current_server_id()
        if not server_id:
            return
        self.registry.set_last_server(server_id)
        self._loaded_key = None
        self._reload()

    def on_show(self) -> None:
        if self._current_server_id() and self._loaded_key is None:
            self._reload()

    def set_demo(
        self,
        name: str,
        songs: Sequence[Song],
        albums: Sequence[StreamAlbum] = (),
        artists: Sequence[StreamArtist] = (),
        playlists: Sequence[StreamPlaylist] = (),
    ) -> None:
        """截图/界面回归用的假数据；不发任何请求。"""
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        self.server_combo.addItem(f"{name}  ·  http://192.168.1.2:4533", "demo")
        self.server_combo.blockSignals(False)
        for button in (self.edit_button, self.delete_button, self.test_button,
                       self.refresh_button):
            button.setEnabled(True)
        self.tabs.setEnabled(True)
        self._songs = list(songs)
        self._albums = list(albums)
        self._artists = list(artists)
        self._playlists = list(playlists)
        self.model.set_songs(self._songs)
        self._fill_albums()
        self._fill_artists()
        self._fill_playlists()
        self.status.setText(
            f"{name} · Subsonic 1.16.1 · {len(songs)} 首 / "
            f"{len(albums)} 张专辑 / {len(artists)} 位歌手 / "
            f"{len(playlists)} 个歌单"
        )
        self._loaded_key = ("demo", self._generation)

    def _clear_data(self) -> None:
        self._songs = []
        self._albums = []
        self._artists = []
        self._playlists = []
        self.model.set_songs([])
        self.album_table.setRowCount(0)
        self.artist_table.setRowCount(0)
        self.playlist_table.setRowCount(0)

    # ------------------------------------------------------------ 读取

    def _reload(self, force: bool = False) -> None:
        server_id = self._current_server_id()
        if not server_id:
            return
        if not force and self._loaded_key and self._loaded_key[0] == server_id:
            return
        self._generation += 1
        generation = self._generation
        self.status.setText("正在连接服务器…")
        registry = self.registry

        def work():
            adapter = registry.adapter(server_id)
            info = adapter.ping()
            songs = adapter.songs(limit=SONG_LIMIT)
            albums = adapter.albums(limit=ALBUM_LIMIT)
            artists = adapter.artists(limit=ARTIST_LIMIT)
            playlists = adapter.playlists(limit=PLAYLIST_LIMIT)
            return info, songs, albums, artists, playlists

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(
            lambda data: self._on_loaded(data, server_id, generation)
        )
        task.failed.connect(
            lambda message: self._on_failed(message, server_id, generation)
        )
        task.start()

    def _on_loaded(self, data, server_id: str, generation: int) -> None:
        if generation != self._generation or server_id != self._current_server_id():
            return
        info, tracks, albums, artists, playlists = data
        self.registry.persist_tokens(server_id)
        self._songs = self.registry.songs_for(server_id, tracks)
        self._albums = [album for album in albums if isinstance(album, StreamAlbum)]
        self._artists = [artist for artist in artists if isinstance(artist, StreamArtist)]
        self._playlists = [
            playlist for playlist in playlists if isinstance(playlist, StreamPlaylist)
        ]

        self.model.set_songs(self._songs)
        self._sync_playing()
        self._fill_albums()
        self._fill_artists()
        self._fill_playlists()

        name = self.registry.name_for(server_id)
        self.status.setText(
            f"{name} · {info} · {len(self._songs)} 首 / "
            f"{len(self._albums)} 张专辑 / {len(self._artists)} 位歌手 / "
            f"{len(self._playlists)} 个歌单"
        )
        self._loaded_key = (server_id, self._generation)

    def _on_failed(self, message: str, server_id: str, generation: int) -> None:
        if generation != self._generation or server_id != self._current_server_id():
            return
        self.status.setText(f"读取失败：{message}")
        self._loaded_key = (server_id, self._generation)

    def _fill_albums(self) -> None:
        self.album_table.setRowCount(len(self._albums))
        for row, album in enumerate(self._albums):
            self.album_table.setItem(row, 0, QTableWidgetItem(album.name))
            self.album_table.setItem(
                row, 1, QTableWidgetItem(", ".join(album.artists) or "—")
            )
            self.album_table.setItem(
                row, 2, QTableWidgetItem(str(album.year or "—"))
            )

    def _fill_artists(self) -> None:
        self.artist_table.setRowCount(len(self._artists))
        for row, artist in enumerate(self._artists):
            self.artist_table.setItem(row, 0, QTableWidgetItem(artist.name))

    def _fill_playlists(self) -> None:
        self.playlist_table.setRowCount(len(self._playlists))
        for row, playlist in enumerate(self._playlists):
            self.playlist_table.setItem(row, 0, QTableWidgetItem(playlist.name))
            self.playlist_table.setItem(
                row, 1, QTableWidgetItem(playlist.owner or "—")
            )
            self.playlist_table.setItem(
                row, 2, QTableWidgetItem(str(playlist.song_count or "—"))
            )

    # ------------------------------------------------------------ 交互

    def _on_filter(self, text: str) -> None:
        self.proxy.set_query(text)

    def _play_song(self, song: Song) -> None:
        self.window.actions.play(song, self._songs)

    def _show_menu(self, song: Song, position) -> None:
        show_song_menu(
            self.table, song, self._songs,
            play=lambda item, songs: self.window.actions.play(item, songs),
            play_next=self.window.actions.play_next,
            add_queue=self.window.actions.add_queue,
            position=position,
        )

    def _on_track_changed(self, _song) -> None:
        self._sync_playing()

    def _sync_playing(self) -> None:
        current = self.window.playback.current
        self.model.set_playing(current.id if current is not None else None)
        self.model.set_paused(self.window.playback.paused)

    def _open_album(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._albums):
            AlbumDialog(
                self, self.window, self._current_server_id(), self._albums[row]
            ).exec()

    def _open_artist(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._artists):
            ArtistDialog(
                self, self.window, self._current_server_id(), self._artists[row]
            ).exec()

    def _open_playlist(self, index) -> None:
        row = index.row()
        if 0 <= row < len(self._playlists):
            PlaylistDialog(
                self, self.window, self._current_server_id(), self._playlists[row]
            ).exec()

    # ------------------------------------------------------------ 服务器管理

    def _add_server(self) -> None:
        dialog = ServerDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        server = self.registry.add_server(dialog.data())
        self._refresh_servers(select=server["id"])
        self._reload(force=True)
        self.window.toast(f"已添加服务器：{server['name']}")

    def _edit_server(self) -> None:
        server_id = self._current_server_id()
        server = self.registry.server(server_id)
        if server is None:
            return
        dialog = ServerDialog(self, server)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = self.registry.update_server(server_id, dialog.data())
        if updated is None:
            return
        self._refresh_servers(select=server_id)
        self._reload(force=True)
        self.window.toast("服务器设置已保存")

    def _delete_server(self) -> None:
        server_id = self._current_server_id()
        server = self.registry.server(server_id)
        if server is None:
            return
        answer = QMessageBox.question(
            self,
            "删除服务器",
            f"从列表里移除「{server['name']}」？\n\n"
            "只删除本机保存的地址与凭据，不会改动服务器上的任何数据。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.registry.remove_server(server_id)
        self._refresh_servers()
        if self._current_server_id():
            self._reload(force=True)
        self.window.toast("服务器已移除")

    def _test_server(self) -> None:
        server_id = self._current_server_id()
        if not server_id:
            return
        self.test_button.setEnabled(False)
        self.status.setText("正在测试连接…")
        registry = self.registry

        def work():
            return registry.test(server_id)

        task = BackgroundTask(work, parent=self)
        self._task = task

        def done(message: str) -> None:
            self.test_button.setEnabled(True)
            registry.persist_tokens(server_id)
            if server_id != self._current_server_id():
                return
            self.status.setText(f"连接成功：{message}")
            QMessageBox.information(self, "连接成功", f"服务器可用：\n{message}")

        def failed(message: str) -> None:
            self.test_button.setEnabled(True)
            if server_id != self._current_server_id():
                return
            self.status.setText(f"连接失败：{message}")
            QMessageBox.warning(self, "连接失败", message)

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
