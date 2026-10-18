"""进程执行层：把一条 ncmctl 命令跑成流式信号。

## 为什么必须给子进程一个伪终端（pty）

``ncmctl download`` 一进来就调 ``pb.StartPool()`` → ``termutil.RawModeOn()``，
后者**无条件**去问「我这个进程的标准输出是不是一块真终端」：

* Linux / macOS（``termutil/term_x.go``）：对 ``/dev/tty``（拿不到就退回
  ``os.Stdin``）做 termios ioctl，非终端时返回 ``ENOTTY``
* Windows（``termutil/term_win.go``）：对 ``STD_OUTPUT_HANDLE`` 调
  ``GetConsoleMode``，stdout 是管道时返回 ``ERROR_INVALID_HANDLE``

失败后 ``StartPool`` 把错误原样抛回去，download 在下载任何东西之前就以
``StartPool: ...`` 退出，**一个字节都下不了**。Windows 版还更狠一点：刷进度
时额外用 ``GetConsoleScreenBufferInfo`` / ``SetConsoleCursorPosition``，失败
直接 ``log.Panic``。

注意这跟「用 cmd / powershell 包一层」无关 —— cmd 自己的 stdout 也是我们给的
管道，它拉起的 ncmctl 继承的还是那条管道，照样不是控制台。``CREATE_NEW_CONSOLE``
倒是能给 ncmctl 一块控制台，但输出进的是另一个窗口，GUI 一个字都读不到。

所以要同时满足「子进程看到的是真终端」和「父进程能读走输出」，只有伪终端：

* **Linux / macOS**：标准库 ``pty.openpty()``，配合 ``start_new_session=True``
  让 ``/dev/tty`` 必然打不开，从而退回到我们给的 pty
* **Windows**：``pywinpty``（ConPTY，Windows 10 1809+）。Python 标准库在
  Windows 上没有 pty 概念，这是唯一可行的办法。打包时它会一起进 exe，
  用户不需要额外安装

pty 尺寸必须报成正常行列数：``openpty()`` 给的是 0×0，pb 据此算出的进度条
宽度是 0，整条进度会渲染成空白。见 ``_PTY_ROWS`` / ``_PTY_COLS``。

## 其余约定

* 合并 stdout/stderr，让 ncmctl 的日志与报错按真实顺序出现在同一视图里
* 同时处理 ``\\n`` 与 ``\\r``：前者是普通换行，后者是进度条原地刷新，
  分别发成 ``output`` 与 ``progress`` 两种信号，界面才不会刷屏
* 保留 stdin 写入能力 —— ``login phone`` 需要交互式输入短信验证码
* ``stop()`` 先温和终止，超时后再强杀；按进程组/进程树收，连带子进程一起

pty 的读端由一条守护线程持续读，读到的东西经 :meth:`JobRunner._consume` 切分
后发信号；另一条守护线程等着进程退出，随后收尾并发 ``finished``。信号从非 Qt
线程 emit 是安全的，PyQt 会自动改成队列连接，槽函数仍然在主线程执行。
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from . import ncmctl

#: 当前平台是不是 Windows。这里是全模块唯一的平台判断。
_IS_WINDOWS = sys.platform == "win32"

if not _IS_WINDOWS:
    import fcntl
    import pty
    import signal
    import struct
    import termios

#: 一次从 pty 读多少字节
_READ_CHUNK = 65536

#: 匹配「可能被分块读取截断」的转义序列尾部
_PARTIAL_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*)?$")

#: 伪终端的尺寸（行、列）。``pty.openpty()`` 给的是 0x0，ConPTY 默认 24x80，
#: pb 这种进度条库会据此算出宽度 —— 太窄会把文件名截成省略号，0 则整条渲染
#: 成空白。窗口本身能不能显示这么多列无所谓，反正输出是流式读走的。
_PTY_ROWS = 40
_PTY_COLS = 120

#: 会向子进程注入代码/库的环境变量。ncmctl 是独立程序，不需要它们；
#: 从被污染的终端启动本程序时，这些变量会把任意 .so 或 Python 模块带进子进程。
_INJECTION_ENV = (
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONHOME",
)


def clean_env(env) -> dict[str, str]:
    """复制一份环境变量，剔除可注入类变量；其余（PATH、HOME、库路径）保留。"""
    return {key: value for key, value in env.items() if key not in _INJECTION_ENV}


# --------------------------------------------------------------------------- #
# 伪终端会话：POSIX 与 Windows 两套实现背后的窄接口
# --------------------------------------------------------------------------- #

class _PtySession:
    """一个跑在伪终端上的子进程。

    两个平台给 pty 的 API 差别很大 —— POSIX 是「先开一对 fd，再 Popen 把
    fd 塞给它」，Windows 的 ConPTY 则是「spawn 一把梭，进程和 pty 绑在一起」。
    这里把差异收在下面两个子类里，``JobRunner`` 只对着这个接口写。

    统一约定：``read()`` 返回**已经解码好的** ``str``；终端关闭（子进程退出）
    时返回空串，表示 EOF。
    """

    #: 子进程 pid
    pid: int = 0

    def read(self) -> str:
        """阻塞读一段输出；子进程结束、pty 关闭后返回 ``""``。"""
        raise NotImplementedError

    def write(self, text: str) -> None:
        """往子进程的 stdin 写一段文本。"""
        raise NotImplementedError

    def is_alive(self) -> bool:
        raise NotImplementedError

    def wait(self) -> int:
        """阻塞等到子进程退出，返回退出码。"""
        raise NotImplementedError

    def stop(self) -> None:
        """温和地请求退出。"""
        raise NotImplementedError

    def kill(self) -> None:
        """强杀，连带它拉起的子进程。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放 pty 资源。"""
        raise NotImplementedError


class _PosixSession(_PtySession):
    """Linux / macOS：``pty.openpty()`` + ``subprocess.Popen``。"""

    def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str]) -> None:
        master, slave = pty.openpty()
        try:
            _configure_pty(slave)
            self._proc = subprocess.Popen(  # noqa: S603 - 参数以数组传递，不经 shell
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                # 自成会话，子进程就没有控制终端了：ncmctl 里的 /dev/tty 会打不开，
                # 从而退回到 stdin —— 而 stdin 正是我们给它的 pty，ioctl 才能成功
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            os.close(master)
            os.close(slave)
            raise

        # 父进程不再需要从端；留着它会让 pty 永远读不到 EOF
        os.close(slave)
        self._fd = master
        self.pid = self._proc.pid

        # UTF-8 是变长编码，而 os.read 是按字节数返回的：一个汉字正好跨在
        # 读取边界上时，两半会被分开解码，各变成一个 U+FFFD 替换字符。用增量
        # 解码器把没凑齐的字节留到下一轮，日志里的中文才不会出现「��」。
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self) -> str:
        try:
            chunk = os.read(self._fd, _READ_CHUNK)
        except OSError:
            # 子进程退出、从端全部关闭后，Linux 上读主端会得到 EIO，
            # 这不是错误，就是 EOF
            return ""
        return self._decoder.decode(chunk)

    def write(self, text: str) -> None:
        try:
            os.write(self._fd, text.encode("utf-8"))
        except OSError:
            pass

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def wait(self) -> int:
        return int(self._proc.wait())

    def stop(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def _signal(self, sig: int) -> None:
        """按进程组发信号，连带子进程一起收掉；失败则退回到只发给它自己。"""
        if self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.pid), sig)
        except OSError:
            try:
                self._proc.send_signal(sig)
            except OSError:
                pass


class _WindowsSession(_PtySession):
    """Windows：``pywinpty`` 提供的 ConPTY。

    跟 POSIX 版最大的不同是进程与 pty 绑在一起（``PtyProcess`` 同时管两者），
    所以这里没有单独的 ``Popen``，退出码也从它身上取。
    """

    def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str]) -> None:
        try:
            from winpty import PtyProcess
        except ImportError as exc:  # pragma: no cover - 只在缺依赖的 Windows 上走
            raise RuntimeError(
                "Windows 上需要 pywinpty 才能在伪终端里运行 ncmctl。\n\n"
                "如果你是从源码运行的，请执行：\n"
                "    pip install pywinpty"
            ) from exc

        # dimensions 是 (行, 列)，注意顺序跟 TIOCSWINSZ 一致
        self._proc = PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(_PTY_ROWS, _PTY_COLS)
        )
        self.pid = self._proc.pid

    def read(self) -> str:
        try:
            # pywinpty 内部已经处理了 UTF-8 的跨块截断，直接拿 str 即可
            return self._proc.read(_READ_CHUNK)
        except (EOFError, OSError):
            return ""

    def write(self, text: str) -> None:
        try:
            self._proc.write(text)
        except (EOFError, OSError):
            pass

    def is_alive(self) -> bool:
        try:
            return bool(self._proc.isalive())
        except OSError:
            return False

    def wait(self) -> int:
        try:
            return int(self._proc.wait() or 0)
        except OSError:
            return -1

    def stop(self) -> None:
        # ConPTY 没有「温和信号」这一档：ncmctl 也没装控制台事件处理，
        # 发 CTRL_BREAK 它不会理，所以直接走强杀
        self.kill()

    def kill(self) -> None:
        """用 ``taskkill /T`` 收掉整棵进程树。

        不用 pywinpty 自带的 ``terminate()``：它内部对 ``signal.SIGINT`` 调
        ``os.kill``，而 Windows 的 ``os.kill`` 只认 SIGTERM 与 CTRL_*_EVENT，
        这条路会直接抛 OSError。也不能只杀 ncmctl 自己 —— download 会开并发
        worker，父进程没了它们会变成孤儿继续跑。
        """
        if not self.is_alive():
            return
        argv = ["taskkill", "/F", "/T", "/PID", str(self.pid)]
        try:
            subprocess.run(  # noqa: S603 - 参数以数组传递，不经 shell
                argv, capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                self._proc.kill(signal.SIGTERM)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._proc.close()
        except OSError:
            pass


def _configure_pty(fd: int) -> None:
    """把 pty 调成「适合被程序解析」的样子（仅 POSIX）。

    三处终端加工要关掉，否则会污染流式解析：

    * ``ONLCR`` 把 ``\\n`` 变成 ``\\r\\n``，让每一行后面多出一个空行
    * ``ECHO`` 把写进 stdin 的内容原样回显回来
    * ``ICRNL`` 把 ``\\r`` 变成 ``\\n``，进度条就认不出来了

    另外要把窗口尺寸报上去（见 ``_PTY_COLS``）。任何一步失败都不致命，
    保持原样即可。
    """
    try:
        attrs = termios.tcgetattr(fd)
    except termios.error:
        return

    attrs[0] &= ~termios.ICRNL  # iflag
    attrs[1] &= ~termios.ONLCR  # oflag
    attrs[3] &= ~termios.ECHO   # lflag

    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass

    try:
        fcntl.ioctl(
            fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0)
        )
    except OSError:
        pass


def _make_session(
    argv: list[str], cwd: str | None, env: dict[str, str]
) -> _PtySession:
    """按当前平台挑一个伪终端后端。"""
    factory = _WindowsSession if _IS_WINDOWS else _PosixSession
    return factory(argv, cwd, env)


# --------------------------------------------------------------------------- #
# 作业
# --------------------------------------------------------------------------- #

class JobRunner(QObject):
    """一次 ncmctl 调用的生命周期。

    信号：

    * ``started()``                进程真正跑起来了
    * ``output(str)``              一整行输出（含空行）
    * ``progress(str)``            ``\\r`` 刷新的一行，界面应当覆盖上一行
    * ``finished(int, float)``     退出码与耗时（秒）；被信号杀死时退出码为负
    * ``failed(str)``              连启动都没成功，或进程异常
    """

    started = pyqtSignal()
    output = pyqtSignal(str)
    progress = pyqtSignal(str)
    finished = pyqtSignal(int, float)
    failed = pyqtSignal(str)

    def __init__(
        self,
        binary: str,
        argv: list[str],
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.binary = binary
        self.argv = list(argv)
        self.cwd = cwd
        self.extra_env = dict(extra_env or {})

        self._partial = ""
        self._ansi_carry = ""
        self._started_at = 0.0
        self._finished_at = 0.0
        self._settled = False

        self._session: _PtySession | None = None
        self._reader: threading.Thread | None = None
        self._kill_timer: threading.Timer | None = None

    # ------------------------------------------------------------------ 状态

    @property
    def is_running(self) -> bool:
        session = self._session
        return session is not None and session.is_alive()

    @property
    def command_line(self) -> str:
        """可直接粘贴回终端执行的完整命令。

        密码、请求头、XEAPI 密钥等参数的值会显示成 ``******``：它们本来就会
        出现在进程列表里，再写进界面、剪贴板和导出的日志只会扩大暴露面。
        """
        return ncmctl.describe(ncmctl.redact_argv([self.binary, *self.argv]))

    @property
    def elapsed(self) -> float:
        if not self._started_at:
            return 0.0
        return (self._finished_at or time.monotonic()) - self._started_at

    # ------------------------------------------------------------------ 控制

    def start(self) -> bool:
        """启动进程。返回 False 表示连进程都没能拉起。"""
        if self.is_running:
            return False

        self._started_at = time.monotonic()
        self._finished_at = 0.0
        self._settled = False
        self._partial = ""
        self._ansi_carry = ""

        env = clean_env(os.environ)
        env.update(clean_env(self.extra_env))

        session: _PtySession | None = None
        message = ""
        try:
            session = _make_session(
                [self.binary, *self.argv], self.cwd or None, env
            )
        except FileNotFoundError:
            message = f"无法启动 {self.binary} —— 请确认路径正确且有执行权限"
        except RuntimeError as exc:
            # 例如 Windows 上没装 pywinpty，消息里已经写清楚怎么办
            message = str(exc)
        except OSError as exc:
            message = f"无法分配伪终端：{exc}"

        if session is None:
            self._settled = True
            self.failed.emit(message)
            self.finished.emit(-1, 0.0)
            return False

        self._session = session
        self.started.emit()

        self._reader = threading.Thread(
            target=self._read_loop, args=(session,), name="ncmgui-job-read", daemon=True
        )
        self._reader.start()
        threading.Thread(
            target=self._watch, args=(session, self._reader),
            name="ncmgui-job-wait", daemon=True,
        ).start()
        return True

    def stop(self, grace_ms: int = 2000) -> None:
        """请求停止：先温和终止，超时后强杀。"""
        session = self._session
        if session is None or not session.is_alive():
            return
        session.stop()
        self._cancel_kill_timer()
        self._kill_timer = threading.Timer(grace_ms / 1000, self._force_kill)
        self._kill_timer.daemon = True
        self._kill_timer.start()

    def send_line(self, text: str) -> None:
        """往 stdin 写一行 —— 供 ``login phone`` 输入短信验证码等交互使用。"""
        session = self._session
        if session is None or not session.is_alive():
            return
        session.write(text + "\n")

    def wait_for_finish(self, timeout_ms: int = 1500) -> bool:
        """阻塞等待进程结束，用于退出程序前给子进程一点收尾时间。

        会短暂卡住事件循环，因此只在退出路径上调用。
        """
        session = self._session
        if session is None or not session.is_alive():
            return True
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if not session.is_alive():
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------ 内部

    def _force_kill(self) -> None:
        session = self._session
        if session is not None:
            session.kill()

    def _cancel_kill_timer(self) -> None:
        timer, self._kill_timer = self._kill_timer, None
        if timer is not None:
            timer.cancel()

    def _read_loop(self, session: _PtySession) -> None:
        """守护线程：把 pty 上的输出读干净，直到从端关闭。"""
        while True:
            text = session.read()
            if not text:
                break
            self._consume(self._strip_stream(text))
        session.close()

    def _watch(self, session: _PtySession, reader: threading.Thread) -> None:
        """守护线程：等进程退出，再等输出读干净，然后收尾。

        不把这两件事都塞进读线程，是为了防止 pty 迟迟读不到 EOF
        （例如子进程留下了还握着从端的孙进程）时 ``finished`` 永远不发 ——
        那样控制台标签页会一直显示「运行中」。
        """
        code = session.wait()
        self._finished_at = time.monotonic()

        reader.join(timeout=1.0)
        self._finish(code)

    def _finish(self, code: int) -> None:
        self._cancel_kill_timer()

        # 冲刷没有换行结尾的残留输出
        if self._partial.strip():
            self.output.emit(self._partial.rstrip())
        self._partial = ""

        if self._settled:
            return
        self._settled = True

        if code < 0:
            self.failed.emit(f"进程被信号 {-code} 结束")

        self.finished.emit(code, self.elapsed)

    # ------------------------------------------------------------------ 输出

    def _strip_stream(self, text: str) -> str:
        """清除 ANSI 序列；对可能被分块截断的转义序列留到下一块再处理。"""
        text = self._ansi_carry + text
        self._ansi_carry = ""
        match = _PARTIAL_ESCAPE_RE.search(text)
        if match:
            self._ansi_carry = text[match.start():]
            text = text[:match.start()]
        return ncmctl.strip_ansi(text)

    def _consume(self, text: str) -> None:
        """按 ``\\n`` / ``\\r`` 切分。

        ``\\n`` 结尾的一段是正式日志行；``\\r`` 结尾的一段是进度刷新。
        以 ``\\r`` 分隔的进度条最终会以一个 ``\\n`` 收尾，那一版会成为正式行。
        """
        buf = self._partial + text
        self._partial = ""
        start = 0
        for index, char in enumerate(buf):
            if char == "\n":
                self.output.emit(buf[start:index].rstrip())
                start = index + 1
            elif char == "\r":
                segment = buf[start:index].rstrip()
                if segment:
                    self.progress.emit(segment)
                start = index + 1
        self._partial = buf[start:]
