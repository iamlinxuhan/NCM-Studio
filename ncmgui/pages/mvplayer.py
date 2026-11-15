"""应用内的 MV 播放页。

视频画面直接嵌在内容区里，不另开窗口：QtMultimedia 的 FFmpeg 后端能直接
读取网易云的 mp4 地址（实测不需要 Referer），和 mpv 的外部窗口相比更符合
「一个窗口听歌看 MV」的体验。开始播放前会暂停正在播放的音乐，避免两路声音
叠在一起。

网易云的视频 CDN 会中途掐断连接（FFmpeg 报 ``partial file``），而且播放地址
本身有时效。所以出错或长时间卡住时不是拿旧地址重开，而是重新向服务端要一次
地址、从断点续播；连续失败才会真正放弃。
"""

from __future__ import annotations

import os
import re
import time
import urllib.request
from pathlib import Path

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

from ..api.transport import USER_AGENT
from ..config import config_dir
from ..library import record_mv_play, update_mv_play
from ..mvstream import HttpRangeDevice
from ..playback import STATE_LOADING, STATE_PLAYING
from ..widgets.card import ghost_button
from ..workers import BackgroundTask
from .base import BasePage

#: 卡住多久算「流断了」。HttpRangeDevice 自己会重试，这里只兜底更糟的情况。
STALL_MS = 25000

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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


def _mv_filename(mv) -> str:
    base = f"{mv.artist} - {mv.name}" if getattr(mv, "artist", "") else mv.name
    base = _BAD_FILENAME.sub("_", str(base)).strip().strip(".") or f"mv-{mv.id}"
    return f"{base}.mp4"


def _unique_path(path: Path) -> Path:
    """同名文件已经存在时加序号，不覆盖用户的东西。"""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _mv_cache_path(mv_id: int) -> Path:
    """断流兜底用的完整视频缓存。"""
    return config_dir() / "cache" / "mv" / f"{int(mv_id)}.mp4"


