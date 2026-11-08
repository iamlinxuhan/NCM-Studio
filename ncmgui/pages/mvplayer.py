"""应用内的 MV 播放页。

视频画面直接嵌在内容区里，不另开窗口：QtMultimedia 的 FFmpeg 后端能直接
读取网易云的 mp4 地址（实测不需要 Referer），和 mpv 的外部窗口相比更符合
「一个窗口听歌看 MV」的体验。开始播放前会暂停正在播放的音乐，避免两路声音
叠在一起。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtMultimedia import (
    QAudioDevice,
    QAudioOutput,
    QMediaDevices,
    QMediaPlayer,
)
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..playback import STATE_LOADING, STATE_PLAYING
from ..widgets.card import ghost_button
from .base import BasePage


def _quiet_ffmpeg() -> None:
    """把 FFmpeg 的日志级别压到 error。

    网易云的视频流偶尔会中途断连，FFmpeg 会为每一个丢包打一行
    ``partial file`` —— 一次播放能刷满一整屏终端，真正有用的报错反而被淹没。
    这里只留 error，Qt 自己的播放状态和界面提示不受影响。
    """
    try:
        import ctypes
        from pathlib import Path

        names: list[str] = []
        try:
            import PyQt6

            # Qt 的 ffmpeg 插件优先用轮子里自带的库（RPATH 指向 Qt6/lib），
            # 只按 soname 加载可能拿到系统里另一份，级别设了也不生效
            root = Path(PyQt6.__file__).parent / "Qt6"
            for folder in (root / "lib", root / "bin"):
                for stem in ("libavutil.so.59", "libavutil.so.58",
                             "avutil-59.dll", "avutil-58.dll"):
                    path = folder / stem
                    if path.is_file():
                        names.append(str(path))
        except Exception:  # noqa: BLE001
            pass
        names += ["libavutil.so.59", "libavutil.so.58", "avutil-59.dll"]
        for name in names:
            try:
                ctypes.CDLL(name).av_log_set_level(16)
                return
            except (OSError, AttributeError):
                continue
    except Exception:  # noqa: BLE001 - 日志降噪失败不该影响播放
        pass


_quiet_ffmpeg()


def _device_id(device: QAudioDevice) -> str:
    return bytes(device.id().data()).decode("utf-8", "replace")


def _match_audio_device(name: str) -> QAudioDevice | None:
    """把 mpv 的 ``audio-device`` 名字映射成 Qt 的 ``QAudioDevice``。

    mpv 枚举出来的名字带 ``pipewire/`` / ``pulse/`` 这类后端前缀，Qt 用的
    是去掉前缀后的 id；Windows 上一边是 wasapi 的 GUID、另一边也是 GUID，
    按 id 匹配不上时再退回设备描述。空值和 ``pipewire`` 这类后端默认名都
    表示「跟随系统默认设备」。
    """
    devices = QMediaDevices.audioOutputs()
    if not devices:
        return None
    wanted = str(name or "").strip()
    if wanted in ("", "auto", "pipewire", "pulse", "alsa"):
        return QMediaDevices.defaultAudioOutput()
    aliases = {wanted}
    if "/" in wanted:
        aliases.add(wanted.split("/", 1)[1])
    folded = {item.casefold() for item in aliases}
    for device in devices:
        ident = _device_id(device)
        if ident in aliases or ident.casefold() in folded:
            return device
    for device in devices:
        if device.description().casefold() == wanted.casefold():
            return device
    return None


def _clock(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class MvPlayerPage(BasePage):
    PAGE_TITLE = ""
    SCROLLABLE = False
    TRAILING_STRETCH = False

    backRequested = pyqtSignal()

    def __init__(self, window) -> None:
        self.mv = None
        self._url = ""
        self._seeking = False
        self._retries = 0
        super().__init__(window)

    # ---------------------------------------------------------------- 构建

    def build(self) -> None:
        head = QHBoxLayout()
        head.setSpacing(10)
        self.back_button = ghost_button("‹ 返回", compact=True)
        self.back_button.clicked.connect(self.backRequested.emit)
        head.addWidget(self.back_button)

        self.title = QLabel("MV")
        self.title.setObjectName("PageTitle")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)

        self.meta = QLabel("")
        self.meta.setObjectName("Hint")
        head.addWidget(self.meta)
        self.content.addLayout(head)

        self.video = QVideoWidget()
        self.video.setMinimumHeight(320)
        # QVideoWidget 不认样式表，想让它空着时也是黑的得改调色板
        palette = self.video.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.video.setPalette(palette)
        self.video.setAutoFillBackground(True)
        self.content.addWidget(self.video, 1)

        # 全屏时控制条被盖住，在画面角上留一个退出口。装在视频窗里才能跟着
        # 全屏窗一起显示（QVideoWidget 的帧是画在 widget 上的，子控件能盖住）
        self.exit_fullscreen_button = ghost_button("✕ 退出全屏", compact=True)
        self.exit_fullscreen_button.setParent(self.video)
        self.exit_fullscreen_button.hide()
        self.exit_fullscreen_button.clicked.connect(self._exit_fullscreen)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.play_button = ghost_button("暂停", compact=True)
        self.play_button.clicked.connect(self._toggle)
        controls.addWidget(self.play_button)

        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderPressed.connect(self._begin_seek)
        self.position.sliderReleased.connect(self._end_seek)
        self.position.sliderMoved.connect(self._seek)
        controls.addWidget(self.position, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("Hint")
        controls.addWidget(self.time_label)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        try:
            volume = int(self.cfg.get("player.volume", 70))
        except (TypeError, ValueError):
            volume = 70
        self.volume.setValue(max(0, min(100, volume)))
        self.volume.valueChanged.connect(self._set_volume)
        controls.addWidget(self.volume)

        self.fullscreen_button = ghost_button("全屏", compact=True)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        self.content.addLayout(controls)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)

        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.volume.value() / 100)
        # QAudioOutput 只在创建时挑一次设备，之后系统默认设备变了（比如
        # 连上蓝牙耳机）它不会跟着切，结果歌在耳机里、MV 还在扬声器上。
        # 跟着播放器当前设备走，并在设备列表变化时重新解析。
        # 注：这个版本的 Qt 没有 defaultAudioOutputChanged，所以默认设备
        # 的切换靠 audioOutputsChanged 和每次起播前的同步兜住。
        self._output_device_id = ""
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioOutputsChanged.connect(self._sync_output_device)
        playback = getattr(self.window, "playback", None)
        if playback is not None:
            playback.audioInfoChanged.connect(self._sync_output_device)
        self._sync_output_device()
        self.media.setAudioOutput(self.audio)
        self.media.setVideoOutput(self.video)
        self.media.positionChanged.connect(self._on_position)
        self.media.durationChanged.connect(self._on_duration)
        self.media.playbackStateChanged.connect(self._on_state)
        self.media.mediaStatusChanged.connect(self._on_media_status)
        self.media.errorOccurred.connect(self._on_error)

        # 全屏后键盘焦点可能在视频窗上，页面自己收不到 ESC；挂到应用级过滤
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---------------------------------------------------------------- 加载

    def load(self, mv, url: str) -> None:
        self.mv = mv
        self._url = str(url or "")
        self._retries = 0
        self.title.setText(getattr(mv, "name", "") or "MV")
        self.meta.setText(getattr(mv, "subtitle", ""))
        self.status.setText("正在加载视频…")
        self.play_button.setText("暂停")
        # 音乐和 MV 同时出声会互相盖住；只在音乐确实在响时暂停。
        # 不能用「未暂停」判断 —— 空闲时 toggle() 会顺手放起队列第一首
        playback = self.window.playback
        if (playback.current is not None and not playback.paused
                and playback.state in (STATE_LOADING, STATE_PLAYING)):
            try:
                playback.toggle()
            except Exception:  # noqa: BLE001
                pass
        if not self._url:
            self.status.setText("这个 MV 没有可播放的地址。")
            return
        # 起播前再确认一次：从上次播放到现在，默认设备可能已经换了
        self._sync_output_device()
        self.media.setSource(QUrl(self._url))
        self.media.play()

    def stop(self) -> None:
        # 离开页面时如果还停在全屏，先把全屏窗收掉，否则会盖在整个应用上面
        if self.video.isFullScreen():
            self.video.setFullScreen(False)
            self._sync_fullscreen()
        self.media.stop()
        self.media.setSource(QUrl())
        self._url = ""
        self.position.setRange(0, 0)
        self.time_label.setText("0:00 / 0:00")
        self.status.setText("")

    # ---------------------------------------------------------------- 控制

    def _toggle(self) -> None:
        if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media.pause()
        else:
            self.media.play()

    def _begin_seek(self) -> None:
        self._seeking = True

    def _end_seek(self) -> None:
        self._seeking = False
        self.media.setPosition(self.position.value())

    def _seek(self, value: int) -> None:
        self.time_label.setText(f"{_clock(value)} / {_clock(self.media.duration())}")

    def _set_volume(self, value: int) -> None:
        self.audio.setVolume(max(0, min(100, value)) / 100)

    def _sync_output_device(self, *_args) -> None:
        """让 MV 的声音跟着播放器当前选中的输出设备。"""
        playback = getattr(self.window, "playback", None)
        name = str(getattr(playback, "audio_device", "") or "")
        device = _match_audio_device(name)
        if device is None or device.isNull():
            # 显式配置的设备可能刚被拔掉；退回系统默认，比继续走旧设备好
            device = QMediaDevices.defaultAudioOutput()
        if device is None or device.isNull():
            return
        ident = _device_id(device)
        if ident == self._output_device_id:
            return
        self._output_device_id = ident
        self.audio.setDevice(device)

    def _toggle_fullscreen(self) -> None:
        self.video.setFullScreen(not self.video.isFullScreen())
        self._sync_fullscreen()
        if self.video.isFullScreen():
            # 全屏窗是独立顶层窗口，主动要一次焦点，ESC 才收得到
            self.video.setFocus()

    def _exit_fullscreen(self) -> None:
        if self.video.isFullScreen():
            self.video.setFullScreen(False)
        self._sync_fullscreen()

    def _sync_fullscreen(self) -> None:
        full = self.video.isFullScreen()
        self.fullscreen_button.setText("退出全屏" if full else "全屏")
        self.exit_fullscreen_button.setVisible(full)
        if full:
            self.exit_fullscreen_button.raise_()
            self._place_exit_button()

    def _place_exit_button(self) -> None:
        if not self.exit_fullscreen_button.isVisible():
            return
        self.exit_fullscreen_button.adjustSize()
        margin = 18
        size = self.exit_fullscreen_button.size()
        self.exit_fullscreen_button.move(
            max(0, self.video.width() - size.width() - margin), margin)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        kind = event.type()
        if (kind == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and self.video.isFullScreen()):
            self._exit_fullscreen()
            return True
        if obj is self.video:
            if kind == QEvent.Type.Resize and self.exit_fullscreen_button.isVisible():
                self._place_exit_button()
            elif kind == QEvent.Type.WindowStateChange:
                self._sync_fullscreen()
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- 回调

    def _on_position(self, milliseconds: int) -> None:
        if not self._seeking:
            self.position.setValue(int(milliseconds))
        self.time_label.setText(
            f"{_clock(milliseconds)} / {_clock(self.media.duration())}")

    def _on_duration(self, milliseconds: int) -> None:
        self.position.setRange(0, max(0, int(milliseconds)))
        self.time_label.setText(
            f"{_clock(self.media.position())} / {_clock(milliseconds)}")

    def _on_state(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_button.setText("暂停")
            self.status.setText("")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.play_button.setText("播放")
        else:
            self.play_button.setText("播放")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.status.setText("播放结束。")
            self.play_button.setText("重新播放")

    def _on_error(self, _error, message: str) -> None:
        # 视频流的 CDN 偶尔会提前断开（FFmpeg 报 partial file）。重开一次
        # 通常就能接着放，比直接甩一句「播放失败」有用得多。
        if self._url and self._retries < 1:
            self._retries += 1
            position = max(0, self.media.position())
            self.status.setText("视频流中断，正在重试…")
            QTimer.singleShot(1200, lambda: self._retry(position))
            return
        self.status.setText(f"播放失败：{message or '请检查网络或视频源'}")

    def _retry(self, position: int) -> None:
        if not self._url:
            return
        # 同一个地址直接重设可能被忽略，先清空再设
        self.media.setSource(QUrl())
        self.media.setSource(QUrl(self._url))
        self.media.play()
        if position > 0:
            self.media.setPosition(position)
        self.status.setText("")
