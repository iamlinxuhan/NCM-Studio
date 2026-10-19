"""发现页：每日推荐、推荐歌单、新碟、推荐歌手。

启动和切页都只发一批请求。未登录时公开区域照常显示，每日推荐换成登录提示。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..api import ApiError, AuthRequired
from ..images import placeholder
from ..widgets.card import Card, accent_button, ghost_button
from ..widgets.covercard import CoverGrid
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage


class DiscoverPage(BasePage):
    PAGE_TITLE = "发现"
    PAGE_SUBTITLE = "每日推荐、推荐歌单、新碟与热门歌手。"

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._daily: list = []
        self._loaded_public = False
        self._daily_table_visible = False
        super().__init__(window)

    def build(self) -> None:
        self.actions = self.window.actions
        self._build_hero()
        self._build_daily_table()
        self._build_public()
        self.actions.likedChanged.connect(self._refresh_daily_marker)
        self.window.image_store.imageReady.connect(self._on_image)
        self._hero_key = ""

    # ---------------------------------------------------------------- 构建

    def _build_hero(self) -> None:
        card = Card("每日推荐", "每天 6 点更新，按你的口味挑 30 首。")
        row = QHBoxLayout()
        row.setSpacing(16)

        self.hero_cover = QLabel()
        self.hero_cover.setFixedSize(140, 140)
        self.hero_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hero_cover.setPixmap(placeholder(140))
        row.addWidget(self.hero_cover)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.hero_title = QLabel("登录后查看今日推荐")
        self.hero_title.setObjectName("CardTitle")
        self.hero_title.setWordWrap(True)
        self.hero_subtitle = QLabel("")
        self.hero_subtitle.setObjectName("Hint")
        self.hero_subtitle.setWordWrap(True)
        right.addWidget(self.hero_title)
        right.addWidget(self.hero_subtitle)
        right.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.play_daily_button = accent_button("播放全部", glow=False)
        self.play_daily_button.clicked.connect(self._play_daily)
        self.queue_daily_button = ghost_button("加入队列", compact=True)
        self.queue_daily_button.clicked.connect(self._queue_daily)
        self.toggle_daily_button = ghost_button("查看列表", compact=True)
        self.toggle_daily_button.clicked.connect(self._toggle_daily_table)
        self.login_button = ghost_button("去登录", compact=True)
        self.login_button.clicked.connect(lambda: self.window.show_page("account"))
        buttons.addWidget(self.play_daily_button)
        buttons.addWidget(self.queue_daily_button)
        buttons.addWidget(self.toggle_daily_button)
        buttons.addWidget(self.login_button)
        buttons.addStretch(1)
        right.addLayout(buttons)
        row.addLayout(right, 1)
        card.body.addLayout(row)
        self.content.addWidget(card)

    def _build_daily_table(self) -> None:
        self.daily_table_holder = QWidget()
        layout = QVBoxLayout(self.daily_table_holder)
        layout.setContentsMargins(0, 0, 0, 0)
        self.daily_model = SongTableModel(self)
        self.daily_table = SongTableView()
        self.daily_table.setModel(self.daily_model)
        self.daily_table.setMinimumHeight(320)
        self.daily_table.songActivated.connect(self._play_daily_song)
        self.daily_table.contextMenuRequested.connect(self._daily_menu)
        layout.addWidget(self.daily_table)
        self.daily_table_holder.setVisible(False)
        self.content.addWidget(self.daily_table_holder)

    def _build_public(self) -> None:
        self.playlist_title = self._section_title("推荐歌单")
        self.playlist_grid = CoverGrid(self.window.image_store)
        self.playlist_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("playlist", int(payload))
        )
        self.content.addWidget(self.playlist_title)
        self.content.addWidget(self.playlist_grid)

        self.album_title = self._section_title("新碟上架")
        self.album_grid = CoverGrid(self.window.image_store)
        self.album_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("album", int(payload))
        )
        self.content.addWidget(self.album_title)
        self.content.addWidget(self.album_grid)

        self.artist_title = self._section_title("热门歌手")
        self.artist_grid = CoverGrid(self.window.image_store, round_cover=True)
        self.artist_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("artist", int(payload))
        )
        self.content.addWidget(self.artist_title)
        self.content.addWidget(self.artist_grid)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("CardTitle")
        return label

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        self.actions.ensure_liked()
        if self.window.defer_page_loads:
            return
        self._sync_login_state()
        if self._task is not None and self._task.is_running:
            return
        self._reload()

    def _reload(self) -> None:
        self.status.setText("正在加载发现内容…")
        client = self.window.client
        logged_in = self.window.is_logged_in()

        def work():
            data = {"daily": [], "daily_error": ""}
            if logged_in:
                try:
                    data["daily"] = client.daily_songs()
                except AuthRequired:
                    data["daily_error"] = "登录已失效，请重新登录。"
                except ApiError as exc:
                    data["daily_error"] = str(exc)
            data["playlists"] = client.personalized_playlists(12)
            data["albums"] = client.new_albums(12)
            data["artists"] = client.top_artists(12)
            return data

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, data) -> None:
        self.status.setText("")
        self._daily = list(data.get("daily") or [])
        self.daily_model.set_songs(self._daily)
        self._daily_table_visible = bool(self._daily)

        if self._daily:
            self.hero_title.setText(f"今日推荐 · {len(self._daily)} 首")
            self.hero_subtitle.setText(
                " - ".join(song.display for song in self._daily[:3]) + " …"
            )
            first = next((song for song in self._daily if song.pic_url), None)
            if first is not None:
                self._hero_key = f"song:{first.id}"
                self.hero_cover.setPixmap(placeholder(140))
                pixmap = self.window.image_store.get(self._hero_key)
                if pixmap is not None:
                    self._set_hero_pixmap(pixmap)
                else:
                    self.window.image_store.request(self._hero_key, first.pic_url)
        elif self.window.is_logged_in():
            self.hero_title.setText("每日推荐暂时不可用")
            self.hero_subtitle.setText(str(data.get("daily_error") or ""))
        self._sync_login_state()

        self.playlist_grid.set_items([
            {
                "key": f"playlist:{item.id}",
                "title": item.name,
                "subtitle": item.subtitle,
                "payload": item.id,
                "url": item.cover_url,
            }
            for item in (data.get("playlists") or [])
        ])
        self.album_grid.set_items([
            {
                "key": f"album:{item.id}",
                "title": item.name,
                "subtitle": item.artist_text,
                "payload": item.id,
                "url": item.pic_url,
            }
            for item in (data.get("albums") or [])
        ])
        self.artist_grid.set_items([
            {
                "key": f"artist:{item.id}",
                "title": item.name,
                "subtitle": item.subtitle,
                "payload": item.id,
                "url": item.pic_url,
            }
            for item in (data.get("artists") or [])
        ])
        self._loaded_public = True

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"加载失败：{message}")

    def _on_image(self, key: str, pixmap) -> None:
        if key == self._hero_key:
            self._set_hero_pixmap(pixmap)

    def _set_hero_pixmap(self, pixmap) -> None:
        self.hero_cover.setPixmap(
            pixmap.scaled(140, 140, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
        )

    def _sync_login_state(self) -> None:
        logged_in = self.window.is_logged_in()
        self.login_button.setVisible(not logged_in)
        self.play_daily_button.setEnabled(bool(self._daily))
        self.queue_daily_button.setEnabled(bool(self._daily))
        self.toggle_daily_button.setEnabled(bool(self._daily))
        if not logged_in:
            self.hero_title.setText("登录后查看今日推荐")
            self.hero_subtitle.setText("每日推荐需要登录；下面的推荐歌单、新碟和歌手不需要。")

    # ---------------------------------------------------------------- 交互

    def _play_daily(self) -> None:
        if not self._daily:
            return
        self.actions.play_all(self._daily)

    def _queue_daily(self) -> None:
        if not self._daily:
            return
        self.actions.add_queue_many(self._daily)

    def _toggle_daily_table(self) -> None:
        if not self._daily:
            return
        self._daily_table_visible = not self._daily_table_visible
        self.daily_table_holder.setVisible(self._daily_table_visible)
        self.toggle_daily_button.setText(
            "收起列表" if self._daily_table_visible else "查看列表"
        )

    def _play_daily_song(self, song) -> None:
        self.actions.play(song, self._daily)

    def _daily_menu(self, song, position) -> None:
        self.actions.show_menu(self.daily_table, song, self._daily, position)

    def _refresh_daily_marker(self) -> None:
        current = self.window.playback.current
        self.daily_model.set_playing(current.id if current else None)

    def on_login_changed(self, logged_in: bool) -> None:
        self._daily = []
        self.daily_model.set_songs([])
        self._sync_login_state()
        self._reload()
