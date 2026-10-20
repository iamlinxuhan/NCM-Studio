"""把播放记录同步到网易云账号的听歌记录。

网易云网页本身用两条件播放日志：

* 真正开始出声时发 ``startPlay``；
* 这一轮结束（切歌、播完、停止）时发 ``play``，带上实际收听秒数。

服务端据此更新账号的听歌记录与听歌排行。请求走网页端同一个出口
（``clientlogusf.music.163.com/weapi/feedback/weblog``），实现见
:meth:`ncmgui.api.client.ApiClient.scrobble`。

计时不在这里重算：直接复用 :class:`~ncmgui.history_recorder.HistoryRecorder`
的 ``playStarted`` / ``playSettled``，保证本地统计和云端记录对得上。

**默认关闭。** 这不是公开 API，属于网页端私有接口；功能可能随官方改版
失效，也可能带来账号风控风险。只在用户明确开启后才上报，失败一律静默，
绝不打断播放。

    .venv/bin/python -m ncmgui.scrobble
"""

from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import QObject

from .api.client import ApiClient
from .api.errors import ApiError
from .config import Config
from .resolver import Song

#: 配置项：是否把播放记录同步到网易云
CONFIG_KEY = "player.scrobble"

#: 上报函数签名：``(song_id, action, seconds)``
Sender = Callable[[int, str, int], None]


class ScrobbleReporter(QObject):
    """把本地播放轮次翻译成网易云播放日志。

    由窗口把 :class:`HistoryRecorder` 的两个信号接过来；本类只做“要不要发、
    发什么”，网络请求放在守护线程里跑，不阻塞界面。
    """

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
        #: 开始这一轮时的 MUSIC_U；中途换号就不把结算记到新账号
        self._round_account = ""

    # ------------------------------------------------------------ 状态

    @property
    def enabled(self) -> bool:
        """每次播放时现读配置，设置页保存后立即生效。"""
        return bool(self._cfg.get(CONFIG_KEY, False))

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
        if not self.enabled or not self._eligible(song):
            return
        account = self._account()
        if not account:
            # 未登录（或登出后）不发；结算时也按“无账号”处理
            self._round_account = ""
            return
        self._round_account = account
        self._dispatch(song.id, "startPlay", 0)  # type: ignore[union-attr]

    def on_settled(self, song: object, listened_ms: int) -> None:
        if not self.enabled or not self._eligible(song):
            return
        # 与网页端一致：按秒四舍五入；不足半秒的结果是 0，接口不接受
        seconds = int(round(int(listened_ms) / 1000.0))
        if seconds <= 0:
            return
        current = self._account()
        if not self._round_account or current != self._round_account:
            # 这一轮开始时没登录，或播放途中换了账号：算旧账号的，
            # 不往新账号里记
            return
        self._dispatch(song.id, "play", seconds)  # type: ignore[union-attr]

    # ------------------------------------------------------------ 上报

    def _account(self) -> str:
        client = self._client_provider()
        return client.jar.value("MUSIC_U") if client is not None else ""

    def _send_default(self, song_id: int, action: str, seconds: int) -> None:
        client = self._client_provider()
        if client is None:
            return
        # 用户可能刚登录/换号：每次上报前重新读一次 cookie 文件
        client.jar.reload()
        client.scrobble(song_id, action, seconds)

    def _dispatch(self, song_id: int, action: str, seconds: int) -> None:
        """后台发送一条日志；失败只当这次没记上。"""
        sender = self._sender

        def run() -> None:
            try:
                sender(song_id, action, seconds)
            except (ApiError, ValueError, OSError):
                pass

        threading.Thread(
            target=run, name="ncmgui-scrobble", daemon=True
        ).start()


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import time

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
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

        def scrobble(self, song_id: int, action: str, seconds: int = 0) -> None:
            self.calls.append((song_id, action, seconds))

    def wait_for(calls: list, count: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while len(calls) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    client = FakeClient()
    cfg = FakeCfg(True)
    reporter = ScrobbleReporter(cfg, client)  # type: ignore[arg-type]

    online = Song(321, "在线")
    local = Song(-1, "本地", source="local")
    streaming = Song(99, "流媒体", source="subsonic")

    # 本地/流媒体歌曲没有网易云 ID，怎么都不该发
    reporter.on_started(local)
    reporter.on_started(streaming)
    reporter.on_settled(local, 60_000)
    reporter.on_settled(streaming, 60_000)
    time.sleep(0.05)
    check("本地/流媒体不上报", client.calls, [])

    # 未登录时既不开始也不结算
    client.jar.account = ""
    reporter.on_started(online)
    reporter.on_settled(online, 60_000)
    time.sleep(0.05)
    check("未登录不上报", client.calls, [])
    client.jar.account = "token-a"

    # 开始播放：立刻发 startPlay，不带时长
    reporter.on_started(online)
    wait_for(client.calls, 1)
    check("开始日志", client.calls[0], (321, "startPlay", 0))

    # 结算：实际收听 12.6 秒 → 四舍五入 13 秒
    reporter.on_settled(online, 12_600)
    wait_for(client.calls, 2)
    check("结束日志", client.calls[1], (321, "play", 13))

    # 不足半秒不产生日志
    reporter.on_settled(online, 400)
    time.sleep(0.05)
    check("过短不上报", len(client.calls), 2)

    # 播放途中换号：开始日志已经算旧账号，结算不能记到新账号
    base = len(client.calls)
    reporter.on_started(online)
    wait_for(client.calls, base + 1)
    client.jar.account = "token-b"
    reporter.on_settled(online, 30_000)
    time.sleep(0.05)
    check("换号后不结算", len(client.calls), base + 1)
    client.jar.account = "token-a"

    # 关闭开关后不再上报
    cfg.enabled = False
    reporter.on_started(online)
    reporter.on_settled(online, 30_000)
    time.sleep(0.05)
    check("关闭后不上报", len(client.calls), base + 1)

    # 默认发送器会先刷新 cookie，再调客户端
    cfg.enabled = True
    reporter._sender = reporter._send_default
    reporter.on_started(online)
    wait_for(client.calls, base + 2)
    check("上报前刷新 Cookie", client.calls[base + 1], (321, "startPlay", 0))
    check("确实刷新了一次", client.jar.reloaded >= 3, True)

    if failures:
        print(f"听歌记录上报自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("听歌记录上报自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
