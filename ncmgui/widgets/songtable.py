"""通用歌曲表格：搜索结果、歌单、专辑、本地曲库共用。

从「我喜欢的音乐」页抽出来的。那边原来把模型、过滤、表格配置写成一整套，
复制到每个新页面会到处漂移；这里只保留一套，页面通过 ``extra_title`` /
``extra_value`` 表达差异。

模型不持有播放队列，也不发网络请求：``set_songs`` / ``set_playing`` 由页面调用。
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableView,
    QWidget,
)

from .. import player, theme
from ..resolver import Song
from .playmarker import PlayMarkerDelegate


class SongTableModel(QAbstractTableModel):
    """歌曲列表模型。第 0 列显示播放标记或序号。"""

    HEADERS = ("#", "歌名", "歌手", "专辑", "时长", "本地")
    COL_INDEX, COL_NAME, COL_ARTIST, COL_ALBUM, COL_DURATION, COL_EXTRA = range(6)

    def __init__(
        self,
        parent=None,
        *,
        extra_title: str = "本地",
        extra_value: Callable[[Song], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._songs: list[Song] = []
        self._playing_id: int | None = None
        self._paused = False
        self._downloaded: set[int] = set()
        self._downloaded_dir = ""
        self._extra_title = extra_title
        self._extra_value = extra_value

    # ------------------------------------------------------------ 数据源

    def set_songs(self, songs: Sequence[Song]) -> None:
        self.beginResetModel()
        self._songs = list(songs)
        self.endResetModel()

    def songs(self) -> list[Song]:
        return list(self._songs)

    def song_at(self, row: int) -> Song | None:
        if 0 <= row < len(self._songs):
            return self._songs[row]
        return None

    def row_of(self, song_id: int | None) -> int | None:
        if song_id is None:
            return None
        for row, song in enumerate(self._songs):
            if song.id == song_id:
                return row
        return None

    @property
    def playing_id(self) -> int | None:
        return self._playing_id

    def set_playing(self, song_id: int | None) -> None:
        if song_id == self._playing_id:
            return
        old = self._playing_id
        self._playing_id = song_id
        for changed in (old, song_id):
            row = self.row_of(changed)
            if row is None:
                continue
            self.dataChanged.emit(
                self.index(row, 0), self.index(row, self.columnCount() - 1)
            )

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        row = self.row_of(self._playing_id)
        if row is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

    def marker(self, song_id: int | None) -> str:
        if song_id is None or song_id != self._playing_id:
            return ""
        return "▶" if self._paused else "❚❚"

    def set_downloaded(self, ids: Iterable[int], directory: str = "") -> None:
        self._downloaded = set(ids)
        self._downloaded_dir = directory
        if self._songs:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._songs) - 1, self.columnCount() - 1),
            )

    def set_extra_title(self, title: str) -> None:
        self._extra_title = title
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, self.COL_EXTRA,
                                    self.COL_EXTRA)

    # ------------------------------------------------------------ 模型接口

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._songs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self._extra_title if section == self.COL_EXTRA else self.HEADERS[section]
        return None

    def _extra(self, song: Song) -> str:
        if self._extra_value is not None:
            return self._extra_value(song) or ""
        return "✓" if song.id in self._downloaded else ""

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        song = self.song_at(index.row())
        if song is None:
            return None
        column = index.column()
        playing = song.id == self._playing_id

        if role == Qt.ItemDataRole.DisplayRole:
            if column == self.COL_INDEX:
                marker = self.marker(song.id)
                return marker or str(index.row() + 1)
            if column == self.COL_NAME:
                tag = song.fee_tag
                return f"{song.name}  [{tag}]" if tag else song.name
            if column == self.COL_ARTIST:
                return ", ".join(song.artists) or "—"
            if column == self.COL_ALBUM:
                return song.album or "—"
            if column == self.COL_DURATION:
                return song.duration_text
            if column == self.COL_EXTRA:
                return self._extra(song)
            return None

        if role == Qt.ItemDataRole.BackgroundRole and playing:
            return QColor(0, 229, 255, 45)

        if role == Qt.ItemDataRole.ForegroundRole:
            if playing:
                return QColor(theme.CYAN)
            if column == self.COL_NAME and song.fee_tag:
                return QColor(theme.AMBER)
            if column == self.COL_INDEX:
                return QColor(theme.TEXT_DIM)
            if column == self.COL_EXTRA and song.id in self._downloaded:
                return QColor(theme.GREEN)

        if role == Qt.ItemDataRole.FontRole and playing:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            self.COL_INDEX, self.COL_DURATION, self.COL_EXTRA,
        ):
            return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [song.display]
            if song.album:
                parts.append(f"专辑：{song.album}")
            if song.fee_label:
                parts.append(f"{song.fee_label}：可能要会员才能听完整版")
            if song.id in self._downloaded and self._downloaded_dir:
                parts.append(f"已下载到 {self._downloaded_dir}")
            if playing:
                parts.append("正在暂停" if self._paused else "正在播放")
            return "\n".join(parts)

        return None


class SongFilterProxy(QSortFilterProxyModel):
    """按搜索框过滤；多个词要求全部出现。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._query = ""

    def set_query(self, query: str) -> None:
        query = (query or "").strip()
        if query == self._query:
            return
        self._query = query
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        song = model.song_at(source_row) if isinstance(model, SongTableModel) else None
        if song is None:
            return False
        return player.matches_query(song.search_text, self._query)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and index.isValid()
            and index.column() == SongTableModel.COL_INDEX
        ):
            source = self.mapToSource(index)
            model = self.sourceModel()
            song = model.song_at(source.row()) if isinstance(model, SongTableModel) else None
            if song is not None:
                marker = model.marker(song.id)
                if marker:
                    return marker
            return str(index.row() + 1)
        return super().data(index, role)


