"""SMTC（Windows 系统媒体控制）接口。

Windows 的媒体键、任务栏「正在播放」浮层和锁屏播放器都走 SMTC
（``Windows.Media.SystemMediaTransportControls``）。谁在系统里注册了这个接口，
谁就能显示当前曲目的标题、歌手、专辑和封面，并收到播放 / 暂停 / 上一首 /
下一首这些指令。

mpv 本身不实现 SMTC，随包 mpv 又是用 ``--no-config --load-scripts=no`` 起的，
所以和 MPRIS 一样，这个接口由界面自己补。做法是创建一个**不出声**的
``MediaPlayer``：它的 ``SystemMediaTransportControls`` 就是向系统注册的媒体会话，
播放状态、元数据和进度都手动写入，实际发声仍然由 mpv 负责。

几个实现上的取舍：

* WinRT 对象全部在独立线程里创建和使用。Qt 在主线程已经把 COM 初始化成 STA，
  再在同一个线程上调 ``init_apartment`` 会撞 ``RPC_E_CHANGED_MODE``；放到
  自己的线程里用 MTA 最省事，也不会让 ``MediaPlayer`` 的初始化拖住界面。
* WinRT 的事件回调来自系统线程，不能直接碰 Qt 控件；回调只把动作塞回
  ``_actionRequested`` 信号，由主线程执行，和 MPRIS 那套一致。
* 状态、元数据、进度都先在主线程拼成普通 dict 再交给工作线程，避免跨线程读
  ``PlaybackController`` 的可变字段。
* 封面优先用在线地址；本地文件没有在线封面时读内嵌图，落到临时目录再用
  ``file://`` 地址交给系统。临时目录在退出时删掉。
* 非 Windows 平台、或者没装 WinRT 绑定时，整个模块退化为空操作：不导入、
  不起线程，播放链路完全不受影响。

本模块的纯函数（按钮映射、状态映射、时间轴换算）可以单独自检：

    .venv/bin/python -m ncmgui.smtc
"""

from __future__ import annotations

import hashlib
import queue
import shutil
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import images
from .covers import image_kind, read_artwork
from .playback import (
    MODE_LOOP,
    MODE_ONE,
    MODE_ORDER,
    MODE_SHUFFLE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PLAYING,
)

try:  # 只有 Windows 装了这些投影包；其他平台导入失败即整体降级
    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    from winrt.windows.foundation import Uri
    from winrt.windows.media import (
        MediaPlaybackAutoRepeatMode,
        MediaPlaybackStatus,
        MediaPlaybackType,
        SystemMediaTransportControlsTimelineProperties,
    )
    from winrt.windows.media.playback import MediaPlayer
    from winrt.windows.storage.streams import RandomAccessStreamReference

    _AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于平台与安装方式
    _AVAILABLE = False

STATUS_PLAYING = "playing"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"

#: 播放器状态 → SMTC 的 ``PlaybackStatus``。装载中也算「播放中」，否则
#: 刚点下一首时系统浮层会闪一下「已停止」。
_STATUS_BY_STATE = {
    STATE_PLAYING: STATUS_PLAYING,
    STATE_LOADING: STATUS_PLAYING,
    STATE_PAUSED: STATUS_PAUSED,
    STATE_IDLE: STATUS_STOPPED,
    STATE_ERROR: STATUS_STOPPED,
}

#: 播放顺序 → SMTC 的循环模式。随机播放沿用 MPRIS 的约定，报成列表循环。
_REPEAT_BY_MODE = {
    MODE_ONE: "track",
    MODE_ORDER: "none",
    MODE_LOOP: "list",
    MODE_SHUFFLE: "list",
}
_REPEAT_TO_MODE = {"track": MODE_ONE, "none": MODE_ORDER, "list": MODE_LOOP}
#: ``MediaPlaybackAutoRepeatMode`` 的枚举值：NONE=0、TRACK=1、LIST=2。
_REPEAT_NAMES = {0: "none", 1: "track", 2: "list"}

#: ``SystemMediaTransportControlsButton`` 的枚举值。只映射播放器真正用得到的
#: 那几个；RECORD、CHANNEL_UP / DOWN 之类保持无动作。
_ACTION_BY_BUTTON = {
    0: "play",
    1: "pause",
    2: "stop",
    4: "seek_forward",
    5: "seek_backward",
    6: "next",
    7: "previous",
}

