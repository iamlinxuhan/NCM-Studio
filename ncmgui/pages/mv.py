"""MV 页：推荐与搜索，点封面在应用内播放。

取播放地址要单独请求一次（``song/enhance/play/mv/url``），所以点击卡片后先在
后台拿地址，再切到 :class:`ncmgui.pages.mvplayer.MvPlayerPage`。视频播放期间
应用里的音乐会被暂停，但不会自动恢复 —— 用户可能接着看下一支 MV。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit

from ..api.models import Mv
from ..widgets.card import ghost_button
from ..widgets.covercard import CoverGrid
from ..workers import BackgroundTask
from .base import BasePage

DEFAULT_LIMIT = 24


class MvPage(BasePage):
    PAGE_TITLE = "MV"
    PAGE_SUBTITLE = "网易云 MV 与视频；点击封面直接在本窗口播放。"
    SCROLLABLE = True

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._url_task: BackgroundTask | None = None
        self._generation = 0
        self._loaded = False
        self._mvs: list[Mv] = []
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索 MV、歌手或歌曲名")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self._search)
        search_row.addWidget(self.search_edit, 1)

        self.search_button = ghost_button("搜索", compact=True)
        self.search_button.clicked.connect(self._search)
        search_row.addWidget(self.search_button)

        self.recommend_button = ghost_button("推荐", compact=True)
        self.recommend_button.clicked.connect(self.reload_recommend)
        search_row.addWidget(self.recommend_button)
        self.content.addLayout(search_row)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

        self.grid = CoverGrid(self.window.image_store)
        self.grid.itemClicked.connect(self._play_mv)
        self.content.addWidget(self.grid)

    # ---------------------------------------------------------------- 加载

    def reload_recommend(self) -> None:
        self._load(lambda: self.window.client.mv_recommend(DEFAULT_LIMIT),
                   "正在加载推荐 MV…")

    def _search(self) -> None:
        keyword = self.search_edit.text().strip()
        if not keyword:
            self.reload_recommend()
            return
        self._load(lambda: self.window.client.mv_search(keyword, limit=30),
                   f"正在搜索「{keyword}」…")

    def _load(self, work, message: str) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        self.status.setText(message)
        self.search_button.setEnabled(False)
        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(lambda mvs: self._on_loaded(mvs, generation))
        task.failed.connect(lambda msg: self._on_load_failed(msg, generation))
        task.start()

    def _on_loaded(self, mvs, generation: int) -> None:
        if generation != self._generation:
            return
        self.search_button.setEnabled(True)
        self._mvs = list(mvs)
        self.grid.set_items([
            {
                "key": f"mv:{mv.id}",
                "title": mv.name,
                "subtitle": mv.subtitle,
                "payload": mv,
                "url": mv.cover_url,
            }
            for mv in self._mvs
        ])
        self.status.setText(
            f"{len(self._mvs)} 支 MV" if self._mvs else "没有找到 MV。")
        self._loaded = True

    def _on_load_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.search_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    # ---------------------------------------------------------------- 播放

    def _play_mv(self, payload) -> None:
        if not isinstance(payload, Mv):
            return
        if self._url_task is not None and self._url_task.is_running:
            self._url_task.cancel()
        self.status.setText(f"正在获取《{payload.name}》的播放地址…")
        client = self.window.client
        task = BackgroundTask(lambda: client.mv_url(payload.id), parent=self)
        self._url_task = task

        def done(result) -> None:
            url, _fee = result
            self.status.setText("")
            self.window.open_mv(payload, url)

        def failed(message: str) -> None:
            self.status.setText(f"无法播放：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 生命周期

    def on_show(self) -> None:
        if not self._loaded:
            self.reload_recommend()

    def shutdown(self) -> None:
        self._generation += 1
        for task in (self._task, self._url_task):
            if task is not None and task.is_running:
                task.cancel()
