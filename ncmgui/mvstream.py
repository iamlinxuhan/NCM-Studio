"""把 MV 的网络请求接管到一个随机访问的 ``QIODevice``。

QMediaPlayer 直接拿 URL 播放时，请求由 FFmpeg 的 HTTP 层发出：不带
``Referer``、UA 不可控，CDN 中途掐连接也没有续传能力 —— 日志里的
``Stream ends prematurely`` / ``partial file`` 就来自这里。

这个设备改为由我们自己发请求：播放器需要哪一段就按 ``Range`` 取哪一段，
带上 ``Referer`` 和浏览器 UA；连接被掐断时从这里换新地址、从当前位置续上，
播放器只看到一个连续的文件。既补上了请求头，也让 seek、时长探测都能工作。

自检::

    .venv/bin/python -m ncmgui.mvstream
"""

from __future__ import annotations

import threading
import time
import urllib.request
from typing import Callable

from PySide6.QtCore import QIODevice

from .api.transport import USER_AGENT
from .netutil import media_url

REFERER = "https://music.163.com/"


class HttpRangeDevice(QIODevice):
    """按需读取远程文件，支持随机访问与断连续传。

    :param provider: 无参回调，返回当前可用的播放地址。地址可能在播放途中
        过期，所以每次重连都要重新问一次，不能把 URL 当常量。
    """

    #: 一次预取的字节数。太小会让请求变得密集，太大则 seek 后白读。
    BLOCK = 2 * 1024 * 1024
    #: 单次请求失败后的重试次数（每次都会重新取地址）。
    ATTEMPTS = 4
    #: 单次 HTTP 请求的超时；播放器线程会等在这里，别设太长。
    TIMEOUT = 15.0

    def __init__(
        self,
        provider: Callable[[], str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._lock = threading.RLock()
        self._size = -1
        self._buf = b""
        self._buf_start = 0
        self._url = ""
        self._closed = False
        self._prefetched = False
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    # ------------------------------------------------------------ QIODevice

    def isSequential(self) -> bool:  # noqa: N802 - Qt 命名
        # 只有声明为「非顺序」播放器才会来问 size()/seek()，mp4 的时长和
        # 拖进度都依赖这个
        return False

    def size(self) -> int:
        with self._lock:
            # 已经预取过就不再现场探测：size() 可能被 GUI 线程同步调用，
            # 这里发网络请求会把窗口冻住
            if self._size < 0 and not self._prefetched:
                try:
                    self._size = self._probe_size()
                except Exception:  # noqa: BLE001 - 探测失败按未知处理
                    self._size = -1
            return max(0, self._size)

    def atEnd(self) -> bool:  # noqa: N802 - Qt 命名
        with self._lock:
            # 位置由 QIODevice 自己维护：read() 会预读进内部缓冲，
            # 自己再记一份 pos 只会和它错位
            return self._size >= 0 and self.pos() >= self._size

    def readData(self, maxlen: int) -> bytes:  # noqa: N802 - Qt 命名
        with self._lock:
            if self._closed or maxlen <= 0:
                return b""
            position = int(self.pos())
            if self._size >= 0 and position >= self._size:
                return b""
            start, end = self._buf_start, self._buf_start + len(self._buf)
            if not (start <= position < end):
                if not self._fill(position):
                    return b""
                start, end = self._buf_start, self._buf_start + len(self._buf)
                if not (start <= position < end):
                    return b""
            offset = position - start
            return self._buf[offset:offset + maxlen]

    def close(self) -> None:  # noqa: D401 - Qt 重写
        # 不阻塞等锁：readData/prefetch 可能正卡在网络请求里，而 close()
        # 是从 GUI 线程调用的。等锁会连带把窗口冻住，这里只置标志，
        # 正在进行的请求会自己结束并发现设备已关闭。
        self._closed = True
        if self._lock.acquire(blocking=False):
            try:
                self._buf = b""
            finally:
                self._lock.release()
        super().close()

    def prefetch(self) -> None:
        """在后台线程里先取好头部，确认 Range 支持并拿到总长。

        ``QMediaPlayer.setSourceDevice`` 会在调用线程里同步问 ``size()``。
        如果 ``size()`` 现场发网络请求，这一段就卡在 GUI 线程上，地址慢
        或代理不通时整个窗口会「没有响应」。所以起播前先在后台预取第一块，
        让随后的 ``size()`` / 首次 ``readData()`` 直接命中内存。
        """
        with self._lock:
            if self._closed or self._buf:
                return
            data, total, _ranged = self._request(0, self.BLOCK - 1)
            if self._closed:
                return
            if total >= 0:
                self._size = total
            if data:
                self._buf = data
                self._buf_start = 0

    # ------------------------------------------------------------ 内部

    def _probe_size(self) -> int:
        _data, total, _ranged = self._request(0, 0)
        return total

    def _request(self, start: int, end: int) -> tuple[bytes, int, bool]:
        """取 ``[start, end]``，返回 ``(数据, 文件总长, 是否 206)``。"""
        url = media_url(self._url or self._provider())
        self._url = url
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Accept": "*/*",
        }
        if end >= start:
            headers["Range"] = f"bytes={start}-{end}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.TIMEOUT) as response:
            status = int(getattr(response, "status", 200) or 200)
            total = -1
            content_range = response.headers.get("Content-Range") or ""
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = -1
            if status != 206:
                # 服务器忽略了 Range。随机访问与 seek 都无从谈起，
                # 整文件读入内存还会随视频大小暴涨 —— 直接拒绝，让上层
                # 走「完整缓存到磁盘再播放」的兜底路径。
                raise OSError("视频服务器不支持 Range 请求")
            length = int(response.headers.get("Content-Length") or 0)
            want = max(1, end - start + 1)
            # 即使响应头声称更大，也只读请求的这一块
            length = min(length, want) if length > 0 else want
            data = response.read(length)
            if total < 0 and length > 0:
                total = start + length
            return data, total, True

    def _fill(self, position: int) -> bool:
        """把 ``position`` 所在的一块读进缓冲区，失败时重试并换地址。"""
        attempt = 0
        while True:
            try:
                data, total, ranged = self._request(
                    position, position + self.BLOCK - 1)
                if self._closed:
                    return False
                if total >= 0:
                    self._size = total
                if not data:
                    return False
                self._buf = data
                # 服务端忽略 Range（回了 200）时数据是整份文件，从头对齐
                self._buf_start = position if ranged else 0
                return True
            except Exception:  # noqa: BLE001 - 网络异常统一重试
                attempt += 1
                if attempt > self.ATTEMPTS:
                    raise
                # 旧地址可能已随断流失效，下一轮重新取一次
                self._url = ""
                time.sleep(min(2.0, 0.5 * attempt))


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    payload = bytes(range(256)) * 32768  # 8 MiB，确保跨多块
    state = {"n": 0, "drop_once": True, "ignore_range": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 命名
            state["n"] += 1
            if state.get("ignore_range"):
                # 模拟不支持 Range 的服务器：回 200 + 整个文件
                state["ignore_range"] = False
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                start, _, last = rng[6:].partition("-")
                start = int(start)
                end = int(last) if last else len(payload) - 1
            else:
                start, end = 0, len(payload) - 1
            body = payload[start:end + 1]
            if start == 3_000_000 and state["drop_once"]:
                # 模拟 CDN 掐连接：声明完整长度，只发一半就关
                state["drop_once"] = False
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                return
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/a.mp4"

    # 这里刻意不创建 QApplication：QIODevice 属于 QtCore，自检不碰界面；
    # CI 的 Linux runner 没有显示器，建 QApplication 会直接 abort
    device = HttpRangeDevice(lambda: url)
    try:
        check("总长", device.size(), len(payload))
        check("非顺序", device.isSequential(), False)
        check("初始位置", device.pos(), 0)
        check("从头读", device.read(100), payload[:100])
        check("seek", device.seek(5000), True)
        check("seek 后读", device.read(64), payload[5000:5064])
        # 跨块边界：读完最后一块的尾部
        device.seek(len(payload) - 10)
        check("尾部读取", device.read(100), payload[-10:])
        check("atEnd", device.atEnd(), True)
        # 断流续传：seek 到远处会触发新请求，第一次（start>0）只给一半。
        # 设备应把短读当成一块数据，下一次请求从当前位置接着要。
        device.seek(3_000_000)
        got = b""
        while len(got) < 2_500_000:
            chunk = device.read(min(256 * 1024, 2_500_000 - len(got)))
            if not chunk:
                break
            got += chunk
        check("断流后续传长度", len(got), 2_500_000)
        check("断流后续传内容", got, payload[3_000_000:5_500_000])
        check("断流后确实重连过", state["n"] > 3, True)

        # 不支持 Range 的服务器必须明确拒绝：整文件 read() 会吃光内存
        state["ignore_range"] = True
        try:
            device._request(0, 0)
        except OSError:
            check("不支持 Range 被拒绝", True, True)
        else:
            check("不支持 Range 被拒绝", False, True)

        # 预取：起播前在后台拿好第一块与总长，之后 size()/首次 read()
        # 都不应再发网络请求（GUI 线程不能碰网络）
        prefetched = HttpRangeDevice(lambda: url)
        try:
            before = state["n"]
            prefetched.prefetch()
            check("预取后总长", prefetched.size(), len(payload))
            check("预取后首块", prefetched.read(64), payload[:64])
            check("预取只发一次请求", state["n"] - before, 1)
        finally:
            prefetched.close()
    finally:
        device.close()
        server.shutdown()

    if failures:
        print(f"MV 数据流自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("MV 数据流自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