#: 系统浮层上的快进 / 快退按钮一次走多少秒。
SEEK_STEP = 10.0

if _AVAILABLE:
    _STATUS_ENUM = {
        STATUS_PLAYING: MediaPlaybackStatus.PLAYING,
        STATUS_PAUSED: MediaPlaybackStatus.PAUSED,
        STATUS_STOPPED: MediaPlaybackStatus.STOPPED,
    }
    _REPEAT_ENUM = {
        "none": MediaPlaybackAutoRepeatMode.NONE,
        "track": MediaPlaybackAutoRepeatMode.TRACK,
        "list": MediaPlaybackAutoRepeatMode.LIST,
    }


# --------------------------------------------------------------------------- #
# 纯函数：元数据与状态换算（不依赖 WinRT，便于在 Linux 上自检）
# --------------------------------------------------------------------------- #

def status_name(state: str) -> str:
    """播放器状态 → ``playing`` / ``paused`` / ``stopped``。"""
    return _STATUS_BY_STATE.get(state, STATUS_STOPPED)


def repeat_name(mode: str) -> str:
    """播放顺序 → ``none`` / ``track`` / ``list``。"""
    return _REPEAT_BY_MODE.get(mode, "list")


def repeat_mode(name: str) -> str:
    """SMTC 的循环模式 → 播放器的四种顺序之一。"""
    return _REPEAT_TO_MODE.get(name, MODE_LOOP)


def repeat_name_for_value(value: int) -> str | None:
    """``MediaPlaybackAutoRepeatMode`` 的整数值 → 循环模式名。"""
    return _REPEAT_NAMES.get(int(value))


def action_for_button(button: int) -> str | None:
    """``SystemMediaTransportControlsButton`` 的整数值 → 内部动作名。"""
    return _ACTION_BY_BUTTON.get(int(button))


def metadata_text(song) -> dict[str, str]:
    """把歌曲翻成 SMTC 要的三行文本。

    SMTC 的 ``artist`` 是单个字符串（不是数组），多个歌手用「、」连起来，
    和界面上的展示习惯一致。
    """
    if song is None:
        return {"title": "", "artist": "", "album": "", "album_artist": ""}
    artists = "、".join(part for part in (song.artists or ()) if part)
    album = song.album or ""
    return {
        "title": song.name or "",
        "artist": artists,
        "album": album,
        "album_artist": artists,
    }


def cover_url(song) -> str:
    """SMTC 封面地址。

    网易云的缩略图地址统一换成 600×600；流媒体服务器的封面地址可能带签名或
    ``api_key``，原样使用，不能再追加 ``param``。
    """
    if song is None:
        return ""
    url = (song.pic_url or "").strip()
    if not url:
        return ""
    if song.source:
        return url
    return images.upgrade_url(url, 600)


def cover_path_for(song) -> str:
    """需要读内嵌封面的本地文件路径；已有在线封面时返回空串。"""
    if song is None or song.pic_url:
        return ""
    return song.path or ""


def track_key(song) -> tuple | None:
    """判断「换歌了没有」的键。本地文件用路径，在线歌曲用来源 + ID。"""
    if song is None:
        return None
    return (song.source, int(song.id), song.path or "")


def capabilities(queue_length: int, has_track: bool, duration: float) -> dict[str, bool]:
    """系统浮层上每个按钮是否可用。语义与 MPRIS 那套保持一致。"""
    has_queue = queue_length > 0
    return {
        "play": has_queue,
        "pause": has_track,
        "stop": has_track,
        "next": has_queue,
        "previous": has_queue,
        "seek": has_track and duration > 0,
    }


def timeline_values(position: float, duration: float) -> tuple[float, float, float]:
    """``(起点, 终点, 当前位置)``，单位秒；没有时长时全部归零。"""
    duration = max(0.0, float(duration or 0.0))
    position = max(0.0, float(position or 0.0))
    if duration <= 0:
        return 0.0, 0.0, 0.0
    return 0.0, duration, min(position, duration)


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #

