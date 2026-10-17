"""本地音乐页：扫描一个目录，点一首就能播。

默认目录就是程序的下载目录（``download.output``，本机是
``~/音乐/ncmctl``）。用户也可以在这里换成任意文件夹；选择会记在
``player.local_dir`` 里，下次打开直接落在上次的位置。

本地歌曲的 ``id`` 是负数（见 :mod:`ncmgui.localmusic`），和网易云的 ID 不会撞上。
播放时 :class:`~ncmgui.playback.PlaybackController` 看见 ``Song.path`` 有值就直接
把文件路径交给 mpv，不取直链、不联网；封面则从音频旁边的同名图片或内嵌标签里读
（见 :mod:`ncmgui.covers`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QTimer,
    Qt,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
)

from .. import localmusic, theme
from ..playback import STATE_PAUSED
from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..widgets.pathpicker import PathPicker
from ..workers import BackgroundTask
from .base import BasePage


class LocalTableModel(QAbstractTableModel):
    """本地文件列表。和红心表格一样，第 0 列兼任播放/暂停标记。"""

    HEADERS = ("#", "歌名", "歌手", "专辑", "音乐质量")
    COL_INDEX = 0
    COL_NAME = 1
    COL_ARTIST = 2
    COL_ALBUM = 3
    COL_QUALITY = 4

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._songs: list[Song] = []
        self._playing_id: int | None = None
        self._paused = False

    # ------------------------------------------------------------ Qt 模型接口

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._songs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._songs)):
            return None
        song = self._songs[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == self.COL_INDEX:
                if song.id == self._playing_id:
                    return "▶" if self._paused else theme.safe_glyph("⏸", "❚❚")
                return str(index.row() + 1)
            if column == self.COL_NAME:
                return song.name
            if column == self.COL_ARTIST:
                return ", ".join(song.artists)
            if column == self.COL_ALBUM:
                return song.album
            if column == self.COL_QUALITY:
                return song.quality
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            return song.path or song.display

        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.COL_INDEX:
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    # ------------------------------------------------------------ 状态

    def set_songs(self, songs: Sequence[Song]) -> None:
        self.beginResetModel()
        self._songs = list(songs)
        self.endResetModel()

    def songs(self) -> list[Song]:
        return list(self._songs)

    def set_playing(self, song_id: int | None) -> None:
        old = self._playing_id
        if old == song_id:
            return
        self._playing_id = song_id
        self._notify(old)
        self._notify(song_id)

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        self._notify(self._playing_id)

    def _notify(self, song_id: int | None) -> None:
        if song_id is None:
            return
        for row, song in enumerate(self._songs):
            if song.id == song_id:
                index = self.index(row, self.COL_INDEX)
                self.dataChanged.emit(index, index)
                return


class LocalPage(BasePage):
    PAGE_TITLE = "本地音乐"
    PAGE_SUBTITLE = "播放下载目录或任意文件夹里的音频。默认使用程序的下载目录。"

    #: 表格自己滚动
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def build(self) -> None:
        self._songs: list[Song] = []
        self._task: BackgroundTask | None = None
        self._generation = 0
        self._demo_mode = False
        self.playback = self.window.playback

        self._build_toolbar()
        self._build_table()

        # 用户改路径时别每敲一个字就扫一遍
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(350)
        self._scan_timer.timeout.connect(self._scan)

        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self._update_status()

    # ---------------------------------------------------------------- 构建

    def _build_toolbar(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.path_picker = PathPicker(
            mode="dir",
            value=str(localmusic.default_directory(self.cfg)),
            default=str(self.cfg.get("download.output", "") or ""),
        )
        self.path_picker.edit.setPlaceholderText("选择本地音乐目录")
        self.path_picker.pathChanged.connect(self._on_path_changed)
        row.addWidget(self.path_picker, 1)

        self.refresh_btn = ghost_button("刷新")
        self.refresh_btn.setToolTip("重新扫描当前目录")
        self.refresh_btn.clicked.connect(self._scan)
        row.addWidget(self.refresh_btn)

        self.play_all_btn = accent_button("播放全部")
        self.play_all_btn.setEnabled(False)
        self.play_all_btn.clicked.connect(lambda: self._play(0))
        row.addWidget(self.play_all_btn)

        self.content.addLayout(row)

    def _build_table(self) -> None:
        self.model = LocalTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.clicked.connect(self._on_clicked)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(LocalTableModel.COL_INDEX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(LocalTableModel.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(LocalTableModel.COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(LocalTableModel.COL_ALBUM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(
            LocalTableModel.COL_QUALITY, QHeaderView.ResizeMode.Interactive
        )
        self.table.setColumnWidth(LocalTableModel.COL_INDEX, 46)
        self.table.setColumnWidth(LocalTableModel.COL_ARTIST, 180)
        self.table.setColumnWidth(LocalTableModel.COL_ALBUM, 180)
        # 「无损 44.1kHz/16bit」是这一列最长的常见取值
        self.table.setColumnWidth(LocalTableModel.COL_QUALITY, 150)

        self.content.addWidget(self.table, 1)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.count_label)
        self.content.addLayout(status_row)

    # ---------------------------------------------------------------- 扫描

    def on_show(self) -> None:
        if self.window.defer_page_loads or self._demo_mode:
            return
        if self.path_picker.path().strip():
            self._scan()

    def _on_path_changed(self, _path: str) -> None:
        if self._demo_mode:
            return
        directory = self.path_picker.path().strip()
        if directory and Path(directory).is_dir():
            # 记下用户自己选的目录；下次打开就落在这里
            self.cfg.set("player.local_dir", directory)
        self._update_status()
        self._scan_timer.start()

    def _scan(self) -> None:
        directory = self.path_picker.path().strip()
        if not directory:
            self._songs = []
            self.model.set_songs([])
            self._update_status("请选择一个本地音乐目录。")
            return

        root = Path(directory).expanduser()
        if not root.is_dir():
            self._songs = []
            self.model.set_songs([])
            self._update_status(f"目录不存在：{root}")
            return

        self._generation += 1
        generation = self._generation
        self._update_status(f"正在扫描 {root} …")

        task = BackgroundTask(lambda: localmusic.scan(root), parent=self)
        self._task = task
        task.done.connect(lambda songs, gen=generation: self._on_scanned(gen, songs))
        task.failed.connect(lambda message, gen=generation: self._on_scan_failed(gen, message))
        task.start()

    def _on_scanned(self, generation: int, songs: object) -> None:
        if generation != self._generation:
            return
        self._task = None
        if not isinstance(songs, list):
            return
        self._songs = songs
        self.model.set_songs(songs)
        self.model.set_playing(self.playback.current.id if self.playback.current else None)
        self.model.set_paused(self.playback.state == STATE_PAUSED)
        self.play_all_btn.setEnabled(bool(songs))
        self._update_status()

    def _on_scan_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._task = None
        self._songs = []
        self.model.set_songs([])
        self.play_all_btn.setEnabled(False)
        self._update_status(f"扫描失败：{message}")

    def _update_status(self, message: str = "") -> None:
        if message:
            self.status_label.setText(message)
        elif self._songs:
            self.status_label.setText(f"目录：{self.path_picker.path()}")
        else:
            self.status_label.setText(
                "目录："
                + (self.path_picker.path() or "未选择")
                + " —— 里面没有音频文件（支持 flac / mp3 / m4a / ogg / wav 等）"
            )
        self.count_label.setText(f"共 {len(self._songs)} 首" if self._songs else "")

    # ---------------------------------------------------------------- 播放

    def _on_clicked(self, index: QModelIndex) -> None:
        self._play(index.row())

    def _play(self, row: int) -> None:
        if not (0 <= row < len(self._songs)):
            return
        self.playback.set_queue(self._songs, row)
        self.playback.play_at(row)
        self.window.open_player_page()

    def _on_track_changed(self, song) -> None:
        self.model.set_playing(getattr(song, "id", None))

    def _on_state_changed(self, state: str) -> None:
        self.model.set_paused(state == STATE_PAUSED)

    # ---------------------------------------------------------------- 演示

    def set_demo(self, songs: Sequence[Song], directory: str = "") -> None:
        """截图用：不碰真实目录，直接摆一份列表。"""
        self._demo_mode = True
        if directory:
            self.path_picker.set_path(directory)
        self._songs = list(songs)
        self.model.set_songs(self._songs)
        self.play_all_btn.setEnabled(bool(self._songs))
        self._update_status()
