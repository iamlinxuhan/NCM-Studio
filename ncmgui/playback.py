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
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import dsp, localmusic, player
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

#: 发出 loadfile 之后最多等多久。本地文件与直链都是秒级加载，超过这个时间
#: 还没有 ``file-loaded``，说明事件通道或 mpv 本身已经出问题了。
_LOAD_TIMEOUT = 12.0

#: 自动恢复的节流：60 秒内最多重启 3 次，避免 mpv 一崩就无限重生。
_RECOVER_WINDOW = 60.0
_RECOVER_LIMIT = 3


class PlaybackController(QObject):
    """一份全局播放状态。窗口里只应该有这一个实例。"""

    trackChanged = pyqtSignal(object)          # Song | None
    stateChanged = pyqtSignal(str)
    positionChanged = pyqtSignal(float, float)  # position, duration
    volumeChanged = pyqtSignal(int)
    #: 实际音质变了（本地文件解析出格式，或直链接口返回了降级后的档位）
    qualityChanged = pyqtSignal()
    modeChanged = pyqtSignal(str)
    queueChanged = pyqtSignal()
    errorRaised = pyqtSignal(str)
    playerExited = pyqtSignal(int)
    #: 一首歌开始装载；统计与「最近播放」用
    trackStarted = pyqtSignal(object)          # Song
    #: 一首歌自然播完（不包括手动切歌）
    trackEnded = pyqtSignal(object)            # Song
    #: 音频工具状态变化。没有运行的播放器时只更新状态，下次装载时生效。
    speedChanged = pyqtSignal(float)
    pitchChanged = pyqtSignal(float)
    equalizerChanged = pyqtSignal()
    abLoopChanged = pyqtSignal(object, object)  # (a, b) 秒；未设置为 None
    audioInfoChanged = pyqtSignal()
    audioDevicesChanged = pyqtSignal()
    #: (剩余秒数, 是否「播完当前曲」)；倒计时取消后剩余为 0
    sleepTimerChanged = pyqtSignal(int, bool)
    sleepTimerFinished = pyqtSignal(str)

    #: mpv 事件从读线程发回来，走队列连接
    _playerEvent = pyqtSignal(str, object)
    _playerFailed = pyqtSignal(str)
    #: 事件通道断了 / mpv 进程意外退出。带上出事的那个 MpvPlayer 对象，
    #: 免得旧播放器的迟到通知把刚换上的新播放器也当成死的
    _playerLost = pyqtSignal(object)
    _playerExited = pyqtSignal(object, int)

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
        #: 当前这首歌**实际**拿到的音质：本地文件是「无损 44.1kHz/16bit」
        #: 这样的描述，流媒体是接口返回的档位值（可能低于请求值）。
        #: 空串表示还不确定 —— 取链还没回来，或者本地文件头读不出来。
        self._quality = ""
        self._mode = normalize_mode(cfg.get("player.mode", DEFAULT_MODE))
        self._history: list[int] = []

        # ---- 音频工具 -------------------------------------------------
        try:
            self._speed = max(0.5, min(2.0, float(cfg.get("player.speed", 1.0))))
        except (TypeError, ValueError):
            self._speed = 1.0
        try:
            self._pitch = max(-12.0, min(12.0, float(cfg.get("player.pitch", 0.0))))
        except (TypeError, ValueError):
            self._pitch = 0.0
        equalizer = cfg.get("player.equalizer", {})
        if not isinstance(equalizer, dict):
            equalizer = {}
        self._eq_enabled = bool(equalizer.get("enabled", False))
        self._eq_gains = dsp.normalize_gains(equalizer.get("gains", ()))
        try:
            self._eq_preamp = max(-12.0, min(12.0, float(equalizer.get("preamp", 0.0))))
        except (TypeError, ValueError):
            self._eq_preamp = 0.0
        self._loudness = bool(cfg.get("player.loudness", False))
        self._audio_device = str(cfg.get("player.audio_device", "") or "")
        raw_volumes = cfg.get("player.device_volumes", {})
        self._device_volumes: dict[str, int] = {}
        if isinstance(raw_volumes, dict):
            for name, value in raw_volumes.items():
                try:
                    self._device_volumes[str(name)] = player.clamp_volume(value)
                except (TypeError, ValueError):
                    continue
        self._capabilities: dsp.Capabilities | None = None
        self._capabilities_binary = ""
        self._audio_info: dict[str, object] = {}
        self._audio_devices: tuple[tuple[str, str], ...] = ()
        self._ab_a: float | None = None
        self._ab_b: float | None = None

        # ---- 定时关闭 -------------------------------------------------
        self._sleep_deadline: float | None = None
        self._sleep_after_track = False
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setInterval(1000)
        self._sleep_timer.timeout.connect(self._sleep_tick)

        self._player: player.MpvPlayer | None = None
        self._cache = player.UrlCache()
        self._local_provider: Callable[[Song], Path | None] | None = None
        #: 流媒体取链：Song(source=subsonic/jellyfin) -> StreamSource。
        #: 返回的对象只需有 url/level/headers 三个属性，播放层不依赖具体适配器
        self._stream_provider: Callable[[Song], object] | None = None

        self._play_token = 0
        self._wanted: tuple[int, Song, str] | None = None
        self._fetch_task: BackgroundTask | None = None
        self._awaiting_load = False
        #: 这次装载开始的时刻；watchdog 据此判断 mpv 是否已经卡住
        self._awaiting_since: float | None = None
        #: 装载完成前拖动的目标位置，file-loaded 之后再真正发给 mpv
        self._pending_seek: float | None = None
        self._shutting_down = False
        #: 最近几次自动恢复的时刻，用于节流
        self._recover_times: list[float] = []

        #: 进度外推的锚点：mpv 的 time-pos 按整秒节流，界面 250ms 刷一次
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()

        self._playerEvent.connect(self._on_player_event)
        self._playerFailed.connect(self._on_player_error)
        self._playerLost.connect(self._on_player_lost)
        self._playerExited.connect(self._on_player_exited)

        self._ticker = QTimer(self)
        self._ticker.setInterval(250)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()

        #: 队列写配置的防抖。拖拽排序会连着发很多次 queueChanged
        self._queue_timer = QTimer(self)
        self._queue_timer.setSingleShot(True)
        self._queue_timer.setInterval(800)
        self._queue_timer.timeout.connect(self._persist_queue)
        self.queueChanged.connect(self._schedule_persist)
        self.trackChanged.connect(self._schedule_persist)
        self._restore_queue()

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
        """**请求**的流媒体档位。它不代表实际播出来的音质，界面上别拿它冒充。"""
        return self._level

    @property
    def quality(self) -> str:
        """当前曲目**实际**的音质；还不知道时为空串。"""
        return self._quality

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def player_running(self) -> bool:
        return self._player is not None and self._player.running

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def pitch(self) -> float:
        return self._pitch

    @property
    def equalizer_enabled(self) -> bool:
        return self._eq_enabled

    @property
    def equalizer_gains(self) -> tuple[float, ...]:
        return self._eq_gains

    @property
    def equalizer_preamp(self) -> float:
        return self._eq_preamp

    @property
    def loudness(self) -> bool:
        return self._loudness

    @property
    def audio_device(self) -> str:
        return self._audio_device

    @property
    def audio_devices(self) -> tuple[tuple[str, str], ...]:
        return self._audio_devices

    @property
    def audio_info(self) -> dict[str, object]:
        return dict(self._audio_info)

    @property
    def ab_loop(self) -> tuple[float | None, float | None]:
        return self._ab_a, self._ab_b

    @property
    def sleep_remaining(self) -> int:
        if self._sleep_after_track:
            return 0
        if self._sleep_deadline is None:
            return 0
        return max(0, int(round(self._sleep_deadline - time.monotonic())))

    @property
    def sleep_after_track(self) -> bool:
        return self._sleep_after_track

    @property
    def mpv_binary(self) -> str:
        return player.find_mpv(self.cfg.get("player.mpv_path", "")) or ""

    def capabilities(self, *, refresh: bool = False) -> dsp.Capabilities:
        """当前 mpv 的能力矩阵。结果按可执行文件路径缓存。"""
        binary = self.mpv_binary
        if (
            refresh
            or self._capabilities is None
            or self._capabilities_binary != binary
        ):
            self._capabilities = dsp.probe(binary)
            self._capabilities_binary = binary
        return self._capabilities

    # ------------------------------------------------------------ 装配

    def set_local_provider(self, provider: Callable[[Song], Path | None] | None) -> None:
        """红心页注入「这首歌下载过没有」。本地文件直接播路径，不取直链。"""
        self._local_provider = provider

    def set_stream_provider(self, provider: Callable[[Song], object] | None) -> None:
        """注入流媒体取链（Subsonic/Jellyfin）。取链在后台线程执行。"""
        self._stream_provider = provider

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

    # ------------------------------------------------------------ 队列编辑

    def insert_next(self, songs: Song | Sequence[Song]) -> int:
        """把歌插到当前曲目后面（没有当前曲目时插到最前面）。

        返回实际插入的条数。正在播放的歌不会被打断。
        """
        items = [songs] if isinstance(songs, Song) else list(songs)
        items = [song for song in items if song is not None]
        if not items:
            return 0
        position = 0 if self._index is None else self._index + 1
        self._queue[position:position] = items
        self.queueChanged.emit()
        return len(items)

    def append(self, songs: Song | Sequence[Song]) -> int:
        """追加到队尾。"""
        items = [songs] if isinstance(songs, Song) else list(songs)
        items = [song for song in items if song is not None]
        if not items:
            return 0
        self._queue.extend(items)
        if self._index is None and self._current is not None:
            self._index = self._index_of(self._current.id)
        self.queueChanged.emit()
        return len(items)

    def remove_at(self, index: int) -> Song | None:
        """移除第 ``index`` 首。返回被移除的歌，越界返回 ``None``。"""
        if not (0 <= index < len(self._queue)):
            return None
        removed = self._queue.pop(index)
        if self._index is None:
            pass
        elif index < self._index:
            self._index -= 1
        elif index == self._index:
            # 移掉正在播的歌：停下，但队列保留。索引落在原位置的下一首上
            was_current = self._current is not None and self._current.id == removed.id
            self._index = index if index < len(self._queue) else (
                len(self._queue) - 1 if self._queue else None
            )
            if was_current:
                self._hard_stop()
                self._current = None
                self._state = STATE_IDLE
                self._paused = True
                self._position = 0.0
                self._duration = 0.0
                self._anchor_pos = 0.0
                self._anchor_at = time.monotonic()
                self.positionChanged.emit(0.0, 0.0)
                self._emit_track()
                self.stateChanged.emit(self._state)
        if not self._queue:
            self._index = None
        self.queueChanged.emit()
        return removed

    def remove_songs(self, song_ids: Iterable[int]) -> int:
        """按歌曲 ID 批量移除（保留第一次出现的匹配）。返回移除条数。"""
        wanted = {int(song_id) for song_id in song_ids}
        if not wanted:
            return 0
        keep = [song for song in self._queue if song.id not in wanted]
        removed = len(self._queue) - len(keep)
        if not removed:
            return 0
        current = self._current
        self._queue = keep
        if current is None:
            self._index = None
        else:
            found = self._index_of(current.id)
            if found is None:
                self._hard_stop()
                self._current = None
                self._index = None
                self._state = STATE_IDLE
                self._paused = True
                self._position = 0.0
                self._duration = 0.0
                self.positionChanged.emit(0.0, 0.0)
                self._emit_track()
                self.stateChanged.emit(self._state)
            else:
                self._index = found
        self.queueChanged.emit()
        return removed

    def move(self, source: int, target: int) -> bool:
        """把第 ``source`` 首移到 ``target``（目标索引按移动后的列表算）。"""
        if not (0 <= source < len(self._queue)):
            return False
        target = max(0, min(int(target), len(self._queue) - 1))
        if source == target:
            return False
        song = self._queue.pop(source)
        self._queue.insert(target, song)
        if self._index == source:
            self._index = target
        elif source < self._index <= target:
            self._index -= 1
        elif target <= self._index < source:
            self._index += 1
        self.queueChanged.emit()
        return True

    def shuffle_remaining(self) -> None:
        """随机打乱当前曲目之后的顺序；当前曲目原位不动。"""
        start = 0 if self._index is None else self._index + 1
        tail = self._queue[start:]
        if len(tail) < 2:
            return
        random.shuffle(tail)
        self._queue[start:] = tail
        self.queueChanged.emit()

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
        if self._audio_device:
            self._device_volumes[self._audio_device] = int(self._volume)
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

    # ------------------------------------------------------------ 音频工具

    def _apply_property(self, name: str, value: object) -> None:
        if self._player is None or not self.player_running:
            return
        try:
            self._player.set_property(name, value)
        except player.PlayerError as exc:
            self.errorRaised.emit(str(exc))

    def _apply_audio_filters(self) -> None:
        if self._player is None or not self.player_running:
            return
        caps = self.capabilities()
        chain = dsp.build_audio_filters(
            equalizer=self._eq_enabled and caps.equalizer,
            gains=self._eq_gains,
            preamp=self._eq_preamp,
            loudness=self._loudness and caps.loudnorm,
            semitones=self._pitch if caps.rubberband else 0.0,
        )
        self._apply_property("af", chain)

    def _apply_player_settings(self, mpv: player.MpvPlayer) -> None:
        """新播放器起来后把倍速、滤镜、输出设备补上。"""
        caps = self.capabilities()
        try:
            if caps.speed:
                mpv.set_property("speed", self._speed)
            if caps.pitch_correction:
                mpv.set_property("audio-pitch-correction", True)
            chain = dsp.build_audio_filters(
                equalizer=self._eq_enabled and caps.equalizer,
                gains=self._eq_gains,
                preamp=self._eq_preamp,
                loudness=self._loudness and caps.loudnorm,
                semitones=self._pitch if caps.rubberband else 0.0,
            )
            if chain:
                mpv.set_property("af", chain)
            if self._audio_device:
                mpv.set_property("audio-device", self._audio_device)
        except player.PlayerError:
            # 某个滤镜不可用不应打断播放；对话框里会显示能力说明
            pass

    def set_speed(self, value: float) -> None:
        try:
            value = max(0.5, min(2.0, round(float(value), 2)))
        except (TypeError, ValueError):
            return
        if abs(value - self._speed) < 0.001:
            return
        self._speed = value
        self._apply_property("speed", value)
        self.speedChanged.emit(value)

    def set_pitch(self, semitones: float) -> None:
        try:
            value = max(-12.0, min(12.0, round(float(semitones), 1)))
        except (TypeError, ValueError):
            return
        if abs(value - self._pitch) < 0.001:
            return
        self._pitch = value
        self._apply_audio_filters()
        self.pitchChanged.emit(value)

    def set_equalizer(
        self,
        *,
        enabled: bool | None = None,
        gains=None,
        preamp: float | None = None,
    ) -> None:
        if enabled is not None:
            self._eq_enabled = bool(enabled)
        if gains is not None:
            self._eq_gains = dsp.normalize_gains(gains)
        if preamp is not None:
            try:
                self._eq_preamp = max(-12.0, min(12.0, float(preamp)))
            except (TypeError, ValueError):
                pass
        self._apply_audio_filters()
        self.equalizerChanged.emit()

    def set_loudness(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._loudness:
            return
        self._loudness = enabled
        self._apply_audio_filters()
        self.equalizerChanged.emit()

    def set_audio_device(self, name: str) -> None:
        name = str(name or "")
        # 记住当前设备的音量，再切到新设备并按它自己的记忆恢复
        self._device_volumes[self._audio_device or "auto"] = int(self._volume)
        self._audio_device = name
        self._apply_property("audio-device", name or "auto")
        remembered = self._device_volumes.get(name or "auto")
        if remembered is not None:
            self.set_volume(remembered)
        self.audioInfoChanged.emit()

    def set_ab_a(self, seconds: float | None) -> None:
        self._ab_a = None if seconds is None else max(0.0, float(seconds))
        self._apply_ab_loop()

    def set_ab_b(self, seconds: float | None) -> None:
        self._ab_b = None if seconds is None else max(0.0, float(seconds))
        self._apply_ab_loop()

    def clear_ab_loop(self) -> None:
        self._ab_a = None
        self._ab_b = None
        self._apply_ab_loop()

    def _apply_ab_loop(self) -> None:
        if self._player is not None and self.player_running:
            try:
                self._player.set_property(
                    "ab-loop-a", self._ab_a if self._ab_a is not None else "no"
                )
                self._player.set_property(
                    "ab-loop-b", self._ab_b if self._ab_b is not None else "no"
                )
            except player.PlayerError as exc:
                self.errorRaised.emit(str(exc))
        self.abLoopChanged.emit(self._ab_a, self._ab_b)

    # ------------------------------------------------------------ 定时关闭

    def start_sleep(self, minutes: int) -> None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return
        if minutes <= 0:
            return
        self._sleep_after_track = False
        self._sleep_deadline = time.monotonic() + minutes * 60
        self._sleep_timer.start()
        self.sleepTimerChanged.emit(minutes * 60, False)

    def sleep_after_current_track(self) -> None:
        self._sleep_after_track = True
        self._sleep_deadline = None
        self._sleep_timer.stop()
        self.sleepTimerChanged.emit(0, True)

    def cancel_sleep(self) -> None:
        was_active = self._sleep_deadline is not None or self._sleep_after_track
        self._sleep_deadline = None
        self._sleep_after_track = False
        self._sleep_timer.stop()
        if was_active:
            self.sleepTimerChanged.emit(0, False)

    def _sleep_tick(self) -> None:
        if self._sleep_deadline is None:
            self._sleep_timer.stop()
            return
        remaining = self.sleep_remaining
        if remaining > 0:
            self.sleepTimerChanged.emit(remaining, False)
            return
        self.cancel_sleep()
        if not self._paused:
            self.toggle()
        self.sleepTimerFinished.emit("定时关闭：已暂停播放")

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
        self._sleep_timer.stop()

    def save_config(self) -> None:
        self.cfg.set("player.volume", int(self._volume))
        self.cfg.set("player.level", self._level)
        self.cfg.set("player.mode", self._mode)
        self.cfg.set("player.speed", float(self._speed))
        self.cfg.set("player.pitch", float(self._pitch))
        self.cfg.set("player.equalizer", {
            "enabled": bool(self._eq_enabled),
            "gains": list(self._eq_gains),
            "preamp": float(self._eq_preamp),
        })
        self.cfg.set("player.loudness", bool(self._loudness))
        self.cfg.set("player.audio_device", self._audio_device)
        # 只保留最近 30 个设备的音量记忆
        items = list(self._device_volumes.items())[-30:]
        self.cfg.set("player.device_volumes", dict(items))
        self._persist_queue()

    # ------------------------------------------------------------ 队列持久化

    @staticmethod
    def _song_to_dict(song: Song) -> dict:
        return {
            "id": int(song.id),
            "name": song.name,
            "artists": list(song.artists),
            "fee": int(song.fee),
            "album": song.album,
            "duration": int(song.duration),
            "pic_url": song.pic_url,
            "path": song.path,
            "quality": song.quality,
            "source": song.source,
            "stream_id": song.stream_id,
        }

    @staticmethod
    def _song_from_dict(data: object) -> Song | None:
        if not isinstance(data, dict):
            return None
        try:
            song_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            return None
        name = str(data.get("name") or "")
        if not song_id or not name:
            return None
        raw_artists = data.get("artists")
        artists = tuple(
            str(artist) for artist in raw_artists
            if artist
        ) if isinstance(raw_artists, list) else ()
        try:
            fee = int(data.get("fee") or 0)
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            fee, duration = 0, 0
        return Song(
            id=song_id,
            name=name,
            artists=artists,
            fee=fee,
            album=str(data.get("album") or ""),
            duration=duration,
            pic_url=str(data.get("pic_url") or ""),
            path=str(data.get("path") or ""),
            quality=str(data.get("quality") or ""),
            source=str(data.get("source") or ""),
            stream_id=str(data.get("stream_id") or ""),
        )

    def _restore_queue(self) -> None:
        """恢复上次的队列，但**不自动播放**。

        只保存歌曲标识和进度，不保存已经解析出来的播放地址 —— 直链有
        ``expi``，下次启动时多半已经过期。
        """
        try:
            raw = self.cfg.get("player.queue", [])
        except Exception:  # noqa: BLE001 - 配置坏了也不能影响启动
            raw = []
        songs = [self._song_from_dict(item) for item in raw] if isinstance(raw, list) else []
        songs = [song for song in songs if song is not None]
        if not songs:
            return
        self._queue = songs
        try:
            index = int(self.cfg.get("player.queue_index", -1))
        except (TypeError, ValueError):
            index = -1
        if not (0 <= index < len(songs)):
            index = 0
        self._index = index
        self._current = songs[index]
        self._duration = self._current.duration / 1000 if self._current.duration else 0.0
        try:
            position = float(self.cfg.get("player.position", 0.0) or 0.0)
        except (TypeError, ValueError):
            position = 0.0
        if self._duration > 0:
            position = min(max(0.0, position), self._duration)
        self._position = position
        self._anchor_pos = position
        self._anchor_at = time.monotonic()
        self._paused = True
        self._state = STATE_IDLE

    def _schedule_persist(self) -> None:
        if self._shutting_down:
            return
        self._queue_timer.start()

    def _persist_queue(self) -> None:
        # 队列可能上千首，但只是几万个字符；写入由 Config 自己节流/原子替换
        self.cfg.set("player.queue", [self._song_to_dict(song) for song in self._queue])
        self.cfg.set("player.queue_index", -1 if self._index is None else int(self._index))
        self.cfg.set("player.position", float(self._position))
        try:
            self.cfg.save()
        except OSError:
            # 配置目录只读/磁盘满时不能影响播放；下次正常退出还会再写一次
            pass

    # ------------------------------------------------------------ 起播

    def _start_song(self, song: Song) -> None:
        # 上一次取链如果还在飞，先把结果作废：否则它回来时会把刚点的新歌
        # 顶掉（本地文件这条路以前不会推进 token）
        self._play_token += 1
        self._wanted = None

        # A-B 循环与音频信息都只对当前文件有意义
        if self._ab_a is not None or self._ab_b is not None:
            self.clear_ab_loop()
        self._audio_info = {}
        self.audioInfoChanged.emit()

        self._current = song
        # 换歌就先忘掉上一首的实际音质；本地分支马上填回来，流媒体等取链结果
        self._set_quality("")
        self._position = 0.0
        self._anchor_pos = 0.0
        self._anchor_at = time.monotonic()
        self._duration = song.duration / 1000 if song.duration else 0.0
        self._paused = False
        self._awaiting_load = True
        #: 真正的装载时刻要等 _load() 发出命令；取链阶段不该被 watchdog 误判
        self._awaiting_since = None
        self._pending_seek = None
        self.positionChanged.emit(0.0, self._duration)
        self._emit_track()
        self.trackStarted.emit(song)
        self._set_state(STATE_LOADING)

        if player.find_mpv(self.cfg.get("player.mpv_path", "")) is None:
            self._fail(self._missing_mpv_message())
            return

        if getattr(song, "path", ""):
            local = Path(song.path)
            if not local.is_file():
                self._fail(f"本地文件不存在：{local}")
                return
            # 本地音乐页扫描时已经算过音质；从别处塞进来的 Song 才现读文件头
            self._set_quality(song.quality or localmusic.describe_quality(local))
            self._load(str(local))
            return

        local = self._local_path(song)
        if local is not None:
            # 红心歌曲命中了下载目录里的文件：这时候播的是文件本身，
            # 音质跟播放设置里的流媒体档位没有关系
            self._set_quality(localmusic.describe_quality(local))
            self._load(str(local))
            return

        if song.source and song.source != "local":
            self._start_stream(song)
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

    def _start_stream(self, song: Song) -> None:
        """流媒体歌曲：后台向服务器换一个带认证的播放地址。"""
        provider = self._stream_provider
        if provider is None:
            self._fail("未配置流媒体服务器，无法播放这首歌")
            return
        token = self._play_token

        def work():
            return provider(song)

        task = BackgroundTask(work, parent=self)
        self._fetch_task = task
        task.done.connect(lambda source, t=token: self._on_stream_source(t, source, ""))
        task.failed.connect(
            lambda message, t=token: self._on_stream_source(t, None, message)
        )
        task.start()

    def _on_stream_source(self, token: int, source, error: str) -> None:
        if token != self._play_token:
            return  # 用户已经点了别的歌；别把新任务的引用也清掉
        self._fetch_task = None
        if source is None:
            name = self._current.name if self._current else ""
            self._fail(f"《{name}》无法播放：{error}")
            return
        url = str(getattr(source, "url", "") or "")
        if not url:
            self._fail("流媒体服务器没有返回播放地址")
            return
        headers = getattr(source, "headers", ()) or ()
        self._load(url, source=source, headers=headers)

    def _load(self, target: str, source=None, headers=()) -> None:
        if source is not None:
            # 接口会在非会员请求高音质时悄悄退回一档，这里记下的才是实际档位
            self._set_quality(getattr(source, "level", "") or "")
        if (
            source is not None
            and getattr(source, "trial", False)
            and not self.cfg.get("player.allow_trial", False)
        ):
            name = self._current.name if self._current else ""
            self._fail(
                f"《{name}》只提供试听片段，完整版需要会员。"
                "可在「设置 → 播放器」里允许试听播放。"
            )
            return
        try:
            mpv = self._ensure_player()
            mpv.load(target, headers=headers)
        except player.PlayerError as exc:
            self._fail(str(exc))
            return

        # 从这一刻起等 file-loaded；watchdog 会盯着，超时就重启播放器
        self._awaiting_since = time.monotonic()

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

        # 回调在 mpv 自己的线程里触发；先把对象放进 holder，回调才能把
        # 「出事的是哪一个播放器」一并带回来。恢复流程里旧播放器的迟到
        # 通知必须能和新播放器区分开。
        holder: list[player.MpvPlayer] = []

        def exited(code: int) -> None:
            if holder:
                self._playerExited.emit(holder[0], code)

        def lost() -> None:
            if holder:
                self._playerLost.emit(holder[0])

        mpv = player.MpvPlayer(
            binary,
            on_event=self._playerEvent.emit,
            on_exit=exited,
            on_error=self._playerFailed.emit,
            on_lost=lost,
            volume=self._volume,
        )
        holder.append(mpv)
        mpv.start()
        self._player = mpv
        self._apply_player_settings(mpv)
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
            self._awaiting_since = None
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
                if self._awaiting_load:
                    # 装载期间 mpv 会自己回一条 pause=false（loadfile 之后
                    # 我们显式解除过暂停）。用户在这期间点的暂停已经写进
                    # _paused，别让这条回声把它覆盖掉 —— 否则从暂停状态
                    # 自动恢复时，新播放器会直接开始播放。
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
            elif name in (
                "audio-codec-name", "audio-params", "audio-bitrate",
                "current-ao", "audio-device", "audio-device-list",
            ):
                self._on_audio_property(name, value)
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

    def _on_audio_property(self, name: str, value) -> None:
        if name == "audio-device-list":
            devices: list[tuple[str, str]] = []
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    device = str(item.get("name") or "")
                    if device and all(device != row[0] for row in devices):
                        devices.append((device, str(item.get("description") or "")))
            new_devices = tuple(devices)
            if new_devices and new_devices != self._audio_devices:
                self._audio_devices = new_devices
                self.audioDevicesChanged.emit()
            return

        if name == "audio-params":
            info = value if isinstance(value, dict) else {}
            self._audio_info["samplerate"] = info.get("samplerate")
            self._audio_info["channels"] = info.get("channel-count")
            self._audio_info["format"] = info.get("format")
            self._audio_info["channel_layout"] = info.get("channel-layout")
        elif value is not None:
            key = {
                "audio-codec-name": "codec",
                "audio-bitrate": "bitrate",
                "current-ao": "ao",
                "audio-device": "device",
            }.get(name, name)
            self._audio_info[key] = value
        self.audioInfoChanged.emit()

    def _on_eof(self) -> None:
        if self._shutting_down or self._awaiting_load:
            return
        if self._current is not None:
            self.trackEnded.emit(self._current)
        if self._sleep_after_track:
            self._sleep_after_track = False
            self._sleep_deadline = None
            self._sleep_timer.stop()
            self._hard_stop()
            self._set_state(STATE_IDLE)
            self.sleepTimerChanged.emit(0, False)
            self.sleepTimerFinished.emit("定时关闭：当前歌曲已播完")
            return
        if self._mode == MODE_ONE:
            self.play_at(self._index if self._index is not None else 0)
            return
        self.next(manual=False)
        # order 模式在末尾停下时 next 已经把状态置 idle；这里不用再做什么

    def _on_player_lost(self, gone: object) -> None:
        """IPC 读线程断了：mpv 进程也许还在，但已经收不到任何事件。"""
        if self._shutting_down or gone is not self._player:
            return
        self._recover_player("事件通道断开")

    def _on_player_exited(self, gone: object, code: int) -> None:
        if self._shutting_down or gone is not self._player:
            # 主动停掉或已经被恢复流程替换掉的旧播放器，不用管
            return
        self._player = None

        recoverable = (
            self._current is not None
            and self._state in (STATE_LOADING, STATE_PLAYING, STATE_PAUSED)
        )
        if recoverable:
            # 自动恢复成功后不弹错误，用户只会听到音乐停一下又接上
            self._recover_player(f"播放器意外退出（{code}）")
            return

        self._set_state(STATE_IDLE)
        self.playerExited.emit(code)
        self.errorRaised.emit(f"播放器意外退出（{code}），点播放可以重新启动。")

    def _on_player_error(self, message: str) -> None:
        if self._shutting_down:
            return
        self.errorRaised.emit(message)

    def _recover_player(self, reason: str) -> None:
        """mpv 死了或者卡住了：按原歌曲、原进度、原暂停状态重新拉起来。

        只在真的有歌在播/在装载时调用。短时间内反复失败就停下并报错，
        否则一个坏掉的 mpv 会陷入「崩溃 → 重启 → 再崩溃」的死循环。
        """
        if self._shutting_down:
            return

        gone = self._player
        self._player = None
        self._awaiting_load = False
        self._awaiting_since = None
        if gone is not None:
            # stop() 可能要在退出命令上等几秒；别让它卡住界面线程
            threading.Thread(
                target=self._dispose_player,
                args=(gone,),
                name="ncmgui-mpv-dispose",
                daemon=True,
            ).start()

        if self._current is None or self._state not in (
            STATE_LOADING,
            STATE_PLAYING,
            STATE_PAUSED,
        ):
            self._set_state(STATE_IDLE)
            return

        now = time.monotonic()
        self._recover_times = [
            stamp for stamp in self._recover_times if now - stamp < _RECOVER_WINDOW
        ]
        if len(self._recover_times) >= _RECOVER_LIMIT:
            self._fail(f"播放器连续出错（{reason}），已停止自动恢复，请重新点播。")
            return
        self._recover_times.append(now)

        position = self._position
        paused = self._paused
        song = self._current

        self._start_song(song)

        # _start_song 会把进度清零、暂停状态复位；这里接回用户看到的位置
        self._paused = paused
        self._anchor_pos = position
        self._anchor_at = time.monotonic()
        if position > 0:
            self._pending_seek = position
            self._position = position
            self.positionChanged.emit(position, self._duration)

    @staticmethod
    def _dispose_player(mpv: player.MpvPlayer) -> None:
        try:
            mpv.stop()
        except Exception:  # noqa: BLE001 - 清理失败不该影响新的播放器
            pass

    # ------------------------------------------------------------ 内部

    def _random_index(self) -> int:
        if len(self._queue) <= 1:
            return 0
        choices = [row for row in range(len(self._queue)) if row != self._index]
        return random.choice(choices)

    def _tick(self) -> None:
        self._check_loading()
        if self._state != STATE_PLAYING or self._paused or self._current is None:
            return
        elapsed = max(0.0, time.monotonic() - self._anchor_at)
        position = self._anchor_pos + elapsed
        if self._duration > 0:
            position = min(position, self._duration)
        self._position = position
        self.positionChanged.emit(position, self._duration)

    def _check_loading(self) -> None:
        """装载看门狗：mpv 没崩、但 ``file-loaded`` 迟迟不来。

        事件通道断掉时 ``on_lost`` 会通知；这里兜住另一种情况 —— 连接还在、
        mpv 却因为某种原因不再响应。取链阶段（``_wanted`` / ``_fetch_task``）
        不算，网络慢不能怪 mpv。
        """
        if (
            self._state != STATE_LOADING
            or not self._awaiting_load
            or self._awaiting_since is None
            or self._wanted is not None
            or (self._fetch_task is not None and self._fetch_task.is_running)
        ):
            return
        if time.monotonic() - self._awaiting_since <= _LOAD_TIMEOUT:
            return
        self._recover_player("加载超时")

    def _hard_stop(self) -> None:
        self._play_token += 1
        self._wanted = None
        self._awaiting_load = False
        self._awaiting_since = None
        if self._player is not None and self._player.running:
            try:
                self._player.stop_playback()
            except player.PlayerError:
                pass

    def _fail(self, message: str) -> None:
        self._awaiting_load = False
        self._awaiting_since = None
        self._set_state(STATE_ERROR)
        self.errorRaised.emit(message)

    def _set_quality(self, quality: str) -> None:
        quality = str(quality or "")
        if quality == self._quality:
            return
        self._quality = quality
        self.qualityChanged.emit()

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