def _prune_mv_cache(keep: int = 5) -> None:
    """缓存目录只留最近看过的几支，别让它无限长。"""
    directory = _mv_cache_path(0).parent
    if not directory.is_dir():
        return
    try:
        files = sorted(
            directory.glob("*.mp4"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


class _Cancelled(Exception):
    """用户取消了下载。"""


class MvPlayerPage(BasePage):
    PAGE_TITLE = ""
    SCROLLABLE = False
    TRAILING_STRETCH = False

    backRequested = pyqtSignal()

    def __init__(self, window) -> None:
        self.mv = None
        self._url = ""
        self._seeking = False
        #: 当前把网络数据喂给播放器的设备；本地缓存播放时为 None
        self._device: HttpRangeDevice | None = None
        #: 全屏时承载视频与控制条的独立窗口
        self._fullscreen_window: QWidget | None = None
        #: 切换播放源时要跳回的位置；-1 表示没有待恢复的位置
        self._resume_position = -1
        #: 本次观看在 mv_events 里的行 ID，0 表示还没写
        self._event_id = 0
        #: 5 秒检查时视频还没播起来，最多再等几轮
        self._history_checks = 0
        #: 状态栏当前显示的是「加载中/重连中」这类临时提示
        self._transient_status = False
        self._download: BackgroundTask | None = None
        #: 断流兜底：把完整视频缓存到本地再播
        self._cache_task: BackgroundTask | None = None
        #: 当前是否在播放本地缓存文件（此时不必再重取网络地址）
        self._local = False
        #: 缓存兜底本身失败过，不要再反复重试
        self._cache_failed = False
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
        # 视频放在容器里：全屏时把 video 挪进全屏窗，退出时放回这里。
        # 不要把控制条做成 QVideoWidget 的子控件 —— 实测全屏后那样整片子
        # 控件都不渲染（FFmpeg 后端把帧画满整个窗口，盖掉了子控件）。
        self.video_host = QWidget()
        video_layout = QVBoxLayout(self.video_host)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self.video)
        self.content.addWidget(self.video_host, 1)

        self.controls = QWidget()
        self.controls.setObjectName("MvControls")
        self.controls.setMinimumHeight(44)
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(8, 4, 8, 4)
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

        self.download_button = ghost_button("下载", compact=True)
        self.download_button.clicked.connect(self._toggle_download)
        controls.addWidget(self.download_button)

        self.fullscreen_button = ghost_button("全屏", compact=True)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)

        # 全屏时控制条和视频一起进独立的全屏窗，退出时再放回这里
        self.controls_host = QWidget()
        host = QVBoxLayout(self.controls_host)
        host.setContentsMargins(0, 0, 0, 0)
        host.addWidget(self.controls)
        self.content.addWidget(self.controls_host)

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

        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.setInterval(STALL_MS)
        self._stall_timer.timeout.connect(self._on_stalled)

        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(5000)
        self._history_timer.timeout.connect(self._record_history)

        # 全屏后键盘焦点可能在视频窗上，页面自己收不到 ESC；挂到应用级过滤
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---------------------------------------------------------------- 加载

    def load(self, mv, url: str) -> None:
        # 上一支 MV 如果没经过 stop() 就被替换，先把观看记录补完整
        self._flush_history()
        self._close_device()
        self.mv = mv
        self._url = str(url or "")
        self._resume_position = -1
        self._event_id = 0
        self._history_checks = 0
        self._local = False
        self._cache_failed = False
        self.title.setText(getattr(mv, "name", "") or "MV")
        self.meta.setText(getattr(mv, "subtitle", ""))
        self.status.setText("正在加载视频…")
        self._transient_status = True
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
        # 之前断流时缓存过完整视频的话，直接播本地 —— 比再赌一次 HTTP 流稳
        cache = _mv_cache_path(mv.id)
        if cache.is_file() and cache.stat().st_size > 0:
            self._play_local(cache, 0)
        else:
            self._start_stream()
        self._history_timer.start()

    def _start_stream(self) -> None:
        """用 :class:`HttpRangeDevice` 接管网络，而不是让播放器直接拉流。

        播放器拿到的是一个「本地设备」，请求由我们发出：带 Referer、按
        Range 取块、断了自己换地址续上。
        """
        mv = self.mv
        client = self.window.client

        def provider() -> str:
            url, _fee = client.mv_url(mv.id)
            return url

        device = HttpRangeDevice(provider, self)
        self._device = device
        # sourceUrl 只是给后端识别格式用的，真实数据全来自 device
        self.media.setSourceDevice(device, QUrl("https://music.163.com/mv.mp4"))
        self.media.play()

    def _close_device(self) -> None:
        device = self._device
        self._device = None
        if device is None:
            return
        try:
            device.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响后续
            pass
        device.deleteLater()

    def stop(self) -> None:
        # 离开页面时如果还停在全屏，先把全屏窗收掉，否则会盖在整个应用上面
        self._exit_fullscreen()
        self._flush_history()
        self._history_timer.stop()
        self._stall_timer.stop()
        if self._download is not None and self._download.is_running:
            self._download.cancel()
        if self._cache_task is not None and self._cache_task.is_running:
            # 兜底缓存只服务当前这支 MV，离开页面就停，别在后台偷偷跑流量
            self._cache_task.cancel()
            self._cache_task = None
        self.media.stop()
        self.media.setSource(QUrl())
        self._close_device()
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

    # ---------------------------------------------------------------- 下载

    def _download_dir(self) -> Path:
        raw = str(self.cfg.get("download.output", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / "NCM-Studio-download"

    def _fetch_mv_file(self, mv, target: Path, task: BackgroundTask | None) -> str:
        """把 MV 完整下载到 ``target``，可被任意后台线程调用。

        网易云的视频 CDN 会中途掐连接：先写 ``.part``，断了用 ``Range`` 从
        已下载的位置续传，同时重新取播放地址（旧地址可能已过期）。最多续 3 次。
        """
        client = self.window.client
        url, _fee = client.mv_url(mv.id)
        part = target.with_name(target.name + ".part")
        attempt = 0
        if task is not None:
            task.percent.emit(-1)
        while True:
            written = part.stat().st_size if part.is_file() else 0
            headers = {
                "User-Agent": USER_AGENT,
                "Referer": "https://music.163.com/",
            }
            if written:
                headers["Range"] = f"bytes={written}-"
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    status = int(getattr(response, "status", 200) or 200)
                    length = int(response.headers.get("Content-Length") or 0)
                    if written and status == 206:
                        base = written
                        total = base + length if length else 0
                        mode = "ab"
                    else:
                        # 服务端不支持续传（回了 200），只能从头写
                        base = 0
                        total = length
                        mode = "wb"
                    got = 0
                    with open(part, mode) as handle:
                        while True:
                            if task is not None and task.cancel_event.is_set():
                                raise _Cancelled("已取消下载")
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            got += len(chunk)
                            if total and task is not None:
                                task.percent.emit(
                                    min(99, (base + got) * 100 // total))
                    if total and base + got < total:
                        # 连接提前关闭时 read() 只返回 b''，不抛异常；
                        # 不核对长度就会把半截文件当成下载完成
                        raise OSError("连接提前中断")
                os.replace(part, target)
                return str(target)
            except _Cancelled:
                try:
                    part.unlink()
                except OSError:
                    pass
                raise
            except Exception:  # noqa: BLE001 - 网络异常统一重试
                attempt += 1
                if attempt > 3:
                    try:
                        part.unlink()
                    except OSError:
                        pass
                    raise
                time.sleep(1.5 * attempt)
                # 旧地址可能已随断流失效，重新取一次再续传
                url, _fee = client.mv_url(mv.id)

    def _toggle_download(self) -> None:
        task = self._download
        if task is not None and task.is_running:
            task.cancel()
            self.download_button.setText("取消中…")
            return
        if self.mv is None:
            self.status.setText("还没有正在播放的 MV。")
            return
        mv = self.mv
        directory = self._download_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"无法创建下载目录：{exc}")
            return

        self.download_button.setText("取消下载")
        self.status.setText("正在获取下载地址…")

        def work() -> str:
            target = _unique_path(directory / _mv_filename(mv))
            return self._fetch_mv_file(mv, target, task)

        task = BackgroundTask(work, parent=self)
        self._download = task

        def on_progress(percent: int) -> None:
            if percent < 0:
                self.status.setText("下载中…")
            else:
                self.status.setText(f"下载中 {percent}%…")

        def on_done(path: str) -> None:
            self._download = None
            self.download_button.setText("下载")
            self.status.setText(f"已下载到 {path}")
            toast = getattr(self, "toast", None)
            if callable(toast):
                toast("MV 已下载")

        def on_failed(message: str) -> None:
            cancelled = task is not None and task.cancel_event.is_set()
            self._download = None
            self.download_button.setText("下载")
            self.status.setText("已取消下载" if cancelled else f"下载失败：{message}")

        task.percent.connect(on_progress)
        task.done.connect(on_done)
        task.failed.connect(on_failed)
        task.start()

    # ---------------------------------------------------------------- 全屏

    def _toggle_fullscreen(self) -> None:
        if self._fullscreen_window is None:
            self._enter_fullscreen()
        else:
            self._exit_fullscreen()

    def _enter_fullscreen(self) -> None:
        """把视频和控制条一起放进一个独立的全屏窗口。

        不能只让 QVideoWidget 自己全屏、再把控制条塞成它的子控件：FFmpeg
        后端会把视频帧画满整个窗口，子控件实测不显示。让两者做兄弟控件最稳。
        """
        window = QWidget()
        window.setObjectName("MvFullscreen")
        window.setWindowTitle(self.title.text() or "MV")
        window.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        # 只给窗口本身刷黑，不能写裸的 “background:” —— 那会连子控件一起
        # 覆盖成黑色，按钮文字跟着消失
        window.setStyleSheet("QWidget#MvFullscreen { background: #000000; }")
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        video_layout = self.video_host.layout()
        controls_layout = self.controls_host.layout()
        if video_layout is not None:
            video_layout.removeWidget(self.video)
        if controls_layout is not None:
            controls_layout.removeWidget(self.controls)

        self.controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 全屏是黑底，浅色主题的深色文字会看不见；这里显式给控制条一套
        # 白字样式，而不是依赖主题的 $TEXT/$PANEL_ALT
        self.controls.setStyleSheet(
            "QWidget#MvControls {"
            " background-color: rgba(8, 12, 20, 230); }"
            "QPushButton#Ghost {"
            " color: #f2f6ff;"
            " background: rgba(255, 255, 255, 0.10);"
            " border: 1px solid rgba(255, 255, 255, 0.38); }"
            "QPushButton#Ghost:hover {"
            " color: #ffffff;"
            " border-color: #8ecae6;"
            " background: rgba(255, 255, 255, 0.18); }"
            "QLabel#Hint { color: #dfe8f5; }")
        layout.addWidget(self.video, 1)
        layout.addWidget(self.controls, 0)
        self._fullscreen_window = window
        window.installEventFilter(self)
        window.showFullScreen()
        window.setFocus()
        self.fullscreen_button.setText("退出全屏")

    def _exit_fullscreen(self) -> None:
        window = self._fullscreen_window
        if window is None:
            return
        self._fullscreen_window = None
        layout = window.layout()
        if layout is not None:
            layout.removeWidget(self.video)
            layout.removeWidget(self.controls)
        video_layout = self.video_host.layout()
        if video_layout is not None:
            video_layout.addWidget(self.video)
        controls_layout = self.controls_host.layout()
        if controls_layout is not None:
            controls_layout.addWidget(self.controls)
        self.controls.setStyleSheet("")
        self.controls.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.fullscreen_button.setText("全屏")
        window.hide()
        window.deleteLater()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if (event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and self._fullscreen_window is not None):
            self._exit_fullscreen()
            return True
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
            if self._transient_status:
                self.status.setText("")
                self._transient_status = False
        else:
            self.play_button.setText("播放")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._stall_timer.stop()
            self._flush_history(completed=True)
            self.status.setText("播放结束。")
            self.play_button.setText("重新播放")
            return
        if status in (QMediaPlayer.MediaStatus.StalledMedia,
                      QMediaPlayer.MediaStatus.BufferingMedia):
            if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._stall_timer.start()
            return
        self._stall_timer.stop()
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,
                      QMediaPlayer.MediaStatus.BufferedMedia):
            self._apply_resume()

    def _apply_resume(self) -> None:
        """重连后回到断点。等媒体真正加载好再 seek，太早会被丢掉。"""
        if self._resume_position <= 0:
            return
        position = self._resume_position
        self._resume_position = -1
        self.media.setPosition(position)

    def _on_error(self, _error, message: str) -> None:
        self._recover(message or "视频播放中断")

    def _on_stalled(self) -> None:
        self._recover("视频流卡住")

    def _recover(self, reason: str) -> None:
        """播放器报错或长时间卡住时的最后兜底。

        正常情况下 :class:`HttpRangeDevice` 自己在内部重试、换地址续传，
        播放器根本看不到断流。走到这里说明设备也救不回来，那就把整支视频
        缓存到本地再播 —— 本地文件不会再有网络断流问题。
        """
        if self.mv is None:
            return
        if self._local:
            self.status.setText(f"播放失败：{reason or '视频文件不可用'}")
            return
        self._stall_timer.stop()
        self._fall_back_to_cache(reason)

    def _fall_back_to_cache(self, reason: str) -> None:
        """网络播放救不回来时，把整支 MV 缓存到本地再续播。

        视频本身不大，完整拉下来后改本地文件播放，就彻底绕开了网络栈。
        """
        if self.mv is None or self._cache_failed:
            return
        if self._cache_task is not None and self._cache_task.is_running:
            return
        position = max(0, int(self.media.position()))
        target = _mv_cache_path(self.mv.id)
        if target.is_file() and target.stat().st_size > 0:
            self._play_local(target, position)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"播放失败：无法建立视频缓存目录（{exc}）")
            return
        mv = self.mv
        self._resume_position = position
        self.status.setText("视频流反复中断，正在缓存完整视频，缓存好会自动续播…")
        self._transient_status = True
        task: BackgroundTask | None = None

        def work() -> str:
            return self._fetch_mv_file(mv, target, task)

        task = BackgroundTask(work, parent=self)
        self._cache_task = task

        def progress(percent: int) -> None:
            if percent < 0:
                self.status.setText("正在缓存视频…（缓存好会自动续播）")
            else:
                self.status.setText(
                    f"正在缓存视频 {percent}%…（缓存好会自动续播）")

        def done(path: str) -> None:
            self._cache_task = None
            _prune_mv_cache()
            self._play_local(Path(path), position)

        def failed(message: str) -> None:
            self._cache_task = None
            self._cache_failed = True
            self._transient_status = False
            self.status.setText(f"播放失败：{message}")

        task.percent.connect(progress)
        task.done.connect(done)
        task.failed.connect(failed)
        task.start()

    def _play_local(self, path: Path, position: int) -> None:
        """改播本地文件。``position`` 是断点，等媒体加载好后会跳过去。"""
        self._local = True
        self._resume_position = max(0, int(position))
        self.media.setSource(QUrl())
        self._close_device()
        self.media.setSource(QUrl.fromLocalFile(str(path)))
        self.media.play()
        self.status.setText("")
        self._transient_status = False

    # ---------------------------------------------------------------- 历史

    def _record_history(self) -> None:
        """播放满 5 秒才记一条，和歌曲历史的策略一致。"""
        if self.mv is None or self._event_id:
            return
        position = int(self.media.position())
        if position < 3000:
            # 网络慢时 5 秒还没真正播起来；只要还在加载就再等一轮，最多 6 轮
            self._history_checks += 1
            if (self._history_checks <= 6
                    and self.media.playbackState()
                    != QMediaPlayer.PlaybackState.StoppedState):
                self._history_timer.start()
            return
        library = getattr(self.window, "library", None)
        if library is None:
            return
        duration = int(getattr(self.mv, "duration", 0) or self.media.duration() or 0)
        self._event_id = record_mv_play(
            library,
            mv_id=int(self.mv.id),
            name=self.mv.name,
            artist=getattr(self.mv, "artist", ""),
            cover_url=getattr(self.mv, "cover_url", ""),
            duration_ms=duration,
            watched_ms=position,
        )

    def _flush_history(self, completed: bool = False) -> None:
        if not self._event_id:
            return
        library = getattr(self.window, "library", None)
        duration = int(
            getattr(self.mv, "duration", 0) or self.media.duration() or 0
        ) if self.mv is not None else 0
        watched = int(self.media.position())
        if completed and duration:
            watched = duration
        if library is not None:
            update_mv_play(library, self._event_id, watched, completed=completed)
        self._event_id = 0
