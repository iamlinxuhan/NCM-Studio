"""播放历史页。

历史记录在本地曲库里（``play_events``），最多显示最近 500 条。点一首歌
会按当时的来源重新播放：本地文件直接播路径，网易云歌曲按 ID 重新取链。
源文件已经被删除的本地歌曲会明确提示，不会静默失败。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
)

from ..library import clear_history, history_count, recent
from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..widgets.songmenu import show_song_menu
from .base import BasePage

HEADERS = ("时间", "歌名", "歌手", "专辑", "时长", "来源", "状态")
COL_TIME, COL_NAME, COL_ARTIST, COL_ALBUM, COL_DURATION, COL_SOURCE, COL_STATE = range(7)

LIMIT = 500


def _duration_text(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


class HistoryPage(BasePage):
    PAGE_TITLE = "播放历史"
    PAGE_SUBTITLE = "最近听过的歌曲。播放满 5 秒才会记一条，最多保留显示 500 条。"

    SCROLLABLE = False
    TRAILING_STRETCH = False

    def build(self) -> None:
        self.actions = self.window.actions
        self.playback = self.window.playback
        self.db = self.window.library
        self._entries: list = []

        self._build_toolbar()
        self._build_table()
        self._build_status()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._reload)

        self.playback.trackChanged.connect(lambda _song: self._sync_marker())

    # ---------------------------------------------------------------- 构建

    def _build_toolbar(self) -> None:
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
        self.content.addLayout(row)

    def _build_table(self) -> None:
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

        self.content.addWidget(self.table, 1)

    def _build_status(self) -> None:
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
        self.content.addLayout(row)

    # ---------------------------------------------------------------- 数据

    def on_show(self) -> None:
        self._reload()

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
        self.db.execute("DELETE FROM play_events WHERE id = ?", (int(entry.id),))
        self._reload()
        self.toast("已从历史中删除这条记录")
