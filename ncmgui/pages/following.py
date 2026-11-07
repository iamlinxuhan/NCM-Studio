"""关注：我关注的人最近分享了什么。

网页端的「关注」页是动态流：谁分享了哪首歌、哪张歌单、哪条评论。这里把
常见事件压成一张卡片，分享的歌曲可以直接播放；关注/取消关注也放在卡片上。
"""

from __future__ import annotations

import json
from datetime import datetime

from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..api.models import song_from_api
from ..widgets.card import Card, ghost_button
from ..workers import BackgroundTask
from .base import BasePage

PAGE_SIZE = 20


class FollowingPage(BasePage):
    PAGE_TITLE = "关注"
    PAGE_SUBTITLE = "你关注的人最近分享和推荐的内容。"
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self._task: BackgroundTask | None = None
        self._events: list[dict] = []
        self._loaded = False
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.status = QLabel("登录后加载关注动态。")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status, 1)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(self._reload)
        toolbar.addWidget(self.refresh_button)
        self.content.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        self.content.addWidget(self.scroll, 1)

    # ---------------------------------------------------------------- 加载

    def on_show(self) -> None:
        if not self.window.is_logged_in():
            self.status.setText("登录后加载关注动态。")
            return
        if not self._loaded:
            self._reload()

    def on_login_changed(self, logged_in: bool) -> None:
        self._loaded = False
        self._events = []
        self._clear()
        if not logged_in:
            self.status.setText("登录后加载关注动态。")

    def _reload(self) -> None:
        if not self.window.is_logged_in():
            self.status.setText("登录后加载关注动态。")
            return
        if self._task is not None and self._task.is_running:
            self._task.cancel()
        self.refresh_button.setEnabled(False)
        self.status.setText("正在加载…")
        client = self.window.client
        task = BackgroundTask(
            lambda: client.following_feed(limit=PAGE_SIZE), parent=self)
        self._task = task

        def done(data) -> None:
            events, _more, _cursor = data
            self._events = events
            self._loaded = True
            self.refresh_button.setEnabled(True)
            self._render()
            self.status.setText(
                f"{len(events)} 条动态" if events else "关注的人最近没有新动态。")

        def failed(message: str) -> None:
            self.refresh_button.setEnabled(True)
            self.status.setText(f"加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 渲染

    def _render(self) -> None:
        self._clear()
        for event in self._events:
            widget = self._event_widget(event)
            if widget is not None:
                self.list_layout.insertWidget(self.list_layout.count() - 1,
                                              widget)

    def _event_widget(self, event: dict) -> QWidget | None:
        parsed = _parse_event(event)
        if parsed is None:
            return None
        user = parsed["user"]
        text = parsed["text"] or "分享了内容"
        song = parsed["song"]

        card = Card()
        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(user or "网易云用户")
        name.setObjectName("CommentUser")
        head.addWidget(name)
        when = QLabel(_time_text(parsed["time"]))
        when.setObjectName("Hint")
        head.addWidget(when)
        head.addStretch(1)

        uid = parsed["user_id"]
        if uid and uid != int(getattr(self.window.client, "uid", 0) or 0):
            follow = ghost_button(
                "已关注" if parsed["followed"] else "关注", compact=True)
            follow.clicked.connect(
                lambda _checked=False, u=uid, b=follow, f=parsed["followed"]:
                self._toggle_follow(u, b, f))
            head.addWidget(follow)
        card.add(head)

        body = QLabel(text)
        body.setObjectName("CommentBody")
        body.setWordWrap(True)
        card.add(body)

        if song is not None:
            play_row = QHBoxLayout()
            play_row.setSpacing(8)
            play = ghost_button("▶ 播放", compact=True)
            play.clicked.connect(lambda _checked=False, s=song: self._play(s))
            play_row.addWidget(play)
            title = QLabel(f"{song.name} — {' / '.join(song.artists)}")
            title.setObjectName("Hint")
            title.setWordWrap(True)
            play_row.addWidget(title, 1)
            card.add(play_row)

        elif parsed["kind"] in ("playlist", "album", "artist") and parsed["id"]:
            open_button = ghost_button(
                {"playlist": "打开歌单", "album": "打开专辑",
                 "artist": "打开歌手"}[parsed["kind"]], compact=True)
            open_button.clicked.connect(
                lambda _checked=False, k=parsed["kind"], i=parsed["id"]:
                self.window.open_detail(k, i))
            card.add(open_button)
        return card

    def _play(self, song) -> None:
        self.window.actions.play(song)

    def _toggle_follow(self, uid: int, button, followed: bool) -> None:
        if not self.window.require_login_silent("关注"):
            return
        client = self.window.client
        target = not followed
        button.setEnabled(False)
        task = BackgroundTask(lambda: client.follow_user(uid, target),
                              parent=self)
        task.done.connect(lambda _result: (
            button.setText("已关注" if target else "关注"),
            button.setEnabled(True),
            self.window.toast("已关注" if target else "已取消关注"),
            self._render(),
        ))
        task.failed.connect(lambda message: (
            button.setEnabled(True),
            self.window.toast(f"操作失败：{message}"),
        ))
        task.start()
        # 本地记下新状态：重绘时不用等下一次刷新
        for event in self._events:
            user = event.get("user") or {}
            if int(user.get("userId") or 0) == int(uid):
                user["followed"] = target

    def _clear(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def shutdown(self) -> None:
        if self._task is not None and self._task.is_running:
            self._task.cancel()


def _parse_event(event: dict) -> dict | None:
    """把动态事件压成页面要的字段；无法识别的类型返回 ``None``。"""
    user = event.get("user") or {}
    info = event.get("info") or {}
    raw = event.get("json") or "{}"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        payload = {}

    text = str(payload.get("msg") or payload.get("title") or "")
    if not text:
        resource = ((info.get("commentThread") or {}).get("resourceInfo")
                    or {})
        text = str(resource.get("name") or "")
    if not text and event.get("actName"):
        text = str(event.get("actName"))

    song = song_from_api(payload.get("song")) if payload.get("song") else None
    kind = ""
    item_id = 0
    for key in ("playlist", "album", "artist"):
        item = payload.get(key)
        if isinstance(item, dict) and item.get("id"):
            kind, item_id = key, int(item.get("id"))
            break
    if not text and song is not None:
        text = f"分享了《{song.name}》"

    nickname = str(user.get("nickname") or "")
    if not nickname and not text:
        return None
    return {
        "user": nickname,
        "user_id": int(user.get("userId") or 0),
        "followed": bool(user.get("followed")),
        "text": text,
        "song": song,
        "kind": kind,
        "id": item_id,
        "time": int(event.get("eventTime") or payload.get("time") or 0),
    }


def _time_text(milliseconds: int) -> str:
    if not milliseconds:
        return ""
    try:
        return datetime.fromtimestamp(milliseconds / 1000).strftime("%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""
