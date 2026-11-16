"""评论区页面：和网页播放器一样占满内容区，不是弹窗。

顶部是歌名和「全部评论 N」，下面是发表框、精彩评论、最新评论。这样一边听
一边翻评论，不会挡住播放页，也不会因为关掉弹窗丢掉正在写的字。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..resolver import Song
from ..widgets.card import Card, accent_button, ghost_button
from ..widgets.pager import Pager
from ..widgets.playbackbar import _heart_icon
from ..workers import BackgroundTask
from .base import BasePage

PAGE_SIZE = 20
#: 发表框的字数上限，网页端也是 1000
MAX_LENGTH = 1000


class CommentsPage(BasePage):
    PAGE_TITLE = ""
    PAGE_SUBTITLE = ""
    SCROLLABLE = False
    TRAILING_STRETCH = False

    #: 点「返回」回上一页
    backRequested = Signal()

    def __init__(self, window) -> None:
        self.song: Song | None = None
        #: 当前评论挂在歌曲还是 MV 上；两者共用这一页
        self.mv = None
        self._subject_id = 0
        self._resource = "song"
        self._task: BackgroundTask | None = None
        self._generation = 0
        self._reply_to = None
        self._liking: set[int] = set()
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        head = QHBoxLayout()
        head.setSpacing(10)
        self.back_button = ghost_button("‹ 返回", compact=True)
        self.back_button.clicked.connect(self.backRequested.emit)
        head.addWidget(self.back_button)

        self.title = QLabel("评论")
        self.title.setObjectName("PageTitle")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)

        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(lambda: self._load(self.pager.page))
        head.addWidget(self.refresh_button)
        self.content.addLayout(head)

        self.meta = QLabel("")
        self.meta.setObjectName("Hint")
        self.meta.setWordWrap(True)
        self.content.addWidget(self.meta)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.list_layout = QVBoxLayout(inner)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        scroll.setWidget(inner)
        self.content.addWidget(scroll, 1)

        # 「全部评论 N」
        count_row = QHBoxLayout()
        count_row.setSpacing(8)
        self.count_label = QLabel("全部评论")
        self.count_label.setObjectName("CardTitle")
        count_row.addWidget(self.count_label)
        self.status = QLabel("")
        self.status.setObjectName("Hint")
        count_row.addWidget(self.status, 1)
        self.list_layout.addLayout(count_row)

        # 发表框
        self._composer_card = Card()
        self.reply_hint = QLabel("")
        self.reply_hint.setObjectName("Hint")
        self.reply_hint.setVisible(False)
        self._composer_card.add(self.reply_hint)
        self.composer = QPlainTextEdit()
        self.composer.setPlaceholderText("说点什么吧")
        self.composer.setFixedHeight(88)
        self.composer.textChanged.connect(self._on_text_changed)
        self.composer.installEventFilter(self)
        self._composer_card.add(self.composer)

        composer_footer = QHBoxLayout()
        composer_footer.setSpacing(8)
        self.counter = QLabel(f"0/{MAX_LENGTH}")
        self.counter.setObjectName("Hint")
        composer_footer.addWidget(self.counter)
        composer_footer.addStretch(1)
        self.post_button = accent_button("发布", glow=False)
        self.post_button.clicked.connect(self._post)
        composer_footer.addWidget(self.post_button)
        self._composer_card.add(composer_footer)
        self.list_layout.addWidget(self._composer_card)

        # 精彩评论
        self.hot_header = QLabel("精彩评论")
        self.hot_header.setObjectName("CardTitle")
        self.hot_header.setVisible(False)
        self.list_layout.addWidget(self.hot_header)
        self.hot_box = QVBoxLayout()
        self.hot_box.setSpacing(8)
        self.list_layout.addLayout(self.hot_box)

        # 最新评论
        self.latest_header = QLabel("最新评论")
        self.latest_header.setObjectName("CardTitle")
        self.list_layout.addWidget(self.latest_header)
        self.latest_box = QVBoxLayout()
        self.latest_box.setSpacing(8)
        self.list_layout.addLayout(self.latest_box)

        self.pager = Pager()
        self.pager.pageChanged.connect(self._load_latest)
        self.list_layout.addWidget(self.pager)
        self.list_layout.addStretch(1)

        self.reply_hint.mouseReleaseEvent = (  # type: ignore[method-assign]
            lambda _event: self._clear_reply())

    # ---------------------------------------------------------------- 加载

    def load(self, song: Song) -> None:
        """切换到某首歌的评论。"""
        self.song = song
        self.mv = None
        self._resource = "song"
        self._subject_id = int(song.id) if song is not None else 0
        self._prepare(song.display if song is not None else "评论")

    def load_mv(self, mv) -> None:
        """切换到某支 MV 的评论（threadId 是 ``R_MV_5_<id>``）。"""
        self.song = None
        self.mv = mv
        self._resource = "mv"
        self._subject_id = int(mv.id) if mv is not None else 0
        self._prepare(mv.name if mv is not None else "MV 评论")

    def _prepare(self, title: str) -> None:
        self._reply_to = None
        self._reply_to_label_reset()
        self.composer.clear()
        self.title.setText(title)
        self.meta.setText("")
        self._clear_comments()
        self.pager.set_page(1, False, 0)
        self._update_count(0)
        if self._subject_id:
            self._load(self._subject_id, page=1, with_hot=True)

    def _thread_id(self) -> str:
        client = self.window.client
        if self._resource == "mv":
            return client.mv_thread_id(self._subject_id)
        return client.song_thread_id(self._subject_id)

    def on_show(self) -> None:
        pass

    def _load(self, song_id: int, page: int = 1, with_hot: bool = False) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self._generation += 1
        generation = self._generation
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载…")
        client = self.window.client

        def work():
            latest, total, more = client.comments(
                song_id, page=page, limit=PAGE_SIZE,
                resource=self._resource)
            hot = []
            if with_hot:
                try:
                    hot, _hot_total, _hot_more = client.comments(
                        song_id, page=1, limit=PAGE_SIZE, hot=True,
                        resource=self._resource)
                except Exception:
                    hot = []
            return latest, total, more, hot

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(
            lambda data: self._on_done(data, page, generation, with_hot))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _load_latest(self, page: int) -> None:
        if self._subject_id:
            self._load(self._subject_id, page=page, with_hot=False)

    def _on_done(self, data, page: int, generation: int,
                 with_hot: bool) -> None:
        if generation != self._generation:
            return
        latest, total, more, hot = data
        self.refresh_button.setEnabled(True)
        self._update_count(int(total or 0))
        self.pager.set_page(page, more, total)

        if with_hot:
            self._clear_layout(self.hot_box)
            for comment in hot:
                self.hot_box.addWidget(self._comment_widget(comment))
            self.hot_header.setVisible(bool(hot))

        self._clear_layout(self.latest_box)
        if not latest:
            empty = QLabel("还没有评论。" if page == 1 else "这一页没有更多评论了。")
            empty.setObjectName("Hint")
            self.latest_box.addWidget(empty)
        else:
            for comment in latest:
                self.latest_box.addWidget(self._comment_widget(comment))
        self.status.setText("")

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        self.status.setText(f"加载失败：{message}")

    def _update_count(self, total: int) -> None:
        # 页面里写完整数字（「全部评论 1969911」）；底部播放条才缩写成 196w+
        self.count_label.setText(f"全部评论 {total}" if total else "全部评论")

    # ---------------------------------------------------------------- 发表

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.composer and event.type() == QEvent.Type.KeyPress:
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._post()
                return True
        return super().eventFilter(obj, event)

    def _on_text_changed(self) -> None:
        length = len(self.composer.toPlainText())
        self.counter.setText(f"{length}/{MAX_LENGTH}")
        self.counter.setStyleSheet("color: #d33;" if length > MAX_LENGTH else "")

    def _start_reply(self, comment) -> None:
        self._reply_to = comment
        self.reply_hint.setText(f"回复 @{comment.user}　（点这里取消）")
        self.reply_hint.setVisible(True)
        self.reply_hint.setCursor(Qt.CursorShape.PointingHandCursor)
        self.composer.setFocus()

    def _clear_reply(self) -> None:
        self._reply_to = None
        self.reply_hint.setVisible(False)
        self.reply_hint.setText("")

    def _reply_to_label_reset(self) -> None:
        self._clear_reply()

    def _post(self) -> None:
        if not self._subject_id:
            return
        if not self.window.require_login_silent("发表评论"):
            return
        text = self.composer.toPlainText().strip()
        if not text:
            self.status.setText("先写点什么再发布。")
            return
        if len(text) > MAX_LENGTH:
            self.status.setText(f"评论最多 {MAX_LENGTH} 字。")
            return
        client = self.window.client
        song_id = self._subject_id
        thread_id = self._thread_id()
        reply = self._reply_to
        reply_to = int(reply.id) if reply is not None else 0

        self.post_button.setEnabled(False)
        self.status.setText("正在发布…")
        task = BackgroundTask(
            lambda: client.add_comment(
                song_id, text, reply_to=reply_to, thread_id=thread_id),
            parent=self,
        )

        def done(_comment) -> None:
            self.post_button.setEnabled(True)
            self.composer.clear()
            self._clear_reply()
            self.window.toast("评论已发布")
            self._load(song_id, page=1, with_hot=True)

        def failed(message: str) -> None:
            self.post_button.setEnabled(True)
            self.status.setText(f"发布失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _toggle_like(self, comment, label: QLabel) -> None:
        if not self.window.require_login_silent("给评论点赞"):
            return
        if comment.id in self._liking:
            return
        self._liking.add(comment.id)
        count = int(label.property("count") or comment.liked or 0)
        liked = not bool(label.property("liked"))

        def paint(on: bool, number: int) -> None:
            label.setProperty("liked", on)
            label.setProperty("count", number)
            label.setText(str(max(0, number)) if number else "")
            label.setIcon(_heart_icon(on))
            label.setToolTip("取消点赞" if on else "点赞")

        paint(liked, count + (1 if liked else -1))
        label.setEnabled(False)
        client = self.window.client
        thread_id = comment.thread_id or self._thread_id()
        task = BackgroundTask(
            lambda: client.like_comment(comment.id, thread_id, liked),
            parent=self,
        )

        def done(_result) -> None:
            self._liking.discard(comment.id)
            label.setEnabled(True)

        def failed(message: str) -> None:
            self._liking.discard(comment.id)
            label.setEnabled(True)
            paint(not liked, count)
            self.window.toast(f"点赞失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 渲染

    def _comment_widget(self, comment) -> QWidget:
        frame = QFrame()
        frame.setObjectName("CommentItem")
        outer = QHBoxLayout(frame)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        avatar = _Avatar(self.window.image_store, comment)
        if comment.user_id:
            avatar.setCursor(Qt.CursorShape.PointingHandCursor)
            avatar.setToolTip(f"查看 {comment.user or '这个用户'} 的主页")
            avatar.clicked.connect(
                lambda uid=comment.user_id: self.window.open_detail(
                    "user", int(uid)))
        outer.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        user = QLabel(comment.user or "匿名用户")
        user.setObjectName("CommentUser")
        user.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(user)
        if comment.ip:
            ip = QLabel(comment.ip)
            ip.setObjectName("Hint")
            ip.setTextFormat(Qt.TextFormat.PlainText)
            head.addWidget(ip)
        head.addStretch(1)
        column.addLayout(head)

        text = comment.content
        if comment.reply_user:
            text = f"回复 @{comment.reply_user}：{text}"
        body = QLabel(text)
        body.setObjectName("CommentBody")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(body)

        if comment.reply:
            reply = QLabel(f"回复：{comment.reply}")
            reply.setObjectName("Hint")
            reply.setTextFormat(Qt.TextFormat.PlainText)
            reply.setWordWrap(True)
            column.addWidget(reply)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        when = QLabel(_short_time(comment.time))
        when.setObjectName("Hint")
        footer.addWidget(when)
        if comment.reply_count:
            footer.addWidget(_hint(f"{comment.reply_count} 条回复"))
        footer.addStretch(1)

        # 心形用图标而不是「♥」字符：字符在未点赞时也是一颗实心黑心，
        # 看不出「点没点过」。图标版点过是红色实心，没点是空心。
        like_button = ghost_button(
            str(comment.liked or ""), compact=True)
        like_button.setIconSize(QSize(14, 14))
        like_button.setIcon(_heart_icon(bool(comment.liked_by_me)))
        like_button.setProperty("count", int(comment.liked or 0))
        like_button.setProperty("liked", bool(comment.liked_by_me))
        like_button.setToolTip("取消点赞" if comment.liked_by_me else "点赞")
        like_button.clicked.connect(
            lambda: self._toggle_like(comment, like_button))
        footer.addWidget(like_button)

        reply_button = ghost_button("回复", compact=True)
        reply_button.clicked.connect(lambda: self._start_reply(comment))
        footer.addWidget(reply_button)
        column.addLayout(footer)

        outer.addLayout(column, 1)
        return frame

    # ---------------------------------------------------------------- 清理

    def _clear_comments(self) -> None:
        self._clear_layout(self.hot_box)
        self._clear_layout(self.latest_box)
        self.hot_header.setVisible(False)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def shutdown(self) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()


class _Avatar(QLabel):
    """圆形头像。评论可能很多，图片走 ImageStore 的缓存和去重。"""

    SIZE = 38
    clicked = Signal()

    def __init__(self, store, comment) -> None:
        super().__init__()
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 直接用头像 URL 作缓存键：同一个人在不同评论里只下载一次
        self._key = str(comment.avatar)
        self._store = store
        self._letter = (comment.user or "?")[:1].upper()
        if store is None or not comment.avatar:
            self._paint_letter()
            return
        store.imageReady.connect(self._on_image)
        cached = store.request(self._key, comment.avatar)
        if cached is not None:
            self._show(cached)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def _on_image(self, key: str, pixmap: QPixmap) -> None:
        if key != self._key:
            return
        self._show(pixmap)

    def _show(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(
            self.SIZE, self.SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        rounded = QPixmap(self.SIZE, self.SIZE)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addEllipse(0, 0, self.SIZE, self.SIZE)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, scaled)
        finally:
            painter.end()
        self.setPixmap(rounded)

    def _paint_letter(self) -> None:
        """没有头像（或还没下下来）时画一个带首字的圆，比白圆点自然。"""
        pixmap = QPixmap(self.SIZE, self.SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PANEL_HI))
            painter.drawEllipse(0, 0, self.SIZE, self.SIZE)
            painter.setPen(QColor(theme.TEXT_DIM))
            font = painter.font()
            font.setPointSize(13)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                pixmap.rect(), Qt.AlignmentFlag.AlignCenter, self._letter)
        finally:
            painter.end()
        self.setPixmap(pixmap)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    return label


def _short_time(milliseconds: int) -> str:
    """网页端评论只显示「09-11」这样的日期，时间太长反而吵。"""
    if not milliseconds:
        return ""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(milliseconds / 1000).strftime("%m-%d")
    except (OSError, ValueError, OverflowError):
        return ""


