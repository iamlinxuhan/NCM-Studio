"""MV 页：推荐与搜索，点封面在应用内播放。

取播放地址要单独请求一次（``song/enhance/play/mv/url``），所以点击卡片后先在
后台拿地址，再切到 :class:`ncmgui.pages.mvplayer.MvPlayerPage`。视频播放期间
应用里的音乐会被暂停，但不会自动恢复 —— 用户可能接着看下一支 MV。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit

from ..api.models import Mv
from ..config import default_video_dir
from ..mvdownload import download_all
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
        #: 当前列表是推荐 / 搜索 / 收藏，决定「下载全部」是否出现
        self._mode = "recommend"
        self._download_task: BackgroundTask | None = None
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

        self.favorites_button = ghost_button("我的收藏", compact=True)
        self.favorites_button.clicked.connect(self.reload_favorites)
        search_row.addWidget(self.favorites_button)

        # 只在收藏视图里出现；和播放页的「下载」一样用「下载」页里的 MV 目录
        self.download_all_button = ghost_button("下载全部", compact=True)
        self.download_all_button.clicked.connect(self._download_all)
        self.download_all_button.hide()
        search_row.addWidget(self.download_all_button)
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
        self._mode = "recommend"
        self.download_all_button.hide()
        self._load(lambda: self.window.client.mv_recommend(DEFAULT_LIMIT),
                   "正在加载推荐 MV…")

    def reload_favorites(self) -> None:
        """官方「收藏 MV」列表。收藏本身走 ``/api/mv/sublist``，不建本地表。"""
        self._mode = "favorites"
        if not self.window.require_login_silent("查看收藏的 MV"):
            return
        self._load(lambda: self.window.client.mv_sublist(200),
                   "正在加载收藏的 MV…")

    def _search(self) -> None:
        keyword = self.search_edit.text().strip()
        if not keyword:
            self.reload_recommend()
            return
        self._mode = "search"
        self.download_all_button.hide()
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
        favorites = self._mode == "favorites"
        self.download_all_button.setVisible(favorites and bool(self._mvs))
        if self._mvs:
            self.status.setText(
                f"收藏的 {len(self._mvs)} 支 MV" if favorites
                else f"{len(self._mvs)} 支 MV")
        else:
            self.status.setText("还没有收藏的 MV。" if favorites else "没有找到 MV。")
        self._loaded = True

    def _on_load_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.search_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    def _download_all(self) -> None:
        """一键下载当前收藏列表里的全部 MV。"""
        task = self._download_task
        if task is not None and task.is_running:
            task.cancel()
            self.status.setText("正在取消下载…")
            return
        if not self._mvs:
            return
        mvs = list(self._mvs)
        raw = str(self.cfg.get("download.mv_output", "") or "").strip()
        directory = (Path(raw).expanduser() if raw else default_video_dir())
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"无法创建 MV 下载目录：{exc}")
            return
        client = self.window.client
        self.download_all_button.setText("取消下载")
        self.status.setText(f"准备下载 {len(mvs)} 支 MV…")
        task = BackgroundTask(
            lambda: download_all(
                client, mvs, directory,
                progress=task.progress.emit,
                cancelled=task.cancel_event.is_set,
            ),
            parent=self,
        )
        self._download_task = task

        def done(count: int) -> None:
            self.download_all_button.setText("下载全部")
            self.status.setText(f"已下载 {count} 支 MV 到 {directory}")

        def failed(message: str) -> None:
            cancelled = task is not None and task.cancel_event.is_set()
            self.download_all_button.setText("下载全部")
            self.status.setText(
                "已取消下载" if cancelled else f"下载失败：{message}")

        task.progress.connect(self.status.setText)
        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

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