class SongTableView(QTableView):
    """配置好的歌曲表格。

    * 双击或回车播放；
    * 单击选中，不直接播放（避免误触把正在播的歌切掉）；
    * 右键菜单交给页面：``contextMenuRequested(song, global_pos)``。
    """

    songActivated = pyqtSignal(object)
    contextMenuRequested = pyqtSignal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(32)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.doubleClicked.connect(self._activate_index)
        self.activated.connect(self._activate_index)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setItemDelegateForColumn(
            SongTableModel.COL_INDEX, PlayMarkerDelegate(self)
        )
        self._configure_header()

    def setModel(self, model) -> None:  # noqa: N802
        # 列宽必须在**挂上模型之后**设置：模型还没设时表头没有 section，
        # 之前设的 Stretch/Fixed 会被随后的 model reset 覆盖成默认的 100px，
        # 结果表格只用了半屏宽。页面通常在构造后才 setModel，所以在这里补一次。
        super().setModel(model)
        self._configure_header()

    def _configure_header(self) -> None:
        header = self.horizontalHeader()
        header.setSectionResizeMode(SongTableModel.COL_INDEX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(SongTableModel.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SongTableModel.COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(SongTableModel.COL_ALBUM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(SongTableModel.COL_DURATION, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(SongTableModel.COL_EXTRA, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(SongTableModel.COL_INDEX, 46)
        self.setColumnWidth(SongTableModel.COL_ARTIST, 170)
        self.setColumnWidth(SongTableModel.COL_ALBUM, 190)
        self.setColumnWidth(SongTableModel.COL_DURATION, 66)
        self.setColumnWidth(SongTableModel.COL_EXTRA, 56)

    # ------------------------------------------------------------ 选中项

    def current_song(self) -> Song | None:
        index = self.currentIndex()
        if not index.isValid():
            return None
        return self._song_for(index)

    def selected_songs(self) -> list[Song]:
        rows: list[Song] = []
        seen: set[int] = set()
        for index in self.selectionModel().selectedRows():
            song = self._song_for(index)
            if song is not None and id(song) not in seen:
                seen.add(id(song))
                rows.append(song)
        return rows

    def _song_for(self, index: QModelIndex) -> Song | None:
        model = self.model()
        if model is None or not index.isValid():
            return None
        source = index
        if isinstance(model, QSortFilterProxyModel):
            source = model.mapToSource(index)
            model = model.sourceModel()
        song = model.song_at(source.row()) if isinstance(model, SongTableModel) else None
        return song

    # ------------------------------------------------------------ 交互

    def _activate_index(self, index: QModelIndex) -> None:
        song = self._song_for(index)
        if song is not None:
            self.songActivated.emit(song)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            song = self.current_song()
            if song is not None:
                self.songActivated.emit(song)
                return
        super().keyPressEvent(event)

    def _show_menu(self, position) -> None:
        index = self.indexAt(position)
        if not index.isValid():
            return
        song = self._song_for(index)
        if song is not None:
            self.contextMenuRequested.emit(song, self.viewport().mapToGlobal(position))
