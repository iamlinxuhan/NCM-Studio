"""「我喜欢的音乐」：红心歌单 + 本地音乐入口。

这个页面只负责**列表**：读红心歌单、搜索、显示「已下载」，点行时把队列交给
全局的 :class:`~ncmgui.playback.PlaybackController`。播放器本身、底部播放条、
播放页都不在这里 —— 切到别的页面也要能继续控制播放。

页面底部还有两个本地音乐入口：文件选择默认落在程序下载目录，也可以直接把整个
下载目录当队列播放。本地歌曲的 ``id`` 是负数（见 :mod:`ncmgui.localmusic`），
不会和网易云的 ID 撞上。

几条容易踩的坑，在 controller 里处理：``loadfile replace`` 会发
``end-file: stop``、旧曲目的 ``eof`` 可能迟到、快速连点要丢弃过期取链结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import localmusic, ncmctl, player, theme
from ..api.models import Mv
from ..config import default_video_dir
from ..library import favorites
from ..lyrics import AUDIO_SUFFIXES, index_audio
from ..mvdownload import download_all
from ..playback import STATE_PLAYING
from ..resolver import Song
from ..widgets.card import ghost_button
from ..widgets.form import combo_data, set_combo_data
from ..widgets.playmarker import PlayMarkerDelegate
from ..workers import BackgroundTask
from .base import BasePage


# --------------------------------------------------------------------------- #
# 表格模型
# --------------------------------------------------------------------------- #

class LikedTableModel(QAbstractTableModel):
    """红心歌单的表格数据。

    用 model/view 而不是 ``QTableWidget``：上千首歌时不至于为每首歌创建六个
    item 控件；过滤和高亮都通过 ``data()`` 返回的角色完成，不需要碰控件本身。
    """

    HEADERS = ("#", "歌名", "歌手", "专辑", "时长", "本地")
    COL_INDEX, COL_NAME, COL_ARTIST, COL_ALBUM, COL_DURATION, COL_LOCAL = range(6)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._songs: list[Song] = []
        self._playing_id: int | None = None
        self._paused = False
        self._downloaded: set[int] = set()
        self._downloaded_dir = ""

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
        """只更新受影响的两行，不整表刷新。"""
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
        """播放/暂停切换只影响当前那一行的第 0 列（▶ / ❚❚）。"""
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        row = self.row_of(self._playing_id)
        if row is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

    def marker(self, song_id: int | None) -> str:
        """第 0 列播放标记：正在出声是 ❚❚，暂停/停止是 ▶。"""
        if song_id is None or song_id != self._playing_id:
            return ""
        # ⏸（U+23F8）在不少字体里只有两三个像素高，表格里几乎看不见；
        # ❚ 的竖条高度跟着字号走，和 ▶ 大小一致
        return "▶" if self._paused else "❚❚"

    def set_downloaded(self, ids: Iterable[int], directory: str = "") -> None:
        self._downloaded = set(ids)
        self._downloaded_dir = directory
        if self._songs:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self._songs) - 1, self.columnCount() - 1)
            )

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
            return self.HEADERS[section]
        return None

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
                return song.name
            if column == self.COL_ARTIST:
                return ", ".join(song.artists) or "—"
            if column == self.COL_ALBUM:
                return song.album or "—"
            if column == self.COL_DURATION:
                return song.duration_text
            if column == self.COL_LOCAL:
                return "✓" if song.id in self._downloaded else ""
            return None

        if role == Qt.ItemDataRole.BackgroundRole and playing:
            return QColor(0, 229, 255, 45)

        if role == Qt.ItemDataRole.ForegroundRole:
            if playing:
                return QColor(theme.CYAN)
            if column == self.COL_INDEX:
                return QColor(theme.TEXT_DIM)
            if column == self.COL_LOCAL and song.id in self._downloaded:
                return QColor(theme.GREEN)

        if role == Qt.ItemDataRole.FontRole and playing:
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            self.COL_INDEX,
            self.COL_DURATION,
            self.COL_LOCAL,
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


class LikedFilterProxy(QSortFilterProxyModel):
    """按搜索框过滤。多个词要求全部出现，见 :func:`ncmgui.player.matches_query`。"""

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
        song = model.song_at(source_row) if isinstance(model, LikedTableModel) else None
        if song is None:
            return False
        return player.matches_query(song.search_text, self._query)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        # 序号列显示**过滤后**的连续编号；▶ / ⏸ 仍然跟随真正在播的那首
        if (
            role == Qt.ItemDataRole.DisplayRole
            and index.isValid()
            and index.column() == LikedTableModel.COL_INDEX
        ):
            source = self.mapToSource(index)
            model = self.sourceModel()
            song = model.song_at(source.row()) if isinstance(model, LikedTableModel) else None
            if song is not None:
                marker = model.marker(song.id)
                if marker:
                    return marker
            return str(index.row() + 1)
        return super().data(index, role)


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

class LikedPage(BasePage):
    PAGE_TITLE = "我的喜欢"
    PAGE_SUBTITLE = (
        "喜欢的歌曲与 MV。歌曲点一首即可流播，已下载的直接播本地文件；"
        "MV 双击在应用内播放。"
    )

    #: 表格自己滚动，不能再套 QScrollArea
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def build(self) -> None:
        self._songs: list[Song] = []
        self._local_index: dict[str, list[Path]] = {}
        self._downloaded_dir = ""

        self._load_generation = 0
        self._load_task: BackgroundTask | None = None
        self._ever_shown = False
        self._download_watch: QTimer | None = None

        self.playback = self.window.playback

        # MV 标签页的状态
        self._mv_items: list[Mv] = []
        self._mv_rows: list[Mv] = []
        self._mv_task: BackgroundTask | None = None
        self._mv_url_task: BackgroundTask | None = None
        self._mv_download_task: BackgroundTask | None = None

        # 标题由 BasePage 统一加（PAGE_TITLE 非空），这里不再重复
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self._build_music_tab()
        self._build_mv_tab()
        self.content.addWidget(self.tabs, 1)

        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search)

        if self.is_logged_in():
            self.status_label.setText("点「刷新」或切换到本页后加载红心歌单。")
        else:
            self.status_label.setText("登录后这里会显示你喜欢的音乐。")

    # ---------------------------------------------------------------- 构建

    def _build_music_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        self._music_layout = layout
        self._build_toolbar()
        self._build_table()
        self.tabs.addTab(page, "音乐")

    def _build_toolbar(self) -> None:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌名 / 歌手 / 专辑")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        layout.addWidget(self.search, 1)

        # 红心页保留自己的档位：从这一页开始的队列按它取链，发现、搜索等
        # 页面则统一用「设置 → 在线音质」。留空表示跟随全局设置。
        self.level_combo = QComboBox()
        for label, value in ncmctl.DOWNLOAD_LEVELS:
            self.level_combo.addItem(label, value)
        set_combo_data(self.level_combo, self._liked_level())
        self.level_combo.setToolTip(
            "红心页专属音质：只影响从本页开始的播放。发现、搜索等页面用"
            "「设置 → 在线音质」。改动对下一首生效。"
        )
        layout.addWidget(self.level_combo)

        self.refresh_btn = ghost_button("刷新")
        self.refresh_btn.clicked.connect(self._reload)
        layout.addWidget(self.refresh_btn)

        self._music_layout.addWidget(bar)

    def _build_table(self) -> None:
        self.model = LikedTableModel(self)
        self.proxy = LikedFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView()
        self.table.setModel(self.proxy)
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
        header.setSectionResizeMode(LikedTableModel.COL_INDEX, header.ResizeMode.Fixed)
        header.setSectionResizeMode(LikedTableModel.COL_NAME, header.ResizeMode.Stretch)
        header.setSectionResizeMode(LikedTableModel.COL_ARTIST, header.ResizeMode.Interactive)
        header.setSectionResizeMode(LikedTableModel.COL_ALBUM, header.ResizeMode.Interactive)
        header.setSectionResizeMode(LikedTableModel.COL_DURATION, header.ResizeMode.Fixed)
        header.setSectionResizeMode(LikedTableModel.COL_LOCAL, header.ResizeMode.Fixed)
        self.table.setItemDelegateForColumn(
            LikedTableModel.COL_INDEX, PlayMarkerDelegate(self.table)
        )
        self.table.setColumnWidth(LikedTableModel.COL_INDEX, 46)
        self.table.setColumnWidth(LikedTableModel.COL_ARTIST, 170)
        self.table.setColumnWidth(LikedTableModel.COL_ALBUM, 190)
        self.table.setColumnWidth(LikedTableModel.COL_DURATION, 66)
        self.table.setColumnWidth(LikedTableModel.COL_LOCAL, 56)

        self._music_layout.addWidget(self.table, 1)

        # 状态、统计与本地音乐入口放在表格下方：工具栏已经被搜索框占满
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")

        self.local_btn = ghost_button("本地音乐…", compact=True)
        self.local_btn.setToolTip("从本地文件播放。文件对话框默认打开程序下载目录。")
        self.local_btn.clicked.connect(self._pick_local)

        self.local_dir_btn = ghost_button("播放下载目录", compact=True)
        self.local_dir_btn.setToolTip("把下载目录里的音频全部加入播放队列。")
        self.local_dir_btn.clicked.connect(self._play_download_dir)

        self.download_btn = ghost_button("下载全部", compact=True)
        self.download_btn.setToolTip(
            "按「下载」页的设置下载缺少的红心歌曲；下载完成一首就标一个 ✓。")
        self.download_btn.clicked.connect(self._download_all)

        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.count_label)
        status_row.addWidget(self.download_btn)
        status_row.addWidget(self.local_btn)
        status_row.addWidget(self.local_dir_btn)
        self._music_layout.addLayout(status_row)

    # ---------------------------------------------------------------- MV

    def _build_mv_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.mv_search = QLineEdit()
        self.mv_search.setPlaceholderText("搜索 MV / 歌手")
        self.mv_search.setClearButtonEnabled(True)
        self.mv_search.textChanged.connect(lambda _text: self._filter_mvs())
        row.addWidget(self.mv_search, 1)

        self.mv_refresh_btn = ghost_button("刷新", compact=True)
        self.mv_refresh_btn.clicked.connect(self._reload_mv)
        row.addWidget(self.mv_refresh_btn)

        self.mv_download_btn = ghost_button("下载全部", compact=True)
        self.mv_download_btn.setToolTip(
            "下载收藏的 MV 到「下载与云盘 → 下载」里设置的 MV 输出目录。")
        self.mv_download_btn.clicked.connect(self._download_mvs)
        row.addWidget(self.mv_download_btn)
        layout.addLayout(row)

        self.mv_status = QLabel("")
        self.mv_status.setObjectName("Hint")
        self.mv_status.setWordWrap(True)
        layout.addWidget(self.mv_status)

        self.mv_table = QTableWidget(0, 3)
        self.mv_table.setHorizontalHeaderLabels(["MV", "歌手", "时长"])
        self.mv_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.mv_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mv_table.setShowGrid(False)
        self.mv_table.setAlternatingRowColors(True)
        self.mv_table.verticalHeader().setVisible(False)
        self.mv_table.verticalHeader().setDefaultSectionSize(32)
        self.mv_table.setWordWrap(False)
        self.mv_table.cellDoubleClicked.connect(self._play_mv_row)
        header = self.mv_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.mv_table.setColumnWidth(1, 180)
        self.mv_table.setColumnWidth(2, 70)
        layout.addWidget(self.mv_table, 1)
        self.tabs.addTab(page, "MV")

    def _reload_mv(self) -> None:
        mode = str(self.cfg.get("mv.favorite_mode", "local") or "local")
        if mode == "official":
            if not self.is_logged_in():
                self._show_mvs([], "登录后这里会显示你在官方账号收藏的 MV。")
                return
            if self._mv_task is not None and self._mv_task.is_running:
                return
            self.mv_status.setText("正在加载收藏的 MV…")
            task = BackgroundTask(
                lambda: self.window.client.mv_sublist(200), parent=self)
            self._mv_task = task
            task.done.connect(lambda items: self._show_mvs(items))
            task.failed.connect(
                lambda message: self.mv_status.setText(f"加载失败：{message}"))
            task.start()
            return
        self._show_mvs(favorites(self.window.library, 500))

    def _show_mvs(self, items, message: str = "") -> None:
        self._mv_items = [item for item in items if isinstance(item, Mv)]
        if not self._mv_items and message:
            self.mv_status.setText(message)
        self._filter_mvs()
        if message and self._mv_items:
            self.mv_status.setText(message)

    def _filter_mvs(self) -> None:
        query = self.mv_search.text().strip().casefold()
        rows = [
            mv for mv in self._mv_items
            if not query or query in mv.name.casefold()
            or query in mv.artist.casefold()
        ]
        self._mv_rows = rows
        self.mv_table.setRowCount(len(rows))
        for row, mv in enumerate(rows):
            values = (mv.name, mv.artist or "—", mv.duration_text or "—")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 2:
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, mv)
                self.mv_table.setItem(row, column, item)
        if rows:
            self.mv_status.setText(f"{len(rows)} 支 MV")
        else:
            self.mv_status.setText(
                "没有匹配的 MV。" if query else "还没有收藏的 MV。")

    def _play_mv_row(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self._mv_rows)):
            return
        mv = self._mv_rows[row]
        if self._mv_url_task is not None and self._mv_url_task.is_running:
            self._mv_url_task.cancel()
        self.mv_status.setText(f"正在获取《{mv.name}》的播放地址…")
        task = BackgroundTask(
            lambda: self.window.client.mv_url(mv.id), parent=self)
        self._mv_url_task = task

        def done(result) -> None:
            url, _fee = result
            self.window.open_mv(mv, url)

        def failed(message: str) -> None:
            self.mv_status.setText(f"无法播放：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _download_mvs(self) -> None:
        """下载收藏的全部 MV，落到「下载」页设置的 MV 输出目录。"""
        task = self._mv_download_task
        if task is not None and task.is_running:
            task.cancel()
            self.mv_status.setText("正在取消下载…")
            return
        if not self._mv_items:
            self.mv_status.setText("还没有收藏的 MV。")
            return
        raw = str(self.cfg.get("download.mv_output", "") or "").strip()
        directory = Path(raw).expanduser() if raw else default_video_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.mv_status.setText(f"无法创建 MV 下载目录：{exc}")
            return
        items = list(self._mv_items)
        client = self.window.client
        self.mv_download_btn.setEnabled(False)
        self.mv_status.setText(f"准备下载 {len(items)} 支 MV…")
        task = BackgroundTask(
            lambda: download_all(
                client, items, directory,
                progress=task.progress.emit,
                cancelled=task.cancel_event.is_set,
            ),
            parent=self,
        )
        self._mv_download_task = task

        def done(result) -> None:
            downloaded, skipped = result
            self.mv_download_btn.setEnabled(True)
            parts = [f"已下载 {downloaded} 支"]
            if skipped:
                parts.append(f"跳过 {skipped} 支已存在")
            self.mv_status.setText("，".join(parts) + f"，目录：{directory}")

        def failed(message: str) -> None:
            cancelled = task is not None and task.cancel_event.is_set()
            self.mv_download_btn.setEnabled(True)
            self.mv_status.setText(
                "已取消下载" if cancelled else f"下载失败：{message}")

        task.progress.connect(self.mv_status.setText)
        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        self._ever_shown = True
        self._ensure_loaded()
        self._reload_mv()

    def _ensure_loaded(self) -> None:
        if not self.is_logged_in():
            return
        if self._load_task is not None and self._load_task.is_running:
            return
        if self._songs:
            self._refresh_downloaded()
            return
        self._reload()

    def _reload(self) -> None:
        if not self.require_login("读取红心歌单"):
            return
        if self._load_task is not None and self._load_task.is_running:
            return

        self._load_generation += 1
        generation = self._load_generation
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("正在读取红心歌单…")

        holder: dict[str, BackgroundTask] = {}

        def work():
            # 直接走程序内的接口客户端：ncmctl 子进程是另一套 TLS 指纹，
            # 和搜索/详情页的请求混在一起反而更显眼，也白多一次进程开销
            client = self.window.client
            playlist = client.liked_playlist()
            holder["task"].progress.emit(f"正在读取「{playlist.name}」…")
            songs = client.playlist_tracks_all(playlist.id)
            if not songs:
                raise RuntimeError("红心歌单里没有可用的歌曲（曲目可能都已下架）")
            directory = str(Path(self.cfg.get("download.output", "")).expanduser())
            index = index_audio(directory) if directory else {}
            downloaded = {song.id for song in songs if song.base in index}
            return songs, downloaded, index, directory

        task = BackgroundTask(work, parent=self)
        holder["task"] = task
        self._load_task = task
        task.progress.connect(self.status_label.setText)
        task.done.connect(lambda data: self._on_loaded(data, generation))
        task.failed.connect(lambda message: self._on_load_failed(message, generation))
        task.start()

    def _on_loaded(self, data, generation: int) -> None:
        if generation != self._load_generation or not self.is_logged_in():
            return
        songs, downloaded, index, directory = data
        # 正在播的是这份红心歌单里的歌 → 让 controller 按 ID 重新定位；
        # 正在播本地音乐（id 为负）时不要碰队列，刷新列表不该打断它
        previous_ids = {song.id for song in self._songs}
        current = self.playback.current
        was_liked_queue = current is not None and current.id in previous_ids

        self._songs = list(songs)
        self._local_index = dict(index)
        self._downloaded_dir = directory

        self.model.set_songs(self._songs)
        self.model.set_downloaded(downloaded, directory)
        self._save_liked_order()
        self._sync_count(len(songs), len(downloaded))
        self.refresh_btn.setEnabled(True)
        self.status_label.setText("")

        if was_liked_queue:
            self.playback.set_queue(self._songs)

    def _save_liked_order(self) -> None:
        """把当前红心顺序整体写进曲库。

        本地曲库的「喜欢顺序」排序只读这张表，不再为了排序请求接口。
        红心页每次真正加载（首次打开或手动刷新）后写入；点红心则由
        :meth:`SongActions._sync_liked_order` 增量同步，避免拿页面上的
        过期列表整表覆盖。
        """
        if not self._songs:
            return
        try:
            self.window.library.replace_liked_order(
                self.window.client.uid, [song.base for song in self._songs]
            )
        except Exception as exc:  # noqa: BLE001 - 排序缓存失败不该让页面崩溃
            # Qt 槽函数里未捕获的异常会让整个进程退出（PyQt6 默认行为），
            # 这只是个排序缓存，写失败最多是「喜欢顺序」排序退化
            self.status_label.setText(f"收藏顺序缓存写入失败：{exc}")

    def _on_load_failed(self, message: str, generation: int) -> None:
        if generation != self._load_generation:
            return
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(f"读取失败：{message}")
        self.toast(f"读取红心歌单失败：{message}")

    def _download_all(self) -> None:
        """在红心页直接下载缺少的歌曲，复用「下载」页的解析与查重。"""
        if not self._songs:
            self.warn("红心歌单还没有加载完成。")
            return
        page = self.window.pages.get("download")
        if page is None:
            return
        if not self.is_logged_in():
            self.require_login("下载红心歌曲")
            return
        page._download_liked()
        self._start_download_watch()

    def _start_download_watch(self) -> None:
        """下载期间定时重扫目录：每完成一首就多一个 ✓。"""
        if self._download_watch is None:
            self._download_watch = QTimer(self)
            self._download_watch.setInterval(1500)
            self._download_watch.timeout.connect(self._poll_downloads)
        self._download_watch.start()
        self._poll_downloads()

    def _poll_downloads(self) -> None:
        self._refresh_downloaded()
        page = self.window.pages.get("download")
        if page is None or not page.is_downloading():
            if self._download_watch is not None:
                self._download_watch.stop()

    def _refresh_downloaded(self) -> None:
        """重新扫一遍下载目录，不动列表本身。"""
        directory = str(Path(self.cfg.get("download.output", "")).expanduser())
        index = index_audio(directory) if directory else {}
        self._local_index = index
        self._downloaded_dir = directory
        downloaded = {song.id for song in self._songs if song.base in index}
        self.model.set_downloaded(downloaded, directory)
        self._sync_count(len(self._songs), len(downloaded))

    def _sync_count(self, total: int, downloaded: int) -> None:
        if total:
            self.count_label.setText(f"共 {total} 首 · 已下载 {downloaded} 首")
        else:
            self.count_label.setText("")

    def _apply_search(self) -> None:
        self.proxy.set_query(self.search.text())
        self._keep_playing_visible()

    def _keep_playing_visible(self) -> None:
        current = self.playback.current
        row = self.model.row_of(current.id) if current is not None else None
        if row is None:
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.table.selectRow(index.row())
            self.table.scrollTo(
                index, QAbstractItemView.ScrollHint.PositionAtCenter
            )

    # ---------------------------------------------------------------- 播放

    def _on_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        if not self.require_login("播放"):
            return
        source = self.proxy.mapToSource(index)
        row = source.row()
        song = self.model.song_at(row)
        if song is None:
            return

        songs = self.model.songs()
        # 从红心页开始的队列用自己的档位；换成别的页面的队列时再切回全局。
        self.playback.set_level_scope("liked")
        self.playback.set_level(self._liked_level())
        # 队列不是这份红心歌单（例如刚才在播本地音乐）时，先换回来
        if [item.id for item in self.playback.queue] != [item.id for item in songs]:
            self.playback.set_queue(songs, row)
        self.playback.play_at(row)
        self.window.open_player_page()

    def _on_track_changed(self, song) -> None:
        song_id = getattr(song, "id", None)
        self.model.set_playing(song_id)
        self._keep_playing_visible()

    def _on_state_changed(self, state: str) -> None:
        # 「播放中」之外（暂停、停止、缓冲、出错）第 0 列都显示 ▶，
        # 因为点一下就是「开始出声」
        self.model.set_paused(state != STATE_PLAYING)

    # ---------------------------------------------------------------- 音质

    def _liked_level(self) -> str:
        """红心页自己的档位；没设置过就跟随「设置 → 在线音质」。"""
        value = str(self.cfg.get("player.liked_level", "") or "")
        return value or str(self.cfg.get("player.level", "exhigh") or "exhigh")

    def _on_level_changed(self, _index: int) -> None:
        value = combo_data(self.level_combo, "exhigh")
        self.cfg.set("player.liked_level", value)
        # 只有当前队列本来就属于红心页时才立刻换档；正在从发现页播的队列
        # 不应该被这里的改动带跑。
        if self.playback.level_scope == "liked":
            self.playback.set_level(value)
        try:
            self.cfg.save()
        except OSError:
            pass

    def local_path(self, song: Song) -> Path | None:
        """给 controller 用：这首歌在下载目录里有对应的文件吗。"""
        paths = self._local_index.get(song.base) or []
        return paths[0] if paths else None

    # ------------------------------------------------------------ 本地音乐

    def _pick_local(self) -> None:
        start = localmusic.default_directory(self.cfg)
        patterns = " ".join(f"*{suffix}" for suffix in sorted(AUDIO_SUFFIXES))
        files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "选择本地音乐",
            str(start),
            f"音频文件 ({patterns});;所有文件 (*)",
        )
        if not files:
            return
        localmusic.remember_directory(self.cfg, files[0])
        songs = localmusic.songs_from_paths(files)
        if not songs:
            self.toast("选中的文件不是可播放的音频。")
            return
        self._play_local_queue(songs)

    def _play_download_dir(self) -> None:
        directory = localmusic.default_directory(self.cfg)
        songs = localmusic.scan(directory)
        if not songs:
            self.toast(f"{directory} 里没有找到音频文件。")
            return
        self._play_local_queue(songs)

    def _play_local_queue(self, songs: Sequence[Song]) -> None:
        if not songs:
            return
        self.playback.set_queue(songs, 0)
        self.playback.play_at(0)
        self.window.open_player_page()
        self.toast(f"开始播放本地音乐：{songs[0].display}（共 {len(songs)} 首）")

    # ---------------------------------------------------------------- 登录

    def on_login_changed(self, logged_in: bool) -> None:
        # 丢掉在途结果：换账号后不能把上一个人的歌单填回来
        self._load_generation += 1

        if logged_in:
            if self._ever_shown:
                self._reload()
            return

        self.playback.clear_queue()
        self._songs = []
        self._local_index = {}
        self.model.set_songs([])
        self.model.set_playing(None)
        self.count_label.setText("")
        self.status_label.setText("登录后这里会显示你喜欢的音乐。")
        self.refresh_btn.setEnabled(True)

    # ---------------------------------------------------------------- 收尾

    def shutdown(self) -> None:
        # 播放器由 MainWindow 的 playback.shutdown() 统一收尾
        self._load_generation += 1
