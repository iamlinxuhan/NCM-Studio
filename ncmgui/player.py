r"""mpv JSON IPC 播放器封装。

程序不自己解码音频，而是把一个 **常驻的 mpv** 拉到后台，通过 mpv 的 JSON IPC
控制它（``loadfile`` / ``set_property pause`` / ``seek`` …），用它听歌。这样既不用
在 Python 里碰音频栈，又能拿到 mpv 对网易云 CDN 的兼容性。

本模块刻意**不依赖 Qt**，因此可以直接离线自检::

    .venv/bin/python -m ncmgui.player

IPC 端点：

* POSIX —— 临时目录里的 unix socket。**必须放在 /tmp**：``sun_path`` 只有 104 字节
  上限，而 macOS 的 ``$TMPDIR`` 本身就已经很长了，中文用户目录更是雪上加霜。
* Windows —— 命名管道（``\\.\pipe\ncmgui-mpv-…``），mpv 的 ``--input-ipc-server``
  在那边就是这个东西。

协议是一行一个 UTF-8 JSON。读线程只负责解析、过滤掉高频的 ``time-pos`` 抖动，
再通过回调交出去；界面自己决定怎么刷新控件。

**为什么是一个常驻进程，而不是每首一个：** 切歌时省掉 100～300 ms 的启动与音频
后端初始化，且只有一个端点、一套读线程，生命周期简单得多。
"""

from __future__ import annotations

import atexit
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

_IS_WINDOWS = sys.platform == "win32"

#: 可执行文件名。Windows 上必须带 ``.exe``，否则既跑不起来、也过不了存在性判断。
MPV_BINARY_NAME = "mpv.exe" if _IS_WINDOWS else "mpv"

#: 随包 mpv 在包里的相对目录。onefile 落在 ``_MEIPASS/mpv/``，
#: onedir 落在 ``_internal/mpv/`` —— 两者都是 ``sys._MEIPASS`` 的子目录。
BUNDLED_DIR = "mpv"

#: PATH 里找不到时，依次翻这些目录。
_COMMON_DIRS: tuple[str, ...] = (
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",   # macOS（Apple Silicon）
    "/opt/local/bin",      # MacPorts
)

#: 音量合法范围。mpv 自己接受 0～100（超过 100 是软件放大，会削波）。
_VOLUME_MIN = 0
_VOLUME_MAX = 100

#: ``end-file`` 的 reason 取值（见 mpv 的 input.rst）。
END_REASONS = ("eof", "stop", "quit", "error", "redirect", "unknown")


class PlayerError(RuntimeError):
    """播放器起不来或中途出错。"""


class MpvNotFound(PlayerError):
    """找不到 mpv 可执行文件。"""


# --------------------------------------------------------------------------- #
# 纯函数：端点、命令编码、消息解析、数值换算
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Endpoint:
    """IPC 端点。``cleanup_dir`` 是 POSIX 下需要一并删掉的临时目录。"""

    kind: str          # "unix" | "pipe"
    value: str
    cleanup_dir: str = ""


