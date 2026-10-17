"""MPRIS（Linux 系统媒体控制）接口。

蓝牙耳机、键盘上的播放/暂停键并不直接发给某个程序：Linux 桌面把耳机传来的
AVRCP 指令交给 ``mpris-proxy``，再以 MPRIS（D-Bus 上的
``org.mpris.MediaPlayer2.Player``）的方法调用转给"当前媒体播放器"。谁注册了
这个接口，谁就能收到这些指令。

mpv 本身不实现 MPRIS，而本程序又是用 ``--no-config --load-scripts=no`` 起的
mpv，所以以前整条链路是断的：耳机按暂停，桌面上没有任何播放器响应。这里用
``dbus-next`` 注册一份最小的 MPRIS 服务，把 D-Bus 调用桥回
:class:`~ncmgui.playback.PlaybackController`。

只做 MPRIS 规范里播放器真正用得到的那部分：

* ``PlaybackStatus`` / ``Metadata`` / ``Position`` / ``Volume`` 等属性；
* ``PlayPause`` / ``Play`` / ``Pause`` / ``Stop`` / ``Next`` / ``Previous`` /
  ``Seek`` / ``SetPosition``；
* ``LoopStatus`` 与 ``Shuffle``（对应界面上的四种播放顺序）。

D-Bus 调用发生在后台 asyncio 线程，不能直接碰 Qt 控件，所以所有动作都通过
``_actionRequested`` 信号排到主线程执行；反过来，主线程只更新几个缓存字段，
``PropertiesChanged`` 用 ``call_soon_threadsafe`` 交给 D-Bus 线程发。

Windows 用的是 SMTC、macOS 用的是 Now Playing，不是 MPRIS；本模块在那些平台
上是空操作（``dbus-next`` 也不在依赖里）。
"""

import asyncio
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from . import APP_NAME, images
from .playback import (
    MODE_ONE,
    MODE_ORDER,
    MODE_SHUFFLE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_LOADING,
    STATE_PAUSED,
    STATE_PLAYING,
)

