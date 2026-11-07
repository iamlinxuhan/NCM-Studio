"""用户主页 / 个人中心。

网易云里点头像就进对方主页，自己的入口是左侧账号卡。这里复用它：
``DetailHost`` 打开别人的主页，左侧账号卡打开自己的「个人中心」。

页面分两块：

* 头部：头像、昵称、等级/VIP、关注与粉丝数、签名，别人主页上还有关注按钮；
* 标签页：歌单（创建 / 收藏）与听歌排行（全部时间 / 最近一周）。

听歌排行里的播放次数接口可能对陌生人隐藏，此时直接显示空态而不是报错。
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..api import ApiError
from ..api.models import Podcast, UserNote, UserProfile
from ..images import placeholder
from ..widgets.card import ghost_button
from ..widgets.covercard import CoverGrid
from ..widgets.songtable import SongTableModel, SongTableView
from ..workers import BackgroundTask
from .base import BasePage

AVATAR_SIZE = 128


class UserPage(BasePage):
    """个人中心 / 他人主页。由详情栈或侧栏账号卡打开。"""

    PAGE_TITLE = ""
    PAGE_SUBTITLE = ""
    SCROLLABLE = False
    TRAILING_STRETCH = False

    def __init__(self, window) -> None:
        self.uid = 0
        #: True 表示这是「个人中心」（自己的主页），登出时要切回未登录态；
        #: 别人主页即使登出也保留资料，只是操作按钮不可用
        self._is_self = False
        self._generation = 0
        self._task: BackgroundTask | None = None
        self._record_task: BackgroundTask | None = None
        self._profile: UserProfile | None = None
        self._created: list = []
        self._collected: list = []
        self._record_songs: list = []
        self._record_counts: dict[int, int] = {}
        #: False = 全部时间，True = 最近一周
        self._week = False
        self._record_loaded = {False: False, True: False}
        self._record_loading = False
        self._avatar_key = ""
        self._notes: list[UserNote] = []
        self._radios: list[Podcast] = []
        self._notes_loaded = False
        self._radios_loaded = False
        self._notes_loading = False
        self._radios_loading = False
        self._notes_task: BackgroundTask | None = None
        self._radios_task: BackgroundTask | None = None
        #: 笔记配图 key -> QLabel；图片回来时按 key 填进去
        self._note_labels: dict[str, QLabel] = {}
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        self.actions = self.window.actions
        self._build_header()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self._build_playlists_tab()
        self._build_notes_tab()
        self._build_radios_tab()
        self._build_record_tab()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.content.addWidget(self.tabs, 1)
        self.window.playback.trackChanged.connect(self._on_track_changed)
        self.window.image_store.imageReady.connect(self._on_image)
        # 启动时 cookie 已经在，但 UID 要等在线校验回来；个人中心先显示
        # 「正在获取账号」，拿到 profile 后再真正加载
        self.window.profileChanged.connect(self._on_profile_changed)

    def _build_header(self) -> None:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(18)

        self.avatar = QLabel()
        self.avatar.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setPixmap(placeholder(AVATAR_SIZE))
        row.addWidget(self.avatar)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.name = QLabel("加载中…")
        self.name.setObjectName("UserName")
        self.name.setWordWrap(True)
        right.addWidget(self.name)

        # VIP / 等级做成小徽章，和网页端一样紧跟在昵称下面
        self.badges = QWidget()
        badge_row = QHBoxLayout(self.badges)
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(8)
        self.vip_badge = QLabel("VIP")
        self.vip_badge.setObjectName("VipBadge")
        self.vip_badge.setVisible(False)
        self.level_badge = QLabel("")
        self.level_badge.setObjectName("LevelBadge")
        self.level_badge.setVisible(False)
        badge_row.addWidget(self.vip_badge)
        badge_row.addWidget(self.level_badge)
        badge_row.addStretch(1)
        self.badges.setVisible(False)
        right.addWidget(self.badges)

        # 关注 / 粉丝用大号数字，这是主页上最先被看到的两项数据
        self.counts = QWidget()
        counts_row = QHBoxLayout(self.counts)
        counts_row.setContentsMargins(0, 2, 0, 0)
        counts_row.setSpacing(18)
        self.follows_label = QLabel("")
        self.follows_label.setObjectName("UserCount")
        self.follows_label.setTextFormat(Qt.TextFormat.RichText)
        self.fans_label = QLabel("")
        self.fans_label.setObjectName("UserCount")
        self.fans_label.setTextFormat(Qt.TextFormat.RichText)
        counts_row.addWidget(self.follows_label)
        counts_row.addWidget(self.fans_label)
        counts_row.addStretch(1)
        self.counts.setVisible(False)
        right.addWidget(self.counts)

        self.meta = QLabel("")
        self.meta.setObjectName("UserSignature")
        self.meta.setWordWrap(True)
        self.signature = QLabel("")
        self.signature.setObjectName("UserSignature")
        self.signature.setWordWrap(True)
        right.addWidget(self.meta)
        right.addWidget(self.signature)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.follow_button = ghost_button("关注", compact=True)
        self.follow_button.clicked.connect(self._toggle_follow)
        self.follow_button.setVisible(False)
        self.refresh_button = ghost_button("刷新", compact=True)
        self.refresh_button.clicked.connect(lambda: self.load(self.uid))
        self.login_button = ghost_button("去登录", compact=True)
        self.login_button.clicked.connect(lambda: self.window.show_page("account"))
        self.login_button.setVisible(False)
        buttons.addWidget(self.follow_button)
        buttons.addWidget(self.login_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        right.addLayout(buttons)
        right.addStretch(1)
        row.addLayout(right, 1)
        self.content.addWidget(header)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

    def _build_playlists_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)

        self.created_title = QLabel("创建的歌单")
        self.created_title.setObjectName("CardTitle")
        self.created_grid = CoverGrid(self.window.image_store)
        self.created_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("playlist", int(payload)))
        column.addWidget(self.created_title)
        column.addWidget(self.created_grid)

        self.collected_title = QLabel("收藏的歌单")
        self.collected_title.setObjectName("CardTitle")
        self.collected_grid = CoverGrid(self.window.image_store)
        self.collected_grid.itemClicked.connect(
            lambda payload: self.window.open_detail("playlist", int(payload)))
        column.addWidget(self.collected_title)
        column.addWidget(self.collected_grid)
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        layout.addWidget(scroll)
        self.tabs.addTab(page, "歌单")

    def _build_notes_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.notes_box = QVBoxLayout(inner)
        self.notes_box.setContentsMargins(0, 0, 0, 0)
        self.notes_box.setSpacing(8)
        scroll.setWidget(inner)
        layout.addWidget(scroll)
        self.tabs.addTab(page, "笔记")

    def _build_radios_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        self.radios_hint = QLabel("")
        self.radios_hint.setObjectName("Hint")
        layout.addWidget(self.radios_hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.radios_grid = CoverGrid(self.window.image_store)
        self.radios_grid.itemClicked.connect(self._open_podcast)
        scroll.setWidget(self.radios_grid)
        layout.addWidget(scroll, 1)
        self.tabs.addTab(page, "播客")

    def _build_record_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.record_hint = QLabel("")
        self.record_hint.setObjectName("Hint")
        toolbar.addWidget(self.record_hint, 1)
        self.record_range = QComboBox()
        self.record_range.addItem("全部时间", False)
        self.record_range.addItem("最近一周", True)
        self.record_range.currentIndexChanged.connect(self._on_range_changed)
        toolbar.addWidget(self.record_range)
        layout.addLayout(toolbar)

        self.record_model = SongTableModel(
            self,
            extra_title="播放",
            extra_value=lambda song: str(self._record_counts.get(song.id, "")),
        )
        self.record_table = SongTableView()
        self.record_table.setModel(self.record_model)
        self.record_table.songActivated.connect(self._play_record_song)
        self.record_table.contextMenuRequested.connect(self._record_menu)
        layout.addWidget(self.record_table, 1)
        self.tabs.addTab(page, "听歌排行")

    # ---------------------------------------------------------------- 加载

    def load(self, uid: int | None = None) -> None:
        """打开某个用户的主页；不传 ``uid`` 时是自己的个人中心。"""
        self._is_self = uid is None or int(uid) == self.window.client.uid
        uid = int(uid or self.window.client.uid or 0)
        if uid <= 0:
            self.uid = 0
            if self.window.is_logged_in():
                self._show_waiting()
            else:
                self._show_logged_out()
            return
        self.uid = uid
        self._generation += 1
        generation = self._generation
        self._profile = None
        self._created = []
        self._collected = []
        self._record_songs = []
        self._record_counts = {}
        self._record_loaded = {False: False, True: False}
        self._week = False
        self.record_range.blockSignals(True)
        self.record_range.setCurrentIndex(0)
        self.record_range.blockSignals(False)
        self._record_loading = False
        self._notes = []
        self._radios = []
        self._notes_loaded = False
        self._radios_loaded = False
        self._notes_loading = False
        self._radios_loading = False
        self._clear_notes()
        self.radios_grid.clear()
        self.radios_hint.setText("")
        self._update_tab_counts()
        for task in (self._notes_task, self._radios_task):
            if task is not None and task.is_running:
                task.cancel()
        self.created_grid.clear()
        self.collected_grid.clear()
        self.record_model.set_songs([])
        self.created_title.setVisible(False)
        self.collected_title.setVisible(False)
        self.follow_button.setVisible(False)
        self.login_button.setVisible(False)
        self.name.setText("加载中…")
        self.meta.setText("")
        self.signature.setText("")
        self.badges.setVisible(False)
        self.counts.setVisible(False)
        self.status.setText("正在加载用户主页…")
        self.refresh_button.setEnabled(False)
        self.avatar.setPixmap(placeholder(AVATAR_SIZE))

        if self._task is not None and self._task.is_running:
            self._task.cancel()
        client = self.window.client

        def work():
            profile = client.user_profile(uid)
            playlists = []
            try:
                playlists = client.user_playlists(uid)
            except ApiError:
                # 未登录或对方完全私密时仍要把资料显示出来
                playlists = []
            return profile, playlists

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(lambda data: self._on_loaded(data, generation))
        task.failed.connect(lambda message: self._on_failed(message, generation))
        task.start()

    def _on_loaded(self, data, generation: int) -> None:
        if generation != self._generation:
            return
        profile, playlists = data
        self._profile = profile
        self.refresh_button.setEnabled(True)
        self.status.setText("")
        self._render_profile(profile)

        self._created = [item for item in playlists
                         if item.creator_id == profile.id]
        self._collected = [item for item in playlists
                           if item.creator_id != profile.id]
        self.created_grid.set_items([
            {
                "key": f"playlist:{item.id}",
                "title": item.name,
                "subtitle": f"{item.track_count} 首",
                "payload": item.id,
                "url": item.cover_url,
            }
            for item in self._created
        ])
        self.collected_grid.set_items([
            {
                "key": f"playlist:{item.id}",
                "title": item.name,
                "subtitle": f"{item.track_count} 首",
                "payload": item.id,
                "url": item.cover_url,
            }
            for item in self._collected
        ])
        self.created_title.setVisible(bool(self._created))
        self.collected_title.setVisible(bool(self._collected))
        if not self._created and not self._collected and not profile.record_visible:
            self.status.setText("这个用户没有公开的歌单。")

        # 笔记和播客的数量直接显示在标签上，和网页端一致；点进标签时才展开
        self._load_notes()
        self._load_radios()
        if self.tabs.currentIndex() == 3:
            self._load_record()

    def _on_failed(self, message: str, generation: int) -> None:
        if generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        self.name.setText("加载失败")
        self.status.setText(f"加载失败：{message}")

    def _render_profile(self, profile: UserProfile) -> None:
        self.name.setText(profile.name)
        self.vip_badge.setVisible(profile.vip)
        self.level_badge.setText(f"LV.{profile.level}")
        self.level_badge.setVisible(profile.level > 0)
        self.badges.setVisible(profile.vip or profile.level > 0)
        self.follows_label.setText(f"关注 <b>{profile.follows}</b>")
        self.fans_label.setText(f"粉丝 <b>{profile.followeds}</b>")
        self.counts.setVisible(True)
        self.meta.setText(
            f"累计听歌 {profile.listen_songs} 首" if profile.listen_songs else "")
        self.signature.setText(profile.signature)
        self.signature.setVisible(bool(profile.signature.strip()))
        is_self = bool(self.window.is_logged_in()
                       and profile.id == self.window.client.uid)
        self.follow_button.setVisible(not is_self)
        self.follow_button.setText("取消关注" if profile.followed else "关注")
        self.follow_button.setEnabled(self.window.is_logged_in())
        self.login_button.setVisible(not self.window.is_logged_in())

        url = profile.avatar
        key = f"avatar:{profile.id}"
        if key != self._avatar_key:
            self._avatar_key = key
            self.avatar.setPixmap(placeholder(AVATAR_SIZE))
        if url:
            cached = self.window.image_store.get(key)
            if cached is not None:
                self._set_avatar(cached)
            else:
                self.window.image_store.request(key, url)
        else:
            self.avatar.setPixmap(placeholder(AVATAR_SIZE))

    def _set_avatar(self, pixmap: QPixmap) -> None:
        self.avatar.setPixmap(_circle(pixmap, AVATAR_SIZE))

    def _on_image(self, key: str, pixmap: QPixmap) -> None:
        if key == self._avatar_key:
            self._set_avatar(pixmap)
            return
        label = self._note_labels.get(key)
        if label is not None:
            try:
                self._set_note_image(label, pixmap)
            except RuntimeError:
                self._note_labels.pop(key, None)

    def _show_waiting(self) -> None:
        """已登录但 UID 还没取回来：先占位，等 ``profileChanged``。"""
        self._profile = None
        self.created_grid.clear()
        self.collected_grid.clear()
        self.record_model.set_songs([])
        self._clear_notes()
        self.radios_grid.clear()
        self.radios_hint.setText("")
        self._update_tab_counts()
        self.created_title.setVisible(False)
        self.collected_title.setVisible(False)
        self.follow_button.setVisible(False)
        self.login_button.setVisible(False)
        self.refresh_button.setEnabled(False)
        self.name.setText("正在获取账号…")
        self.meta.setText("请稍候")
        self.signature.setText("")
        self.badges.setVisible(False)
        self.counts.setVisible(False)
        self.status.setText("")
        self.avatar.setPixmap(placeholder(AVATAR_SIZE))

    def _show_logged_out(self) -> None:
        self._profile = None
        self._generation += 1
        self.created_grid.clear()
        self.collected_grid.clear()
        self.record_model.set_songs([])
        self._notes = []
        self._radios = []
        self._notes_loaded = False
        self._radios_loaded = False
        self._notes_loading = False
        self._radios_loading = False
        self._clear_notes()
        self.radios_grid.clear()
        self.radios_hint.setText("")
        self._update_tab_counts()
        self.created_title.setVisible(False)
        self.collected_title.setVisible(False)
        self.follow_button.setVisible(False)
        self.login_button.setVisible(True)
        self.refresh_button.setEnabled(False)
        self.name.setText("未登录")
        self.meta.setText("登录后查看个人中心")
        self.signature.setText("")
        self.badges.setVisible(False)
        self.counts.setVisible(False)
        self.status.setText("")
        self.avatar.setPixmap(placeholder(AVATAR_SIZE))

    # ---------------------------------------------------------------- 笔记

    def _load_notes(self) -> None:
        if not self.uid or self._notes_loaded or self._notes_loading:
            return
        self._notes_loading = True
        self._clear_notes()
        self._add_notes_status("正在加载笔记…")
        client = self.window.client
        uid = self.uid
        generation = self._generation

        task = BackgroundTask(lambda: client.user_notes(uid, limit=30),
                              parent=self)
        self._notes_task = task

        def done(result) -> None:
            self._notes_loading = False
            notes, _more, _lasttime = result
            if generation != self._generation:
                return
            self._notes = list(notes)
            self._notes_loaded = True
            self._render_notes()

        def failed(message: str) -> None:
            self._notes_loading = False
            if generation != self._generation:
                return
            self._clear_notes()
            self._add_notes_status(f"加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _render_notes(self) -> None:
        self._clear_notes()
        if not self._notes:
            self._add_notes_status("这个用户还没有发过笔记。")
        else:
            for note in self._notes:
                self.notes_box.addWidget(self._note_widget(note))
            self.notes_box.addStretch(1)
        self._update_tab_counts()

    def _clear_notes(self) -> None:
        self._note_labels.clear()
        while self.notes_box.count():
            item = self.notes_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_notes_status(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        self.notes_box.addWidget(label)

    def _note_widget(self, note: UserNote) -> QWidget:
        frame = QFrame()
        frame.setObjectName("CommentItem")
        column = QVBoxLayout(frame)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(10)
        when = QLabel(_note_time(note.time))
        when.setObjectName("Hint")
        head.addWidget(when)
        head.addStretch(1)
        if note.liked:
            head.addWidget(_hint(f"♥ {note.liked}"))
        if note.comments:
            head.addWidget(_hint(f"评论 {note.comments}"))
        column.addLayout(head)

        if note.text:
            body = QLabel(note.text)
            body.setObjectName("CommentBody")
            body.setWordWrap(True)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(body)

        if note.song is not None:
            song_button = ghost_button(f"♪ {note.song.display}", compact=True)
            song_button.clicked.connect(
                lambda _checked=False, song=note.song:
                self.actions.play(song, [song]))
            column.addWidget(song_button, 0, Qt.AlignmentFlag.AlignLeft)

        if note.images:
            images = QHBoxLayout()
            images.setSpacing(6)
            for index, url in enumerate(note.images[:9]):
                label = QLabel()
                label.setFixedSize(96, 96)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setPixmap(placeholder(96))
                key = f"note:{note.id}:{index}"
                self._note_labels[key] = label
                cached = self.window.image_store.request(key, url)
                if cached is not None:
                    self._set_note_image(label, cached)
                images.addWidget(label)
            images.addStretch(1)
            column.addLayout(images)
        return frame

    @staticmethod
    def _set_note_image(label: QLabel, pixmap: QPixmap) -> None:
        label.setPixmap(pixmap.scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation))

    # ---------------------------------------------------------------- 播客

    def _load_radios(self) -> None:
        if not self.uid or self._radios_loaded or self._radios_loading:
            return
        self._radios_loading = True
        self.radios_hint.setText("正在加载播客…")
        client = self.window.client
        uid = self.uid
        include_subscribed = self._is_self
        generation = self._generation

        def work():
            radios = client.user_radios(uid)
            if include_subscribed:
                try:
                    radios = radios + client.subscribed_radios()
                except ApiError:
                    pass
            # 同一个播客可能既创建又收藏，按 ID 去重，保留创建的那份
            merged: dict[int, Podcast] = {}
            for radio in radios:
                merged.setdefault(radio.id, radio)
            return list(merged.values())

        task = BackgroundTask(work, parent=self)
        self._radios_task = task

        def done(radios) -> None:
            self._radios_loading = False
            if generation != self._generation:
                return
            self._radios = list(radios)
            self._radios_loaded = True
            self.radios_grid.set_items([
                {
                    "key": f"podcast:{radio.id}",
                    "title": radio.name,
                    "subtitle": radio.subtitle,
                    "payload": radio,
                    "url": radio.cover_url,
                }
                for radio in self._radios
            ])
            self.radios_hint.setText(
                f"{len(self._radios)} 个播客" if self._radios else "还没有公开的播客。")
            self._update_tab_counts()

        def failed(message: str) -> None:
            self._radios_loading = False
            if generation != self._generation:
                return
            self.radios_hint.setText(f"加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _open_podcast(self, payload) -> None:
        if not isinstance(payload, Podcast):
            return
        from .podcast import PodcastDialog

        PodcastDialog(self.window, payload, self).exec()

    def _update_tab_counts(self) -> None:
        self.tabs.setTabText(1, f"笔记 {len(self._notes)}"
                             if self._notes_loaded else "笔记")
        self.tabs.setTabText(2, f"播客 {len(self._radios)}"
                             if self._radios_loaded else "播客")

    # ------------------------------------------------------------ 听歌排行

    def _on_tab_changed(self, index: int) -> None:
        if index == 1:
            self._load_notes()
        elif index == 2:
            self._load_radios()
        elif index == 3:
            self._load_record()

    def _on_range_changed(self, _index: int) -> None:
        week = bool(self.record_range.currentData())
        if week == self._week:
            return
        self._week = week
        self._load_record()

    def _load_record(self) -> None:
        if not self.uid or self._record_loading:
            return
        if self._record_loaded.get(self._week):
            return
        profile = self._profile
        if profile is not None and not profile.record_visible:
            self.record_hint.setText("这个用户没有公开听歌记录。")
            self.record_model.set_songs([])
            return
        self._record_loading = True
        self.record_hint.setText("正在加载听歌排行…")
        client = self.window.client
        uid = self.uid
        week = self._week
        generation = self._generation

        def work():
            return client.user_play_record(uid, week=week)

        task = BackgroundTask(work, parent=self)
        self._record_task = task

        def done(rows) -> None:
            self._record_loading = False
            if generation != self._generation:
                return
            self._record_loaded[week] = True
            if week != self._week:
                return
            self._record_songs = [song for song, _count in rows]
            self._record_counts = {song.id: count for song, count in rows}
            self.record_model.set_songs(self._record_songs)
            self.record_hint.setText(
                f"{len(rows)} 首" if rows
                else "还没有听歌记录，或者对方没有公开。")
            self._refresh_marker()

        def failed(message: str) -> None:
            self._record_loading = False
            self.record_hint.setText(f"加载失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _play_record_song(self, song) -> None:
        self.actions.play(song, self._record_songs)

    def _record_menu(self, song, position) -> None:
        self.actions.show_menu(self.record_table, song, self._record_songs,
                               position)

    def _on_track_changed(self, _song) -> None:
        self._refresh_marker()

    def _refresh_marker(self) -> None:
        current = self.window.playback.current
        self.record_model.set_playing(current.id if current else None)

    # ---------------------------------------------------------------- 关注

    def _toggle_follow(self) -> None:
        profile = self._profile
        if profile is None:
            return
        if not self.window.require_login_silent("关注用户"):
            return
        target = not profile.followed
        self.follow_button.setEnabled(False)
        client = self.window.client

        def work():
            client.follow_user(profile.id, target)
            return target

        task = BackgroundTask(work, parent=self)

        def done(on) -> None:
            if self._profile is None:
                return
            self._profile = replace(self._profile, followed=bool(on))
            self.follow_button.setEnabled(True)
            self.follow_button.setText("取消关注" if on else "关注")
            self.window.toast("已关注" if on else "已取消关注")

        def failed(message: str) -> None:
            self.follow_button.setEnabled(True)
            self.window.toast(f"操作失败：{message}")

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    # ---------------------------------------------------------------- 生命周期

    def on_show(self) -> None:
        self.actions.ensure_liked()
        if not self.uid:
            self.load()
            return
        self._refresh_marker()

    def _on_profile_changed(self, profile: dict) -> None:
        if self.uid:
            return
        uid = int(profile.get("userId") or 0)
        if uid and self._is_self:
            self.load(uid)

    def on_login_changed(self, logged_in: bool) -> None:
        if not logged_in:
            if self._is_self:
                self.uid = 0
                self._show_logged_out()
            return
        if not self.uid:
            self.load()

    def shutdown(self) -> None:
        self._generation += 1
        for task in (self._task, self._record_task):
            if task is not None and task.is_running:
                task.cancel()


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    return label


def _note_time(milliseconds: int) -> str:
    if not milliseconds:
        return ""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def _circle(pixmap: QPixmap, size: int) -> QPixmap:
    """把头像裁成圆形。"""
    scaled = pixmap.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation)
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
    finally:
        painter.end()
    return canvas
