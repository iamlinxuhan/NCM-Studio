"""加入歌单对话框：选择已有歌单或新建一个。

写操作走后台线程；失败时停留在这个对话框里显示原因，不把用户刚选的内容丢掉。
"""

from __future__ import annotations

from typing import Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..resolver import Song
from ..widgets.card import accent_button, ghost_button
from ..workers import BackgroundTask


class AddToPlaylistDialog(QDialog):
    """把一首或多首歌加入歌单。"""

    def __init__(self, window, songs: Sequence[Song],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.songs = [song for song in songs if song is not None]
        self._task: BackgroundTask | None = None
        self.added = False

        self.setWindowTitle("加入歌单")
        self.resize(440, 520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        if len(self.songs) == 1:
            title = QLabel(f"《{self.songs[0].name}》加入哪个歌单？")
        else:
            title = QLabel(f"把 {len(self.songs)} 首歌加入哪个歌单？")
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._add())
        layout.addWidget(self.list, 1)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.new_button = ghost_button("新建歌单…", compact=True)
        self.new_button.clicked.connect(self._create)
        self.add_button = accent_button("加入", glow=False)
        self.add_button.clicked.connect(self._add)
        self.cancel_button = ghost_button("取消", compact=True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.new_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)

        self._load()

    # ------------------------------------------------------------ 加载

    def _load(self) -> None:
        self._set_busy(True, "正在读取歌单…")
        client = self.window.client

        def work():
            return client.user_playlists()

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_loaded)
        task.failed.connect(self._on_failed)
        task.start()

    def _on_loaded(self, playlists) -> None:
        self.list.clear()
        for playlist in playlists:
            if playlist.special_type == 5:
                continue
            item = QListWidgetItem(f"{playlist.name}（{playlist.track_count} 首）")
            item.setData(Qt.ItemDataRole.UserRole, playlist.id)
            self.list.addItem(item)
        self._set_busy(False, "" if playlists else "账号里还没有自建歌单，先新建一个。")
        if self.list.count():
            self.list.setCurrentRow(0)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False, f"读取歌单失败：{message}")

    # ------------------------------------------------------------ 操作

    def _selected_id(self) -> int:
        item = self.list.currentItem()
        if item is None:
            return 0
        try:
            return int(item.data(Qt.ItemDataRole.UserRole) or 0)
        except (TypeError, ValueError):
            return 0

    def _add(self) -> None:
        playlist_id = self._selected_id()
        if not playlist_id:
            self.status.setText("先选中一个歌单。")
            return
        self._submit(playlist_id)

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, "新建歌单", "歌单名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        self._set_busy(True, "正在新建歌单…")
        client = self.window.client
        ids = [song.id for song in self.songs]

        def work():
            playlist_id = client.playlist_create(name)
            client.playlist_add_tracks(playlist_id, ids)
            return playlist_id

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(self._on_added)
        task.failed.connect(self._on_failed)
        task.start()

    def _submit(self, playlist_id: int) -> None:
        self._set_busy(True, "正在加入歌单…")
        client = self.window.client
        ids = [song.id for song in self.songs]

        task = BackgroundTask(
            lambda: client.playlist_add_tracks(playlist_id, ids), parent=self
        )
        self._task = task
        task.done.connect(lambda _result: self._on_added(None))
        task.failed.connect(self._on_failed)
        task.start()

    def _on_added(self, _playlist_id) -> None:
        self.added = True
        self._set_busy(False, "已加入歌单。")
        self.accept()

    def _set_busy(self, busy: bool, message: str) -> None:
        self.add_button.setEnabled(not busy)
        self.new_button.setEnabled(not busy)
        self.list.setEnabled(not busy)
        self.status.setText(message)

    def reject(self) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        super().reject()
