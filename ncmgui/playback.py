"""全局播放控制：队列、播放顺序、进度，以及对 mpv 的唯一一份状态。

以前这些全都长在「我喜欢的音乐」页面里，切到别的页面就既看不到、也控制不了。
现在播放状态上移到这个 controller，页面、底部播放条、播放页都只是视图：

    window.playback.trackChanged.connect(...)
    window.playback.play_at(3)

**线程边界：** :class:`~ncmgui.player.MpvPlayer` 的回调在读线程里发生，controller
把它们转成 Qt 信号再回主线程；所有对 mpv 的写命令都从主线程发出。

**三处竞态保护是原样搬过来的，不要省：**

* ``_play_token`` —— 连点两首歌时，先发出的取链结果可能后到，必须丢掉；
* ``_awaiting_load`` —— ``loadfile replace`` 时旧曲目会先发 ``end-file: stop``，
  而旧曲目恰好自然播完的 ``eof`` 也可能迟到，不能当成新曲目播完；
* ``UrlCache`` + ``_wanted`` —— 直链有 ``expi``，提前作废；取链同时只跑一个。
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import player
from .resolver import Resolver, Song
from .workers import BackgroundTask

# --------------------------------------------------------------------------- #
# 播放顺序
# --------------------------------------------------------------------------- #

MODE_ORDER = "order"
MODE_LOOP = "loop"
MODE_ONE = "one"
MODE_SHUFFLE = "shuffle"

#: ``(显示名, 值)``，界面下拉/按钮直接用
MODES: tuple[tuple[str, str], ...] = (
    ("顺序播放", MODE_ORDER),
    ("列表循环", MODE_LOOP),
    ("单曲循环", MODE_ONE),
    ("随机播放", MODE_SHUFFLE),
)

#: 配置文件里的值认不出时回退到列表循环（与 1.2 的「自动下一首」行为一致）
DEFAULT_MODE = MODE_LOOP


def normalize_mode(value: object) -> str:
    text = str(value or "")
    return text if text in (MODE_ORDER, MODE_LOOP, MODE_ONE, MODE_SHUFFLE) else DEFAULT_MODE


def mode_label(value: str) -> str:
    for label, mode in MODES:
        if mode == value:
            return label
    return value


#: 状态取值。界面按这几个字符串决定按钮与文案。
STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_ERROR = "error"

#: 随机播放时记住多少首历史，供「上一首」回退
_HISTORY_LIMIT = 200


class PlaybackController(QObject):
    """一份全局播放状态。窗口里只应该有这一个实例。"""

    trackChanged = pyqtSignal(object)          # Song | None
    stateChanged = pyqtSignal(str)
    positionChanged = pyqtSignal(float, float)  # position, duration
    volumeChanged = pyqtSignal(int)
    modeChanged = pyqtSignal(str)
    queueChanged = pyqtSignal()
    errorRaised = pyqtSignal(str)
    playerExited = pyqtSignal(int)

    #: mpv 事件从读线程发回来，走队列连接
    _playerEvent = pyqtSignal(str, object)
    _playerFailed = pyqtSignal(str)

    def __init__(
        self,
        cfg,
        *,
        binary: Callable[[], str] | None = None,
        opts: Callable[[], object] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._binary = binary or (lambda: "")
        self._opts = opts or (lambda: None)

        self._queue: list[Song] = []
        self._index: int | None = None
        self._current: Song | None = None
        self._state = STATE_IDLE
        self._position = 0.0
        self._duration = 0.0
        self._paused = False
        self._volume = player.clamp_volume(cfg.get("player.volume", 70))
        self._level = str(cfg.get("player.level", "exhigh") or "exhigh")
        self._mode = normalize_mode(cfg.get("player.mode", DEFAULT_MODE))
        self._history: list[int] = []

        self._player: player.MpvPlayer | None = None
        self._cache = player.UrlCache()
        self._local_provider: Callable[[Song], Path | None] | None = None

        self._play_token = 0
        self._wanted: tuple[int, Song, str] | None = None
        self._fetch_task: BackgroundTask | None = None
        self._awaiting_load = False
        #: 装载完成前拖动的目标位置，file-loaded 之后再真正发给 mpv
        self._pending_seek: float | None = None
        self._shutting_down = False

        #: 进度外推的锚点：mpv 的 time-pos 按整秒节流，界面 250ms 刷一次
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()

        self._playerEvent.connect(self._on_player_event)
        self._playerFailed.connect(self._on_player_error)
        self.playerExited.connect(self._on_player_exited)

        self._ticker = QTimer(self)
        self._ticker.setInterval(250)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()

    # ------------------------------------------------------------ 只读状态

    @property
    def queue(self) -> list[Song]:
        return list(self._queue)

    @property
    def current(self) -> Song | None:
        return self._current

    @property
    def index(self) -> int | None:
        return self._index

    @property
    def state(self) -> str:
        return self._state

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def position(self) -> float:
        return self._position

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def level(self) -> str:
        return self._level

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def player_running(self) -> bool:
        return self._player is not None and self._player.running

    # ------------------------------------------------------------ 装配

    def set_local_provider(self, provider: Callable[[Song], Path | None] | None) -> None:
        """红心页注入「这首歌下载过没有」。本地文件直接播路径，不取直链。"""
        self._local_provider = provider

    # ------------------------------------------------------------ 队列

    def set_queue(self, songs: Sequence[Song], start_index: int | None = None) -> None:
        """替换队列。

        正在播的歌如果还在新队列里，就按歌曲 ID 重新定位，播放不中断
        （红心页刷新后常见）；不在了就停下，避免「列表里没有、声音还在」。
        """
        self._queue = list(songs)
        self._history.clear()

        if self._current is not None:
            found = self._index_of(self._current.id)
            if found is None:
                self._hard_stop()
                self._current = None
                self._index = None
                self._state = STATE_IDLE
                self._paused = True
                self._position = 0.0
                self._duration = 0.0
                self._anchor_pos = 0.0
                self._anchor_at = time.monotonic()
                self.positionChanged.emit(0.0, 0.0)
                self._emit_track()
                self.stateChanged.emit(self._state)
            else:
                self._index = found
        elif start_index is not None and 0 <= start_index < len(self._queue):
            self._index = start_index
        else:
            self._index = None

        self.queueChanged.emit()

    def clear_queue(self) -> None:
        self._hard_stop()
        self._queue = []
        self._index = None
        self._current = None
        self._history.clear()
        self._state = STATE_IDLE
        self._paused = True
        self._position = 0.0
        self._duration = 0.0
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()
        self.queueChanged.emit()
        self.positionChanged.emit(0.0, 0.0)
        self._emit_track()
        self.stateChanged.emit(self._state)

    def play_at(self, index: int, *, remember: bool = True) -> None:
        """播放队列里的第 ``index`` 首。"""
        if not (0 <= index < len(self._queue)):
            return
        previous = self._index
        if remember and previous is not None and previous != index:
            self._remember(previous)
        self._index = index
        self._start_song(self._queue[index])

    def play_song(self, song: Song) -> None:
        """播放一首歌；不在队列里就追加到末尾，再播。"""
        for row, item in enumerate(self._queue):
            if item.id == song.id:
                self.play_at(row)
                return
        self._queue.append(song)
        self._index = len(self._queue) - 1
        self.queueChanged.emit()
        self._start_song(song)

    def _index_of(self, song_id: int) -> int | None:
        for row, song in enumerate(self._queue):
            if song.id == song_id:
                return row
        return None

    def _remember(self, index: int) -> None:
        self._history.append(index)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[: len(self._history) - _HISTORY_LIMIT]

    # ------------------------------------------------------------ 传输控制

    def toggle(self) -> None:
        """播放 / 暂停。没有当前曲目时从队列第一首开始。"""
        if self._current is None:
            if self._queue:
                self.play_at(self._index if self._index is not None else 0)
            return

        if self._state in (STATE_IDLE, STATE_ERROR) or not self.player_running:
            self.play_at(self._index if self._index is not None else 0)
            return

        # 装载阶段也要响应：mpv 可能已经出声，只是 file-loaded 还没轮到主线程。
        # 以前这里直接 return，主线程一忙就会「点了暂停没反应」
        self._paused = not self._paused
        self._anchor_pos = self._position
        self._anchor_at = time.monotonic()
        try:
            if self._player is not None:
                self._player.pause(self._paused)
        except player.PlayerError as exc:
            self._fail(str(exc))
            return
        if self._awaiting_load:
            # 暂停要立刻有反馈；恢复播放则继续显示「正在缓冲…」，
            # 等 file-loaded 把状态推进到 playing
            self._set_state(STATE_PAUSED if self._paused else STATE_LOADING)
            return
        self._set_state(STATE_PAUSED if self._paused else STATE_PLAYING)

    def next(self, manual: bool = True) -> None:
        """下一首。自然结束时 ``manual=False``（单曲循环只在这里重播）。"""
        if not self._queue:
            return
        if self._index is None:
            self.play_at(0)
            return

        if self._mode == MODE_SHUFFLE:
            self.play_at(self._random_index())
            return

        if self._mode == MODE_ONE and not manual:
            self.play_at(self._index)
            return

        target = self._index + 1
        if target >= len(self._queue):
            if self._mode == MODE_LOOP:
                target = 0
            else:
                self._hard_stop()
                self._set_state(STATE_IDLE)
                return
        self.play_at(target)

    def previous(self) -> None:
        if not self._queue:
            return

        # 随机播放的「上一首」优先回历史，否则随机回去会让用户找不到刚才那首
        if self._mode == MODE_SHUFFLE and self._history:
            target = self._history.pop()
            if 0 <= target < len(self._queue):
                self.play_at(target, remember=False)
                return

        if self._index is None:
            self.play_at(len(self._queue) - 1)
            return
        target = self._index - 1
        if target < 0:
            target = len(self._queue) - 1 if self._mode != MODE_ORDER else 0
        self.play_at(target)

    def seek(self, seconds: float) -> None:
        value = max(0.0, float(seconds or 0.0))
        if self._duration > 0:
            value = min(value, self._duration)
        self._position = value
        self._anchor_pos = value
        self._anchor_at = time.monotonic()
        self.positionChanged.emit(self._position, self._duration)
        if self._awaiting_load:
            # 文件还没装载完，此刻的 seek 会落在 idle 上被 mpv 丢掉 ——
            # 先记下来，file-loaded 后补发
            self._pending_seek = value
            return
        if self._player is not None and self.player_running:
            try:
                self._player.seek(value)
            except player.PlayerError as exc:
                self._fail(str(exc))

    def set_volume(self, value: int) -> None:
        self._volume = player.clamp_volume(value, self._volume)
        if self._player is not None and self.player_running:
            try:
                self._player.set_volume(self._volume)
            except player.PlayerError:
                pass
        self.volumeChanged.emit(self._volume)

    def set_level(self, level: str) -> None:
        """改动对下一首生效，不影响正在播放的这一首。"""
        self._level = str(level or "exhigh")

    def set_mode(self, mode: str) -> None:
        value = normalize_mode(mode)
        if value == self._mode:
            return
        self._mode = value
        if value != MODE_SHUFFLE:
            self._history.clear()
        self.modeChanged.emit(value)

    def cycle_mode(self) -> None:
        order = [mode for _label, mode in MODES]
        self.set_mode(order[(order.index(self._mode) + 1) % len(order)])

    def stop(self) -> None:
        """停下当前曲目，保留队列与当前歌曲信息。"""
        self._hard_stop()
        self._state = STATE_IDLE
        self._paused = True
        self._position = 0.0
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()
        self.positionChanged.emit(0.0, self._duration)
        self.stateChanged.emit(self._state)

    def shutdown(self) -> None:
        self._shutting_down = True
        self._play_token += 1
        self._wanted = None
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._ticker.stop()

    def save_config(self) -> None:
        self.cfg.set("player.volume", int(self._volume))
        self.cfg.set("player.level", self._level)
        self.cfg.set("player.mode", self._mode)

    # ------------------------------------------------------------ 起播

    def _start_song(self, song: Song) -> None:
        self._current = song
        self._position = 0.0
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()
        self._duration = song.duration / 1000 if song.duration else 0.0
        self._paused = False
        self._awaiting_load = True
        self._pending_seek = None
        self.positionChanged.emit(0.0, self._duration)
        self._emit_track()
        self._set_state(STATE_LOADING)

        if player.find_mpv(self.cfg.get("player.mpv_path", "")) is None:
            self._fail(self._missing_mpv_message())
            return

        if getattr(song, "path", ""):
            local = Path(song.path)
            if not local.is_file():
                self._fail(f"本地文件不存在：{local}")
                return
            self._load(str(local))
            return

        local = self._local_path(song)
        if local is not None:
            self._load(str(local))
            return

        cached = self._cache.get((song.id, self._level))
        if cached is not None:
            self._load(cached.url, source=cached)
            return

        if not self._binary():
            self._fail("找不到 ncmctl，无法获取播放地址。请在「设置」里指定它的路径。")
            return

        self._play_token += 1
        self._wanted = (self._play_token, song, self._level)
        self._pump_wanted()

    def _pump_wanted(self) -> None:
        """同时只有一个取链任务在飞；最新的那个排队等它完成。"""
        if self._fetch_task is not None and self._fetch_task.is_running:
            return
        wanted = self._wanted
        if wanted is None:
            return
        token, song, level = wanted
        binary = self._binary()
        opts = self._opts()

        def work():
            resolver = Resolver(binary, opts=opts, timeout="20s")
            return resolver.song_url(song.id, level)

        task = BackgroundTask(work, parent=self)
        self._fetch_task = task
        task.done.connect(lambda source, t=token: self._on_url(t, source, ""))
        task.failed.connect(lambda message, t=token: self._on_url(t, None, message))
        task.start()

    def _on_url(self, token: int, source, error: str) -> None:
        self._fetch_task = None
        wanted = self._wanted

        # 用户已经点了别的歌：这次结果作废，接着去取最新的那首
        if wanted is None or token != wanted[0]:
            self._pump_wanted()
            return

        self._wanted = None
        _, song, level = wanted

        if source is None:
            self._fail(f"《{song.name}》无法播放：{error}")
            return

        self._cache.put((song.id, level), source)
        self._load(source.url, source=source)

    def _load(self, target: str, source=None) -> None:
        try:
            mpv = self._ensure_player()
            mpv.load(target)
        except player.PlayerError as exc:
            self._fail(str(exc))
            return

        if source is not None and getattr(source, "trial", False):
            self.errorRaised.emit(
                f"《{self._current.name if self._current else ''}》只是试听片段 —— "
                "完整版需要会员。"
            )

    def _ensure_player(self) -> player.MpvPlayer:
        if self._player is not None and self._player.running:
            return self._player

        binary = player.find_mpv(self.cfg.get("player.mpv_path", ""))
        if binary is None:
            raise player.MpvNotFound(self._missing_mpv_message())

        mpv = player.MpvPlayer(
            binary,
            on_event=self._playerEvent.emit,
            on_exit=self.playerExited.emit,
            on_error=self._playerFailed.emit,
            volume=self._volume,
        )
        mpv.start()
        self._player = mpv
        return mpv

    def _local_path(self, song: Song) -> Path | None:
        path = str(getattr(song, "path", "") or "")
        if path:
            candidate = Path(path)
            return candidate if candidate.is_file() else None
        if self._local_provider is not None:
            try:
                return self._local_provider(song)
            except Exception:  # noqa: BLE001 - provider 是界面给的，坏了不该拖垮播放
                return None
        return None

    def _missing_mpv_message(self) -> str:
        if sys.platform == "darwin":
            return (
                "没有找到 mpv。macOS 首版不随包提供播放器，请先安装 "
                "brew install mpv，再到「设置 → 播放器」里确认路径。"
            )
        return (
            "没有找到 mpv。Linux 可执行 sudo apt install mpv；"
            "官方 AppImage / Windows 安装包已内置 mpv。"
            "若仍报错，请在「设置 → 播放器」里指定路径。"
        )

    # ------------------------------------------------------------ mpv 事件

    def _on_player_event(self, kind: str, payload) -> None:
        if self._shutting_down:
            return
        data = payload or {}

        if kind == "file-loaded":
            self._awaiting_load = False
            # mpv 用 --pause=yes 启动（防止进程一拉起就出声），装载完要按
            # 界面上的暂停状态同步一次 —— 装载期间用户可能已经点过暂停，
            # 无条件解除会把那次点击吞掉
            if self._player is not None and self._player.running:
                try:
                    self._player.pause(self._paused)
                    if self._pending_seek is not None:
                        self._player.seek(self._pending_seek)
                except player.PlayerError:
                    pass
                self._pending_seek = None
            self._anchor_pos = self._position
            self._anchor_at = time.monotonic()
            self._set_state(STATE_PAUSED if self._paused else STATE_PLAYING)
            return

        if kind == "property":
            name = data.get("name")
            value = data.get("value")
            if name == "duration" and value:
                try:
                    self._duration = float(value)
                except (TypeError, ValueError):
                    pass
                self.positionChanged.emit(self._position, self._duration)
            elif name == "time-pos":
                try:
                    self._position = float(value) if value is not None else 0.0
                except (TypeError, ValueError):
                    self._position = 0.0
                self._anchor_pos = self._position
                self._anchor_at = time.monotonic()
                self.positionChanged.emit(self._position, self._duration)
            elif name == "pause":
                if value is None:
                    # 属性暂时不可用（比如 idle）时别当成 False，否则界面
                    # 会在用户刚点完暂停后被这次空事件改回播放
                    return
                self._paused = bool(value)
                self._anchor_pos = self._position
                self._anchor_at = time.monotonic()
                if self._current is not None:
                    self._set_state(STATE_PAUSED if self._paused else STATE_PLAYING)
            elif name == "volume" and value is not None:
                try:
                    self._volume = player.clamp_volume(value, self._volume)
                except (TypeError, ValueError):
                    pass
                self.volumeChanged.emit(self._volume)
            return

        if kind == "end-file":
            reason = str(data.get("reason") or "")
            if reason == "eof":
                # 旧曲目被 replace 时可能先到 eof，丢给下一首就错位了
                if self._awaiting_load:
                    return
                QTimer.singleShot(0, self._on_eof)
            elif reason == "error":
                if self._current is not None:
                    self._cache.invalidate((self._current.id, self._level))
                detail = str(data.get("error") or "未知错误")
                self._fail(
                    f"播放失败（{detail}），音频地址可能已过期，再点一次试试。"
                )
            # reason == "stop"：loadfile replace 的正常产物，忽略
            return

        if kind == "shutdown":
            self._set_state(STATE_IDLE)

    def _on_eof(self) -> None:
        if self._shutting_down or self._awaiting_load:
            return
        if self._mode == MODE_ONE:
            self.play_at(self._index if self._index is not None else 0)
            return
        self.next(manual=False)
        # order 模式在末尾停下时 next 已经把状态置 idle；这里不用再做什么

    def _on_player_exited(self, code: int) -> None:
        if self._shutting_down:
            return
        self._player = None
        self._set_state(STATE_IDLE)
        self.errorRaised.emit(f"播放器意外退出（{code}），点播放可以重新启动。")

    def _on_player_error(self, message: str) -> None:
        if self._shutting_down:
            return
        self.errorRaised.emit(message)

    # ------------------------------------------------------------ 内部

    def _random_index(self) -> int:
        if len(self._queue) <= 1:
            return 0
        choices = [row for row in range(len(self._queue)) if row != self._index]
        return random.choice(choices)

    def _tick(self) -> None:
        if self._state != STATE_PLAYING or self._paused or self._current is None:
            return
        elapsed = max(0.0, time.monotonic() - self._anchor_at)
        position = self._anchor_pos + elapsed
        if self._duration > 0:
            position = min(position, self._duration)
        self._position = position
        self.positionChanged.emit(position, self._duration)

    def _hard_stop(self) -> None:
        self._play_token += 1
        self._wanted = None
        if self._player is not None and self._player.running:
            try:
                self._player.stop_playback()
            except player.PlayerError:
                pass

    def _fail(self, message: str) -> None:
        self._set_state(STATE_ERROR)
        self.errorRaised.emit(message)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self.stateChanged.emit(state)

    def _emit_track(self) -> None:
        self.trackChanged.emit(self._current)


# --------------------------------------------------------------------------- #
# 纯函数：队列小工具（给界面和自检用）
# --------------------------------------------------------------------------- #

def total_duration(songs: Iterable[Song]) -> float:
    """队列总时长（秒）。接口没给时长的按 0 算。"""
    return sum(song.duration / 1000 for song in songs if song.duration > 0)


def clamp_position(value: float, duration: float) -> float:
    """把位置收进 ``[0, duration]``；时长未知时只保证非负。"""
    value = max(0.0, float(value or 0.0))
    return min(value, duration) if duration > 0 else value