class SmtcService(QObject):
    """把播放器状态暴露成 Windows 媒体会话，并接收系统发来的控制指令。

    没有 WinRT 绑定、或者不在 Windows 上时，所有方法都是安全的空操作。
    """

    #: 工作线程 → 主线程。带上动作名和参数，由 :meth:`_handle_action` 执行
    _actionRequested = pyqtSignal(str, object)

    #: 两次进度上报之间至少隔这么久。系统浮层按秒显示，4 Hz 的动画帧没必要
    #: 全部转发过去；用户拖动进度条会造成大的跳变，那种情况单独放行。
    POSITION_INTERVAL = 0.9
    POSITION_JUMP = 1.5

    def __init__(self, controller, window=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._window = window
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._smtc = None
        self._player = None
        self._cover_dir: str | None = None
        self._last_submit = 0.0
        self._last_position = -1.0

        # 工作线程只读这些值；全部在主线程更新
        self._enabled = False
        self._status = ""
        self._track_key = object()
        self._timeline: tuple[float, float, float] | None = None

        self._actionRequested.connect(self._handle_action)

        controller.trackChanged.connect(self._on_track_changed)
        controller.stateChanged.connect(self._on_state_changed)
        controller.positionChanged.connect(self._on_position_changed)
        controller.modeChanged.connect(self._on_mode_changed)
        controller.queueChanged.connect(self._on_queue_changed)

    # ------------------------------------------------------------ 对外状态

    @property
    def available(self) -> bool:
        return _AVAILABLE and sys.platform == "win32"

    @property
    def running(self) -> bool:
        return self._ready.is_set()

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """起工作线程并注册媒体会话。重复调用无副作用。"""
        if not self.available or self._thread is not None:
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="ncmgui-smtc", daemon=True
        )
        self._thread.start()
        self._submit()

    def shutdown(self) -> None:
        """通知工作线程移除事件处理器并关闭媒体会话；不阻塞退出流程。"""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)  # 把等在队列上的线程叫醒
        except queue.Full:
            pass
        thread.join(timeout=2.0)

    # ------------------------------------------------------------ 主线程更新

    def _on_track_changed(self, _song=None) -> None:
        self._submit()

    def _on_state_changed(self, _state: str = "") -> None:
        self._submit()

    def _on_position_changed(self, position: float, _duration: float = 0.0) -> None:
        now = time.monotonic()
        if (
            now - self._last_submit < self.POSITION_INTERVAL
            and abs(position - self._last_position) < self.POSITION_JUMP
        ):
            return
        self._last_submit = now
        self._last_position = position
        self._submit()

    def _on_mode_changed(self, _mode: str = "") -> None:
        self._submit()

    def _on_queue_changed(self) -> None:
        self._submit()

    def _snapshot(self) -> dict:
        c = self._controller
        song = c.current
        duration = float(c.duration or 0.0)
        return {
            "enabled": song is not None,
            "track_key": track_key(song),
            "metadata": metadata_text(song),
            "cover_url": cover_url(song),
            "cover_path": cover_path_for(song),
            "status": status_name(c.state),
            "position": float(c.position or 0.0),
            "duration": duration,
            "caps": capabilities(len(c.queue), song is not None, duration),
            "repeat": repeat_name(c.mode),
            "shuffle": c.mode == MODE_SHUFFLE,
        }

    def _submit(self) -> None:
        """把最新状态塞进队列。队列满时丢掉最旧的一份，始终保留最新状态。"""
        if self._thread is None:
            return
        snapshot = self._snapshot()
        try:
            self._queue.put_nowait(snapshot)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(snapshot)
        except queue.Full:
            pass

    # ------------------------------------------------------------ 工作线程

    def _thread_main(self) -> None:
        player = None
        smtc = None
        removers: list = []
        try:
            try:
                # Qt 的 COM 只初始化在主线程；这里是新线程，用 MTA 最省事。
                # 已经初始化过时会抛错，忽略即可。
                init_apartment(ApartmentType.MULTI_THREADED)
            except Exception:  # noqa: BLE001 - 已初始化或系统不支持
                pass

            self._cover_dir = tempfile.mkdtemp(prefix="ncm-studio-smtc-")
            player = MediaPlayer()
            player.auto_play = False
            try:
                # 关掉自带的命令管理器：否则它会试图控制这个没有音源的
                # MediaPlayer，按钮事件也不会送到我们的回调里。
                player.command_manager.is_enabled = False
            except Exception:  # noqa: BLE001 - 旧系统没有这个属性
                pass

            smtc = player.system_media_transport_controls
            smtc.is_enabled = False
            removers = self._listen(smtc)
            self._smtc = smtc
            # 必须留着引用：MediaPlayer 被回收时系统媒体会话会一起消失
            self._player = player
            self._ready.set()

            while not self._stop.is_set():
                try:
                    snapshot = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                if snapshot is None:
                    break
                self._apply(snapshot)
        except Exception:  # noqa: BLE001 - 媒体控件不可用不能影响播放
            pass
        finally:
            self._ready.clear()
            self._smtc = None
            self._player = None
            if smtc is not None:
                try:
                    smtc.is_enabled = False
                except Exception:  # noqa: BLE001
                    pass
                for remove, token, _handler in removers:
                    try:
                        remove(token)
                    except Exception:  # noqa: BLE001
                        pass
            if player is not None:
                try:
                    player.system_media_transport_controls.display_updater.clear_all()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    player.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                uninit_apartment()
            except Exception:  # noqa: BLE001
                pass
            if self._cover_dir:
                shutil.rmtree(self._cover_dir, ignore_errors=True)
                self._cover_dir = None

    def _listen(self, smtc) -> list:
        """注册事件处理器，返回 ``(remove, token, handler)`` 列表供退出时反注册。

        列表里一并留着 handler 的引用：WinRT 的事件注册不该被指望替 Python
        对象续命，回调被回收后媒体键会毫无征兆地失灵。

        晚注册失败（旧系统没有某个事件）只跳过那一项，不影响其余按钮。
        """
        listeners = (
            ("add_button_pressed", "remove_button_pressed", self._on_button),
            (
                "add_playback_position_change_requested",
                "remove_playback_position_change_requested",
                self._on_position_requested,
            ),
            (
                "add_auto_repeat_mode_change_requested",
                "remove_auto_repeat_mode_change_requested",
                self._on_repeat_requested,
            ),
            (
                "add_shuffle_enabled_change_requested",
                "remove_shuffle_enabled_change_requested",
                self._on_shuffle_requested,
            ),
        )
        removers: list = []
        for add_name, remove_name, handler in listeners:
            try:
                token = getattr(smtc, add_name)(handler)
            except Exception:  # noqa: BLE001
                continue
            removers.append((getattr(smtc, remove_name), token, handler))
        return removers

    def _apply(self, snap: dict) -> None:
        smtc = self._smtc
        if smtc is None:
            return
        try:
            if snap["enabled"] and not self._enabled:
                # 先把会话打开，再写元数据，系统浮层才会带着完整信息出现
                smtc.is_enabled = True
                self._enabled = True
                # 会话是重新打开的，系统那边已经回到默认值，本地记账一并作废，
                # 让下面把状态、时间轴和元数据完整重写一遍。否则同一首歌
                # 「停止后再播」时状态值没变，会被去重逻辑跳过。
                self._status = ""
                self._timeline = None
                self._track_key = object()
            if not snap["enabled"]:
                if self._enabled:
                    smtc.is_enabled = False
                    self._enabled = False
                return

            self._apply_caps(smtc, snap)
            self._apply_mode(smtc, snap)
            if snap["track_key"] != self._track_key:
                self._apply_metadata(smtc, snap)

            if snap["status"] != self._status:
                smtc.playback_status = _STATUS_ENUM[snap["status"]]
                self._status = snap["status"]

            self._apply_timeline(smtc, snap)
        except Exception:  # noqa: BLE001 - 单个属性失败不该中断整个会话
            pass

    def _apply_caps(self, smtc, snap: dict) -> None:
        """逐项设置按钮可用状态。

        不按快照做去重：媒体会话刚创建时，部分属性可能还没就绪而抛错，
        去重会让失败的那一项永远不会再试。每秒最多一次赋值，代价可以忽略。
        """
        caps = snap["caps"]
        for name, value in (
            ("is_play_enabled", caps["play"]),
            ("is_pause_enabled", caps["pause"]),
            ("is_stop_enabled", caps["stop"]),
            ("is_next_enabled", caps["next"]),
            ("is_previous_enabled", caps["previous"]),
            ("is_fast_forward_enabled", caps["seek"]),
            ("is_rewind_enabled", caps["seek"]),
        ):
            try:
                setattr(smtc, name, value)
            except Exception:  # noqa: BLE001 - 旧系统可能缺某一项
                pass

    def _apply_mode(self, smtc, snap: dict) -> None:
        try:
            smtc.auto_repeat_mode = _REPEAT_ENUM[snap["repeat"]]
        except Exception:  # noqa: BLE001
            pass
        try:
            smtc.shuffle_enabled = bool(snap["shuffle"])
        except Exception:  # noqa: BLE001
            pass

    def _apply_metadata(self, smtc, snap: dict) -> None:
        updater = smtc.display_updater
        # 换歌时先清空，避免新歌没有封面时还挂着上一首的图
        updater.clear_all()
        updater.type = MediaPlaybackType.MUSIC
        music = updater.music_properties
        text = snap["metadata"]
        music.title = text["title"]
        music.artist = text["artist"]
        music.album_artist = text["album_artist"]
        music.album_title = text["album"]
        updater.thumbnail = self._cover_reference(snap)
        updater.update()
        # 只有真正写进去了才记账；失败时下一份快照会重试
        self._track_key = snap["track_key"]

    def _apply_timeline(self, smtc, snap: dict) -> None:
        start, end, position = timeline_values(snap["position"], snap["duration"])
        stamp = (round(start, 1), round(end, 1), round(position, 1))
        if stamp == self._timeline:
            return
        timeline = SystemMediaTransportControlsTimelineProperties()
        timeline.start_time = timedelta(seconds=start)
        timeline.end_time = timedelta(seconds=end)
        timeline.min_seek_time = timedelta(seconds=start)
        timeline.max_seek_time = timedelta(seconds=end)
        timeline.position = timedelta(seconds=position)
        smtc.update_timeline_properties(timeline)
        self._timeline = stamp

    def _cover_reference(self, snap: dict):
        """构造系统能读的封面引用；失败就返回 ``None``，不影响文本信息。"""
        try:
            url = snap["cover_url"]
            if url:
                return RandomAccessStreamReference.create_from_uri(Uri(url))

            path = snap["cover_path"]
            if not path or not self._cover_dir:
                return None
            data = read_artwork(path)
            kind = image_kind(data) if data else None
            if not data or kind is None:
                return None
            suffix, _mime = kind
            digest = hashlib.sha1(
                repr(snap["track_key"]).encode("utf-8")
            ).hexdigest()[:16]
            target = Path(self._cover_dir) / f"{digest}{suffix}"
            target.write_bytes(data)
            return RandomAccessStreamReference.create_from_uri(Uri(target.as_uri()))
        except Exception:  # noqa: BLE001 - 封面不可用是可接受的降级
            return None

    # ------------------------------------------------------------ 事件 → 主线程

    def _on_button(self, _sender, args) -> None:
        try:
            action = action_for_button(int(args.button))
        except Exception:  # noqa: BLE001
            return
        if action:
            self._actionRequested.emit(action, None)

    def _on_position_requested(self, _sender, args) -> None:
        try:
            seconds = args.requested_playback_position.total_seconds()
        except Exception:  # noqa: BLE001
            return
        self._actionRequested.emit("set_position", float(seconds))

    def _on_repeat_requested(self, _sender, args) -> None:
        try:
            name = repeat_name_for_value(int(args.requested_auto_repeat_mode))
        except Exception:  # noqa: BLE001
            return
        if name:
            self._actionRequested.emit("set_repeat", name)

    def _on_shuffle_requested(self, _sender, args) -> None:
        try:
            enabled = bool(args.requested_shuffle_enabled)
        except Exception:  # noqa: BLE001
            return
        self._actionRequested.emit("set_shuffle", enabled)

    def _handle_action(self, action: str, payload) -> None:
        c = self._controller
        if action == "play":
            if c.current is None or c.paused or c.state in (STATE_IDLE, STATE_ERROR):
                c.toggle()
        elif action == "pause":
            if c.current is not None and not c.paused and c.state != STATE_IDLE:
                c.toggle()
        elif action == "stop":
            c.stop()
        elif action == "next":
            c.next()
        elif action == "previous":
            c.previous()
        elif action == "seek_forward":
            c.seek(c.position + SEEK_STEP)
        elif action == "seek_backward":
            c.seek(max(0.0, c.position - SEEK_STEP))
        elif action == "set_position":
            try:
                seconds = float(payload)
            except (TypeError, ValueError):
                return
            c.seek(max(0.0, seconds))
        elif action == "set_repeat":
            c.set_mode(repeat_mode(str(payload)))
        elif action == "set_shuffle":
            c.set_mode(MODE_SHUFFLE if payload else MODE_LOOP)


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from .resolver import Song

    problems: list[str] = []

    def check(label: str, condition: bool) -> None:
        if not condition:
            problems.append(label)

    check("播放中状态", status_name(STATE_PLAYING) == STATUS_PLAYING)
    check("装载中算播放", status_name(STATE_LOADING) == STATUS_PLAYING)
    check("暂停状态", status_name(STATE_PAUSED) == STATUS_PAUSED)
    check("停止状态", status_name(STATE_IDLE) == STATUS_STOPPED)
    check("错误状态", status_name(STATE_ERROR) == STATUS_STOPPED)
    check("未知状态", status_name("???") == STATUS_STOPPED)

    check("单曲循环", repeat_name(MODE_ONE) == "track")
    check("顺序播放", repeat_name(MODE_ORDER) == "none")
    check("列表循环", repeat_name(MODE_LOOP) == "list")
    check("随机报列表循环", repeat_name(MODE_SHUFFLE) == "list")
    check("循环模式回写", repeat_mode("track") == MODE_ONE)
    check("循环模式回写 none", repeat_mode("none") == MODE_ORDER)
    check("循环模式回写 list", repeat_mode("list") == MODE_LOOP)
    check("循环模式未知值", repeat_mode("???") == MODE_LOOP)
    check("循环枚举 0", repeat_name_for_value(0) == "none")
    check("循环枚举 1", repeat_name_for_value(1) == "track")
    check("循环枚举 2", repeat_name_for_value(2) == "list")
    check("循环枚举越界", repeat_name_for_value(9) is None)

    for value, action in (
        (0, "play"),
        (1, "pause"),
        (2, "stop"),
        (4, "seek_forward"),
        (5, "seek_backward"),
        (6, "next"),
        (7, "previous"),
    ):
        check(f"按键 {value}", action_for_button(value) == action)
    check("录音键无动作", action_for_button(3) is None)
    check("频道键无动作", action_for_button(9) is None)

    song = Song(
        id=186016,
        name="晴天",
        artists=("周杰伦", "杨瑞代"),
        album="叶惠美",
        pic_url="https://p1.music.126.net/a.jpg?param=130y130",
    )
    text = metadata_text(song)
    check("标题", text["title"] == "晴天")
    check("多歌手", text["artist"] == "周杰伦、杨瑞代")
    check("专辑", text["album"] == "叶惠美")
    check("专辑歌手", text["album_artist"] == "周杰伦、杨瑞代")
    check("空歌曲", metadata_text(None)["title"] == "")
    check("网易云封面放大", "600y600" in cover_url(song))
    check("在线封面不需要读盘", cover_path_for(song) == "")

    local = Song(id=-7, name="本地", path="/tmp/a.flac", source="local")
    check("本地无封面地址", cover_url(local) == "")
    check("本地封面路径", cover_path_for(local) == "/tmp/a.flac")
    check("本地换歌键", track_key(local) == ("local", -7, "/tmp/a.flac"))
    check("空换歌键", track_key(None) is None)

    check("时间轴常规", timeline_values(30, 100) == (0.0, 100.0, 30.0))
    check("时间轴截断", timeline_values(150, 100) == (0.0, 100.0, 100.0))
    check("时间轴负值", timeline_values(-5, 100) == (0.0, 100.0, 0.0))
    check("时间轴无时长", timeline_values(5, 0) == (0.0, 0.0, 0.0))

    caps = capabilities(3, True, 100)
    check("可播放", caps["play"])
    check("可暂停", caps["pause"])
    check("可切歌", caps["next"] and caps["previous"])
    check("可拖动", caps["seek"])
    empty = capabilities(0, False, 0)
    check("空队列全部禁用", not any(empty.values()))

    if sys.platform != "win32":
        check("非 Windows 不加载 WinRT", not _AVAILABLE)

    if problems:
        print("SMTC 自检失败：")
        for item in problems:
            print(" -", item)
        return 1
    print(f"SMTC 自检通过（WinRT 可用：{_AVAILABLE}）")
    return 0


if __name__ == "__main__":
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    raise SystemExit(_selftest())