try:  # Linux 之外的平台不装 dbus-next，导入失败时整个服务退化为空操作
    from dbus_next import Variant
    from dbus_next.aio import MessageBus
    from dbus_next.service import (
        PropertyAccess,
        ServiceInterface,
        dbus_property,
        method,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于平台与安装方式
    _AVAILABLE = False

#: D-Bus 上的服务名。``mpris-proxy`` 按接口名发现播放器，后缀是本程序的标识。
BUS_NAME = "org.mpris.MediaPlayer2.ncmstudio"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
#: ``org.mpris.MediaPlayer2.DesktopEntry`` 要求给出 .desktop 文件名（不含后缀）
DESKTOP_ENTRY = "ncm-studio"

_STATUS_BY_STATE = {
    STATE_PLAYING: "Playing",
    STATE_LOADING: "Playing",
    STATE_PAUSED: "Paused",
    STATE_IDLE: "Stopped",
    STATE_ERROR: "Stopped",
}


def track_id(song) -> str:
    """``mpris:trackid`` 必须是合法的 D-Bus 对象路径。

    本地歌曲的 ID 是负数（见 :mod:`ncmgui.localmusic`），对象路径里不能有
    ``-``，取绝对值即可；流媒体 ID 本来就是正数。
    """
    if song is None:
        return f"{OBJECT_PATH}/Track/none"
    return f"{OBJECT_PATH}/Track/{abs(int(song.id))}"


def metadata_for(song) -> dict:
    """把 :class:`~ncmgui.resolver.Song` 翻译成 MPRIS 的 ``Metadata``。

    值的类型必须写成 ``Variant``：``a{sv}`` 里每个值都带自己的签名，dbus-next
    不会替我们猜（``mpris:length`` 是 int64，``xesam:artist`` 是字符串数组）。
    """
    if song is None:
        return {}

    meta: dict = {
        "mpris:trackid": Variant("o", track_id(song)),
        "xesam:title": Variant("s", song.name or ""),
    }
    if song.artists:
        meta["xesam:artist"] = Variant("as", list(song.artists))
    if song.album:
        meta["xesam:album"] = Variant("s", song.album)
    if song.duration and song.duration > 0:
        # Song.duration 是毫秒，MPRIS 用微秒
        meta["mpris:length"] = Variant("x", int(song.duration) * 1000)
    if song.pic_url:
        meta["mpris:artUrl"] = Variant("s", images.upgrade_url(song.pic_url, 600))
    if song.path:
        try:
            meta["xesam:url"] = Variant("s", Path(song.path).as_uri())
        except ValueError:
            pass
    return meta


def loop_status_for(mode: str) -> str:
    if mode == MODE_ONE:
        return "Track"
    if mode == MODE_ORDER:
        return "None"
    return "Playlist"


def mode_for_loop_status(value: str) -> str:
    if value == "Track":
        return MODE_ONE
    if value == "None":
        return MODE_ORDER
    return "loop"


if _AVAILABLE:

    class _RootInterface(ServiceInterface):
        """``org.mpris.MediaPlayer2``：身份信息，以及 Raise / Quit。"""

        def __init__(self, service: "MprisService") -> None:
            super().__init__("org.mpris.MediaPlayer2")
            self._service = service

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> "s":  # noqa: N802 - D-Bus 属性名
            return APP_NAME

        @dbus_property(access=PropertyAccess.READ)
        def DesktopEntry(self) -> "s":  # noqa: N802
            return DESKTOP_ENTRY

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> "as":  # noqa: N802
            # 不实现 OpenUri：本程序的播放链路要先解析歌曲 ID
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> "as":  # noqa: N802
            return []

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> "b":  # noqa: N802
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> "b":  # noqa: N802
            return True

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> "b":  # noqa: N802
            return False

        @method()
        def Raise(self) -> None:  # noqa: N802
            self._service.dispatch("raise")

        @method()
        def Quit(self) -> None:  # noqa: N802
            self._service.dispatch("quit")

    class _PlayerInterface(ServiceInterface):
        """``org.mpris.MediaPlayer2.Player``：播放控制与状态。"""

        def __init__(self, service: "MprisService") -> None:
            super().__init__("org.mpris.MediaPlayer2.Player")
            self._service = service

        # ------------------------------------------------------------ 只读状态

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":  # noqa: N802
            return self._service.status

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":  # noqa: N802
            return self._service.metadata

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":  # noqa: N802
            return self._service.position_us

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> "d":  # noqa: N802
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":  # noqa: N802
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":  # noqa: N802
            return self._service.can_play

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":  # noqa: N802
            return self._service.can_pause

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":  # noqa: N802
            return self._service.can_go_next

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":  # noqa: N802
            return self._service.can_go_previous

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":  # noqa: N802
            return self._service.can_seek

        # ------------------------------------------------------------ 可写属性

        @dbus_property(access=PropertyAccess.READWRITE)
        def Volume(self) -> "d":  # noqa: N802
            return self._service.volume / 100.0

        @Volume.setter
        def Volume(self, value: "d") -> None:  # noqa: N802
            self._service.dispatch("set_volume", value)

        @dbus_property(access=PropertyAccess.READWRITE)
        def LoopStatus(self) -> "s":  # noqa: N802
            return self._service.loop_status

        @LoopStatus.setter
        def LoopStatus(self, value: "s") -> None:  # noqa: N802
            self._service.dispatch("set_loop", value)

        @dbus_property(access=PropertyAccess.READWRITE)
        def Shuffle(self) -> "b":  # noqa: N802
            return self._service.shuffle

        @Shuffle.setter
        def Shuffle(self, value: "b") -> None:  # noqa: N802
            self._service.dispatch("set_shuffle", value)

        # ------------------------------------------------------------ 方法

        @method()
        def PlayPause(self) -> None:  # noqa: N802
            self._service.dispatch("toggle")

        @method()
        def Play(self) -> None:  # noqa: N802
            self._service.dispatch("play")

        @method()
        def Pause(self) -> None:  # noqa: N802
            self._service.dispatch("pause")

        @method()
        def Stop(self) -> None:  # noqa: N802
            self._service.dispatch("stop")

        @method()
        def Next(self) -> None:  # noqa: N802
            self._service.dispatch("next")

        @method()
        def Previous(self) -> None:  # noqa: N802
            self._service.dispatch("previous")

        @method()
        def Seek(self, offset: "x") -> None:  # noqa: N802
            self._service.dispatch("seek", int(offset))

        @method()
        def SetPosition(self, track: "o", position: "x") -> None:  # noqa: N802
            self._service.dispatch(
                "set_position", {"track_id": str(track), "position": int(position)}
            )

        @method()
        def OpenUri(self, uri: "s") -> None:  # noqa: N802
            # 不实现：ID 之外的曲目没法走网易云取链
            return


class MprisService(QObject):
    """把播放器状态暴露成 MPRIS，并接收桌面/耳机发来的控制指令。

    没有 dbus-next、或者不在 Linux 上时，所有方法都是安全的空操作。
    """

    #: D-Bus 线程 → 主线程。带上动作名和参数，由 :meth:`_handle_action` 执行
    _actionRequested = pyqtSignal(str, object)

    def __init__(self, controller, window=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._window = window
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False
        self._root_iface = None
        self._player_iface = None

        self._status = _STATUS_BY_STATE.get(controller.state, "Stopped")
        self._metadata = metadata_for(controller.current)
        self._position_us = int(max(0.0, controller.position) * 1_000_000)
        self._duration_us = int(max(0.0, controller.duration) * 1_000_000)
        self._volume = int(controller.volume)
        self._loop_status = loop_status_for(controller.mode)
        self._shuffle = controller.mode == MODE_SHUFFLE
        self._can_play = bool(controller.queue)
        self._can_pause = controller.current is not None
        self._can_go_next = bool(controller.queue)
        self._can_go_previous = bool(controller.queue)
        self._can_seek = controller.duration > 0

        self._actionRequested.connect(self._handle_action)

        controller.trackChanged.connect(self._on_track_changed)
        controller.stateChanged.connect(self._on_state_changed)
        controller.positionChanged.connect(self._on_position_changed)
        controller.volumeChanged.connect(self._on_volume_changed)
        controller.modeChanged.connect(self._on_mode_changed)
        controller.queueChanged.connect(self._on_queue_changed)

    # ------------------------------------------------------------ 对外状态

    @property
    def available(self) -> bool:
        return _AVAILABLE and sys.platform.startswith("linux")

    @property
    def running(self) -> bool:
        return self._player_iface is not None

    @property
    def status(self) -> str:
        return self._status

    @property
    def metadata(self) -> dict:
        return self._metadata

    @property
    def position_us(self) -> int:
        return self._position_us

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def loop_status(self) -> str:
        return self._loop_status

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def can_play(self) -> bool:
        return self._can_play

    @property
    def can_pause(self) -> bool:
        return self._can_pause

    @property
    def can_go_next(self) -> bool:
        return self._can_go_next

    @property
    def can_go_previous(self) -> bool:
        return self._can_go_previous

    @property
    def can_seek(self) -> bool:
        return self._can_seek

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """在后台线程连上会话总线并注册服务。重复调用无副作用。"""
        if not self.available or self._thread is not None:
            return
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main, name="ncmgui-mpris", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        """通知 D-Bus 线程断开连接；不阻塞退出流程。"""
        self._stop_requested = True
        loop = self._loop
        event = self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass
        self._thread = None

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:  # noqa: BLE001 - 媒体键不可用不该影响播放
            pass

    async def _serve(self) -> None:
        try:
            bus = await MessageBus().connect()
        except Exception:  # noqa: BLE001 - 没有会话总线（ssh、纯 tty）
            return

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()

        self._root_iface = _RootInterface(self)
        self._player_iface = _PlayerInterface(self)
        bus.export(OBJECT_PATH, self._root_iface)
        bus.export(OBJECT_PATH, self._player_iface)
        try:
            await bus.request_name(BUS_NAME)
        except Exception:  # noqa: BLE001
            bus.disconnect()
            self._root_iface = None
            self._player_iface = None
            return

        await self._stop_event.wait()
        bus.disconnect()
        self._root_iface = None
        self._player_iface = None

    # ------------------------------------------------------------ 主线程更新

    def _on_track_changed(self, song) -> None:
        self._metadata = metadata_for(song)
        self._can_pause = song is not None
        self._can_seek = bool(song is not None and self._duration_us > 0)
        self._notify(
            {
                "Metadata": self._metadata,
                "CanPause": self._can_pause,
                "CanSeek": self._can_seek,
            }
        )

    def _on_state_changed(self, state: str) -> None:
        status = _STATUS_BY_STATE.get(state, "Stopped")
        if status == self._status:
            return
        self._status = status
        self._notify({"PlaybackStatus": status})

    def _on_position_changed(self, position: float, duration: float) -> None:
        self._position_us = int(max(0.0, position) * 1_000_000)
        self._duration_us = int(max(0.0, duration) * 1_000_000)
        self._can_seek = self._duration_us > 0

    def _on_volume_changed(self, volume: int) -> None:
        self._volume = int(volume)
        self._notify({"Volume": self._volume / 100.0})

    def _on_mode_changed(self, mode: str) -> None:
        loop_status = loop_status_for(mode)
        shuffle = mode == MODE_SHUFFLE
        changed = {}
        if loop_status != self._loop_status:
            self._loop_status = loop_status
            changed["LoopStatus"] = loop_status
        if shuffle != self._shuffle:
            self._shuffle = shuffle
            changed["Shuffle"] = shuffle
        if changed:
            self._notify(changed)

    def _on_queue_changed(self) -> None:
        can_go = bool(self._controller.queue)
        changed = {}
        for attr, key in (
            ("_can_play", "CanPlay"),
            ("_can_go_next", "CanGoNext"),
            ("_can_go_previous", "CanGoPrevious"),
        ):
            if getattr(self, attr) != can_go:
                setattr(self, attr, can_go)
                changed[key] = can_go
        if changed:
            self._notify(changed)

    # ------------------------------------------------------------ D-Bus → 主线程

    def dispatch(self, action: str, payload=None) -> None:
        """在 D-Bus 线程里被调用，把动作排队到主线程。"""
        self._actionRequested.emit(action, payload)

    def _handle_action(self, action: str, payload) -> None:
        c = self._controller
        if action == "toggle":
            c.toggle()
        elif action == "play":
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
        elif action == "seek":
            c.seek(c.position + float(payload or 0) / 1_000_000)
            self._emit_seeked()
        elif action == "set_position":
            song = c.current
            if song is not None and isinstance(payload, dict):
                if str(payload.get("track_id", "")) == track_id(song):
                    c.seek(float(payload.get("position", 0)) / 1_000_000)
                    self._emit_seeked()
        elif action == "set_volume":
            try:
                value = int(round(float(payload) * 100))
            except (TypeError, ValueError):
                return
            c.set_volume(max(0, min(100, value)))
        elif action == "set_loop":
            c.set_mode(mode_for_loop_status(str(payload)))
        elif action == "set_shuffle":
            c.set_mode(MODE_SHUFFLE if payload else "loop")
        elif action == "raise":
            self._raise_window()
        elif action == "quit":
            self._close_window()

    def _raise_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            # 最小化时先还原；最大化保持原样，只把窗口提到前面
            if window.isMinimized():
                window.showNormal()
            else:
                window.show()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            pass

    def _close_window(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.close()
        except RuntimeError:
            pass

    # ------------------------------------------------------------ 主线程 → D-Bus

    def _notify(self, props: dict) -> None:
        iface = self._player_iface
        loop = self._loop
        if iface is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(iface.emit_properties_changed, props)
        except RuntimeError:
            pass

    def _emit_seeked(self) -> None:
        iface = self._player_iface
        loop = self._loop
        if iface is None or loop is None:
            return
        position = self._position_us

        def emit() -> None:
            try:
                iface.Seeked(position)
            except Exception:  # noqa: BLE001
                pass

        try:
            loop.call_soon_threadsafe(emit)
        except RuntimeError:
            pass


def _self_test() -> int:
    """离线自检：只验纯函数，不碰会话总线。"""
    from .resolver import Song

    problems: list[str] = []
    song = Song(
        id=-7,
        name="夜航",
        artists=("甲", "乙"),
        album="专辑",
        duration=185_000,
        pic_url="https://p1.music.126.net/a.jpg?param=130y130",
        path="/tmp/夜航.flac",
    )

    if track_id(song) != f"{OBJECT_PATH}/Track/7":
        problems.append(f"本地歌曲 trackid 不对：{track_id(song)}")
    if track_id(None) != f"{OBJECT_PATH}/Track/none":
        problems.append("空歌曲的 trackid 不对")

    if _AVAILABLE:
        meta = metadata_for(song)
        expected = {
            "mpris:trackid": ("o", track_id(song)),
            "xesam:title": ("s", "夜航"),
            "xesam:artist": ("as", ["甲", "乙"]),
            "xesam:album": ("s", "专辑"),
            "mpris:length": ("x", 185_000_000),
            "xesam:url": ("s", "file:///tmp/%E5%A4%9C%E8%88%AA.flac"),
        }
        for key, (signature, value) in expected.items():
            got = meta.get(key)
            if got is None:
                problems.append(f"Metadata 缺 {key}")
            elif got.signature != signature or got.value != value:
                problems.append(
                    f"Metadata[{key}] 应为 {signature} {value!r}，实际 "
                    f"{got.signature} {got.value!r}"
                )
        if metadata_for(None) != {}:
            problems.append("没有歌曲时 Metadata 应为空")

    for mode, expected_status in (
        (MODE_ORDER, "None"),
        (MODE_ONE, "Track"),
        ("loop", "Playlist"),
        (MODE_SHUFFLE, "Playlist"),
    ):
        if loop_status_for(mode) != expected_status:
            problems.append(f"{mode} 的 LoopStatus 应为 {expected_status}")
    for status, expected_mode in (("None", MODE_ORDER), ("Track", MODE_ONE), ("Playlist", "loop")):
        if mode_for_loop_status(status) != expected_mode:
            problems.append(f"LoopStatus {status} 应映射到 {expected_mode}")

    if problems:
        for problem in problems:
            print("×", problem)
        return 1
    print(f"MPRIS 自检通过（dbus-next {'可用' if _AVAILABLE else '缺失，仅验纯函数'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
