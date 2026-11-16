"""播放历史页。

历史记录在本地曲库里：歌曲在 ``play_events``，MV 在 ``mv_events``，各最多
显示最近 500 条。点一首歌会按当时的来源重新播放：本地文件直接播路径，网易云
歌曲按 ID 重新取链。源文件已经被删除的本地歌曲会明确提示，不会静默失败。
MV 同理 —— 按 ``mv_id`` 重新要播放地址，再进 MV 播放页。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api.models import Mv
from ..library import (
    clear_history,
    clear_mv_history,
    history_count,
    mv_count,
    recent,
    recent_mv,
)
from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..widgets.songmenu import show_song_menu
from ..workers import BackgroundTask
from .base import BasePage

HEADERS = ("时间", "歌名", "歌手", "专辑", "时长", "来源", "状态")
COL_TIME, COL_NAME, COL_ARTIST, COL_ALBUM, COL_DURATION, COL_SOURCE, COL_STATE = range(7)

MV_HEADERS = ("时间", "MV", "歌手", "时长", "状态")
MV_COL_TIME, MV_COL_NAME, MV_COL_ARTIST, MV_COL_DURATION, MV_COL_STATE = range(5)

LIMIT = 500


def _duration_text(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


class HistoryPage(BasePage):
    PAGE_TITLE = "播放历史"
    PAGE_SUBTITLE = (
        "最近听过的歌曲与看过的 MV。播放满 5 秒才会记一条，各最多保留显示 500 条。"
    )

    SCROLLABLE = False
    TRAILING_STRETCH = False

    def build(self) -> None:
        self.actions = self.window.actions
        self.playback = self.window.playback
        self.db = self.window.library
        self._entries: list = []
        self._mv_entries: list = []
        self._mv_task: BackgroundTask | None = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_song_tab(), "歌曲")
        self.tabs.addTab(self._build_mv_tab(), "MV")
        self.content.addWidget(self.tabs, 1)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._reload)

        self._mv_search_timer = QTimer(self)
        self._mv_search_timer.setSingleShot(True)
        self._mv_search_timer.setInterval(200)
        self._mv_search_timer.timeout.connect(self._reload_mv)

        self.playback.trackChanged.connect(lambda _song: self._sync_marker())

    # ---------------------------------------------------------------- 歌曲

    def _build_song_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        self.song_layout = layout
        self._build_toolbar(layout)
        self._build_table(layout)
        self._build_status(layout)
        return panel

    def _build_toolbar(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌名 / 歌手 / 专辑")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        row.addWidget(self.search, 1)

        self.refresh_btn = ghost_button("刷新", compact=True)
        self.refresh_btn.clicked.connect(self._reload)
        row.addWidget(self.refresh_btn)

        self.clear_btn = ghost_button("清空历史", compact=True)
        self.clear_btn.clicked.connect(self._clear)
        row.addWidget(self.clear_btn)

        self.play_all_btn = accent_button("播放全部", glow=False)
        self.play_all_btn.setMinimumHeight(34)
        self.play_all_btn.clicked.connect(self._play_all)
        row.addWidget(self.play_all_btn)
        layout.addLayout(row)

    def _build_table(self, layout: QVBoxLayout) -> None:
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.cellDoubleClicked.connect(self._play_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_ALBUM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_DURATION, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_TIME, 94)
        self.table.setColumnWidth(COL_ARTIST, 90)
        self.table.setColumnWidth(COL_ALBUM, 110)
        self.table.setColumnWidth(COL_DURATION, 48)
        self.table.setColumnWidth(COL_SOURCE, 64)
        self.table.setColumnWidth(COL_STATE, 62)

        layout.addWidget(self.table, 1)

    def _build_status(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")
        row.addWidget(self.status_label, 1)
        row.addWidget(self.count_label)
        layout.addLayout(row)

    # ---------------------------------------------------------------- MV

    def _build_mv_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.mv_search = QLineEdit()
        self.mv_search.setPlaceholderText("搜索 MV 名称 / 歌手")
        self.mv_search.setClearButtonEnabled(True)
        self.mv_search.textChanged.connect(
            lambda _text: self._mv_search_timer.start())
        row.addWidget(self.mv_search, 1)

        self.mv_refresh_btn = ghost_button("刷新", compact=True)
        self.mv_refresh_btn.clicked.connect(self._reload_mv)
        row.addWidget(self.mv_refresh_btn)

        self.mv_clear_btn = ghost_button("清空 MV 历史", compact=True)
        self.mv_clear_btn.clicked.connect(self._clear_mv)
        row.addWidget(self.mv_clear_btn)
        layout.addLayout(row)

        self.mv_table = QTableWidget(0, len(MV_HEADERS))
        self.mv_table.setHorizontalHeaderLabels(list(MV_HEADERS))
        self.mv_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.mv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mv_table.setShowGrid(False)
        self.mv_table.setAlternatingRowColors(True)
        self.mv_table.setWordWrap(False)
        self.mv_table.verticalHeader().setVisible(False)
        self.mv_table.verticalHeader().setDefaultSectionSize(30)
        self.mv_table.cellDoubleClicked.connect(self._play_mv_row)

        mv_header = self.mv_table.horizontalHeader()
        mv_header.setSectionResizeMode(MV_COL_TIME, QHeaderView.ResizeMode.Fixed)
        mv_header.setSectionResizeMode(MV_COL_NAME, QHeaderView.ResizeMode.Stretch)
        mv_header.setSectionResizeMode(MV_COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        mv_header.setSectionResizeMode(MV_COL_DURATION, QHeaderView.ResizeMode.Fixed)
        mv_header.setSectionResizeMode(MV_COL_STATE, QHeaderView.ResizeMode.Fixed)
        self.mv_table.setColumnWidth(MV_COL_TIME, 94)
        self.mv_table.setColumnWidth(MV_COL_ARTIST, 110)
        self.mv_table.setColumnWidth(MV_COL_DURATION, 48)
        self.mv_table.setColumnWidth(MV_COL_STATE, 62)
        layout.addWidget(self.mv_table, 1)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.mv_status_label = QLabel("")
        self.mv_status_label.setObjectName("Hint")
        self.mv_status_label.setWordWrap(True)
        self.mv_count_label = QLabel("")
        self.mv_count_label.setObjectName("Hint")
        status_row.addWidget(self.mv_status_label, 1)
        status_row.addWidget(self.mv_count_label)
        layout.addLayout(status_row)
        return panel

    # ---------------------------------------------------------------- 数据

    def on_show(self) -> None:
        self._reload()
        self._reload_mv()

    def _reload(self) -> None:
        query = self.search.text()
        self._show_entries(
            recent(self.db, LIMIT, query=query),
            history_count(self.db),
            searching=bool(query.strip()),
        )

    def set_demo(self, entries) -> None:
        """截图用：直接展示给定的历史记录，不读数据库。"""
        self._show_entries(entries, len(entries))

    def _show_entries(self, entries, total: int, *, searching: bool = False) -> None:
        self._entries = list(entries)
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            values = (
                entry.short_time_text,
                entry.title,
                ", ".join(entry.artists) or "—",
                entry.album or "—",
                _duration_text(entry.duration_ms),
                "本地" if entry.source == "local" else "网易云",
                "完整" if entry.completed else "未播完",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (COL_DURATION, COL_SOURCE, COL_STATE):
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                    item.setToolTip(entry.time_text)
                self.table.setItem(row, column, item)

        if self._entries:
            self.count_label.setText(f"显示 {len(self._entries)} 条 · 共 {total} 条")
            self.status_label.setText("")
        else:
            self.count_label.setText(f"共 {total} 条" if total else "")
            self.status_label.setText(
                "没有匹配的记录。" if searching else "还没有播放记录。"
            )
        self.play_all_btn.setEnabled(bool(self._entries))
        self.clear_btn.setEnabled(total > 0)
        self._sync_marker()

    def _entry_at(self, row: int):
        item = self.table.item(row, 0)
        if item is None:
            return None
        entry = item.data(Qt.ItemDataRole.UserRole)
        return entry if entry is not None else None

    def _song(self, entry) -> Song | None:
        if entry is None:
            return None
        return entry.to_song()

    def _playable(self) -> list[tuple[object, Song]]:
        pairs = []
        for entry in self._entries:
            song = self._song(entry)
            if song is not None:
                pairs.append((entry, song))
        return pairs

    # ---------------------------------------------------------------- 播放

    def _play_row(self, row: int, _column: int) -> None:
        entry = self._entry_at(row)
        song = self._song(entry)
        if entry is None:
            return
        if song is None:
            self.warn(
                "这首歌现在无法播放：本地文件已经不存在，或者记录里没有歌曲 ID。"
            )
            return
        songs = [item for _entry, item in self._playable()]
        self.actions.play(song, songs or [song])

    def _play_all(self) -> None:
        pairs = self._playable()
        if not pairs:
            self.warn("这些记录对应的歌曲现在都无法播放。")
            return
        skipped = len(self._entries) - len(pairs)
        self.actions.play_all([song for _entry, song in pairs])
        if skipped:
            self.toast(f"已跳过 {skipped} 首无法播放的记录")

    def _sync_marker(self) -> None:
        current = self.playback.current
        playing_id = current.id if current is not None else None
        for row, entry in enumerate(self._entries):
            item = self.table.item(row, COL_NAME)
            if item is None:
                continue
            if entry.track_id == playing_id and playing_id is not None:
                item.setText(f"▶ {entry.title}")
            else:
                item.setText(entry.title)

    # ---------------------------------------------------------------- MV 历史

    def _reload_mv(self) -> None:
        query = self.mv_search.text()
        entries = recent_mv(self.db, LIMIT, query=query)
        total = mv_count(self.db)
        self._show_mv_entries(entries, total, searching=bool(query.strip()))

    def _show_mv_entries(self, entries, total: int, *, searching: bool = False) -> None:
        self._mv_entries = list(entries)
        self.mv_table.setRowCount(len(self._mv_entries))
        for row, entry in enumerate(self._mv_entries):
            values = (
                entry.short_time_text,
                entry.name,
                entry.artist or "—",
                _duration_text(entry.duration_ms),
                "看完" if entry.completed else "未看完",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (MV_COL_DURATION, MV_COL_STATE):
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                    item.setToolTip(entry.time_text)
                self.mv_table.setItem(row, column, item)

        if self._mv_entries:
            self.mv_count_label.setText(
                f"显示 {len(self._mv_entries)} 条 · 共 {total} 条")
            self.mv_status_label.setText("")
        else:
            self.mv_count_label.setText(f"共 {total} 条" if total else "")
            self.mv_status_label.setText(
                "没有匹配的记录。" if searching else "还没有观看记录。"
            )
        self.mv_clear_btn.setEnabled(total > 0)

    def _mv_entry_at(self, row: int):
        item = self.mv_table.item(row, 0)
        if item is None:
            return None
        entry = item.data(Qt.ItemDataRole.UserRole)
        return entry if entry is not None else None

    def _play_mv_row(self, row: int, _column: int) -> None:
        entry = self._mv_entry_at(row)
        if entry is None:
            return
        if self._mv_task is not None and self._mv_task.is_running:
            self._mv_task.cancel()
        self.mv_status_label.setText(f"正在获取《{entry.name}》的播放地址…")
        client = self.window.client
        mv = Mv(
            id=entry.mv_id,
            name=entry.name,
            artist=entry.artist,
            cover_url=entry.cover_url,
            duration=entry.duration_ms,
        )
        task = BackgroundTask(lambda: client.mv_url(entry.mv_id), parent=self)
        self._mv_task = task

        def done(result) -> None:
            url, _fee = result
            self.mv_status_label.setText("")
            self.window.open_mv(mv, url)

        def failed(message: str) -> None:
            self.mv_status_label.setText(f"无法播放：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 杂项

    def _clear(self) -> None:
        total = history_count(self.db)
        if total == 0:
            return
        answer = QMessageBox.question(
            self, "清空播放历史",
            f"删除全部 {total} 条播放记录？<br>统计页的数据也会一起清零，"
            "删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        clear_history(self.db)
        self._reload()
        self.toast("播放历史已清空")

    def _clear_mv(self) -> None:
        total = mv_count(self.db)
        if total == 0:
            return
        answer = QMessageBox.question(
            self, "清空 MV 历史",
            f"删除全部 {total} 条 MV 观看记录？删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        clear_mv_history(self.db)
        self._reload_mv()
        self.toast("MV 历史已清空")

    def _show_menu(self, position) -> None:
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        entry = self._entry_at(row)
        song = self._song(entry)
        if entry is None:
            return
        if song is None:
            menu = QMenu(self.table)
            menu.addAction("这首歌现在无法播放").setEnabled(False)
            menu.exec(self.table.viewport().mapToGlobal(position))
            return
        selected_rows = sorted({index.row() for index in self.table.selectedIndexes()})
        songs = []
        for selected in selected_rows:
            item = self._song(self._entry_at(selected))
            if item is not None:
                songs.append(item)
        if song not in songs:
            songs = [song]
        show_song_menu(
            self.table, song, songs,
            play=lambda item, queue: self.actions.play(item, queue),
            play_next=self.actions.play_next,
            add_queue=self.actions.add_queue,
            extra=[("从历史中删除", lambda _song: self._delete_row(row))],
            position=self.table.viewport().mapToGlobal(position),
        )

    def _delete_row(self, row: int) -> None:
        entry = self._entry_at(row)
        if entry is None:
            return
        try:
            self.db.execute(
                "DELETE FROM play_events WHERE id = ?", (int(entry.id),))
        except Exception as exc:  # noqa: BLE001 - 槽函数里异常会让进程退出
            self.status_label.setText(f"删除失败：{exc}")
            return
        self._reload()
