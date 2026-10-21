"""本地曲库页：歌曲、文件夹、本地歌手、本地专辑、本地歌单。

数据来自 ``config_dir()/library.db``（见 :mod:`ncmgui.library`），不再每次
进页面都遍历目录。目录可以加多个，扫描是增量的：路径、大小、mtime 都没变
的文件不会重新读标签；扫描在后台线程里跑，标签读取在数据库锁外完成。

本地歌曲仍是 ``Song(source="local", path=...)``，播放器看见 ``path`` 就直接
把文件交给 mpv。歌曲 ID 用负数，和网易云的 ID 隔开。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import QModelIndex, Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import localmusic
from ..library import (
    album_list,
    artist_list,
    counts,
    folder_list,
    forget_directory,
    playlists,
    scan as scan_library,
    tracks,
)
from ..library.queries import SORTS
from ..playback import STATE_PLAYING
from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..widgets.songmenu import show_song_menu
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage


def _duration_text(ms: int) -> str:
    """``3723000`` → ``1 小时 2 分``；用于文件夹/专辑合计。"""
    seconds = max(0, int(ms)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _make_table(headers: Sequence[str], stretches: Sequence[int] = (0,)) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(30)
    header = table.horizontalHeader()
    for column in range(len(headers)):
        if column in stretches:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        else:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
    return table


def _tune_song_columns(table: SongTableView) -> None:
    """本地歌曲的「音质」比在线页的「本地」标记长，给它多留一点宽度。

    歌手/专辑列相应收窄，保证窗口缩到最小时歌名仍有可用宽度。
    """
    table.setColumnWidth(SongTableModel.COL_ARTIST, 120)
    table.setColumnWidth(SongTableModel.COL_ALBUM, 150)
    table.setColumnWidth(SongTableModel.COL_EXTRA, 132)


def _fill_table(table: QTableWidget, rows: Sequence[Sequence[object]],
                payloads: Sequence[object] | None = None,
                align_columns: Sequence[int] = ()) -> None:
    table.setRowCount(len(rows))
    for row, cells in enumerate(rows):
        for column, value in enumerate(cells):
            item = QTableWidgetItem(str(value))
            if column in align_columns:
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            if column == 0 and payloads is not None:
                item.setData(Qt.ItemDataRole.UserRole, payloads[row])
            table.setItem(row, column, item)


class LocalPage(BasePage):
    PAGE_TITLE = "本地曲库"
    PAGE_SUBTITLE = "扫描一个或多个目录，按文件夹、歌手、专辑浏览；本地歌单只保存在本机。"

    SCROLLABLE = False
    TRAILING_STRETCH = False

    TAB_SONGS = 0

    def build(self) -> None:
        self.playback = self.window.playback
        self.actions = self.window.actions
        self.db = self.window.library

        self._songs: list[Song] = []
        self._playlist_songs: list[Song] = []
        self._playlist_id: int | None = None
        self._filter_kind = ""
        self._filter_value = ""
        self._filter_artist = ""
        self._scan_task: BackgroundTask | None = None
        self._generation = 0
        self._demo_mode = False
        self._ever_shown = False
        #: 「喜欢顺序」用的 文件名主干 → 红心歌单序号。None 表示还没取到。
        self._liked_rank: dict[str, int] | None = None
        self._liked_task: BackgroundTask | None = None
        #: 取失败后冷却一段时间，免得搜索框每敲一个字都发一次请求
        self._liked_retry_at = 0.0

        self._dirs = self._load_directories()

        self._build_toolbar()
        self._build_tabs()
        self._build_status()
        self._connect_playback()

        # 先显示数据库里已有的内容；真正的扫描等第一次切到本页
        self._reload_all()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._reload_songs)

    # ---------------------------------------------------------------- 构建

    def _build_toolbar(self) -> None:
        first = QHBoxLayout()
        first.setContentsMargins(0, 0, 0, 0)
        first.setSpacing(8)

        self.dirs_label = QLabel("")
        self.dirs_label.setObjectName("Hint")
        self.dirs_label.setWordWrap(True)
        first.addWidget(self.dirs_label, 1)

        self.add_dir_btn = ghost_button("添加目录…", compact=True)
        self.add_dir_btn.clicked.connect(self._add_directory)
        first.addWidget(self.add_dir_btn)

        self.remove_dir_btn = ghost_button("移除目录…", compact=True)
        self.remove_dir_btn.clicked.connect(self._remove_directory)
        first.addWidget(self.remove_dir_btn)

        self.scan_btn = accent_button("扫描曲库")
        self.scan_btn.setMinimumHeight(34)
        self.scan_btn.clicked.connect(self._toggle_scan)
        first.addWidget(self.scan_btn)

        self.content.addLayout(first)

        second = QHBoxLayout()
        second.setContentsMargins(0, 0, 0, 0)
        second.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌名 / 歌手 / 专辑")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        second.addWidget(self.search, 1)

        self.sort_combo = QComboBox()
        for label, value in SORTS:
            self.sort_combo.addItem(label, value)
        saved_sort = str(self.cfg.get("library.sort", "name") or "name")
        index = self.sort_combo.findData(saved_sort)
        self.sort_combo.setCurrentIndex(index if index >= 0 else 0)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.sort_combo.setToolTip("列表排序方式")
        second.addWidget(self.sort_combo)

        self.desc_btn = ghost_button("", compact=True)
        self.desc_btn.setCheckable(True)
        self.desc_btn.setChecked(bool(self.cfg.get("library.descending", False)))
        self.desc_btn.toggled.connect(self._on_desc_changed)
        self._sync_desc_text()
        second.addWidget(self.desc_btn)

        self.filter_label = QLabel("")
        self.filter_label.setObjectName("Hint")
        self.filter_label.setMaximumWidth(360)
        self.filter_label.setVisible(False)
        second.addWidget(self.filter_label)

        self.filter_clear = ghost_button("×", compact=True)
        self.filter_clear.setToolTip("清除文件夹/歌手/专辑筛选")
        self.filter_clear.setVisible(False)
        self.filter_clear.clicked.connect(self._clear_filter)
        second.addWidget(self.filter_clear)

        self.play_all_btn = accent_button("播放全部", glow=False)
        self.play_all_btn.setMinimumHeight(34)
        self.play_all_btn.setEnabled(False)
        self.play_all_btn.clicked.connect(self._play_all)
        second.addWidget(self.play_all_btn)

        self.content.addLayout(second)

    def _build_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # ---------------------------------------------------------- 歌曲
        songs_page = QWidget()
        songs_layout = QVBoxLayout(songs_page)
        songs_layout.setContentsMargins(0, 8, 0, 0)
        songs_layout.setSpacing(6)

        self.model = SongTableModel(
            self, extra_title="音质", extra_value=lambda song: song.quality
        )
        self.table = SongTableView()
        self.table.setModel(self.model)
        _tune_song_columns(self.table)
        self.table.songActivated.connect(self._play_song)
        self.table.contextMenuRequested.connect(self._show_song_menu)
        songs_layout.addWidget(self.table, 1)
        self.tabs.addTab(songs_page, "歌曲")

        # ---------------------------------------------------------- 文件夹
        self.folder_table = _make_table(("文件夹", "歌曲数", "总时长"))
        self.folder_table.setColumnWidth(1, 90)
        self.folder_table.setColumnWidth(2, 110)
        self.folder_table.cellDoubleClicked.connect(self._open_folder)
        self.tabs.addTab(self.folder_table, "文件夹")

        # ---------------------------------------------------------- 本地歌手
        self.artist_table = _make_table(("歌手", "歌曲数", "专辑数"))
        self.artist_table.setColumnWidth(1, 90)
        self.artist_table.setColumnWidth(2, 90)
        self.artist_table.cellDoubleClicked.connect(self._open_artist)
        self.tabs.addTab(self.artist_table, "本地歌手")

        # ---------------------------------------------------------- 本地专辑
        self.album_table = _make_table(("专辑", "歌手", "歌曲数"))
        self.album_table.setColumnWidth(1, 220)
        self.album_table.setColumnWidth(2, 90)
        self.album_table.cellDoubleClicked.connect(self._open_album)
        self.tabs.addTab(self.album_table, "本地专辑")

        # ---------------------------------------------------------- 本地歌单
        self.tabs.addTab(self._build_playlists_tab(), "本地歌单")

        self.content.addWidget(self.tabs, 1)

    def _build_playlists_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.new_pl_btn = ghost_button("新建歌单", compact=True)
        self.new_pl_btn.clicked.connect(self._new_playlist)
        self.rename_pl_btn = ghost_button("重命名", compact=True)
        self.rename_pl_btn.clicked.connect(self._rename_playlist)
        self.delete_pl_btn = ghost_button("删除", compact=True)
        self.delete_pl_btn.clicked.connect(self._delete_playlist)
        for button in (self.new_pl_btn, self.rename_pl_btn, self.delete_pl_btn):
            bar.addWidget(button)
        bar.addStretch(1)
        self.pl_play_btn = accent_button("播放全部", glow=False)
        self.pl_play_btn.setMinimumHeight(32)
        self.pl_play_btn.clicked.connect(self._play_playlist_all)
        self.pl_queue_btn = ghost_button("加入队列", compact=True)
        self.pl_queue_btn.clicked.connect(self._queue_playlist)
        bar.addWidget(self.pl_play_btn)
        bar.addWidget(self.pl_queue_btn)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.playlist_list = QListWidget()
        self.playlist_list.currentItemChanged.connect(self._on_playlist_selected)
        self.playlist_list.setMinimumWidth(200)
        self.playlist_list.setMaximumWidth(300)
        left_layout.addWidget(self.playlist_list, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self.pl_model = SongTableModel(
            self, extra_title="音质", extra_value=lambda song: song.quality
        )
        self.pl_table = SongTableView()
        self.pl_table.setModel(self.pl_model)
        _tune_song_columns(self.pl_table)
        self.pl_table.songActivated.connect(
            lambda song: self.actions.play(song, self._playlist_songs)
        )
        self.pl_table.contextMenuRequested.connect(self._show_playlist_menu)
        right_layout.addWidget(self.pl_table, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        self.pl_count_label = QLabel("")
        self.pl_count_label.setObjectName("Hint")
        bottom.addWidget(self.pl_count_label, 1)
        self.pl_up_btn = ghost_button("上移", compact=True)
        self.pl_up_btn.clicked.connect(lambda: self._move_playlist_track(-1))
        self.pl_down_btn = ghost_button("下移", compact=True)
        self.pl_down_btn.clicked.connect(lambda: self._move_playlist_track(1))
        self.pl_remove_btn = ghost_button("移除", compact=True)
        self.pl_remove_btn.clicked.connect(self._remove_from_playlist)
        for button in (self.pl_up_btn, self.pl_down_btn, self.pl_remove_btn):
            bottom.addWidget(button)
        right_layout.addLayout(bottom)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 700])
        layout.addWidget(splitter, 1)
        return page

    def _build_status(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        row.addWidget(self.status_label, 1)
        row.addWidget(self.progress)
        row.addWidget(self.count_label)
        self.content.addLayout(row)

    def _connect_playback(self) -> None:
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)

    # ---------------------------------------------------------------- 目录

    def _load_directories(self) -> list[str]:
        raw = self.cfg.get("library.dirs", [])
        dirs: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if text and text not in dirs:
                    dirs.append(text)
        if not dirs:
            fallback = str(self.cfg.get("player.local_dir", "") or "").strip()
            if not fallback:
                fallback = str(localmusic.default_directory(self.cfg))
            dirs = [fallback]
            self.cfg.set("library.dirs", dirs)
        return dirs

    def _save_directories(self) -> None:
        self.cfg.set("library.dirs", list(self._dirs))
        self._update_dirs_label()

    def _update_dirs_label(self) -> None:
        if not self._dirs:
            self.dirs_label.setText("还没有曲库目录。")
        elif len(self._dirs) == 1:
            self.dirs_label.setText(f"曲库目录：{self._dirs[0]}")
        else:
            self.dirs_label.setText(
                f"曲库目录：{self._dirs[0]} 等 {len(self._dirs)} 个"
            )
        self.dirs_label.setToolTip("\n".join(self._dirs))

    def _add_directory(self) -> None:
        start = self._dirs[0] if self._dirs else str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "添加曲库目录", start)
        if not directory:
            return
        try:
            path = str(Path(directory).expanduser().resolve())
        except OSError:
            path = str(Path(directory).expanduser())
        if path in self._dirs:
            self.toast("这个目录已经在曲库里了。")
            return
        self._dirs.append(path)
        self._save_directories()
        self._start_scan()

    def _remove_directory(self) -> None:
        if len(self._dirs) <= 1:
            self.warn("曲库至少需要一个目录。可以先把新目录加进来，再移除旧的。")
            return
        choice, ok = QInputDialog.getItem(
            self, "移除曲库目录", "选择要移除的目录：", self._dirs, 0, False
        )
        if not ok or not choice:
            return
        answer = QMessageBox.question(
            self, "移除曲库目录",
            f"从曲库移除这个目录下的歌曲索引？<br><br>{choice}<br><br>"
            "不会删除磁盘上的文件。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._dirs = [item for item in self._dirs if item != choice]
        self._save_directories()
        forget_directory(self.db, choice)
        self._reload_all()
        self.status_label.setText(f"已从曲库移除：{choice}")

    # ---------------------------------------------------------------- 扫描

    def on_show(self) -> None:
        if self._demo_mode or self.window.defer_page_loads:
            return
        if not self._ever_shown:
            self._ever_shown = True
            self._start_scan()
            return
        # 切回本页时只刷新轻量列表，不重复扫描（扫描按钮随时可用）
        self._reload_all()

    def _toggle_scan(self) -> None:
        if self._scan_task is not None and self._scan_task.is_running:
            self._scan_task.cancel()
            self.status_label.setText("正在停止扫描…已经写入的条目会保留。")
            self.scan_btn.setEnabled(False)
            return
        self._start_scan()

    def _start_scan(self) -> None:
        if self._demo_mode:
            return
        if self._scan_task is not None and self._scan_task.is_running:
            return
        dirs = [item for item in self._dirs if Path(item).expanduser().is_dir()]
        if not dirs:
            self.status_label.setText("没有可扫描的目录，请先添加一个存在的文件夹。")
            return

        self._generation += 1
        generation = self._generation
        self.scan_btn.setText("取消扫描")
        self.scan_btn.setEnabled(True)
        self.add_dir_btn.setEnabled(False)
        self.remove_dir_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status_label.setText("正在扫描曲库…")

        holder: dict[str, BackgroundTask] = {}

        def work():
            task = holder["task"]

            def report(done: int, total: int, added: int, updated: int) -> None:
                if total:
                    task.progress.emit(f"正在扫描 {done}/{total}：新增 {added}，更新 {updated}")
                    task.percent.emit(int(done * 100 / total))
                else:
                    task.progress.emit("正在扫描曲库…")

            return scan_library(
                self.db,
                dirs,
                progress=report,
                cancel=lambda: task.cancel_event.is_set(),
            )

        task = BackgroundTask(work, parent=self)
        holder["task"] = task
        self._scan_task = task
        task.progress.connect(self._on_scan_progress)
        task.percent.connect(self._on_scan_percent)
        task.done.connect(lambda result, gen=generation: self._on_scan_done(gen, result))
        task.failed.connect(lambda message, gen=generation: self._on_scan_failed(gen, message))
        task.start()

    def _on_scan_progress(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_scan_percent(self, value: int) -> None:
        if value < 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(max(0, min(100, int(value))))

    def _scan_finished(self) -> None:
        self._scan_task = None
        self.scan_btn.setText("重新扫描")
        self.scan_btn.setEnabled(True)
        self.add_dir_btn.setEnabled(True)
        self.remove_dir_btn.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        QTimer.singleShot(700, self.progress.hide)

    def _on_scan_done(self, generation: int, result: object) -> None:
        if generation != self._generation:
            return
        self._scan_finished()
        self._reload_all()
        added = int(getattr(result, "added", 0))
        updated = int(getattr(result, "updated", 0))
        removed = int(getattr(result, "removed", 0))
        seconds = float(getattr(result, "seconds", 0.0))
        if getattr(result, "cancelled", False):
            self.status_label.setText(
                f"扫描已取消：新增 {added} 首，更新 {updated} 首。"
            )
        elif added or updated or removed:
            self.status_label.setText(
                f"扫描完成：新增 {added} 首，更新 {updated} 首，"
                f"移除 {removed} 首，用时 {seconds:.1f} 秒。"
            )
        else:
            total = len(self._songs)
            self.status_label.setText(f"曲库已是最新，共索引 {total} 首。")

    def _on_scan_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._scan_finished()
        self._reload_all()
        self.status_label.setText(f"扫描失败：{message}")

    # ---------------------------------------------------------------- 刷新

    def _reload_all(self) -> None:
        self._folders = folder_list(self.db)
        self._artists = artist_list(self.db)
        self._albums = album_list(self.db)
        self._reload_songs()
        self._fill_folder_table()
        self._fill_artist_table()
        self._fill_album_table()
        self._reload_playlists()
        self._update_dirs_label()

    def _query_kwargs(self) -> dict:
        kwargs: dict = {
            "query": self.search.text(),
            "sort": self.sort_combo.currentData() or "name",
            "descending": self.desc_btn.isChecked(),
        }
        if self._filter_kind == "folder" and self._filter_value:
            kwargs["folder"] = self._filter_value
        elif self._filter_kind == "artist" and self._filter_value:
            kwargs["artist"] = self._filter_value
        elif self._filter_kind == "album" and self._filter_value:
            kwargs["album"] = self._filter_value
            if self._filter_artist:
                kwargs["artist"] = self._filter_artist
        return kwargs

    def _reload_songs(self) -> None:
        if self._demo_mode:
            return
        self._songs = tracks(self.db, **self._query_kwargs())
        if (self.sort_combo.currentData() or "name") == "liked":
            self._ensure_liked_rank()
            self._songs = self._sort_by_liked(self._songs)
        self.model.set_songs(self._songs)
        self._sync_markers(self.model)
        self.play_all_btn.setEnabled(bool(self._songs))
        self._update_counts()

    # ------------------------------------------------------------ 喜欢顺序

    def _ensure_liked_rank(self) -> None:
        """取红心歌单的顺序，缓存成 ``base → 序号``。

        本地文件名就是 ncmctl 的 ``歌手 - 歌名``，和红心歌单里的
        :attr:`Song.base` 同一套规则，因此能一一对上。
        """
        if self._liked_rank is not None:
            return
        if self._liked_task is not None and self._liked_task.is_running:
            return
        if not self.is_logged_in():
            self.status_label.setText("「喜欢顺序」需要登录网易云，当前按歌名排列。")
            return
        if time.monotonic() < self._liked_retry_at:
            return

        def work():
            client = self.window.client
            playlist = client.liked_playlist()
            songs = client.playlist_tracks_all(playlist.id)
            return {song.base: index for index, song in enumerate(songs)}

        task = BackgroundTask(work, parent=self)
        self._liked_task = task
        task.done.connect(self._on_liked_rank_loaded)
        task.failed.connect(self._on_liked_rank_failed)
        task.start()

    def _on_liked_rank_loaded(self, rank) -> None:
        # 取列表期间可能已经登出/换号，旧顺序不能再用
        if not self.is_logged_in():
            return
        self._liked_rank = dict(rank)
        if (self.sort_combo.currentData() or "name") == "liked":
            self._reload_songs()

    def _on_liked_rank_failed(self, message: str) -> None:
        self._liked_retry_at = time.monotonic() + 30.0
        if (self.sort_combo.currentData() or "name") == "liked":
            self.status_label.setText(f"读取「喜欢顺序」失败：{message}，暂时按歌名排列。")
            self._reload_songs()

    def _sort_by_liked(self, songs: list[Song]) -> list[Song]:
        """红心歌单里有的按收藏顺序排，没有的按歌名排在后面。"""
        rank = self._liked_rank
        if not rank:
            return songs
        descending = self.desc_btn.isChecked()
        matched = [song for song in songs if song.base in rank]
        rest = [song for song in songs if song.base not in rank]
        matched.sort(key=lambda song: rank[song.base], reverse=descending)
        rest.sort(key=lambda song: song.name.casefold(), reverse=descending)
        return matched + rest

    def on_login_changed(self, logged_in: bool) -> None:
        # 换号或退出后，上一份收藏顺序就失效了
        self._liked_rank = None
        if (self.sort_combo.currentData() or "name") == "liked":
            self._reload_songs()

    def _update_counts(self) -> None:
        total, total_ms = counts(self.db)
        if self._filter_kind:
            self.count_label.setText(
                f"显示 {len(self._songs)} 首 · 曲库共 {total} 首"
            )
        else:
            self.count_label.setText(
                f"共 {total} 首 · {_duration_text(total_ms)}"
            )

    def _sync_markers(self, model: SongTableModel) -> None:
        current = self.playback.current
        model.set_playing(current.id if current is not None else None)
        model.set_paused(self.playback.state != STATE_PLAYING)

    def _fill_folder_table(self) -> None:
        _fill_table(
            self.folder_table,
            [(entry.path, entry.count, _duration_text(entry.duration_ms))
             for entry in self._folders],
            [entry.path for entry in self._folders],
            align_columns=(1, 2),
        )

    def _fill_artist_table(self) -> None:
        _fill_table(
            self.artist_table,
            [(entry.name, entry.count, entry.album_count)
             for entry in self._artists],
            [entry.name for entry in self._artists],
            align_columns=(1, 2),
        )

    def _fill_album_table(self) -> None:
        _fill_table(
            self.album_table,
            [(entry.name, entry.artist or "未知歌手", entry.count)
             for entry in self._albums],
            [(entry.name, entry.artist) for entry in self._albums],
            align_columns=(2,),
        )

    # ---------------------------------------------------------------- 筛选

    def _set_filter(self, kind: str, value: str, label: str,
                    artist: str = "") -> None:
        self._filter_kind = kind
        self._filter_value = value
        self._filter_artist = artist
        self.filter_label.setText(label)
        self.filter_label.setToolTip(label)
        self.filter_label.setVisible(True)
        self.filter_clear.setVisible(True)
        self.tabs.setCurrentIndex(self.TAB_SONGS)
        self._reload_songs()

    def _clear_filter(self) -> None:
        self._filter_kind = ""
        self._filter_value = ""
        self._filter_artist = ""
        self.filter_label.setVisible(False)
        self.filter_clear.setVisible(False)
        self._reload_songs()

    def _open_folder(self, row: int, _column: int) -> None:
        item = self.folder_table.item(row, 0)
        if item is None:
            return
        path = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self._set_filter("folder", path, f"文件夹：{path}")

    def _open_artist(self, row: int, _column: int) -> None:
        item = self.artist_table.item(row, 0)
        if item is None:
            return
        name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self._set_filter("artist", name, f"歌手：{name}")

    def _open_album(self, row: int, _column: int) -> None:
        item = self.album_table.item(row, 0)
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        name, artist = str(payload[0]), str(payload[1])
        label = f"专辑：{name}" + (f" · {artist}" if artist else "")
        self._set_filter("album", name, label, artist=artist)

    def _on_sort_changed(self, _index: int) -> None:
        self.cfg.set("library.sort", self.sort_combo.currentData() or "name")
        self._reload_songs()

    def _on_desc_changed(self, checked: bool) -> None:
        self.cfg.set("library.descending", bool(checked))
        self._sync_desc_text()
        self._reload_songs()

    def _sync_desc_text(self) -> None:
        self.desc_btn.setText("降序 ↓" if self.desc_btn.isChecked() else "升序 ↑")

    # ---------------------------------------------------------------- 播放

    def _play_song(self, song: Song) -> None:
        self.actions.play(song, self._songs)

    def _play_all(self) -> None:
        self.actions.play_all(self._songs)

    def _on_track_changed(self, song) -> None:
        song_id = getattr(song, "id", None)
        self.model.set_playing(song_id)
        self.pl_model.set_playing(song_id)

    def _on_state_changed(self, state: str) -> None:
        paused = state != STATE_PLAYING
        self.model.set_paused(paused)
        self.pl_model.set_paused(paused)

    # ---------------------------------------------------------------- 右键菜单

    def _selected_songs(self, clicked: Song) -> list[Song]:
        selected = self.table.selected_songs()
        if clicked not in selected:
            return [clicked]
        return selected

    def _show_song_menu(self, song: Song, position) -> None:
        songs = self._selected_songs(song)
        show_song_menu(
            self.table, song, self._songs,
            play=lambda item, queue: self.actions.play(item, queue),
            play_next=self.actions.play_next,
            add_queue=self.actions.add_queue,
            search=self.actions.search,
            extra=[("加入本地歌单…", lambda _song: self._add_to_local_playlist(songs))],
            position=position,
        )

    # ---------------------------------------------------------------- 本地歌单

    def _reload_playlists(self, select_id: int | None = None) -> None:
        keep = select_id if select_id is not None else self._playlist_id
        items = playlists.list_playlists(self.db)
        self.playlist_list.blockSignals(True)
        self.playlist_list.clear()
        selected_row = -1
        for row, playlist in enumerate(items):
            item = QListWidgetItem(f"{playlist.name}（{playlist.track_count} 首）")
            item.setData(Qt.ItemDataRole.UserRole, int(playlist.id))
            item.setToolTip(playlist.name)
            self.playlist_list.addItem(item)
            if playlist.id == keep:
                selected_row = row
        if selected_row < 0 and items:
            selected_row = 0
        if selected_row >= 0:
            self.playlist_list.setCurrentRow(selected_row)
        self.playlist_list.blockSignals(False)

        if items:
            self._on_playlist_selected(self.playlist_list.currentItem())
        else:
            self._playlist_id = None
            self._playlist_songs = []
            self.pl_model.set_songs([])
            self.pl_count_label.setText("还没有本地歌单")

        has_selection = self._playlist_id is not None
        self.rename_pl_btn.setEnabled(has_selection)
        self.delete_pl_btn.setEnabled(has_selection)
        self.pl_play_btn.setEnabled(bool(self._playlist_songs))
        self.pl_queue_btn.setEnabled(bool(self._playlist_songs))
        self.pl_up_btn.setEnabled(bool(self._playlist_songs))
        self.pl_down_btn.setEnabled(bool(self._playlist_songs))
        self.pl_remove_btn.setEnabled(bool(self._playlist_songs))

    def _on_playlist_selected(self, current: QListWidgetItem | None,
                              _previous: QListWidgetItem | None = None) -> None:
        if current is None:
            return
        self._playlist_id = int(current.data(Qt.ItemDataRole.UserRole) or 0)
        self._reload_playlist_songs()

    def _reload_playlist_songs(self) -> None:
        if self._playlist_id is None:
            self._playlist_songs = []
        else:
            self._playlist_songs = playlists.tracks(self.db, self._playlist_id)
        self.pl_model.set_songs(self._playlist_songs)
        self._sync_markers(self.pl_model)
        self.pl_count_label.setText(
            f"共 {len(self._playlist_songs)} 首" if self._playlist_songs else "歌单还是空的"
        )
        has_songs = bool(self._playlist_songs)
        self.pl_play_btn.setEnabled(has_songs)
        self.pl_queue_btn.setEnabled(has_songs)
        self.pl_up_btn.setEnabled(has_songs)
        self.pl_down_btn.setEnabled(has_songs)
        self.pl_remove_btn.setEnabled(has_songs)

    def _current_playlist_name(self) -> str:
        item = self.playlist_list.currentItem()
        return item.text().rsplit("（", 1)[0] if item is not None else ""

    def _new_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "新建本地歌单", "歌单名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            playlist_id = playlists.create(self.db, name)
        except Exception as exc:  # noqa: BLE001 - 数据库错误要变成提示
            self.warn(f"新建歌单失败：{exc}")
            return
        self._reload_playlists(select_id=playlist_id)
        self.toast(f"已新建歌单：{name}")

    def _rename_playlist(self) -> None:
        playlist_id = self._playlist_id
        if playlist_id is None:
            return
        old = self._current_playlist_name()
        name, ok = QInputDialog.getText(
            self, "重命名本地歌单", "歌单名称：", text=old
        )
        name = (name or "").strip()
        if not ok or not name or name == old:
            return
        playlists.rename(self.db, playlist_id, name)
        self._reload_playlists(select_id=playlist_id)
        self.toast("歌单已重命名")

    def _delete_playlist(self) -> None:
        playlist_id = self._playlist_id
        if playlist_id is None:
            return
        name = self._current_playlist_name()
        answer = QMessageBox.question(
            self, "删除本地歌单",
            f"删除歌单「{name}」？<br>只删除歌单，不会删除歌曲文件。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        playlists.delete(self.db, playlist_id)
        self._playlist_id = None
        self._reload_playlists()
        self.toast("歌单已删除")

    def _add_to_local_playlist(self, songs: Sequence[Song]) -> None:
        if not songs:
            return
        items = playlists.list_playlists(self.db)
        names = [f"{item.name}（{item.track_count} 首）" for item in items]
        names.append("新建歌单…")
        choice, ok = QInputDialog.getItem(
            self, "加入本地歌单", f"把 {len(songs)} 首加入：", names, 0, False
        )
        if not ok or not choice:
            return

        if choice == "新建歌单…" or not items:
            name, ok = QInputDialog.getText(self, "新建本地歌单", "歌单名称：")
            name = (name or "").strip()
            if not ok or not name:
                return
            playlist_id = playlists.create(self.db, name)
            target_name = name
        else:
            index = names.index(choice)
            if index >= len(items):
                return
            playlist_id = items[index].id
            target_name = items[index].name

        added = playlists.add_tracks(
            self.db, playlist_id, [song.id for song in songs]
        )
        self._reload_playlists(select_id=playlist_id)
        if added:
            self.toast(f"已加入 {added} 首到「{target_name}」")
        else:
            self.toast(f"这些歌已经在「{target_name}」里了")

    def _play_playlist_all(self) -> None:
        if self._playlist_songs:
            self.actions.play_all(self._playlist_songs)

    def _queue_playlist(self) -> None:
        if self._playlist_songs:
            self.actions.add_queue_many(self._playlist_songs)

    def _show_playlist_menu(self, song: Song, position) -> None:
        selected = self.pl_table.selected_songs()
        if song not in selected:
            selected = [song]
        show_song_menu(
            self.pl_table, song, self._playlist_songs,
            play=lambda item, queue: self.actions.play(item, queue),
            play_next=self.actions.play_next,
            add_queue=self.actions.add_queue,
            extra=[("从本地歌单移除", lambda _song: self._remove_songs(selected))],
            position=position,
        )

    def _remove_from_playlist(self) -> None:
        selected = self.pl_table.selected_songs()
        if not selected:
            self.toast("先选中要移除的歌曲。")
            return
        self._remove_songs(selected)

    def _remove_songs(self, songs: Sequence[Song]) -> None:
        if self._playlist_id is None or not songs:
            return
        playlist_id = self._playlist_id
        removed = playlists.remove_tracks(
            self.db, playlist_id, [song.id for song in songs]
        )
        self._reload_playlists(select_id=playlist_id)
        self.toast(f"已移除 {removed} 首")

    def _move_playlist_track(self, delta: int) -> None:
        if self._playlist_id is None or not self._playlist_songs:
            return
        row = self.pl_table.currentIndex().row()
        if row < 0:
            self.toast("先选中要移动的歌曲。")
            return
        target = row + delta
        if not (0 <= target < len(self._playlist_songs)):
            return
        ids = [song.id for song in self._playlist_songs]
        ids[row], ids[target] = ids[target], ids[row]
        playlists.set_order(self.db, self._playlist_id, ids)
        self._reload_playlists(select_id=self._playlist_id)
        self.pl_table.selectRow(target)
        self.pl_table.scrollTo(
            self.pl_model.index(target, 0),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    # ---------------------------------------------------------------- 演示

    def set_demo(self, songs: Sequence[Song], directory: str = "") -> None:
        """截图用：不扫真实目录，直接摆一份歌曲列表。"""
        self._demo_mode = True
        if directory:
            self._dirs = [directory]
            self._update_dirs_label()
        self._songs = list(songs)
        self.model.set_songs(self._songs)
        self.play_all_btn.setEnabled(bool(self._songs))
        self.count_label.setText(f"共 {len(self._songs)} 首 · 演示数据")

    # ---------------------------------------------------------------- 收尾

    def shutdown(self) -> None:
        self._generation += 1
        if self._scan_task is not None and self._scan_task.is_running:
            self._scan_task.cancel()
