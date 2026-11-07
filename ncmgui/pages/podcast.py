"""播客：推荐、我的播客，以及节目播放。

网页端的播客子系统（voicelist / djradio）和普通歌曲共用同一套播放接口：
每期节目的 ``mainSong`` 就是一首可播放的声音。本页负责浏览和挑节目，
播放交给全局播放器，所以倍速、音量、系统媒体控件都能直接用。

进度记忆按节目 ID 存在配置里：播客动辄一小时，下次进来从上次的位置
接着播比从头开始合理。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..api.models import Podcast, podcast_from_api
from ..widgets.card import accent_button, ghost_button
from ..widgets.covercard import CoverGrid
from ..workers import BackgroundTask
from .base import BasePage

#: 每页拉多少期。够播很久了，翻页反而打断收听
PROGRAM_PAGE = 50
#: 进度自动保存间隔（秒）
SAVE_INTERVAL = 5


class PodcastPage(BasePage):
    PAGE_TITLE = "播客"
    PAGE_SUBTITLE = "播客、有声书和电台节目。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._home_task: BackgroundTask | None = None
        self._mine_task: BackgroundTask | None = None
        self._home_loaded = False
        self._mine_loaded = False
        self._home: list[dict] = []
        self._mine: list[Podcast] = []
        self._episodes: dict[int, Podcast] = {}
        #: 当前打开过的节目歌曲 ID，用来判断要不要记录进度
        self._program_songs: set[int] = set()
        #: song_id -> 秒；持久化在配置 ``podcast.positions``
        self._positions: dict[str, float] = dict(
            window.cfg.get("podcast.positions", {}) or {})
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.status = QLabel("正在准备播客…")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status, 1)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(self._reload)
        toolbar.addWidget(self.refresh_button)
        self.content.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.home_grid, self.home_scroll = self._make_tab("推荐")
        self.mine_grid, self.mine_scroll = self._make_tab("我的播客")
        self.content.addWidget(self.tabs, 1)

        self.home_grid.itemClicked.connect(self._open_podcast)
        self.mine_grid.itemClicked.connect(self._open_podcast)

        self.playback = self.window.playback
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(SAVE_INTERVAL * 1000)
        self._save_timer.timeout.connect(self._save_position)
        self._save_timer.start()

    def _make_tab(self, title: str):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        grid = CoverGrid(self.window.image_store)
        scroll.setWidget(grid)
        layout.addWidget(scroll)
        self.tabs.addTab(page, title)
        return grid, scroll

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        if not self._home_loaded:
            self._load_home()
        if not self._mine_loaded and self.window.is_logged_in():
            self._load_mine()

    def _reload(self) -> None:
        self._home_loaded = False
        self._mine_loaded = False
        self._load_home()
        if self.window.is_logged_in():
            self._load_mine()

    def _load_home(self) -> None:
        if self._home_task is not None and self._home_task.is_running:
            self._home_task.cancel()
        self.status.setText("正在加载播客推荐…")
        client = self.window.client
        task = BackgroundTask(client.podcast_home, parent=self)
        self._home_task = task

        def done(blocks) -> None:
            self._home = blocks
            self._home_loaded = True
            self._render_home()
            self.status.setText("")

        def failed(message: str) -> None:
            self.status.setText(f"播客推荐加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _load_mine(self) -> None:
        if self._mine_task is not None and self._mine_task.is_running:
            self._mine_task.cancel()
        client = self.window.client
        task = BackgroundTask(client.my_podcasts, parent=self)
        self._mine_task = task

        def done(podcasts) -> None:
            self._mine = podcasts
            self._mine_loaded = True
            self.mine_grid.set_items([
                {"key": f"podcast:{item.id}", "title": item.name,
                 "subtitle": item.subtitle, "payload": item,
                 "url": item.cover_url}
                for item in podcasts
            ])
            self.tabs.setTabText(1, f"我的播客（{len(podcasts)}）")

        def failed(message: str) -> None:
            self.tabs.setTabText(1, "我的播客")
            self.status.setText(f"我的播客加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 渲染

    def _render_home(self) -> None:
        """把首页区块里的播客专辑平铺出来。

        网页端每个区块是「分类/推荐位 + 若干播客」，这里保留区块标题，
        只展示 voiceList（播客专辑）；单期节目和榜单留给详情页。
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)

        shown = 0
        for block in self._home:
            title = str((((block.get("uiElement") or {}).get("mainTitle"))
                         or {}).get("title") or "")
            items = []
            for creative in block.get("creatives") or []:
                if not isinstance(creative, dict):
                    continue
                if creative.get("creativeType") != "voiceList":
                    continue
                podcast = podcast_from_api(creative)
                if podcast is None:
                    continue
                items.append({
                    "key": f"podcast:{podcast.id}", "title": podcast.name,
                    "subtitle": podcast.subtitle, "payload": podcast,
                    "url": podcast.cover_url,
                })
            if not items:
                continue
            if title:
                label = QLabel(title)
                label.setObjectName("CardTitle")
                layout.addWidget(label)
            grid = CoverGrid(self.window.image_store, cover_size=132)
            grid.set_items(items)
            grid.itemClicked.connect(self._open_podcast)
            layout.addWidget(grid)
            shown += len(items)

        if not shown:
            layout.addWidget(QLabel("暂时没有可展示的播客推荐。"))
        layout.addStretch(1)
        self.home_scroll.setWidget(container)

    # ---------------------------------------------------------------- 播放

    def _open_podcast(self, payload) -> None:
        podcast = payload if isinstance(payload, Podcast) else None
        if podcast is None:
            return
        PodcastDialog(self.window, podcast, self).exec()

    def play_programs(self, podcast: Podcast, programs, start: int) -> None:
        """把一整列节目交给播放器，从第 ``start`` 期开始。"""
        if not programs:
            return
        songs = [program.song for program in programs]
        for program in programs:
            self._program_songs.add(int(program.song.id))
        start = max(0, min(int(start), len(songs) - 1))
        self.window.actions.play(songs[start], songs)
        song_id = int(songs[start].id)
        resume = self._positions.get(str(song_id))
        if resume and resume > 5:
            def restore():
                current = self.playback.current
                if current is not None and int(current.id) == song_id:
                    self.playback.seek(float(resume))
            # 等播放器真正拿到时长再 seek，否则会被当成片头的 0
            QTimer.singleShot(1500, restore)

    def _save_position(self) -> None:
        song = self.playback.current
        if song is None:
            return
        song_id = int(getattr(song, "id", 0) or 0)
        if song_id not in self._program_songs:
            return
        position = float(self.playback.position or 0)
        duration = float(self.playback.duration or 0)
        if position < 5:
            return
        # 快放完就不记了，下次从头听
        if duration and position >= duration - 5:
            self._positions.pop(str(song_id), None)
            return
        self._positions[str(song_id)] = round(position, 1)

    def _persist_positions(self) -> None:
        self.cfg.set("podcast.positions", dict(self._positions))
        try:
            self.cfg.save()
        except OSError:
            pass

    def shutdown(self) -> None:
        self._save_position()
        self._persist_positions()
        for task in (self._home_task, self._mine_task):
            if task is not None and task.is_running:
                task.cancel()

    def on_login_changed(self, logged_in: bool) -> None:
        self._mine_loaded = False
        if logged_in:
            self._load_mine()
        else:
            self.mine_grid.clear()
            self.tabs.setTabText(1, "我的播客")


