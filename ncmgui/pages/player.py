"""播放页：黑胶封面 + 歌词 + 进度 + 播放队列。

它不是导航项 —— 点歌或点底部播放条的封面才会切过来，左上角「返回」或 Esc
回到刚才那一页。自动下一首不会把用户从别的页面拽回来（见 ``MainWindow``）。

歌词有两种来源：本地文件旁边的 ``.lrc`` / 内嵌标签，或者 ``ncmctl`` 的 Lyric
接口。加载在后台线程里做，切歌后旧结果按代际号丢弃。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPropertyAnimation,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import images, lyrics, ncmctl, theme
from ..playback import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PLAYING,
)
from ..resolver import Resolver, Song
from ..widgets.card import Card, ghost_button
from ..widgets.vinyl import VinylWidget
from ..workers import BackgroundTask
from .base import BasePage

#: 用户自己滚动歌词后，暂停自动跟随多久（秒）
FOLLOW_PAUSE = 3.0


class QueueModel(QAbstractListModel):
    """播放队列。用 model/view，上千首也不会为每首建控件。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._songs: list[Song] = []
        self._current_id: int | None = None

    def set_songs(self, songs: Sequence[Song], current_id: int | None = None) -> None:
        self.beginResetModel()
        self._songs = list(songs)
        self._current_id = current_id
        self.endResetModel()

    def set_current(self, song_id: int | None) -> None:
        if song_id == self._current_id:
            return
        old = self._current_id
        self._current_id = song_id
        for changed in (old, song_id):
            row = self.row_of(changed)
            if row is not None:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

    def row_of(self, song_id: int | None) -> int | None:
        if song_id is None:
            return None
        for row, song in enumerate(self._songs):
            if song.id == song_id:
                return row
        return None

    def song_at(self, row: int) -> Song | None:
        return self._songs[row] if 0 <= row < len(self._songs) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._songs)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        song = self.song_at(index.row())
        if song is None:
            return None
        current = song.id == self._current_id

        if role == Qt.ItemDataRole.DisplayRole:
            duration = f"  ·  {song.duration_text}" if song.duration else ""
            marker = "  ▶  " if current else f"  {index.row() + 1:02d}  "
            return f"{marker}{song.display}{duration}"
        if role == Qt.ItemDataRole.ForegroundRole and current:
            return QColor(theme.CYAN)
        if role == Qt.ItemDataRole.FontRole and current:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ToolTipRole:
            return song.display
        return None


