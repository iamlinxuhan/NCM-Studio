"""歌曲评论对话框：热门 / 最新，只读。

评论只依赖公开接口，未登录也能看。写评论不在本程序里做 —— 网易云对评论写入
的风控比红心更严，强行实现只会频繁失败。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..resolver import Song
from ..widgets.card import ghost_button
from ..widgets.pager import Pager
from ..workers import BackgroundTask

PAGE_SIZE = 20


class CommentsDialog(QDialog):
    """一首歌的评论。"""

    def __init__(self, window, song: Song, parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.song = song
        self._task: BackgroundTask | None = None
        self._generation = 0

        self.setWindowTitle(f"评论 · {song.name}")
        self.resize(620, 700)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(song.display)
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("最新评论", False)
        self.kind_combo.addItem("热门评论", True)
        self.kind_combo.currentIndexChanged.connect(lambda _i: self.load(1))
        toolbar.addWidget(self.kind_combo)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        toolbar.addWidget(self.status, 1)

        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(lambda: self.load(self.pager.page))
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        self.pager = Pager()
        self.pager.pageChanged.connect(self.load)
        layout.addWidget(self.pager)

        self.load(1)

    # ------------------------------------------------------------ 加载

    def load(self, page: int = 1) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        hot = bool(self.kind_combo.currentData())
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载评论…")

        client = self.window.client
        song_id = int(self.song.id)
        task = BackgroundTask(
            lambda: client.comments(song_id, page=page, limit=PAGE_SIZE, hot=hot),
            parent=self,
        )
        self._task = task
        task.done.connect(lambda data: self._on_done(data, page, generation))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _on_done(self, data, page: int, generation: int) -> None:
        if generation != self._generation:
            return
        comments, total, has_more = data
        self.refresh_button.setEnabled(True)
        self.pager.set_page(page, has_more, total)
        self._clear()
        if not comments:
            self.status.setText("还没有评论。" if page == 1 else "这一页没有更多评论了。")
            return
        self.status.setText(f"共 {total} 条" if total else "")
        for comment in comments:
            self.list_layout.insertWidget(self.list_layout.count() - 1,
                                          _comment_widget(comment))

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    def _clear(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def _comment_widget(comment) -> QWidget:
    frame = QFrame()
    frame.setObjectName("CommentItem")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(5)

    head = QHBoxLayout()
    head.setSpacing(8)
    user = QLabel(comment.user or "匿名用户")
    user.setObjectName("CommentUser")
    head.addWidget(user)
    if comment.ip:
        ip = QLabel(comment.ip)
        ip.setObjectName("Hint")
        head.addWidget(ip)
    head.addStretch(1)
    if comment.liked:
        liked = QLabel(f"♥ {comment.liked}")
        liked.setObjectName("Hint")
        head.addWidget(liked)
    layout.addLayout(head)

    body = QLabel(comment.content)
    body.setObjectName("CommentBody")
    body.setWordWrap(True)
    body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(body)

    if comment.reply:
        reply = QLabel(f"回复：{comment.reply}")
        reply.setObjectName("Hint")
        reply.setWordWrap(True)
        layout.addWidget(reply)

    footer = QLabel(comment.time_text)
    footer.setObjectName("Hint")
    layout.addWidget(footer)
    return frame
