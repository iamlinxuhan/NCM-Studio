"""本地 MV：扫描目录里的视频文件，双击在应用内播放。

目录与「本地曲库」共用（``library.dirs``），这里只展示视频条目。扫描仍由
曲库的扫描器处理，本页只负责过滤、搜索与播放。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..config import default_video_dir
from ..library import tracks
from ..library.scan import scan
from ..resolver import Song
from ..widgets.card import ghost_button
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage


class LocalMvPage(BasePage):
    PAGE_TITLE = "本地 MV"
    PAGE_SUBTITLE = "扫描本地视频文件，双击在应用内播放；目录与「本地曲库」共用。"

    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._scan_task: BackgroundTask | None = None
        self._videos: list[Song] = []
        self._ever_shown = False
        super().__init__(window)

    def build(self) -> None:
        self.db = self.window.library

        row = QHBoxLayout()
        row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件名 / 歌手")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._reload)
        row.addWidget(self.search, 1)

        self.scan_button = ghost_button("重新扫描", compact=True)
        self.scan_button.clicked.connect(self._start_scan)
        row.addWidget(self.scan_button)
        self.content.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

        self.model = SongTableModel(self, extra_title="类型",
                                    extra_value=lambda _song: "视频")
        self.table = SongTableView()
        self.table.setModel(self.model)
        self.table.songActivated.connect(self._play)
        self.content.addWidget(self.table, 1)

    # ---------------------------------------------------------------- 数据

    def on_show(self) -> None:
        if self.window.defer_page_loads:
            return
        if not self._ever_shown:
            self._ever_shown = True
            self._start_scan()
        else:
            self._reload()

    def _directories(self) -> list[str]:
        """曲库目录 + MV 下载目录。

        下载的 MV 默认落在「视频/NCM-Studio」，它通常不在音乐曲库目录里；
        本地 MV 页把它一并扫描，用户下完就能直接看到。
        """
        dirs: list[str] = []
        raw = self.cfg.get("library.dirs", [])
        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if text and text not in dirs:
                    dirs.append(text)
        raw_mv = str(self.cfg.get("download.mv_output", "") or "").strip()
        mv_dir = raw_mv or str(default_video_dir())
        if mv_dir and mv_dir not in dirs:
            dirs.append(mv_dir)
        return dirs

    def _reload(self) -> None:
        self._videos = tracks(
            self.db, kind="video", query=self.search.text(), sort="name")
        self.model.set_songs(self._videos)
        if self._videos:
            self.status.setText(f"{len(self._videos)} 个本地视频")
        else:
            self.status.setText(
                "还没有本地视频。先在「本地曲库」添加目录，或把视频放进"
                "曲库目录后点「重新扫描」。")

    def _play(self, song: Song) -> None:
        if not song.path or not Path(song.path).is_file():
            self.warn("这个视频文件已经不在了。")
            return
        self.window.open_local_mv(song)

    # ---------------------------------------------------------------- 扫描

    def _start_scan(self) -> None:
        task = self._scan_task
        if task is not None and task.is_running:
            return
        directories = self._directories()
        existing = [item for item in directories if Path(item).is_dir()]
        if not existing:
            self.status.setText(
                "还没有可扫描的目录。请先把视频放进 MV 下载目录，"
                "或到「本地曲库」添加一个目录。")
            return
        self.scan_button.setEnabled(False)
        self.status.setText("正在扫描…")
        task = BackgroundTask(
            lambda: scan(self.db, existing),
            parent=self,
        )
        self._scan_task = task

        def done(result) -> None:
            self.scan_button.setEnabled(True)
            self._reload()
            self.status.setText(
                f"扫描完成：新增 {result.added}，更新 {result.updated}，"
                f"移除 {result.removed}；当前 {len(self._videos)} 个视频")

        def failed(message: str) -> None:
            self.scan_button.setEnabled(True)
            self.status.setText(f"扫描失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()