class PlayerPage(BasePage):
    """内容区的播放页（不是导航页）。"""

    PAGE_TITLE = ""
    PAGE_SUBTITLE = ""
    SCROLLABLE = False
    TRAILING_STRETCH = False

    backRequested = pyqtSignal()

    def build(self) -> None:
        self.playback = self.window.playback
        self.covers = self.window.covers

        self._current_song: Song | None = None
        self._lyric_generation = 0
        self._lyric_task: BackgroundTask | None = None
        self._lyric_lines: list[tuple[float, str]] = []
        self._lyric_rows: list[tuple[QLabel, QLabel | None]] = []
        self._current_line = -1
        self._follow_until = 0.0
        self._scroll_anim: QPropertyAnimation | None = None
        self._queue_visible = False
        self._demo_mode = False
        self._placeholder = images.placeholder(330)
        self._cover_id: int | None = None

        self._build_ui()
        self._connect()
        self._on_track_changed(self.playback.current)
        self._on_state_changed(self.playback.state)
        self._on_queue_changed()

    # ---------------------------------------------------------------- 构建

    def _build_ui(self) -> None:
        self.content.setContentsMargins(24, 16, 24, 18)
        self.content.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.back_btn = ghost_button("‹  返回", compact=True)
        self.back_btn.clicked.connect(self.backRequested.emit)
        self.state_label = QLabel("未在播放")
        self.state_label.setObjectName("Hint")
        # 音质放在顶部：进度与时间由底部常驻播放条统一负责，播放页不再重复一套
        self.level_label = QLabel("")
        self.level_label.setObjectName("Hint")
        self.queue_btn = ghost_button("播放队列", compact=True)
        self.queue_btn.setCheckable(True)
        self.queue_btn.clicked.connect(self.toggle_queue)
        top.addWidget(self.back_btn)
        top.addStretch(1)
        top.addWidget(self.state_label)
        top.addSpacing(8)
        top.addWidget(self.level_label)
        top.addWidget(self.queue_btn)
        self.content.addLayout(top)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(2)

        # 左：黑胶
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.vinyl = VinylWidget()
        self.vinyl.set_art(self._placeholder)
        left_layout.addWidget(self.vinyl, 1)
        self.splitter.addWidget(left)

        # 中：歌曲信息 + 歌词
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(8, 4, 8, 0)
        center_layout.setSpacing(4)

        self.title_label = QLabel("未在播放")
        self.title_label.setObjectName("PlayerTitle")
        self.title_label.setWordWrap(True)
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("PlayerArtist")
        center_layout.addWidget(self.title_label)
        center_layout.addWidget(self.artist_label)
        center_layout.addSpacing(6)

        self.lyric_scroll = QScrollArea()
        self.lyric_scroll.setObjectName("LyricsArea")
        self.lyric_scroll.setWidgetResizable(True)
        self.lyric_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.lyric_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.lyric_scroll.viewport().installEventFilter(self)
        self.lyric_scroll.verticalScrollBar().actionTriggered.connect(
            lambda _action: self._pause_follow()
        )

        self.lyric_body = QWidget()
        self.lyric_body.setObjectName("LyricsBody")
        self.lyric_layout = QVBoxLayout(self.lyric_body)
        self.lyric_layout.setContentsMargins(10, 12, 10, 24)
        self.lyric_layout.setSpacing(10)
        self.lyric_layout.addStretch(1)
        self.lyric_scroll.setWidget(self.lyric_body)
        center_layout.addWidget(self.lyric_scroll, 1)
        self.splitter.addWidget(center)

        # 右：队列卡片
        self.queue_card = Card()
        self.queue_card.setMinimumWidth(240)
        queue_head = QHBoxLayout()
        queue_title = QLabel("播放队列")
        queue_title.setObjectName("CardTitle")
        self.queue_count = QLabel("")
        self.queue_count.setObjectName("Hint")
        queue_head.addWidget(queue_title)
        queue_head.addStretch(1)
        queue_head.addWidget(self.queue_count)
        self.queue_card.add(queue_head)

        self.queue_model = QueueModel(self)
        self.queue_view = QListView()
        self.queue_view.setObjectName("QueueList")
        self.queue_view.setModel(self.queue_model)
        self.queue_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_view.setUniformItemSizes(True)
        self.queue_view.clicked.connect(self._on_queue_clicked)
        self.queue_card.add(self.queue_view, 1)
        self.queue_card.setVisible(False)
        self.splitter.addWidget(self.queue_card)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([360, 620, 280])
        self.content.addWidget(self.splitter, 1)
        self._refresh_meta()

    def _connect(self) -> None:
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)
        self.playback.modeChanged.connect(lambda _mode: self._refresh_meta())
        self.covers.coverReady.connect(self._on_cover)

    # ---------------------------------------------------------------- 播放态

    def _on_track_changed(self, song) -> None:
        self._current_song = song
        self.queue_model.set_current(getattr(song, "id", None))

        if song is None:
            self._cover_id = None
            self.title_label.setText("未在播放")
            self.artist_label.setText("")
            self.vinyl.set_art(self._placeholder)
            self._clear_lyrics("点一首歌，这里会显示歌词。")
            return

        self._cover_id = int(song.id)
        self.title_label.setText(song.name or "未知曲目")
        self.artist_label.setText(", ".join(song.artists) or "未知歌手")
        self._refresh_meta()

        cached = self.covers.get(self._cover_id)
        self.vinyl.set_art(cached if cached is not None else self._placeholder)
        if cached is None:
            self.covers.request(song)

        if not self._demo_mode:
            self._load_lyrics(song)

    def _on_state_changed(self, state: str) -> None:
        playing = state == STATE_PLAYING
        self.vinyl.set_playing(playing)
        self.state_label.setText(
            {
                STATE_IDLE: "未在播放",
                STATE_LOADING: "正在缓冲…",
                STATE_PLAYING: "正在播放",
                STATE_PAUSED: "已暂停",
                STATE_ERROR: "播放失败",
            }.get(state, state)
        )

    def _refresh_meta(self) -> None:
        level = self.playback.level
        label = level
        for name, value in ncmctl.DOWNLOAD_LEVELS:
            if value == level:
                label = name
                break
        self.level_label.setText(f"音质：{label}")

    def _on_cover(self, song_id: int, pixmap: QPixmap) -> None:
        if self._cover_id is None or int(song_id) != self._cover_id:
            return
        self.vinyl.set_art(pixmap)

    # ------------------------------------------------------------ 播放位置

    def _on_position_changed(self, position: float, _duration: float) -> None:
        # 进度条与时间只在底部常驻播放条上有一份；这一页用位置来对齐歌词
        self._update_current_line(position)

    # ---------------------------------------------------------------- 歌词

    def _load_lyrics(self, song: Song) -> None:
        self._lyric_generation += 1
        generation = self._lyric_generation
        self._lyric_lines = []
        self._lyric_rows = []
        self._current_line = -1
        self._clear_lyrics("正在加载歌词…")

        local = str(getattr(song, "path", "") or "")
        if local:
            text = lyrics.read_local(Path(local))
            # 下载时译文被插在原文下一行（同一时间戳），这里拆回去，
            # 让本地歌词也用和在线歌词一样的「原文 + 小号译文」排版
            original, translated = lyrics.split_translation(text)
            self._apply_payload(
                {"lrc": {"lyric": original}, "tlyric": {"lyric": translated}},
                generation,
            )
            return

        if self.window.defer_page_loads:
            self._clear_lyrics("播放时显示歌词")
            return
        if not self.window.binary:
            self._clear_lyrics("找不到 ncmctl，无法获取歌词。")
            return

        binary = self.window.binary
        opts = self.opts()
        song_id = int(song.id)

        def work():
            resolver = Resolver(binary, opts=opts, timeout="15s")
            return resolver.lyric(song_id)

        task = BackgroundTask(work, parent=self)
        self._lyric_task = task
        task.done.connect(lambda payload: self._apply_payload(payload, generation))
        task.failed.connect(lambda _message: self._lyric_failed(generation))
        task.start()

    def _apply_payload(self, payload, generation: int) -> None:
        if generation != self._lyric_generation:
            return
        parsed = lyrics.parse(payload)
        if lyrics.has_content(parsed.original):
            lines = lyrics.parse_timed_lines(parsed.original)
            translations = lyrics.translation_map(parsed.translation)
        elif lyrics.has_content(parsed.translation):
            lines = lyrics.parse_timed_lines(parsed.translation)
            translations = {}
        else:
            self._clear_lyrics("暂无歌词")
            return

        if not lines:
            self._clear_lyrics("暂无歌词")
            return
        self._build_lyrics(lines, translations)

    def _lyric_failed(self, generation: int) -> None:
        if generation != self._lyric_generation:
            return
        self._clear_lyrics("歌词获取失败")

    def _clear_lyrics(self, placeholder: str = "") -> None:
        while self.lyric_layout.count():
            item = self.lyric_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 只 deleteLater 的话，控件会带着旧几何位置留到下一轮事件循环，
                # 切歌/改布局时会和新建的歌词重叠一帧。先摘下来再销毁。
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._lyric_rows = []
        if placeholder:
            label = QLabel(placeholder)
            label.setObjectName("LyricLine")
            label.setProperty("current", "false")
            label.setProperty("translation", "false")
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lyric_layout.addWidget(label)
        self.lyric_layout.addStretch(1)

    def _build_lyrics(self, lines: list[tuple[float, str]], translations: dict[int, str]) -> None:
        self._clear_lyrics()
        self._lyric_lines = list(lines)
        self._current_line = -1

        for seconds, body in lines:
            original = QLabel(body)
            original.setObjectName("LyricLine")
            original.setProperty("current", "false")
            original.setProperty("translation", "false")
            original.setWordWrap(True)
            original.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # 插在最后的弹簧之前
            self.lyric_layout.insertWidget(self.lyric_layout.count() - 1, original)

            translated = translations.get(int(round(seconds * 100)))
            translation_label = None
            if translated:
                translation_label = QLabel(translated)
                translation_label.setObjectName("LyricLine")
                translation_label.setProperty("current", "false")
                translation_label.setProperty("translation", "true")
                translation_label.setWordWrap(True)
                translation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.lyric_layout.insertWidget(
                    self.lyric_layout.count() - 1, translation_label
                )
            self._lyric_rows.append((original, translation_label))

        self._update_current_line(self.playback.position)

    def _update_current_line(self, position: float) -> None:
        if not self._lyric_lines:
            return
        index = lyrics.line_index_at(self._lyric_lines, position)
        if index == self._current_line:
            return

        if 0 <= self._current_line < len(self._lyric_rows):
            self._set_line_state(self._lyric_rows[self._current_line], False)
        self._current_line = index
        if index < 0 or index >= len(self._lyric_rows):
            return

        row = self._lyric_rows[index]
        self._set_line_state(row, True)
        if time.monotonic() >= self._follow_until:
            self._scroll_to(row[0])

    def _set_line_state(self, row: tuple[QLabel, QLabel | None], current: bool) -> None:
        for label in row:
            if label is None:
                continue
            label.setProperty("current", "true" if current else "false")
            label.style().unpolish(label)
            label.style().polish(label)

    def _scroll_to(self, label: QLabel) -> None:
        bar = self.lyric_scroll.verticalScrollBar()
        target = (
            label.y()
            + label.height() // 2
            - int(self.lyric_scroll.viewport().height() * 0.42)
        )
        target = max(bar.minimum(), min(bar.maximum(), target))
        if abs(target - bar.value()) < 2:
            return

        animation = QPropertyAnimation(bar, b"value", self)
        animation.setDuration(200)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.start()
        self._scroll_anim = animation

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.lyric_scroll.viewport() and event.type() == QEvent.Type.Wheel:
            self._pause_follow()
        return super().eventFilter(obj, event)

    def _pause_follow(self) -> None:
        self._follow_until = time.monotonic() + FOLLOW_PAUSE

    # ---------------------------------------------------------------- 队列

    def _on_queue_changed(self) -> None:
        if self._demo_mode:
            return
        current = self.playback.current
        self.queue_model.set_songs(
            self.playback.queue, getattr(current, "id", None)
        )
        self._update_queue_count()

    def _update_queue_count(self) -> None:
        total = len(self.playback.queue) if not self._demo_mode else self.queue_model.rowCount()
        self.queue_count.setText(f"共 {total} 首" if total else "队列为空")

    def _on_queue_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self.playback.play_at(index.row())

    @property
    def queue_visible(self) -> bool:
        return self._queue_visible

    def toggle_queue(self) -> None:
        self.set_queue_visible(not self._queue_visible)

    def set_queue_visible(self, visible: bool) -> None:
        visible = bool(visible)
        self._queue_visible = visible
        self.queue_card.setVisible(visible)
        self.queue_btn.setChecked(visible)
        if visible:
            self._update_queue_count()
            sizes = self.splitter.sizes()
            if len(sizes) == 3 and sizes[2] < 80:
                total = max(1, sum(sizes))
                self.splitter.setSizes(
                    [int(total * 0.36), int(total * 0.40), int(total * 0.24)]
                )

    # ---------------------------------------------------------------- 演示

    def set_demo(
        self,
        song: Song,
        cover: QPixmap | None,
        lines: list[tuple[float, str]],
        *,
        queue: Sequence[Song] = (),
        position: float = 0.0,
    ) -> None:
        """截图用：不联网、不起 mpv，直接把播放页摆成一个正在播放的样子。"""
        self._demo_mode = True
        self._on_track_changed(song)
        if cover is not None:
            self.vinyl.set_art(cover)
        self._build_lyrics(lines, {})
        self._on_state_changed(STATE_PLAYING)
        # 截图不等 450ms 的唱臂动画，否则会拍到停在半路的样子
        self.vinyl.finish_arm_animation()
        self._on_position_changed(position, 0.0)
        if queue:
            self.queue_model.set_songs(queue, song.id)
            self.queue_count.setText(f"共 {len(queue)} 首")

    def on_show(self) -> None:
        self.set_queue_visible(self._queue_visible)
