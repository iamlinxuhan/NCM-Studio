"""「我喜欢的音乐」：红心歌单 + 流媒体播放。

列表来自账号的红心歌单（``specialType == 5``），点一首歌就交给随包的 mpv 播放。
播放地址由 ``ncmctl`` 调 ``SongDownloadUrlV1`` 取，默认只缓存到接口给的
``expi`` 前 60 秒；本地已经下载过的歌直接播本地文件，不浪费一次接口调用。

几条容易踩的坑，都在这里处理掉了：

* **``loadfile replace`` 会给旧曲目发 ``end-file: stop``**，不能当成「播完了」去
  自动下一首，否则点新歌会顺带跳到再下一首；
* 旧曲目恰好自然播完时，``eof`` 可能在新曲目的 ``file-loaded`` 之前到达，靠
  ``_awaiting_load`` 丢弃这种过期事件；
* 快速连点只保留「一个在飞 + 最新的一个待取」，不会为每次点击都起一个 ncmctl。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Sequence

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import ncmctl, player, theme
from ..lyrics import index_audio
from ..resolver import Resolver, Song
from ..widgets.card import Card, ghost_button
from ..widgets.form import combo_data, set_combo_data
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
                return "▶" if playing else str(index.row() + 1)
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
        # 序号列显示**过滤后**的连续编号；▶ 仍然跟随真正在播的那首
        if (
            role == Qt.ItemDataRole.DisplayRole
            and index.isValid()
            and index.column() == LikedTableModel.COL_INDEX
        ):
            source = self.mapToSource(index)
            model = self.sourceModel()
            song = model.song_at(source.row()) if isinstance(model, LikedTableModel) else None
            if song is not None and song.id == model.playing_id:
                return "▶"
            return str(index.row() + 1)
        return super().data(index, role)


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

class LikedPage(BasePage):
    PAGE_TITLE = "我喜欢的音乐"
    PAGE_SUBTITLE = "红心歌单。点一首歌即可流播 —— 已下载的直接播本地文件。"

    #: 表格自己滚动，不能再套 QScrollArea
    SCROLLABLE = False
    TRAILING_STRETCH = False

    #: mpv 事件从读线程发回来，走队列连接
    playerEvent = pyqtSignal(str, object)
    playerExited = pyqtSignal(int)
    playerFailed = pyqtSignal(str)

    def build(self) -> None:
        self._songs: list[Song] = []
        self._local_index: dict[str, list[Path]] = {}
        self._downloaded_dir = ""

        self._load_generation = 0
        self._load_task: BackgroundTask | None = None

        #: 取链的「最新一次请求」，配合 _play_token 丢弃过期结果
        self._play_token = 0
        self._wanted: tuple[int, Song, str] | None = None
        self._fetch_task: BackgroundTask | None = None

        self._player: player.MpvPlayer | None = None
        self._playing_id: int | None = None
        self._playing_song: Song | None = None
        self._position = 0.0
        self._duration = 0.0
        self._paused = False
        self._scrubbing = False
        self._awaiting_load = False
        self._volume = player.clamp_volume(self.cfg.get("player.volume", 70))
        self._cache = player.UrlCache()
        self._ever_shown = False
        self._shutting_down = False

        # 标题由 BasePage 统一加（PAGE_TITLE 非空），这里不再重复
        self._build_toolbar()
        self._build_table()
        self._build_controls()

        self.playerEvent.connect(self._on_player_event)
        self.playerExited.connect(self._on_player_exited)
        self.playerFailed.connect(self._on_player_failed)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search)

        self._set_idle_ui()
        if self.is_logged_in():
            self.status_label.setText("点「刷新」或切换到本页后加载红心歌单。")
        else:
            self.status_label.setText("登录后这里会显示你喜欢的音乐。")

    # ---------------------------------------------------------------- 构建

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

        self.level_combo = QComboBox()
        for label, value in ncmctl.DOWNLOAD_LEVELS:
            self.level_combo.addItem(label, value)
        set_combo_data(self.level_combo, self.cfg.get("player.level", "exhigh"))
        self.level_combo.setToolTip("流播音质。改动对下一首生效，不影响正在播放的这一首。")
        layout.addWidget(self.level_combo)

        self.refresh_btn = ghost_button("刷新")
        self.refresh_btn.clicked.connect(self._reload)
        layout.addWidget(self.refresh_btn)

        self.content.addWidget(bar)

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
        self.table.setColumnWidth(LikedTableModel.COL_INDEX, 46)
        self.table.setColumnWidth(LikedTableModel.COL_ARTIST, 170)
        self.table.setColumnWidth(LikedTableModel.COL_ALBUM, 190)
        self.table.setColumnWidth(LikedTableModel.COL_DURATION, 66)
        self.table.setColumnWidth(LikedTableModel.COL_LOCAL, 56)

        self.content.addWidget(self.table, 1)

        # 状态与统计放在表格下方单独一行：工具栏已经被搜索框占满，挤在一起
        # 只会把「共 N 首」截成半个字
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.count_label)
        self.content.addLayout(status_row)

    def _build_controls(self) -> None:
        card = Card()

        # 第一行：当前曲目 + 状态
        top = QHBoxLayout()
        top.setSpacing(10)
        self.now_label = QLabel("还没有播放")
        self.now_label.setObjectName("CardTitle")
        self.now_label.setWordWrap(True)
        self.state_label = QLabel("")
        self.state_label.setObjectName("Hint")
        top.addWidget(self.now_label, 1)
        top.addWidget(self.state_label)
        card.add(top)

        # 第二行：时间 + 进度条
        middle = QHBoxLayout()
        middle.setSpacing(10)
        self.pos_label = QLabel("--:--")
        self.pos_label.setObjectName("Hint")
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setEnabled(False)
        self.progress.sliderPressed.connect(self._on_scrub_start)
        self.progress.sliderReleased.connect(self._on_scrub_end)
        self.total_label = QLabel("--:--")
        self.total_label.setObjectName("Hint")
        middle.addWidget(self.pos_label)
        middle.addWidget(self.progress, 1)
        middle.addWidget(self.total_label)
        card.add(middle)

        # 第三行：传输控制 + 音量
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.prev_btn = ghost_button("上一首")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.play_btn = ghost_button("播放")
        self.play_btn.setMinimumWidth(88)
        self.play_btn.clicked.connect(self._toggle_play)
        self.next_btn = ghost_button("下一首")
        self.next_btn.clicked.connect(lambda: self._step(1))
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.play_btn)
        bottom.addWidget(self.next_btn)

        bottom.addStretch(1)
        volume_label = QLabel("音量")
        volume_label.setObjectName("Hint")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(140)
        self.volume.setValue(self._volume)
        self.volume.valueChanged.connect(self._on_volume)
        self.volume_label = QLabel(f"{self._volume}%")
        self.volume_label.setObjectName("Hint")
        self.volume_label.setMinimumWidth(38)
        bottom.addWidget(volume_label)
        bottom.addWidget(self.volume)
        bottom.addWidget(self.volume_label)
        card.add(bottom)

        self.content.addWidget(card)

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        self._ever_shown = True
        self._ensure_loaded()

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
            resolver = Resolver(self.window.binary, opts=self.opts(), timeout="20s")
            resolver.progress = lambda message: holder["task"].progress.emit(message)
            songs = resolver.liked_songs()
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
        self._songs = list(songs)
        self._local_index = dict(index)
        self._downloaded_dir = directory

        self.model.set_songs(self._songs)
        self.model.set_downloaded(downloaded, directory)
        self._sync_count(len(songs), len(downloaded))
        self.refresh_btn.setEnabled(True)
        self.status_label.setText("")

        # 正在播的歌已经不在新列表里了（换了账号、或红心被取消）：
        # 光清高亮不够，那首歌还在响，得连播放一起停掉
        if self._playing_id is not None and self.model.row_of(self._playing_id) is None:
            self._stop_playback()

    def _on_load_failed(self, message: str, generation: int) -> None:
        if generation != self._load_generation:
            return
        self.refresh_btn.setEnabled(True)
        self.status_label.setText(f"读取失败：{message}")
        self.toast(f"读取红心歌单失败：{message}")

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
        row = self.model.row_of(self._playing_id)
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
        song = self.model.song_at(source.row())
        if song is not None:
            self._play_song(song)

    def _play_song(self, song: Song) -> None:
        level = combo_data(self.level_combo, "exhigh")

        # 本地已经下过的，直接播本地文件：省一次接口调用，也不受直链过期影响
        local = self._local_path(song)
        if local is not None:
            self._start_playback(song, str(local))
            return

        if player.find_mpv(self.cfg.get("player.mpv_path", "")) is None:
            self._report_missing_mpv()
            return

        cached = self._cache.get((song.id, level))
        if cached is not None:
            self._start_playback(song, cached.url, source=cached)
            return

        self._play_token += 1
        self._wanted = (self._play_token, song, level)
        self.state_label.setText(f"正在取播放地址…（{song.name}）")
        self._pump_wanted()

    def _pump_wanted(self) -> None:
        """保证同时只有一个取链任务在飞；最新的那个排队等它完成。"""
        if self._fetch_task is not None and self._fetch_task.is_running:
            return
        wanted = self._wanted
        if wanted is None:
            return
        token, song, level = wanted

        def work():
            resolver = Resolver(self.window.binary, opts=self.opts(), timeout="20s")
            return resolver.song_url(song.id, level)

        task = BackgroundTask(work, parent=self)
        self._fetch_task = task
        task.done.connect(lambda source, t=token: self._on_url(t, source, ""))
        task.failed.connect(lambda message, t=token: self._on_url(t, None, message))
        task.start()

    def _on_url(self, token: int, source, error: str) -> None:
        self._fetch_task = None
        wanted = self._wanted

        # 用户已经点了别的歌：这次结果作废，接着去取最新的那首
        if wanted is None or token != wanted[0]:
            self._pump_wanted()
            return

        self._wanted = None
        _, song, level = wanted

        if source is None:
            self._on_url_failed(song, error)
            return

        self._cache.put((song.id, level), source)
        self._start_playback(song, source.url, source=source)

    def _on_url_failed(self, song: Song, message: str) -> None:
        self.state_label.setText("")
        self._toast_error(f"《{song.name}》无法播放：{message}")
        if "登录" in message:
            # 凭据过期了：顺手刷新一下账号页的状态，让用户看到「已登出」
            self.window.refresh_login()

    def _start_playback(self, song: Song, target: str, source=None) -> None:
        # 本地文件这条路不经过 _play_song 的预检查，这里统一兜一次
        if player.find_mpv(self.cfg.get("player.mpv_path", "")) is None:
            self._report_missing_mpv()
            return

        self._playing_song = song
        self._playing_id = song.id
        self._position = 0.0
        self._duration = song.duration / 1000 if song.duration else 0.0
        self._awaiting_load = True
        self._paused = False

        self.model.set_playing(song.id)
        self._keep_playing_visible()
        self.now_label.setText(song.display)
        self.state_label.setText("正在缓冲…")
        self.play_btn.setText("暂停")
        self.progress.setEnabled(True)
        self._update_progress_ui()

        if source is not None and source.trial:
            self._toast_error(
                f"《{song.name}》只是试听片段 —— 完整版需要会员。"
            )

        try:
            mpv = self._ensure_player()
            mpv.load(target)
        except player.PlayerError as exc:
            self._on_player_failed(str(exc))

    def _local_path(self, song: Song) -> Path | None:
        paths = self._local_index.get(song.base) or []
        return paths[0] if paths else None

    def _ensure_player(self) -> player.MpvPlayer:
        if self._player is not None and self._player.running:
            return self._player

        binary = player.find_mpv(self.cfg.get("player.mpv_path", ""))
        if binary is None:
            raise player.MpvNotFound(
                "找不到 mpv。Linux / Windows 的发行包已内置；"
                "macOS 首版需要先 brew install mpv，或到「设置」页指定路径。"
            )

        self.state_label.setText("正在启动播放器…")
        mpv = player.MpvPlayer(
            binary,
            on_event=self.playerEvent.emit,
            on_exit=self.playerExited.emit,
            on_error=self.playerFailed.emit,
            volume=self._volume,
        )
        mpv.start()
        self._player = mpv
        return mpv

    def _report_missing_mpv(self) -> None:
        if sys.platform == "darwin":
            hint = "macOS 首版不随包提供播放器。请先安装：<code>brew install mpv</code>，"
            hint += "再到「设置 → 播放器」里确认路径。"
        else:
            hint = (
                "没有找到 mpv。Linux 可执行 <code>sudo apt install mpv</code>；"
                "官方 AppImage / Windows 安装包已内置 mpv，若仍报错请在"
                "「设置 → 播放器」里指定路径。"
            )
        self.warn(f"<b>未找到播放器</b><br><br>{hint}", "无法播放")
        self.state_label.setText("未找到 mpv")

    def _toggle_play(self) -> None:
        if self._playing_id is None:
            songs = self.model.songs()
            if songs:
                self._play_song(songs[0])
            else:
                self.toast("列表还是空的，先刷新一下红心歌单。")
            return

        mpv = self._player
        if mpv is None or not mpv.running:
            # 播放器中途退出了：重新拉起并重放当前这首
            song = self._playing_song
            if song is not None:
                self._play_song(song)
            return

        self._paused = not self._paused
        try:
            mpv.pause(self._paused)
        except player.PlayerError as exc:
            self._on_player_failed(str(exc))
            return
        self._sync_play_button()

    def _step(self, delta: int) -> None:
        songs = self.model.songs()
        if not songs:
            return
        current = self.model.row_of(self._playing_id)
        target = player.step_index(current, len(songs), delta)
        if target is not None:
            self._play_song(songs[target])

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        mpv = self._player
        if mpv is None or not mpv.running:
            return
        try:
            mpv.seek(player.seek_seconds(self.progress.value(), self._duration))
        except player.PlayerError as exc:
            self._on_player_failed(str(exc))

    def _on_volume(self, value: int) -> None:
        self._volume = player.clamp_volume(value, self._volume)
        self.volume_label.setText(f"{self._volume}%")
        if self._player is not None and self._player.running:
            self._player.set_volume(self._volume)

    # ------------------------------------------------------------ mpv 事件

    def _on_player_event(self, kind: str, payload) -> None:
        if self._shutting_down:
            return
        data = payload or {}

        if kind == "file-loaded":
            self._awaiting_load = False
            self._paused = False
            # mpv 是用 --pause=yes 启动的（防止进程一拉起就自己出声），
            # 所以文件装载完必须显式解除暂停，否则第一首永远不动
            mpv = self._player
            if mpv is not None and mpv.running:
                try:
                    mpv.pause(False)
                except player.PlayerError:
                    pass
            self._sync_play_button()
            self.state_label.setText("正在播放")
            return

        if kind == "property":
            name = data.get("name")
            value = data.get("value")
            if name == "duration" and value:
                try:
                    self._duration = float(value)
                except (TypeError, ValueError):
                    pass
            elif name == "time-pos":
                try:
                    self._position = float(value) if value is not None else 0.0
                except (TypeError, ValueError):
                    self._position = 0.0
            elif name == "pause":
                self._paused = bool(value)
                self._sync_play_button()
            elif name == "volume" and value is not None:
                try:
                    self._volume = player.clamp_volume(value, self._volume)
                    self.volume.blockSignals(True)
                    self.volume.setValue(self._volume)
                    self.volume.blockSignals(False)
                    self.volume_label.setText(f"{self._volume}%")
                except (TypeError, ValueError):
                    pass
            return

        if kind == "end-file":
            reason = str(data.get("reason") or "")
            if reason == "eof":
                # 旧曲目被 replace 时可能先到 eof，丢给下一首就错位了
                if self._awaiting_load:
                    return
                QTimer.singleShot(0, self._auto_next)
            elif reason == "error":
                self._cache.invalidate(
                    (self._playing_id or 0, combo_data(self.level_combo, "exhigh"))
                )
                detail = str(data.get("error") or "未知错误")
                self.state_label.setText("播放失败")
                self._toast_error(
                    f"播放失败（{detail}），音频地址可能已过期，再点一次试试。"
                )
            # reason == "stop"：loadfile replace 的正常产物，忽略
            return

        if kind == "shutdown":
            self.state_label.setText("播放器已退出")

    def _auto_next(self) -> None:
        if self._shutting_down or self._awaiting_load:
            return
        self._step(1)

    def _on_player_exited(self, code: int) -> None:
        # 主动退出时 _shutting_down 为真；这里只处理意外退出
        if self._shutting_down:
            return
        self._player = None
        self.state_label.setText(f"播放器已退出（{code}），点播放可重新启动")
        self._sync_play_button()

    def _on_player_failed(self, message: str) -> None:
        if self._shutting_down:
            return
        self.state_label.setText("播放器出错")
        self._toast_error(message)

    # ---------------------------------------------------------------- 刷新

    def _tick(self) -> None:
        if self._scrubbing:
            return
        if self._playing_id is None:
            return
        self._update_progress_ui()

    def _update_progress_ui(self) -> None:
        self.progress.setValue(
            player.progress_value(self._position, self._duration or None)
        )
        self.pos_label.setText(player.format_clock(self._position))
        self.total_label.setText(
            player.format_clock(self._duration if self._duration else None)
        )

    def _sync_play_button(self) -> None:
        self.play_btn.setText("继续" if self._paused else "暂停")

    def _set_idle_ui(self) -> None:
        self.now_label.setText("还没有播放")
        self.state_label.setText("")
        self.play_btn.setText("播放")
        self.progress.setEnabled(False)
        self.progress.setValue(0)
        self.pos_label.setText("--:--")
        self.total_label.setText("--:--")

    def _clear_playing(self) -> None:
        self._playing_id = None
        self._playing_song = None
        self._position = 0.0
        self._duration = 0.0
        self.model.set_playing(None)
        self._set_idle_ui()

    def _stop_playback(self) -> None:
        if self._player is not None and self._player.running:
            try:
                self._player.stop_playback()
            except player.PlayerError:
                pass
        self._clear_playing()

    def _toast_error(self, message: str) -> None:
        self.toast(message)

    # ---------------------------------------------------------------- 登录

    def on_login_changed(self, logged_in: bool) -> None:
        # 丢掉所有在途结果：换账号后不能把上一个人的歌单/直链填回来
        self._load_generation += 1
        self._play_token += 1
        self._wanted = None
        self._cache.clear()

        if logged_in:
            if self._ever_shown:
                self._reload()
            return

        self._stop_playback()
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._songs = []
        self._local_index = {}
        self.model.set_songs([])
        self.model.set_playing(None)
        self.count_label.setText("")
        self.status_label.setText("登录后这里会显示你喜欢的音乐。")
        self.refresh_btn.setEnabled(True)

    # ---------------------------------------------------------------- 收尾

    def shutdown(self) -> None:
        self._shutting_down = True
        self._load_generation += 1
        self._play_token += 1
        self._wanted = None
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._clear_playing()

    def save_config(self) -> None:
        self.cfg.set("player.level", combo_data(self.level_combo, "exhigh"))
        self.cfg.set("player.volume", int(self._volume))