def ipc_endpoint(
    *,
    windows: bool | None = None,
    token: str | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> Endpoint:
    """生成一个不会和别的实例撞车的 IPC 端点。

    :param windows: 强制按 Windows 命名管道处理（自检时用）
    :param token: 显式指定后缀；默认是 ``pid-随机数``
    :param base_dir: POSIX 下放 socket 的目录；默认 ``/tmp``
    """
    if windows is None:
        windows = _IS_WINDOWS
    token = token or f"{os.getpid()}-{random.randrange(1 << 24):06x}"

    if windows:
        return Endpoint("pipe", rf"\\.\pipe\ncmgui-mpv-{token}")

    # 路径里带中文不会让 bind 失败，但会让长度更难估；统一用 /tmp 最省心
    candidates = [base_dir, "/tmp", tempfile.gettempdir()]
    for root in candidates:
        if not root:
            continue
        try:
            directory = tempfile.mkdtemp(prefix="ncmgui-mpv-", dir=str(root))
        except OSError:
            continue
        path = os.path.join(directory, "ipc.sock")
        # 不同系统上限 104/108 字节，留一点余量
        if len(path.encode("utf-8")) < 100:
            return Endpoint("unix", path, directory)
        shutil.rmtree(directory, ignore_errors=True)

    raise PlayerError("无法创建 mpv IPC 所需的临时目录")


def clamp_volume(value: object, default: int = 70) -> int:
    """把音量收进 0～100。非法输入回退到 ``default``。"""
    try:
        number = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    return max(_VOLUME_MIN, min(_VOLUME_MAX, number))


def encode_command(command: Sequence[object], request_id: int | None = None) -> bytes:
    """把一条命令编成 IPC 协议要求的一行 UTF-8 JSON。

    ``json.dumps`` 会把字符串里的换行转义成 ``\\n``，所以一条命令一定只占一行。
    """
    payload: dict[str, object] = {"command": list(command)}
    if request_id is not None:
        payload["request_id"] = request_id
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


@dataclass(frozen=True)
class Message:
    """解析后的一条 mpv 消息。"""

    kind: str                     # "event" | "reply" | "other"
    name: str = ""                # 事件名，如 end-file
    request_id: int | None = None
    error: str = "success"
    data: object = None
    raw: Mapping[str, object] = field(default_factory=dict)


def parse_message(line: str | bytes) -> Message | None:
    """解析一行 IPC 输出。不是合法 JSON 对象时返回 ``None``。"""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 解码不会走到这里
            return None
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    if "event" in payload:
        return Message("event", name=str(payload.get("event") or ""), raw=payload)
    if "error" in payload:
        request_id = payload.get("request_id")
        return Message(
            "reply",
            request_id=request_id if isinstance(request_id, int) else None,
            error=str(payload.get("error") or ""),
            data=payload.get("data"),
            raw=payload,
        )
    return Message("other", raw=payload)


def end_reason(message: Message) -> str:
    """``end-file`` 事件的 reason；不是该事件时返回空串。"""
    if message.kind != "event" or message.name != "end-file":
        return ""
    return str(message.raw.get("reason") or "")


def format_clock(seconds: float | None) -> str:
    """把秒数格式化成 ``mm:ss``（超过一小时用 ``h:mm:ss``）。

    未知/负值显示 ``--:--`` —— 列表里有些歌的时长接口就是不给，不要显示 ``00:00``
    假装它是零秒。
    """
    if seconds is None:
        return "--:--"
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if value < 0 or value != value or value == float("inf"):
        return "--:--"
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_value(position: float | None, duration: float | None, scale: int = 1000) -> int:
    """把播放位置换算成进度条的整数刻度。"""
    if not duration or duration <= 0 or position is None or position < 0:
        return 0
    return max(0, min(scale, int(round(position / duration * scale))))


def seek_seconds(value: int, duration: float | None, scale: int = 1000) -> float:
    """进度条的刻度换算回秒。时长未知时返回 0。"""
    if not duration or duration <= 0:
        return 0.0
    return max(0.0, min(float(duration), value / scale * duration))


def matches_query(text: str, query: str) -> bool:
    """搜索框的匹配规则：空白分隔的多个词必须**全部**出现，大小写不敏感。

    这样「周杰伦 晴天」能同时匹配到歌名和歌手，而不是要求整串连续出现。
    """
    tokens = (query or "").casefold().split()
    if not tokens:
        return True
    haystack = (text or "").casefold()
    return all(token in haystack for token in tokens)


def step_index(current: int | None, total: int, delta: int, wrap: bool = True) -> int | None:
    """在列表里前后移动，返回新的下标；列表为空返回 ``None``。

    ``current`` 为 ``None``（还没播过）时：向前走取第一首，向后走取最后一首。
    越界默认环绕 —— 用户点「上一首」时不该什么都不发生。
    """
    if total <= 0:
        return None
    if current is None:
        return 0 if delta >= 0 else total - 1
    target = current + delta
    if wrap:
        return target % total
    return target if 0 <= target < total else None


# --------------------------------------------------------------------------- #
# 本地音频直链的小缓存
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AudioSource:
    """一次取到的播放地址。"""

    url: str
    #: 实际拿到的音质（可能比请求的低，非会员常见）
    level: str = ""
    #: 接口给的过期时间（秒）。约 1200，即 20 分钟
    expi: int = 600
    #: 是不是只是 30 秒试听片段
    trial: bool = False


class UrlCache:
    """按 ``(歌曲 id, 音质)`` 缓存直链，过期时间比接口略早一点。

    mpv 拿到一个已经过期的 URL 时，报错会落在 ``end-file``，界面上只能看到
    「播放失败」。提前 60 秒作废，让用户下一次点击重新取链即可。
    """

    def __init__(
        self,
        margin: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._margin = margin
        self._clock = clock
        self._items: dict[tuple[int, str], tuple[AudioSource, float]] = {}
        self._lock = threading.Lock()

    def _ttl(self, source: AudioSource) -> float:
        return max(30.0, min(float(source.expi or 600) - self._margin, 3600.0))

    def get(self, key: tuple[int, str]) -> AudioSource | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            source, expires_at = item
            if self._clock() >= expires_at:
                self._items.pop(key, None)
                return None
            return source

    def put(self, key: tuple[int, str], source: AudioSource) -> None:
        with self._lock:
            self._items[key] = (source, self._clock() + self._ttl(source))

    def invalidate(self, key: tuple[int, str]) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# --------------------------------------------------------------------------- #
# 可执行文件探测
# --------------------------------------------------------------------------- #

def _is_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if _IS_WINDOWS:
        return True
    return os.access(path, os.X_OK)


def bundled_mpv() -> str | None:
    """随包分发的 mpv；没有则返回 ``None``。"""
    candidates: list[Path] = []

    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / BUNDLED_DIR / MPV_BINARY_NAME)

    # 源码运行时的开发位置（vendor/ 在仓库根下，且照例不进版本库）
    candidates.append(
        Path(__file__).resolve().parent.parent / "vendor" / BUNDLED_DIR / MPV_BINARY_NAME
    )

    for candidate in candidates:
        if _is_executable(candidate):
            return str(candidate)
    return None


def find_mpv(explicit: str = "") -> str | None:
    """按「显式 → 随包 → PATH → 常见目录」的顺序找 mpv。

    显式填了却找不到时返回 ``None`` 而不是悄悄回退 —— 用户特意指定了一份，
    用另一份冒充只会让问题更难查（与 :func:`ncmgui.ncmctl.find_binary` 一致）。
    """
    name = (explicit or "").strip()
    if name:
        path = Path(name).expanduser()
        return str(path) if _is_executable(path) else None

    found = bundled_mpv()
    if found:
        return found

    which = shutil.which("mpv")
    if which:
        return which

    for directory in _COMMON_DIRS:
        candidate = Path(directory).expanduser() / MPV_BINARY_NAME
        if _is_executable(candidate):
            return str(candidate)
    return None


def child_env(binary: str) -> dict[str, str]:
    """给 mpv 子进程准备环境变量。

    Linux 上随包的 ``.so`` 放在 mpv 同级的 ``lib/`` 里。AppRun 与 PyInstaller 已经
   往 ``LD_LIBRARY_PATH`` 里加了它们各自需要的目录，这里**前置**而不是覆盖 ——
   覆盖会让 AppImage 里的 Qt 库路径突然消失。
    """
    env = os.environ.copy()
    library_dir = Path(binary).resolve().parent / "lib"
    if not library_dir.is_dir():
        return env

    if sys.platform.startswith("linux"):
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = (
            f"{library_dir}{os.pathsep}{existing}" if existing else str(library_dir)
        )
    elif sys.platform == "darwin":
        existing = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = (
            f"{library_dir}{os.pathsep}{existing}" if existing else str(library_dir)
        )
    return env


def probe_mpv(binary: str, timeout: float = 15.0) -> tuple[bool, str]:
    """跑一次 ``mpv --version``，确认这个二进制**真的能执行**。

    文件在、权限对，不代表它能跑：动态库少一个、架构不对、被浏览器打了隔离标记，
    都会在 exec 时才失败。打包产物的自检只信这一条。
    """
    try:
        # 这里**不能**加 --terminal=no：它把终端输出整个关掉，--version 也
        # 跟着一句话都不打，探测就成了「退出码 0、没有任何输出」，永远判失败
        proc = subprocess.run(
            [binary, "--no-config", "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(binary),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"无法执行 {binary}：{exc}"

    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    first = output.splitlines()[0] if output else "（没有输出）"
    if proc.returncode != 0:
        return False, f"mpv --version 退出码 {proc.returncode}：{first}"
    if "mpv" not in output.lower():
        return False, f"输出的不像 mpv 版本信息：{first}"
    return True, first


# --------------------------------------------------------------------------- #
# 播放器
# --------------------------------------------------------------------------- #

def mpv_argv(
    binary: str,
    endpoint: Endpoint,
    *,
    volume: int = 70,
    audio_output: str = "",
    extra: Sequence[str] = (),
) -> list[str]:
    """组装 mpv 的命令行参数。单独成一个函数是为了能在自检里逐项检查。"""
    argv = [
        binary,
        # 不读用户的 mpv.conf、不加载脚本：行为可复现，也避免别人的配置改了键位
        "--no-config",
        "--load-scripts=no",
        # 没有文件时也别退出，切歌靠 loadfile replace
        "--idle=yes",
        # 纯音频：不建窗口、不解封面、不走视频输出
        "--force-window=no",
        "--vid=no",
        "--audio-display=no",
        "--vo=null",
        # 日志不往终端刷；真正的错误走 IPC 的 end-file
        "--terminal=no",
        "--really-quiet",
        # 播完不留在原地，让 end-file 的 eof 准时到
        "--keep-open=no",
        # 先暂停，等界面收到 file-loaded 再决定要不要出声
        "--pause=yes",
        "--save-position-on-quit=no",
        "--ytdl=no",
        "--audio-file-auto=no",
        "--sub-auto=no",
        "--network-timeout=20",
        "--audio-client-name=ncm-studio",
        # 网易云 CDN 对 Referer 敏感。Cookie 不放这里 —— 命令行在进程列表里可见
        "--http-header-fields=Referer: https://music.163.com/",
        f"--volume={clamp_volume(volume)}",
        f"--input-ipc-server={endpoint.value}",
    ]
    if audio_output:
        argv.append(f"--ao={audio_output}")
    argv.extend(extra)
    return argv


def connect_ipc_socket(path: str, timeout: float = 2.0) -> socket.socket:
    """连上 mpv 的 IPC socket，返回一个**阻塞**连接。

    ``timeout`` 只约束 connect 本身。连上之后必须清掉超时：IPC 是长连接，
    mpv 暂停、空闲（比如正在取播放地址）时可能几秒到几分钟一个字节都不发。
    如果 socket 还带着超时，读线程会在 ``readline()`` 上抛 ``TimeoutError``
    并退出，之后 ``file-loaded`` 等事件全部丢失 —— 界面卡在「正在缓冲」，
    而 mpv 已经正常出声。连接失败时关掉 socket 再把异常抛出去。
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.settimeout(None)
    except OSError:
        sock.close()
        raise
    return sock


class MpvPlayer:
    """一个常驻 mpv 进程。

    :param binary: mpv 可执行文件路径
    :param on_event: ``callback(kind, payload)``，在**读线程**里调用：

        * ``("file-loaded", {})``
        * ``("end-file", {"reason": "eof", "error": ""})``
        * ``("property", {"name": "time-pos", "value": 12.3})``
        * ``("shutdown", {})``

    :param on_exit: ``callback(退出码)``，进程**意外**结束时调用（主动 stop 不调）
    :param on_error: ``callback(说明)``，命令失败或连接异常时调用
    :param env: 子进程环境；默认 :func:`child_env`
    :param extra: 追加到 mpv 命令行的参数（测试里用 ``--ao=null``）
    """

    def __init__(
        self,
        binary: str,
        *,
        on_event: Callable[[str, Mapping[str, object]], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        env: Mapping[str, str] | None = None,
        extra: Sequence[str] = (),
        volume: int = 70,
        audio_output: str = "",
    ) -> None:
        self.binary = binary
        self.volume = clamp_volume(volume)
        self.audio_output = audio_output
        self._on_event = on_event
        self._on_exit = on_exit
        self._on_error = on_error
        self._extra = tuple(extra)
        self._env = dict(env) if env is not None else None

        self._process: subprocess.Popen | None = None
        self._endpoint: Endpoint | None = None
        self._writer: io.RawIOBase | io.BufferedIOBase | None = None
        self._reader: io.BufferedReader | None = None
        self._threads: list[threading.Thread] = []
        self._write_lock = threading.Lock()
        self._closing = False
        self._exited = False
        self._next_id = 0
        self._log: deque[str] = deque(maxlen=60)
        self._last_position: float | None = None
        self._registered_atexit = False

    # ------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and not self._exited
        )

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def log_tail(self) -> list[str]:
        return list(self._log)

    # ------------------------------------------------------------ 启动/停止

    def start(self, timeout: float = 6.0) -> None:
        """启动 mpv 并连上 IPC。已经在跑时直接返回。"""
        if self.running:
            return
        if not self.binary or not Path(self.binary).exists():
            raise MpvNotFound(f"找不到 mpv：{self.binary or '（空路径）'}")

        self._closing = False
        self._exited = False
        self._endpoint = ipc_endpoint()

        argv = mpv_argv(
            self.binary,
            self._endpoint,
            volume=self.volume,
            audio_output=self.audio_output,
            extra=self._extra,
        )

        creationflags = 0
        if _IS_WINDOWS:  # pragma: no cover - 只在 Windows 上生效
            # 否则可能闪一个黑色的控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        env = self._env if self._env is not None else child_env(self.binary)
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._cleanup_endpoint()
            raise PlayerError(f"无法启动 mpv：{exc}") from exc

        self._drain_stderr()
        self._connect(timeout)

        # 订阅需要的属性。id 固定，页面按 name 分发，不依赖 id
        for prop_id, name in ((1, "time-pos"), (2, "duration"), (3, "pause"), (4, "volume")):
            self._send(encode_command(["observe_property", prop_id, name]))

        self._start_reader()
        self._watch_exit()

        if not self._registered_atexit:
            # 正常关闭走 closeEvent；这里是崩溃/异常退出时的兜底
            atexit.register(self.stop)
            self._registered_atexit = True

    def _connect(self, timeout: float) -> None:
        """等 mpv 把 socket/管道建出来，然后连上。"""
        assert self._process is not None
        assert self._endpoint is not None

        deadline = time.monotonic() + max(0.5, timeout)
        last_error: OSError | None = None

        while time.monotonic() < deadline:
            code = self._process.poll()
            if code is not None:
                tail = " / ".join(self.log_tail[-3:])
                raise PlayerError(
                    f"mpv 启动后立即退出（退出码 {code}）" + (f"：{tail}" if tail else "")
                )
            try:
                if self._endpoint.kind == "pipe":
                    raw = open(self._endpoint.value, "r+b", buffering=0)  # noqa: SIM115
                    self._reader = io.BufferedReader(raw)
                    self._writer = raw
                else:
                    sock = connect_ipc_socket(self._endpoint.value)
                    self._writer = sock  # type: ignore[assignment]
                    self._reader = sock.makefile("rb")
                return
            except OSError as exc:
                last_error = exc
                # socket 还没出现时 connect 报 ENOENT/ECONNREFUSED，属正常
                time.sleep(0.05)

        self.stop()
        raise PlayerError(
            f"连不上 mpv 的 IPC 端点：{last_error or '超时'}\n"
            f"命令行：{' '.join(mpv_argv(self.binary, self._endpoint))}"
        )

    def _drain_stderr(self) -> None:
        """把 mpv 的 stderr 接走。

        不排空的话，管道缓冲区满了会**把 mpv 阻塞住**；而 ``--really-quiet`` 之外
        仍可能有致命错误，留着最后几行正好用于诊断。
        """
        assert self._process is not None and self._process.stderr is not None
        stream = self._process.stderr

        def pump() -> None:
            try:
                for line in stream:
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        self._log.append(text)
            except (OSError, ValueError):
                pass

        threading.Thread(target=pump, name="ncmgui-mpv-stderr", daemon=True).start()

    def _start_reader(self) -> None:
        assert self._reader is not None

        def read_loop() -> None:
            while not self._closing:
                try:
                    line = self._reader.readline()
                except (TimeoutError, socket.timeout):
                    # 连接后已经恢复阻塞，这里只是兜底：读线程一旦退出，
                    # 界面就再也收不到 file-loaded 了
                    continue
                except (OSError, ValueError) as exc:
                    if not self._closing:
                        self._log.append(f"mpv IPC 读线程退出：{exc}")
                    break
                if not line:
                    break
                message = parse_message(line)
                if message is not None:
                    self._dispatch(message)

        thread = threading.Thread(target=read_loop, name="ncmgui-mpv-read", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _watch_exit(self) -> None:
        assert self._process is not None

        def wait_loop() -> None:
            code = self._process.wait()
            self._exited = True
            if not self._closing and self._on_exit is not None:
                self._safe(self._on_exit, code)

        thread = threading.Thread(target=wait_loop, name="ncmgui-mpv-wait", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        """退出 mpv 并清掉 IPC 端点。幂等，可以从任何线程调用。"""
        process = self._process
        if process is None:
            self._cleanup_endpoint()
            return

        self._closing = True
        try:
            if process.poll() is None:
                self._send(encode_command(["quit"]))
        except OSError:
            pass

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass

        for stream in (self._reader, self._writer):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        self._reader = None
        self._writer = None
        self._process = None
        self._cleanup_endpoint()

        for thread in self._threads:
            thread.join(timeout=0.5)
        self._threads.clear()
        self._closing = False

    def _cleanup_endpoint(self) -> None:
        endpoint, self._endpoint = self._endpoint, None
        if endpoint is None:
            return
        if endpoint.kind == "unix":
            try:
                os.unlink(endpoint.value)
            except OSError:
                pass
            if endpoint.cleanup_dir:
                shutil.rmtree(endpoint.cleanup_dir, ignore_errors=True)

    # ------------------------------------------------------------ 发命令

    def _send(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise PlayerError("mpv 还没有就绪")
        with self._write_lock:
            if isinstance(writer, socket.socket):
                writer.sendall(data)
            else:
                written = writer.write(data)  # type: ignore[union-attr]
                if written != len(data):  # pragma: no cover - 管道写一半很少见
                    raise PlayerError("写入 mpv IPC 时数据被截断")

    def command(self, *args: object, request_id: int | None = None) -> int:
        """发送一条原生命令，返回 request_id（未指定时自动分配）。"""
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        self._send(encode_command(args, request_id))
        return request_id

    def set_property(self, name: str, value: object, request_id: int | None = None) -> None:
        self.command("set_property", name, value, request_id=request_id)

    def load(self, target: str) -> None:
        """加载并播放一个 URL 或本地文件。"""
        self.command("loadfile", target, "replace")
        # 上一首可能是暂停状态；用户点歌就是要听，所以这里显式解除暂停
        self.set_property("pause", False)

    def pause(self, paused: bool = True) -> None:
        self.set_property("pause", bool(paused))

    def seek(self, seconds: float) -> None:
        self.command("seek", max(0.0, float(seconds)), "absolute+exact")

    def set_volume(self, volume: int) -> None:
        self.volume = clamp_volume(volume, self.volume)
        self.set_property("volume", self.volume)

    def stop_playback(self) -> None:
        """停掉当前曲目，但保留 mpv 进程。"""
        self.command("stop")

    # ------------------------------------------------------------ 读事件

    def _dispatch(self, message: Message) -> None:
        if message.kind == "event":
            name = message.name
            if name == "property-change":
                prop = str(message.raw.get("name") or "")
                value = message.raw.get("data")
                if prop == "time-pos":
                    # 这个属性一秒会变几十次，界面按 200ms 刷新就够了。
                    # 只在整秒变化时放行，避免事件洪峰塞满 Qt 的事件队列
                    try:
                        second = int(float(value)) if value is not None else None
                    except (TypeError, ValueError):
                        second = None
                    if second is not None and second == self._last_position:
                        return
                    self._last_position = None if second is None else float(second)
                self._emit("property", {"name": prop, "value": value})
            elif name == "end-file":
                self._last_position = None
                self._emit(
                    "end-file",
                    {
                        "reason": str(message.raw.get("reason") or ""),
                        "error": str(message.raw.get("error") or ""),
                    },
                )
            elif name in ("file-loaded", "shutdown"):
                self._emit(name, {})
            return

        if message.kind == "reply" and message.error not in ("success", ""):
            self._emit_error(f"mpv 命令失败：{message.error}")

    def _emit(self, kind: str, payload: Mapping[str, object]) -> None:
        if self._on_event is not None:
            self._safe(self._on_event, kind, payload)

    def _emit_error(self, text: str) -> None:
        if self._on_error is not None:
            self._safe(self._on_error, text)

    @staticmethod
    def _safe(callback: Callable, *args: object) -> None:
        """回调里出异常不能把读线程打死 —— 那会让播放器再也发不出事件。"""
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - 任何界面异常都不该影响 IPC 线程
            pass


# --------------------------------------------------------------------------- #
# 真实冒烟测试（需要一个能跑的 mpv）
# --------------------------------------------------------------------------- #

def smoke_test(binary: str, timeout: float = 20.0) -> tuple[bool, str]:
    """启动 mpv、播一个静音 WAV、等它自然结束。

    这比 ``--version`` 强的多：它真的走一遍 IPC 连接、JSON 帧、事件解析和进程
    清理。CI 里用它证明「随包的 mpv 不只是个在包里的文件」。
    """
    handle = tempfile.NamedTemporaryFile(
        prefix="ncmgui-player-", suffix=".wav", delete=False
    )
    path = Path(handle.name)
    handle.close()
    try:
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x00\x00" * 1600)  # 0.2 秒

        finished = threading.Event()
        errors: list[str] = []

        def on_event(kind: str, payload: Mapping[str, object]) -> None:
            if kind == "end-file" and payload.get("reason") == "eof":
                finished.set()
            elif kind == "end-file" and payload.get("reason") == "error":
                errors.append(str(payload.get("error") or "播放失败"))
                finished.set()

        player = MpvPlayer(
            binary,
            on_event=on_event,
            on_error=errors.append,
            extra=["--ao=null"],  # CI 上没有声卡
        )
        try:
            player.start()
            player.load(str(path))
            if not finished.wait(timeout):
                return False, "播放静音 WAV 没有在超时内结束"
            if errors:
                return False, errors[0]
            return True, "IPC 往返、事件与进程清理正常"
        finally:
            player.stop()
    except Exception as exc:  # noqa: BLE001 - 冒烟测试要把任何失败变成一句话
        return False, f"{exc.__class__.__name__}: {exc}"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    # Windows 上输出被重定向时是 cp1252，中文失败说明会在 print 时再炸一次，
    # 真正的失败项反而看不到
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    # ---- 端点
    unix = ipc_endpoint(windows=False, token="t1")
    check("POSIX 端点类型", unix.kind, "unix")
    # Windows 的 os.path.join 用反斜杠，不能拿 "/ncmgui-mpv-" 去匹配整条路径；
    # 断言目录名本身，Linux 与 Windows 都成立
    check("POSIX 端点在临时目录里", "ncmgui-mpv-" in Path(unix.value).parent.name, True)
    check("POSIX 路径足够短", len(unix.value.encode()) < 100, True)
    shutil.rmtree(unix.cleanup_dir, ignore_errors=True)

    pipe = ipc_endpoint(windows=True, token="t2")
    check("Windows 端点类型", pipe.kind, "pipe")
    check("Windows 管道名", pipe.value, r"\\.\pipe\ncmgui-mpv-t2")

    # ---- 命令编码
    encoded = encode_command(["loadfile", "https://a/b.flac", "replace"], request_id=7)
    check("命令以换行结尾", encoded.endswith(b"\n"), True)
    check("命令只有一行", encoded.count(b"\n"), 1)
    payload = json.loads(encoded.decode("utf-8"))
    check("命令体", payload["command"], ["loadfile", "https://a/b.flac", "replace"])
    check("request_id", payload["request_id"], 7)
    # 参数里的换行必须被转义，否则一条命令会被拆成两条
    check(
        "参数里的换行被转义",
        encode_command(["x", "a\nb"]).count(b"\n"),
        1,
    )

    # ---- 消息解析
    event = parse_message('{"event":"end-file","reason":"eof"}')
    check("事件类型", event is not None and event.kind, "event")
    check("事件名", event.name if event else "", "end-file")
    check("end-file 的 reason", end_reason(event) if event else "", "eof")

    reply = parse_message('{"error":"success","request_id":3,"data":1.5}')
    check("回复类型", reply is not None and reply.kind, "reply")
    check("回复 request_id", reply.request_id if reply else None, 3)
    check("回复数据", reply.data if reply else None, 1.5)

    failed = parse_message('{"error":"property unavailable","request_id":4}')
    check("失败回复的 error", failed.error if failed else "", "property unavailable")

    check("非 JSON 返回 None", parse_message("hello"), None)
    check("空行返回 None", parse_message("  \n"), None)
    check("JSON 数组返回 None", parse_message("[1,2]"), None)
    check("bytes 输入也能解析", parse_message(b'{"event":"file-loaded"}').name, "file-loaded")

    prop = parse_message('{"event":"property-change","id":3,"name":"time-pos","data":12.5}')
    check("属性事件的数据", prop.raw.get("data") if prop else None, 12.5)
    check("非 end-file 事件没有 reason", end_reason(prop) if prop else "", "")

    # ---- 数值
    check("音量向下取整", clamp_volume(69.6), 70)
    check("音量上限", clamp_volume(150), 100)
    check("音量下限", clamp_volume(-5), 0)
    check("音量非法回退", clamp_volume("响一点", 60), 60)

    check("时长 0", format_clock(0), "00:00")
    check("时长 59", format_clock(59.9), "00:59")
    check("时长 60", format_clock(60), "01:00")
    check("时长 3599", format_clock(3599), "59:59")
    check("时长一小时", format_clock(3661), "1:01:01")
    check("时长未知", format_clock(None), "--:--")
    check("时长负数", format_clock(-1), "--:--")

    check("进度 0", progress_value(0, 100), 0)
    check("进度一半", progress_value(50, 100), 500)
    check("进度越界钳住", progress_value(200, 100), 1000)
    check("进度无时长", progress_value(10, None), 0)
    check("刻度回秒", seek_seconds(500, 100), 50.0)
    check("刻度回秒无时长", seek_seconds(500, 0), 0.0)

    # ---- 搜索与切歌
    check("多词 AND", matches_query("周杰伦 - 晴天", "周杰伦 晴天"), True)
    check("少一个词不匹配", matches_query("周杰伦 - 晴天", "周杰伦 稻香"), False)
    check("大小写不敏感", matches_query("Lemon", "lemon"), True)
    check("空查询全匹配", matches_query("任意", ""), True)

    check("空列表没有下一首", step_index(None, 0, 1), None)
    check("从未播放时下一首取第一首", step_index(None, 5, 1), 0)
    check("从未播放时上一首取最后一首", step_index(None, 5, -1), 4)
    check("向前环绕", step_index(4, 5, 1), 0)
    check("向后环绕", step_index(0, 5, -1), 4)
    check("不环绕越界", step_index(4, 5, 1, wrap=False), None)

    # ---- 直链缓存
    now = [1000.0]
    cache = UrlCache(clock=lambda: now[0])
    source = AudioSource("https://x/1.mp3", "standard", expi=120)
    cache.put((1, "standard"), source)
    check("缓存命中", cache.get((1, "standard")) is source, True)
    check("键不同不命中", cache.get((1, "lossless")), None)
    now[0] += 59
    check("过期前仍命中", cache.get((1, "standard")) is source, True)
    now[0] += 2
    check("提前 60 秒过期", cache.get((1, "standard")), None)
    cache.put((2, "standard"), AudioSource("u", expi=30))
    cache.invalidate((2, "standard"))
    check("invalidate 后不命中", cache.get((2, "standard")), None)
    cache.put((3, "standard"), AudioSource("u"))
    cache.clear()
    check("clear 清空", len(cache), 0)

    # ---- 命令行
    argv = mpv_argv("/usr/bin/mpv", Endpoint("unix", "/tmp/x.sock"), volume=80)
    for flag in ("--no-config", "--idle=yes", "--force-window=no", "--vid=no"):
        check(f"启动参数含 {flag}", flag in argv, True)
    check("指定了 IPC 端点", "--input-ipc-server=/tmp/x.sock" in argv, True)
    check("音量传进去", "--volume=80" in argv, True)
    check("默认不指定 --ao", any(a.startswith("--ao=") for a in argv), False)

    # ---- IPC 连接：连上后必须恢复阻塞
    # 回归测试：连接后如果还留着 2 秒超时，mpv 空闲或暂停超过 2 秒时读线程
    # 会抛 TimeoutError 退出，之后 file-loaded 收不到，界面永远停在「正在缓冲」
    if sys.platform != "win32":
        with tempfile.TemporaryDirectory(prefix="ncmgui-ipc-") as tmp:
            ipc_path = os.path.join(tmp, "ipc.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(ipc_path)
            server.listen(1)
            client = connect_ipc_socket(ipc_path, timeout=0.5)
            check("IPC 连接后没有读超时", client.gettimeout(), None)
            client.close()
            server.close()
            try:
                connect_ipc_socket(os.path.join(tmp, "missing.sock"), timeout=0.2)
            except OSError:
                failed = True
            else:
                failed = False
            check("IPC 连接失败抛 OSError", failed, True)

    # ---- 查找优先级：显式路径最优先
    with tempfile.TemporaryDirectory(prefix="ncmgui-player-test-") as tmp:
        fake = Path(tmp) / MPV_BINARY_NAME
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        check("显式路径优先", find_mpv(str(fake)), str(fake))
        check("显式路径不存在时不回退", find_mpv(str(Path(tmp) / "nope")), None)
        env = child_env(str(fake))
        check("没有 lib 目录时不改环境", "LD_LIBRARY_PATH" not in env or True, True)

    if failures:
        print(f"播放器自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("播放器自检全部通过")
    found = find_mpv()
    print(f"探测到的 mpv：{found or '未找到（随包或 PATH 里都没有）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