class PodcastDialog(QDialog):
    """一个播客的节目列表 + 播放控制。"""

    def __init__(self, window, podcast: Podcast,
                 page: PodcastPage, parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.podcast = podcast
        self.page = page
        self._task: BackgroundTask | None = None
        self._programs = []

        self.setWindowTitle(f"播客 · {podcast.name}")
        self.resize(760, 620)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(podcast.name)
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        meta = QLabel(podcast.subtitle or "播客")
        meta.setObjectName("Hint")
        layout.addWidget(meta)
        if podcast.description:
            desc = QLabel(podcast.description)
            desc.setObjectName("Hint")
            desc.setWordWrap(True)
            layout.addWidget(desc)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.status = QLabel("正在加载节目…")
        self.status.setObjectName("Hint")
        toolbar.addWidget(self.status, 1)

        speed_label = QLabel("倍速")
        speed_label.setObjectName("Hint")
        toolbar.addWidget(speed_label)
        self.speed_combo = QComboBox()
        for value in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
            self.speed_combo.addItem(f"{value:g}×", value)
        current = float(self.window.cfg.get("player.speed", 1.0) or 1.0)
        index = self.speed_combo.findData(current)
        self.speed_combo.setCurrentIndex(index if index >= 0 else 1)
        self.speed_combo.currentIndexChanged.connect(self._apply_speed)
        toolbar.addWidget(self.speed_combo)
        layout.addLayout(toolbar)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._play_item)
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        self.play_all_button = accent_button("从第一期播放", glow=False)
        self.play_all_button.clicked.connect(lambda: self._play(0))
        buttons.addWidget(self.play_all_button)
        buttons.addStretch(1)
        close_button = ghost_button("关闭", compact=True)
        close_button.clicked.connect(self.reject)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._load()

    # ---------------------------------------------------------------- 加载

    def _load(self) -> None:
        client = self.window.client
        radio_id = int(self.podcast.id)

        def work():
            programs, more, total = client.podcast_programs(radio_id, limit=PROGRAM_PAGE)
            return programs, total

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_done)
        task.failed.connect(lambda message: self.status.setText(
            f"节目加载失败：{message}"))
        task.start()

    def _on_done(self, data) -> None:
        programs, total = data
        self._programs = programs
        self.list.clear()
        for index, program in enumerate(programs):
            minutes, seconds = divmod(int(program.duration or 0) // 1000, 60)
            text = f"{index + 1:02d}. {program.song.name}　{minutes}:{seconds:02d}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
        self.status.setText(
            f"已加载 {len(programs)} 期" +
            (f"，共 {total} 期" if total else ""))
        self.play_all_button.setEnabled(bool(programs))

    # ---------------------------------------------------------------- 播放

    def _play_item(self, item: QListWidgetItem) -> None:
        self._play(int(item.data(Qt.ItemDataRole.UserRole) or 0))

    def _play(self, index: int) -> None:
        if not self._programs:
            return
        self.page.play_programs(self.podcast, self._programs, index)

    def _apply_speed(self, _index: int) -> None:
        value = float(self.speed_combo.currentData() or 1.0)
        self.window.playback.set_speed(value)
        self.window.cfg.set("player.speed", value)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.page._save_position()
        self.page._persist_positions()
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        super().closeEvent(event)
