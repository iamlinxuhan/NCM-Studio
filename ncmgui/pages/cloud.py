"""云盘：查看/播放/删除云盘歌曲，以及上传本地音乐。

上传会真实改动账号状态，所以保留二次确认。列表只走只读接口；删除前也会
再确认一次，并把接口错误翻译成人话。
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import ncmctl
from ..widgets.card import accent_button, ghost_button
from ..widgets.form import FormCard
from ..workers import BackgroundTask
from .base import BasePage

MINSIZE_PRESETS = [
    ("不过滤", ""),
    ("512 KB", "512KB"),
    ("1 MB", "1MB"),
    ("5 MB", "5MB"),
    ("10 MB", "10MB"),
]

REGEXP_PRESETS = [
    ("所有识别的音频", ""),
    ("仅 FLAC", r".*\.flac$"),
    ("仅 MP3", r".*\.mp3$"),
    ("排除 live 曲目", r"^(?!.*[Ll]ive).*$"),
]

HEADERS = ("歌曲", "歌手", "专辑", "大小", "上传时间")


def format_size(size: int) -> str:
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def upload_time_text(stamp: int) -> str:
    try:
        value = int(stamp or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    # 接口给的是毫秒或秒都可能出现，按量级判断
    if value > 10_000_000_000:
        value //= 1000
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


class CloudPage(BasePage):
    PAGE_TITLE = "音乐云盘"
    PAGE_SUBTITLE = "查看云盘歌曲与容量，或把本地音乐上传到账号（单文件上限 500 MB）。"

    def build(self) -> None:
        self._entries = []
        self._task: BackgroundTask | None = None
        self._loaded = False

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_list(), "云盘歌曲")
        self.tabs.addTab(self._build_upload(), "上传")
        self.content.addWidget(self.tabs, 1)

    # ---------------------------------------------------------------- 列表

    def _build_list(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self.capacity_label = QLabel("尚未加载")
        self.capacity_label.setObjectName("Hint")
        self.capacity_bar = QProgressBar()
        self.capacity_bar.setRange(0, 1000)
        self.capacity_bar.setValue(0)
        self.capacity_bar.setTextVisible(False)
        self.capacity_bar.setFixedHeight(8)
        self.refresh_btn = ghost_button("刷新", compact=True)
        self.refresh_btn.clicked.connect(self._reload)
        self.play_all_btn = accent_button("播放全部", glow=False)
        self.play_all_btn.clicked.connect(self._play_all)
        top.addWidget(self.capacity_label, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.play_all_btn)
        layout.addLayout(top)
        layout.addWidget(self.capacity_bar)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.doubleClicked.connect(self._play_selected)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        widths = (260, 140, 180, 90, 130)
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(380)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.delete_btn = ghost_button("删除选中", compact=True)
        self.delete_btn.clicked.connect(self._delete_selected)
        note = QLabel("删除会真实移除云盘文件；本地文件不受影响。")
        note.setObjectName("Hint")
        actions.addWidget(self.delete_btn)
        actions.addWidget(note, 1)
        layout.addLayout(actions)
        return page

    def on_show(self) -> None:
        if not self._loaded:
            self._reload()

    def on_login_changed(self, logged_in: bool) -> None:
        self._loaded = False
        self._entries = []
        self.table.setRowCount(0)
        self.capacity_label.setText("尚未加载" if logged_in else "登录后可查看云盘")
        if logged_in and not self.window.defer_page_loads:
            self._reload()

    def _reload(self) -> None:
        if not self.require_login("查看云盘"):
            return
        if self._task is not None and self._task.is_running:
            return
        self.refresh_btn.setEnabled(False)
        self.capacity_label.setText("正在加载…")
        client = self.window.client

        def work():
            return client.cloud(limit=1000, offset=0)

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, payload) -> None:
        self.refresh_btn.setEnabled(True)
        self._task = None
        try:
            songs, meta = payload
        except (TypeError, ValueError):
            self.capacity_label.setText("云盘返回格式异常")
            return
        self._entries = list(songs)
        self._loaded = True
        self._fill_table()
        self._fill_capacity(meta)

    def _on_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self._task = None
        self.capacity_label.setText("云盘加载失败")
        self.toast(f"云盘加载失败：{message}")

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            song = entry.song
            values = (
                song.name,
                ", ".join(song.artists) or "未知歌手",
                song.album or "—",
                format_size(entry.size),
                upload_time_text(entry.upload_time),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, column, item)

    def _fill_capacity(self, meta: dict) -> None:
        size = int(meta.get("size") or 0)
        max_size = int(meta.get("max_size") or 0)
        count = int(meta.get("count") or len(self._entries))
        if max_size > 0:
            percent = max(0.0, min(1.0, size / max_size))
            self.capacity_bar.setValue(int(round(percent * 1000)))
            self.capacity_label.setText(
                f"{count} 首 · 已用 {format_size(size)} / {format_size(max_size)}"
                f"（{percent * 100:.1f}%）"
            )
        else:
            self.capacity_bar.setValue(0)
            self.capacity_label.setText(
                f"{count} 首 · 已用 {format_size(size)}（容量上限未知）"
            )

    def _selected_rows(self) -> list[int]:
        rows = {index.row() for index in self.table.selectedIndexes()}
        return sorted(row for row in rows if 0 <= row < len(self._entries))

    def _play_selected(self, _index=None) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        songs = [self._entries[row].song for row in rows]
        self.window.actions.play(songs[0], songs)
        self.toast(f"已播放 {songs[0].display}")

    def _play_all(self) -> None:
        if not self._entries:
            self.toast("云盘里还没有歌曲")
            return
        songs = [entry.song for entry in self._entries]
        self.window.actions.play_all(songs)

    def _delete_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.toast("请先选择要删除的歌曲")
            return
        if not self.require_login("删除云盘歌曲"):
            return
        names = "、".join(self._entries[row].song.name for row in rows[:3])
        if len(rows) > 3:
            names += f" 等 {len(rows)} 首"
        answer = QMessageBox.question(
            self,
            "删除云盘歌曲",
            f"确定从云盘删除 <b>{names}</b> 吗？<br><br>"
            "删除后无法恢复，本地文件不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        ids = [int(self._entries[row].song.id) for row in rows]
        client = self.window.client
        self.delete_btn.setEnabled(False)
        self.toast("正在删除…")

        def work():
            client.cloud_delete(ids)

        task = BackgroundTask(work, parent=self)
        task.done.connect(lambda _result: self._on_deleted(len(ids)))
        task.failed.connect(self._on_delete_failed)
        task.start()

    def _on_deleted(self, count: int) -> None:
        self.delete_btn.setEnabled(True)
        self.toast(f"已从云盘删除 {count} 首")
        self._reload()

    def _on_delete_failed(self, message: str) -> None:
        self.delete_btn.setEnabled(True)
        if isinstance(message, str) and not message:
            message = "未知错误"
        self.toast(f"删除失败：{message}")

    # ---------------------------------------------------------------- 上传

    def _build_upload(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(14)

        source = FormCard(
            "上传来源",
            "选一个音乐文件，或者选一个文件夹批量上传。",
        )
        self.source = source.add_path(
            "文件或目录",
            mode="any",
            value=self.cfg.get("cloud.source", ""),
            hint="点「选择…」可以选文件，也可以选整个文件夹。",
        )
        layout.addWidget(source)

        options = FormCard("上传设置")
        self.minsize = options.add_combo(
            "体积下限",
            MINSIZE_PRESETS,
            self.cfg.get("cloud.minsize", ""),
            hint="小于该体积的文件会被跳过，用来滤掉试听片段。",
        )
        self.regexp = options.add_combo(
            "文件名过滤",
            REGEXP_PRESETS,
            self.cfg.get("cloud.regexp", ""),
            hint="按正则匹配文件路径；仅在来源是文件夹时生效。",
        )
        self.parallel = options.add_spin(
            "并发数",
            int(self.cfg.get("cloud.parallel", 3)),
            1, 10,
            hint="并发过高容易被限流，建议不超过 5。",
        )
        layout.addWidget(options)

        actions = FormCard()
        self.start_button = accent_button("开始上传")
        self.start_button.clicked.connect(self._start)
        open_button = ghost_button("打开来源位置")
        open_button.clicked.connect(self.source.reveal)
        actions.add_row_of(self.start_button, open_button)
        layout.addWidget(actions)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ 内部

    def _start(self) -> None:
        source = self.source.path()
        if not source:
            self.warn("请先选择要上传的文件或文件夹。")
            return
        if not self.check_path(self.source, "上传源", require_exists=True):
            return
        if not self.require_login("上传到云盘"):
            return

        self.save_config()
        argv = ncmctl.build_cloud(
            source,
            minsize=self.minsize.currentData() or "",
            parallel=self.parallel.value(),
            regexp=self.regexp.currentData() or "",
            opts=self.opts(),
        )

        submitted = self.window.submit(
            "云盘上传",
            argv,
            confirm=(
                "确认上传到云盘？",
                f"将把以下内容上传到你的网易云音乐云盘：<br><br>"
                f"<code>{source}</code><br><br>"
                "这会真实修改账号的云盘内容，并占用云盘容量。",
            ),
        )
        if submitted:
            self.tabs.setCurrentIndex(0)
            QTimer.singleShot(1200, self._reload)

    def save_config(self) -> None:
        self.cfg.set("cloud.source", self.source.path())
        self.cfg.set("cloud.minsize", self.minsize.currentData() or "")
        self.cfg.set("cloud.regexp", self.regexp.currentData() or "")
        self.cfg.set("cloud.parallel", self.parallel.value())
