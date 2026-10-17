"""全局底部播放条。

不管当前在哪一页，这一条都在：上一首/播放/下一首、进度、音量和播放顺序。
歌曲信息（封面、歌名、歌手）不在这里重复 —— 播放页的歌词上方已经有一份。
它只读 :class:`~ncmgui.playback.PlaybackController` 的状态，自己不留第二份 ——
否则暂停状态在两个地方各记一份，迟早对不上。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QWidget,
)

from .. import player, theme
from ..playback import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PLAYING,
    mode_label,
)
from .card import ghost_button


class SeekSlider(QSlider):
    """点轨道任意处就跳过去的滑块。

    ``QSlider`` 默认点轨道是「翻一页」，对一首歌来说太钝。点在滑块本身上时
    仍然交给父类，拖动才不会被吃掉。
    """

    clickedValue = pyqtSignal(int)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            if not handle.contains(event.position().toPoint()):
                value = QStyle.sliderValueFromPosition(
                    self.minimum(),
                    self.maximum(),
                    int(event.position().x()),
                    max(1, self.width()),
                )
                self.setValue(value)
                self.clickedValue.emit(value)
                event.accept()
                return
        super().mousePressEvent(event)


class PlaybackBar(QWidget):
    """窗口底部的常驻播放条。"""

    #: 用户点了「播放列表」：打开播放页并展开队列
    queueRequested = pyqtSignal()

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.playback = window.playback

        self._song = None
        self._scrubbing = False

        self.setObjectName("PlaybackBar")
        self.setFixedHeight(70)
        self._build()
        self._connect()

        self._on_track_changed(self.playback.current)
        self._on_state_changed(self.playback.state)
        self._on_mode_changed(self.playback.mode)
        self._on_volume_changed(self.playback.volume)
        self._on_position_changed(self.playback.position, self.playback.duration)
        self._on_queue_changed()

    # ---------------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(10)

        self.prev_btn = self._glyph_button("⏮", "|<", "上一首")
        self.prev_btn.clicked.connect(self.playback.previous)
        self.play_btn = self._glyph_button("▶", "播放", "播放 / 暂停", width=38)
        self.play_btn.clicked.connect(self.playback.toggle)
        self.next_btn = self._glyph_button("⏭", ">|", "下一首")
        self.next_btn.clicked.connect(lambda: self.playback.next(manual=True))
        layout.addWidget(self.prev_btn)
        layout.addWidget(self.play_btn)
        layout.addWidget(self.next_btn)
        layout.addSpacing(4)

        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("PlaybackTime")
        # 「正在缓冲…」比时间串短，固定宽度就不会带着进度条一起动
        self.time_label.setFixedWidth(96)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.time_label)

        self.progress = SeekSlider(Qt.Orientation.Horizontal)
        self.progress.setObjectName("PlaybackProgress")
        self.progress.setRange(0, 1000)
        self.progress.setEnabled(False)
        self.progress.sliderPressed.connect(self._on_scrub_start)
        self.progress.sliderReleased.connect(self._on_scrub_end)
        self.progress.sliderMoved.connect(self._on_scrub_move)
        self.progress.clickedValue.connect(self._seek_to_value)
        layout.addWidget(self.progress, 1)

        self.mode_btn = ghost_button(mode_label(self.playback.mode), compact=True)
        # 四种模式的文案长短不一（顺序 / 列表循环），固定宽度才不会被撑来撑去
        self.mode_btn.setFixedWidth(88)
        self.mode_btn.clicked.connect(self.playback.cycle_mode)
        layout.addWidget(self.mode_btn)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setObjectName("PlaybackVolume")
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(86)
        self.volume.valueChanged.connect(self.playback.set_volume)
        layout.addWidget(self.volume)

        self.queue_btn = ghost_button("播放列表", compact=True)
        self.queue_btn.clicked.connect(self.queueRequested.emit)
        layout.addWidget(self.queue_btn)

    def _glyph_button(self, glyph: str, fallback: str, tip: str, width: int = 32) -> QPushButton:
        button = ghost_button(theme.safe_glyph(glyph, fallback), compact=True)
        button.setFixedWidth(width)
        button.setToolTip(tip)
        return button

    def _connect(self) -> None:
        self.playback.trackChanged.connect(self._on_track_changed)
        self.playback.stateChanged.connect(self._on_state_changed)
        self.playback.positionChanged.connect(self._on_position_changed)
        self.playback.volumeChanged.connect(self._on_volume_changed)
        self.playback.modeChanged.connect(self._on_mode_changed)
        self.playback.queueChanged.connect(self._on_queue_changed)

    # ---------------------------------------------------------------- 状态

    def _on_track_changed(self, song) -> None:
        # 歌曲信息不在这条上显示（播放页歌词上方有一份），这里只关心
        # 有没有歌可播 —— 它决定播放键和进度条是否可用
        self._song = song
        self.progress.setEnabled(
            song is not None and self.playback.duration > 0
        )
        self._on_state_changed(self.playback.state)

    def _on_state_changed(self, state: str) -> None:
        playing = state == STATE_PLAYING
        self.play_btn.setText(
            theme.safe_glyph("⏸", "❚❚") if playing else theme.safe_glyph("▶", "播放")
        )
        self.play_btn.setToolTip("暂停" if playing else "播放")
        self.progress.setEnabled(
            self._song is not None and self.playback.duration > 0
            and state not in (STATE_IDLE, STATE_ERROR)
        )
        if state == STATE_LOADING:
            self.time_label.setText("正在缓冲…")
        elif state == STATE_ERROR:
            self.time_label.setText("播放失败")
        elif state == STATE_IDLE and self._song is None:
            self.time_label.setText("--:-- / --:--")
        self.play_btn.setEnabled(self._song is not None or bool(self.playback.queue))
        if state in (STATE_PAUSED, STATE_PLAYING) and self.playback.duration > 0:
            self._on_position_changed(self.playback.position, self.playback.duration)

    def _on_position_changed(self, position: float, duration: float) -> None:
        if duration > 0 and not self.progress.isEnabled():
            # 本地文件一开始没有时长信息，mpv 报上来之后才能拖动
            self.progress.setEnabled(True)
        if self._scrubbing:
            return
        if duration > 0:
            self.progress.setValue(player.progress_value(position, duration))
            self.time_label.setText(
                f"{player.format_clock(position)} / {player.format_clock(duration)}"
            )
        elif self._song is None:
            self.progress.setValue(0)
            self.time_label.setText("--:-- / --:--")
        else:
            self.progress.setValue(0)
            self.time_label.setText(f"{player.format_clock(position)} / --:--")

    def _on_volume_changed(self, value: int) -> None:
        value = player.clamp_volume(value, self.playback.volume)
        self.volume.blockSignals(True)
        self.volume.setValue(value)
        self.volume.blockSignals(False)
        self.volume.setToolTip(f"音量 {value}%")

    def _on_mode_changed(self, mode: str) -> None:
        label = mode_label(mode)
        self.mode_btn.setText(label)
        self.mode_btn.setToolTip(f"播放顺序：{label}（点击切换）")

    def _on_queue_changed(self) -> None:
        has_queue = bool(self.playback.queue)
        self.prev_btn.setEnabled(has_queue)
        self.next_btn.setEnabled(has_queue)
        self.play_btn.setEnabled(self._song is not None or has_queue)

    # ---------------------------------------------------------------- 演示

    def set_demo(
        self,
        song,
        *,
        position: float = 0.0,
        duration: float = 0.0,
        volume: int = 70,
        mode: str = "loop",
    ) -> None:
        """截图用：不经过播放控制器，把播放条摆成正在播放的样子。"""
        self._song = song
        self._on_state_changed(STATE_PLAYING)
        self.progress.setEnabled(duration > 0)
        if duration > 0:
            self.progress.setValue(player.progress_value(position, duration))
            self.time_label.setText(
                f"{player.format_clock(position)} / {player.format_clock(duration)}"
            )
        self.volume.setValue(player.clamp_volume(volume, 70))
        self._on_mode_changed(mode)
        self.prev_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self.play_btn.setEnabled(True)

    # ---------------------------------------------------------------- 拖动

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_move(self, value: int) -> None:
        duration = self.playback.duration
        if duration > 0:
            seconds = player.seek_seconds(value, duration)
            self.time_label.setText(
                f"{player.format_clock(seconds)} / {player.format_clock(duration)}"
            )

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        self._seek_to_value(self.progress.value())

    def _seek_to_value(self, value: int) -> None:
        duration = self.playback.duration
        if duration > 0:
            self.playback.seek(player.seek_seconds(value, duration))
