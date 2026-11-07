"""排行榜页。

左边是官方榜、曲风榜这些分组，右边选中榜单的歌曲预览。榜单本身就是官方
歌单，所以「打开歌单」仍然走 ``open_detail("playlist", id)``，评论、收藏、
下载这些能力不用再实现一遍。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..api.models import Chart
from ..images import placeholder
from ..widgets.card import accent_button, ghost_button
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

PREVIEW_LIMIT = 100
COVER_SIZE = 96


class ChartsPage(BasePage):
    PAGE_TITLE = "排行榜"
    PAGE_SUBTITLE = "官方榜、曲风榜与特色榜；双击歌曲直接播放。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._track_task: BackgroundTask | None = None
        self._generation = 0
        self._loaded = False
        self._charts: dict[int, Chart] = {}
        self._chart: Chart | None = None
        self._songs: list = []
        self._cover_key = ""
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        self.actions = self.window.actions
        body = QHBoxLayout()
        body.setSpacing(14)

        self.list = QListWidget()
        self.list.setObjectName("ChartList")
        self.list.setMinimumWidth(210)
        self.list.setMaximumWidth(260)
        # 榜单名比栏宽时省略而不是冒出横向滚动条
        self.list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.itemSelectionChanged.connect(self._on_selection)
        body.addWidget(self.list)

        right = QVBoxLayout()
        right.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.cover = QLabel()
        self.cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setPixmap(placeholder(COVER_SIZE))
        header.addWidget(self.cover)

        column = QVBoxLayout()
        column.setSpacing(4)
        self.title = QLabel("选择左侧的榜单")
        self.title.setObjectName("PageTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("Hint")
        self.meta.setWordWrap(True)
        column.addWidget(self.title)
        column.addWidget(self.meta)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.play_button = accent_button("播放全部", glow=False)
        self.play_button.clicked.connect(self._play_all)
        self.play_button.setEnabled(False)
        self.open_button = ghost_button("打开歌单", compact=True)
        self.open_button.clicked.connect(self._open_playlist)
        self.open_button.setEnabled(False)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(self.reload)
        buttons.addWidget(self.play_button)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        column.addStretch(1)
        header.addLayout(column, 1)
        right.addLayout(header)

        self.model = SongTableModel(self)
        self.table = SongTableView()
        self.table.setModel(self.model)
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._menu)
        right.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        right.addWidget(self.status)

        body.addLayout(right, 1)
        self.content.addLayout(body, 1)

        self.window.playback.trackChanged.connect(self._on_track_changed)
        self.window.image_store.imageReady.connect(self._on_image)

    # ---------------------------------------------------------------- 加载

    def reload(self) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        self.status.setText("正在加载榜单…")
        self.refresh_button.setEnabled(False)
        client = self.window.client

        task = BackgroundTask(lambda: client.charts(), parent=self)
        self._task = task
        task.done.connect(lambda groups: self._on_charts(groups, generation))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _on_charts(self, groups, generation: int) -> None:
        if generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        self.list.blockSignals(True)
        self.list.clear()
        self._charts.clear()
        first: QListWidgetItem | None = None
        for group in groups:
            header = QListWidgetItem(group.name)
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.list.addItem(header)
            for chart in group.charts:
                item = QListWidgetItem(chart.name)
                item.setData(Qt.ItemDataRole.UserRole, chart.id)
                if chart.subtitle:
                    item.setToolTip(chart.subtitle)
                self.list.addItem(item)
                self._charts[chart.id] = chart
                if first is None:
                    first = item
        self.list.blockSignals(False)
        self._loaded = True
        if first is None:
            self.status.setText("榜单暂时不可用，稍后再试。")
            return
        self.status.setText("")
        self.list.setCurrentItem(first)
        self._on_selection()

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    def _on_selection(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        chart_id = item.data(Qt.ItemDataRole.UserRole)
        chart = self._charts.get(int(chart_id or 0))
        if chart is None or chart is self._chart:
            return
        self._chart = chart
        self._songs = []
        self.model.set_songs([])
        self.play_button.setEnabled(False)
        self.open_button.setEnabled(True)
        self.title.setText(chart.name)
        self.meta.setText(chart.subtitle)
        self._cover_key = f"chart:{chart.id}"
        placeholder_pixmap = placeholder(COVER_SIZE)
        self.cover.setPixmap(placeholder_pixmap)
        if chart.cover_url:
            cached = self.window.image_store.request(
                self._cover_key, chart.cover_url)
            if cached is not None:
                self._set_cover(cached)
        self._load_tracks(chart)

    def _load_tracks(self, chart: Chart) -> None:
        if self._track_task is not None and self._track_task.is_running:
            self._track_task.cancel()
        generation = self._generation
        self.status.setText("正在加载歌曲…")
        client = self.window.client

        task = BackgroundTask(
            lambda: client.playlist_tracks(chart.id, limit=PREVIEW_LIMIT),
            parent=self,
        )
        self._track_task = task

        def done(songs) -> None:
            if generation != self._generation or self._chart is not chart:
                return
            self._songs = list(songs)
            self.model.set_songs(self._songs)
            self.play_button.setEnabled(bool(self._songs))
            self.status.setText(
                f"{len(self._songs)} 首" if self._songs else "这个榜单暂时没有可播放的歌曲。")
            self._refresh_marker()

        def failed(message: str) -> None:
            if generation != self._generation:
                return
            self.status.setText(f"加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 播放

    def _play_all(self) -> None:
        if self._songs:
            self.actions.play_all(self._songs)

    def _play_song(self, song) -> None:
        self.actions.play(song, self._songs)

    def _menu(self, song, position) -> None:
        self.actions.show_menu(self.table, song, self._songs, position)

    def _open_playlist(self) -> None:
        if self._chart is not None:
            self.window.open_detail("playlist", self._chart.id)

    def _on_track_changed(self, _song) -> None:
        self._refresh_marker()

    def _refresh_marker(self) -> None:
        current = self.window.playback.current
        self.model.set_playing(current.id if current else None)

    # ---------------------------------------------------------------- 封面

    def _set_cover(self, pixmap: QPixmap) -> None:
        self.cover.setPixmap(pixmap.scaled(
            COVER_SIZE, COVER_SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation))

    def _on_image(self, key: str, pixmap: QPixmap) -> None:
        if key == self._cover_key:
            self._set_cover(pixmap)

    # ---------------------------------------------------------------- 生命周期

    def on_show(self) -> None:
        self.actions.ensure_liked()
        if not self._loaded:
            self.reload()
        else:
            self._refresh_marker()

    def shutdown(self) -> None:
        self._generation += 1
        for task in (self._task, self._track_task):
            if task is not None and task.is_running:
                task.cancel()
