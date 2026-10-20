"""把播放记录同步到网易云账号的听歌记录。

网易云网页本身用两条播放日志：

* 真正开始出声时发 ``startPlay``；
* 这一轮结束（切歌、播完、停止）时发 ``play``，带上实际收听秒数。

报文形状以 2026-10 的网页端 Firefox 抓包为准：动作名是 ``startPlay``
（大写 P），``play`` 里没有 ``end`` 字段。

服务端据此更新账号的听歌记录与听歌排行。请求走网页端同一个出口
（``clientlogusf.music.163.com/weapi/feedback/weblog``），实现见
:meth:`ncmgui.api.client.ApiClient.scrobble`。

计时不在这里重算：直接复用 :class:`~ncmgui.history_recorder.HistoryRecorder`
的信号。只有一轮累计听满 5 秒才会收到 ``playQualified``，这时才补发
``startPlay``；快速切歌不会在几秒内制造一串开始日志，``startPlay`` 与
``play`` 也总是成对出现。

**账号隔离**：每条日志在入队时就把当时的 ``MUSIC_U`` 和 ``ApiClient``
一起固定下来。后台线程真正发送前会重新读一遍 Cookie 文件，取一份
:class:`~ncmgui.api.cookies.CookieSnapshot`，比对其中的 ``music_u``；
退出登录或切换账号时这条日志直接丢弃。比对通过后，请求头的 Cookie 和
CSRF 都用这份快照，不再回头读 CookieJar——核对与组包之间即使又换了号，
也不会串到新账号上。

**会话代数**：每条任务还记下入队时的代数。换号、登出、关闭同步、改状态
目录/代理（``reload_api``）或退出程序时，代数递增并清空待发队列；工作
线程在消费前、发送器在真正组包前各核对一次。队列是串行、有界的，因此
关闭后最多只剩一条已经进入发送函数、刚通过检查的任务，不会出现“关掉了
还在刷”的情况。

**限流与熔断**：同一账号每小时最多 120 条、每天最多 200 条日志；
接口连续 3 次返回限流就暂停 10 分钟，并通过 ``pausedChanged`` 通知界面。

**默认关闭。** 这不是公开 API，属于网页端私有接口；功能可能随官方改版
失效，也可能带来账号风控风险。只在用户明确开启后才上报，失败一律静默，
绝不打断播放。

上游 ncmctl 也有 ``scrobble`` 命令，但那是「刷够 300 首」的打卡工具：
把整首歌的时长当作收听时长、只发 ``play``，官方帮助里自己标注了高风控
风险。本模块只上报真实发生的播放，不采用那套伪造时长的做法。

    .venv/bin/python -m ncmgui.scrobble
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from .api.client import ApiClient
from .api.errors import ApiError, RateLimited
from .config import Config
from .resolver import Song

#: 配置项：是否把播放记录同步到网易云
CONFIG_KEY = "player.scrobble"

#: 低于这个实际收听时长就不往云端报。
#:
#: 与本地历史记录的门槛一致：快速试听、连续切歌的轮次既没有同步价值，
#: 又会在短时间内制造一串很短的播放日志。少发一条就少一次暴露。
MIN_LISTENED_MS = 5000

#: 同一账号每小时最多发多少条日志（``startPlay`` 与 ``play`` 合计）。
#: 正常听歌一小时远到不了这个量；超过说明行为异常，宁可少记。
MAX_LOGS_PER_HOUR = 120

#: 同一账号每天最多发多少条日志。即使整夜挂着播放器，也不该在一天里
#: 产生几百条记录；到量后当天停止上报，第二天自然恢复。
MAX_LOGS_PER_DAY = 200

#: 连续多少次限流后暂停上报
FAILURE_LIMIT = 3
#: 熔断时长（秒）
PAUSE_SECONDS = 600.0
#: 待发送队列上限；界面线程不会因为网络慢而堆积任务
QUEUE_SIZE = 32

PAUSE_MESSAGE = "接口连续返回限流，听歌记录同步已暂停，稍后自动恢复。"
DAILY_MESSAGE = "今日听歌记录上报已达上限，明天自动恢复。"

#: 上报函数签名：``(song_id, action, seconds, account, client, generation)``
Sender = Callable[[int, str, int, str, ApiClient | None, int], None]


@dataclass(frozen=True)
class _Task:
    """一条待发送的日志。

    ``account``、``client`` 与 ``generation`` 都是入队时的快照：账号防串号，
    客户端防旧会话复用，代数让换号/登出/关闭同步/重建客户端能一次性作废
    所有未发送的任务。
    """

    song_id: int
    action: str
    seconds: int
    account: str
    client: object | None = None
    generation: int = 0


class ScrobbleReporter(QObject):
    """把本地播放轮次翻译成网易云播放日志。

    由窗口把 :class:`HistoryRecorder` 的信号接过来；本类只做“要不要发、
    发什么”，网络请求放在一条串行的守护线程里跑，不阻塞界面，也保证
    ``startPlay`` 一定排在对应的 ``play`` 前面。
    """

    #: 熔断状态变化：``(是否暂停, 原因)``
    pausedChanged = pyqtSignal(bool, str)

    def __init__(
        self,
        cfg: Config,
        client: ApiClient | None = None,
        *,
        client_provider: Callable[[], ApiClient | None] | None = None,
        sender: Sender | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        #: 设置页改过状态目录/代理后窗口会重建 ApiClient；这里每次现取，
        #: 避免继续用指向旧 home 的客户端把记录报到别的账号上
        self._client_provider = client_provider or (lambda: client)
        self._sender = sender or self._send_default

        #: 当前这一轮的歌曲与账号快照；只有结算信号对得上才发 ``play``
        self._round_song_id: int | None = None
        self._round_account = ""
        self._start_sent = False

        self._lock = threading.Lock()
        self._queue: queue.Queue[_Task | None] = queue.Queue(QUEUE_SIZE)
        self._closed = False
        #: 会话代数。换号、登出、关闭同步、重建客户端时递增，已入队但还没
        #: 发出的日志随之作废；工作线程在消费前和真正发送前各校验一次。
        self._generation = 0
        self._clock = time.monotonic

        self._failures = 0
        self._paused_until = 0.0
        self._paused_account = ""
        self._budget_account = ""
        self._budget_start = 0.0
        self._budget_count = 0
        #: 当日计数（内存即可，重启归零）：换号或跨天自动重置
        self._day_account = ""
        self._day_key = ""
        self._day_count = 0
        self._day_paused = False

        self._worker = threading.Thread(
            target=self._work, name="ncmgui-scrobble", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------ 状态

    @property
    def enabled(self) -> bool:
        """每次播放时现读配置，设置页保存后立即生效。"""
        return bool(self._cfg.get(CONFIG_KEY, False))

    @property
    def paused(self) -> bool:
        """是否因连续限流/当日配额而暂时停止上报。"""
        return bool(self._pause_reason())

    @property
    def pause_reason(self) -> str:
        """当前的暂停原因；正常运行时为空串。"""
        return self._pause_reason()

    @staticmethod
    def _eligible(song: object) -> bool:
        """只有网易云在线歌曲有可上报的 ID；本地/流媒体歌曲跳过。"""
        return (
            isinstance(song, Song)
            and not song.source
            and int(song.id) > 0
        )

    # ------------------------------------------------------------ 事件

    def on_started(self, song: object) -> None:
        """开始一轮播放：只记下身份快照，等听满 5 秒再发 ``startPlay``。"""
        if not self.enabled or not self._eligible(song):
            return
        self._resume_if_expired()
        account = self._account()
        if not account:
            self._clear_round()
            return
        self._resume_for_account(account)
        if self._pause_reason():
            self._clear_round()
            return
        # 上一轮如果没收到结算就直接开始新的一轮，旧轮次作废
        self._round_song_id = int(song.id)  # type: ignore[union-attr]
        self._round_account = account
        self._start_sent = False

    def on_qualified(self, song: object) -> None:
        """本轮听满 5 秒：这时才发 ``startPlay``。"""
        if not self.enabled or not self._eligible(song):
            return
        if self._song_id(song) != self._round_song_id or not self._round_account:
            return
        if self._start_sent:
            return
        if self._account() != self._round_account:
            # 播放途中换了账号/退出登录：这一轮不属于当前账号
            return
        self._resume_if_expired()
        if self._pause_reason():
            return
        if self._dispatch(self._song_id(song), "startPlay", 0,
                          self._round_account):
            self._start_sent = True

    def on_settled(self, song: object, listened_ms: int) -> None:
        """一轮结束：按实际收听秒数发 ``play``。"""
        if not self.enabled or not self._eligible(song):
            self._clear_round()
            return
        listened_ms = int(listened_ms)
        if listened_ms < MIN_LISTENED_MS:
            self._clear_round()
            return
        # 与网页端一致：按秒四舍五入
        seconds = int(round(listened_ms / 1000.0))
        if seconds <= 0:
            self._clear_round()
            return
        if self._song_id(song) != self._round_song_id or not self._round_account:
            self._clear_round()
            return
        account = self._round_account
        self._clear_round()
        if self._account() != account:
            return
        self._resume_if_expired()
        if self._pause_reason():
            return
        self._dispatch(self._song_id(song), "play", seconds, account)

    # ------------------------------------------------------------ 上报

    @staticmethod
    def _song_id(song: object) -> int:
        return int(getattr(song, "id", 0))

    def _clear_round(self) -> None:
        self._round_song_id = None
        self._round_account = ""
        self._start_sent = False

    def _account(self) -> str:
        client = self._client_provider()
        return client.jar.value("MUSIC_U") if client is not None else ""

    def _send_default(
        self, song_id: int, action: str, seconds: int, account: str,
        client: ApiClient | None, generation: int,
    ) -> None:
        """后台发送。发之前再校验一次开关、会话代数和账号。

        核对与组包共用同一份 :class:`CookieSnapshot`：比对完 ``music_u``
        之后不再让传输层自己去读 CookieJar，换号插不进这两步之间。
        """
        # 已关闭同步/已登出/客户端被重建：即使工作线程刚刚通过检查，
        # 这里也再拦一道。真正的网络请求只可能来自已经走到这一步的任务，
        # 而队列在 invalidate() 时已被清空，最多只剩这一条在途。
        if client is None or not self.enabled:
            return
        with self._lock:
            if generation != self._generation or self._closed:
                return
        # 用户可能刚登录/换号：每次上报前重新读一次 cookie 文件
        client.jar.reload()
        session = client.web_session()
        if session.music_u != account:
            # 排队期间退出登录或切换了账号。这条日志属于旧账号，
            # 宁可不记，也不能用新账号的身份发出去。
            return
        client.scrobble(song_id, action, seconds, session=session)

    def invalidate(self) -> None:
        """作废当前会话的全部上报。

        换号、登出、关闭听歌同步、设置页重建客户端时调用：递增代数并清空
        待发队列，工作线程手里的任务也会在发送前发现代数不符而丢弃。
        """
        with self._lock:
            if self._closed:
                return
            self._generation += 1
        self._clear_round()
        self._drain_queue()

    def _drain_queue(self) -> None:
        """丢弃所有还没被工作线程取走的任务（不动关机哨兵）。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if item is None:
                    # 关机哨兵：放回去，等 shutdown 之后的正常退出
                    self._queue.put_nowait(None)
                    return
            finally:
                self._queue.task_done()

    def _dispatch(
        self, song_id: int, action: str, seconds: int, account: str
    ) -> bool:
        """把一条日志放进串行队列；队列满或已熔断时直接丢掉。"""
        if not account:
            return False
        client = self._client_provider()
        if client is None:
            return False
        with self._lock:
            if self._closed:
                return False
            generation = self._generation
        if self._pause_reason() or not self._admit(account):
            return False
        try:
            self._queue.put_nowait(
                _Task(song_id, action, seconds, account, client, generation))
        except queue.Full:
            return False
        return True

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                # 消费前重新核对：同步可能刚被关掉，账号/客户端/会话代数
                # 也可能刚变过
                if not self.enabled:
                    continue
                with self._lock:
                    generation = self._generation
                if task.generation != generation:
                    continue
                current = self._client_provider()
                if task.client is not None and current is not task.client:
                    # 设置页重建过客户端：这条日志属于旧会话，丢弃而不是
                    # 拿到新客户端里重发
                    continue
                try:
                    self._sender(
                        task.song_id, task.action, task.seconds, task.account,
                        task.client, task.generation,
                    )
                except RateLimited:
                    self._note_failure(task.account)
                except (ApiError, ValueError, OSError):
                    # 单条失败只当这次没记上，不影响后续播放
                    pass
                else:
                    self._note_success()
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------ 限流/熔断

    def _pause_reason(self) -> str:
        with self._lock:
            # 跨天后当日上限自动失效；否则熔断会把第二天的上报也一起挡住
            if self._day_paused and self._day_key == time.strftime("%Y-%m-%d"):
                return DAILY_MESSAGE
            if self._paused_until and self._clock() < self._paused_until:
                return PAUSE_MESSAGE
        return ""

    def _resume_if_expired(self) -> None:
        with self._lock:
            if not self._paused_until or self._clock() < self._paused_until:
                return
            self._paused_until = 0.0
            self._paused_account = ""
            self._failures = 0
        self.pausedChanged.emit(False, "")

    def _resume_for_account(self, account: str) -> None:
        """换号后不再沿用上一个账号的熔断与当日额度。"""
        with self._lock:
            tripped_other = (
                (self._paused_until and self._paused_account
                 and self._paused_account != account)
                or (self._day_paused and self._day_account
                    and self._day_account != account)
            )
            if not tripped_other:
                return
            self._paused_until = 0.0
            self._paused_account = ""
            self._failures = 0
            self._day_paused = False
            self._day_account = ""
            self._day_key = ""
            self._day_count = 0
        self.pausedChanged.emit(False, "")

    def _admit(self, account: str) -> bool:
        """账号级小时/当日配额；超了就不再发。

        小时窗口用于挡住短时间内的异常爆发；当日上限兜住整夜挂机这类
        长时间的稳定上报。两者都只在内存里计数，重启后归零。
        """
        now = self._clock()
        day = time.strftime("%Y-%m-%d")
        notice: tuple[bool, str] | None = None
        with self._lock:
            if self._day_account != account or self._day_key != day:
                if self._day_paused:
                    notice = (False, "")
                self._day_account = account
                self._day_key = day
                self._day_count = 0
                self._day_paused = False

            allowed = False
            if self._day_count >= MAX_LOGS_PER_DAY:
                if not self._day_paused:
                    self._day_paused = True
                    notice = (True, DAILY_MESSAGE)
            else:
                if (self._budget_account != account
                        or now - self._budget_start >= 3600.0):
                    self._budget_account = account
                    self._budget_start = now
                    self._budget_count = 0
                if self._budget_count < MAX_LOGS_PER_HOUR:
                    self._budget_count += 1
                    self._day_count += 1
                    allowed = True
        if notice is not None:
            self.pausedChanged.emit(*notice)
        return allowed

    def _note_failure(self, account: str) -> None:
        with self._lock:
            self._failures += 1
            if self._failures < FAILURE_LIMIT:
                return
            self._failures = 0
            self._paused_until = self._clock() + PAUSE_SECONDS
            self._paused_account = account
        # 可能在工作线程里，Qt 会把它排队到主线程
        self.pausedChanged.emit(True, PAUSE_MESSAGE)

    def _note_success(self) -> None:
        with self._lock:
            self._failures = 0

    def shutdown(self) -> None:
        """停止后台线程；未发送的任务随之丢弃。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import time

    from PyQt6.QtCore import QCoreApplication

    from . import ensure_utf8_stdout
    from .api.cookies import CookieSnapshot
    from .api.errors import RateLimited

    ensure_utf8_stdout()
    app = QCoreApplication.instance() or QCoreApplication([])
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    class FakeCfg:
        def __init__(self, enabled: bool) -> None:
            self.enabled = enabled

        def get(self, key: str, default: object = None) -> object:
            return self.enabled if key == CONFIG_KEY else default

    class FakeJar:
        def __init__(self) -> None:
            self.reloaded = 0
            self.account = "token-a"

        def reload(self) -> bool:
            self.reloaded += 1
            return True

        def value(self, name: str) -> str:
            return self.account if name == "MUSIC_U" else ""

    class FakeClient:
        def __init__(self) -> None:
            self.jar = FakeJar()
            self.calls: list[tuple[int, str, int]] = []
            self.sessions: list[object] = []
            #: 模拟「核对完账号之后、真正组包之前」另一个线程换了号
            self.switch_before_send = ""

        def web_session(self):
            return CookieSnapshot(
                f"MUSIC_U={self.jar.account}; __csrf=csrf",
                "csrf",
                self.jar.account,
            )

        def scrobble(self, song_id: int, action: str, seconds: int = 0,
                     *, session=None) -> None:
            if self.switch_before_send:
                self.jar.account = self.switch_before_send
            self.sessions.append(session)
            self.calls.append((song_id, action, seconds))

    def wait_for(calls: list, count: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(calls) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    client = FakeClient()
    cfg = FakeCfg(True)
    reporter = ScrobbleReporter(cfg, client)  # type: ignore[arg-type]
    paused: list[bool] = []
    reporter.pausedChanged.connect(
        lambda state, _reason: paused.append(state))

    online = Song(321, "在线")
    local = Song(-1, "本地", source="local")
    streaming = Song(99, "流媒体", source="subsonic")

    # 本地/流媒体歌曲没有网易云 ID，怎么都不该发
    for song in (local, streaming):
        reporter.on_started(song)
        reporter.on_qualified(song)
        reporter.on_settled(song, 60_000)
    time.sleep(0.05)
    check("本地/流媒体不上报", client.calls, [])

    # 未登录时既不开始也不结算
    client.jar.account = ""
    reporter.on_started(online)
    reporter.on_qualified(online)
    reporter.on_settled(online, 60_000)
    time.sleep(0.05)
    check("未登录不上报", client.calls, [])
    client.jar.account = "token-a"

    # 开始播放不立刻发；听满 5 秒才发 startPlay
    reporter.on_started(online)
    time.sleep(0.05)
    check("开始时不立刻发送", client.calls, [])
    reporter.on_qualified(online)
    wait_for(client.calls, 1)
    check("满 5 秒发开始日志", client.calls[0], (321, "startPlay", 0))
    reporter.on_qualified(online)
    time.sleep(0.05)
    check("开始日志只发一次", len(client.calls), 1)

    # 结算：实际收听 12.6 秒 → 四舍五入 13 秒
    reporter.on_settled(online, 12_600)
    wait_for(client.calls, 2)
    check("结束日志", client.calls[1], (321, "play", 13))

    # 低于 5 秒的轮次不往云端发（快速试听/连续切歌）
    reporter.on_started(online)
    reporter.on_settled(online, 4_999)
    time.sleep(0.05)
    check("不足 5 秒不上报", len(client.calls), 2)

    # 播放途中换号：开始日志已经算旧账号，结算不能记到新账号
    base = len(client.calls)
    reporter.on_started(online)
    client.jar.account = "token-b"
    reporter.on_qualified(online)
    reporter.on_settled(online, 30_000)
    time.sleep(0.05)
    check("换号后不发送", len(client.calls), base)
    client.jar.account = "token-a"

    # 真正的高危路径：任务已入队，执行前 Cookie 被切成另一个账号。
    # 默认发送器必须在 reload 之后比对快照，不能拿新账号发旧日志。
    reporter._sender = reporter._send_default
    base = len(client.calls)
    client.jar.account = "token-b"
    reporter._dispatch(321, "startPlay", 0, "token-a")
    time.sleep(0.2)
    check("排队期间换号，旧任务丢弃", len(client.calls), base)
    check("确实重新读过 Cookie", client.jar.reloaded >= 1, True)
    client.jar.account = "token-a"

    # 默认发送器在账号未变时正常发送
    base = len(client.calls)
    reporter._dispatch(321, "startPlay", 0, "token-a")
    wait_for(client.calls, base + 1)
    check("账号一致时正常发送", client.calls[base], (321, "startPlay", 0))

    # 核对通过后、真正组包前又换了号：请求必须沿用核对时的快照，
    # 不能再从 CookieJar 现取新账号的 Cookie
    race = FakeClient()
    race.jar.account = "token-a"
    race.switch_before_send = "token-b"
    racer = ScrobbleReporter(cfg, race)  # type: ignore[arg-type]
    racer._dispatch(321, "startPlay", 0, "token-a")
    wait_for(race.calls, 1)
    check("核对后换号仍发一条", len(race.calls), 1)
    snap = race.sessions[-1] if race.sessions else None
    check("核对后换号沿用旧账号", getattr(snap, "music_u", ""), "token-a")
    check("核对后换号沿用旧 Cookie",
          "MUSIC_U=token-a" in getattr(snap, "header", ""), True)
    racer.shutdown()

    # reload_api 重建客户端后，旧客户端名下的排队任务作废：既不落到新
    # 客户端，也不拿旧客户端继续发
    holder = {"client": FakeClient()}
    holder["client"].jar.account = "token-a"
    replacement = ScrobbleReporter(
        cfg, client_provider=lambda: holder["client"])  # type: ignore[arg-type]
    gate = threading.Event()
    blocked = threading.Event()

    def blocking(song_id, action, seconds, account, client, generation) -> None:
        blocked.set()
        gate.wait(2.0)

    replacement._sender = blocking  # type: ignore[assignment]
    replacement._dispatch(321, "block", 1, "token-a")
    blocked.wait(2.0)
    stale = holder["client"]
    replacement._dispatch(322, "play", 10, "token-a")
    holder["client"] = FakeClient()
    holder["client"].jar.account = "token-a"
    gate.set()
    time.sleep(0.2)
    check("重建客户端后旧任务作废", len(holder["client"].calls), 0)
    check("旧客户端不会再收到任务", len(stale.calls), 0)
    replacement.shutdown()

    # invalidate()：代数递增并清空队列，已排队但没开始发的一条也不能发
    sent: list[int] = []
    guard = threading.Event()
    started = threading.Event()

    def held_sender(song_id, action, seconds, account, client, generation):
        if action == "hold":
            started.set()
            guard.wait(2.0)
            return
        sent.append(song_id)

    inv_client = FakeClient()
    inv_client.jar.account = "token-a"
    invalidator = ScrobbleReporter(cfg, inv_client)  # type: ignore[arg-type]
    invalidator._sender = held_sender  # type: ignore[assignment]
    invalidator._dispatch(1, "hold", 1, "token-a")
    started.wait(2.0)
    invalidator._dispatch(2, "play", 10, "token-a")
    before = invalidator._generation
    invalidator.invalidate()
    check("invalidate 递增代数", invalidator._generation > before, True)
    guard.set()
    time.sleep(0.2)
    check("invalidate 清空队列", sent, [])

    # 陈旧代数的任务即使被直接交给默认发送器也不能发
    base = len(inv_client.calls)
    invalidator._send_default(3, "startPlay", 0, "token-a",
                              inv_client, before)
    check("过期会话代数不发送", len(inv_client.calls), base)
    invalidator.shutdown()

    # 消费时同步已被关掉：队列里的任务直接丢弃
    off_cfg = FakeCfg(True)
    off_sent: list[int] = []
    off_started = threading.Event()
    off_gate = threading.Event()

    def off_sender(song_id, action, seconds, account, client, generation):
        if action == "hold":
            off_started.set()
            off_gate.wait(2.0)
            return
        off_sent.append(song_id)

    off_client = FakeClient()
    off_client.jar.account = "token-a"
    toggler = ScrobbleReporter(off_cfg, off_client)  # type: ignore[arg-type]
    toggler._sender = off_sender  # type: ignore[assignment]
    toggler._dispatch(1, "hold", 1, "token-a")
    off_started.wait(2.0)
    toggler._dispatch(2, "play", 10, "token-a")
    off_cfg.enabled = False
    off_gate.set()
    time.sleep(0.2)
    check("关闭同步后队列不再发送", off_sent, [])
    toggler.shutdown()

    # 关闭开关后不再上报
    cfg.enabled = False
    base = len(client.calls)
    reporter.on_started(online)
    reporter.on_qualified(online)
    reporter.on_settled(online, 30_000)
    time.sleep(0.05)
    check("关闭后不上报", len(client.calls), base)

    # 连续限流触发熔断，之后的任务直接丢弃
    cfg.enabled = True
    def always_limited(song_id: int, action: str, seconds: int,
                       account: str, client, generation) -> None:
        raise RateLimited("限流")
    reporter._sender = always_limited
    deadline = time.monotonic() + 2.0
    while not reporter.paused and time.monotonic() < deadline:
        reporter._dispatch(321, "play", 10, "token-a")
        time.sleep(0.02)
    check("连续限流后熔断", reporter.paused, True)
    app.processEvents()  # 工作线程发出的信号是排队投递的
    check("熔断通知界面", True in paused, True)
    check("熔断后不再入队", reporter._dispatch(321, "play", 10, "token-a"),
          False)
    # 换号后解除熔断
    reporter._resume_for_account("token-b")
    check("换号解除熔断", reporter.paused, False)

    reporter.shutdown()
    del app
    if failures:
        print(f"听歌记录上报自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("听歌记录上报自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
