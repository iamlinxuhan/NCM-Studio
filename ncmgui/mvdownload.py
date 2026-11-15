"""下载 MV 的公共实现。

MV 播放页的「下载」、收藏列表的「全部下载」、下载页的「下载收藏的 MV」
都走这里，保证三个入口的文件名、去重规则与断点续传行为完全一致。

自检::

    .venv/bin/python -m ncmgui.mvdownload
"""

from __future__ import annotations

import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Callable

from .api.transport import USER_AGENT

REFERER = "https://music.163.com/"

#: 重试次数（每次都会重新取播放地址，旧地址可能已过期）
ATTEMPTS = 4

#: 单个 MV 的大小上限。异常 CDN 或恶意响应不该把磁盘写满。
MAX_MV_BYTES = 2 * 1024 ** 3

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class DownloadCancelled(Exception):
    """用户取消了下载。"""


def _parse_content_range(value: str) -> tuple[int, int] | None:
    """解析 ``Content-Range: bytes 100-199/2000``，返回 ``(起点, 总长)``。"""
    text = str(value or "").strip()
    if not text.startswith("bytes "):
        return None
    try:
        span, _, total_text = text[6:].partition("/")
        start = int(span.split("-", 1)[0])
        total = int(total_text)
    except (ValueError, IndexError):
        return None
    if start < 0 or total <= 0 or total > MAX_MV_BYTES:
        return None
    return start, total


def mv_filename(mv) -> str:
    """``歌手 - 名称.mp4``；非法字符替换掉，为空时退回 id。"""
    base = f"{mv.artist} - {mv.name}" if getattr(mv, "artist", "") else mv.name
    base = _BAD_FILENAME.sub("_", str(base)).strip().strip(".") or f"mv-{mv.id}"
    return f"{base}.mp4"


def unique_path(path: Path) -> Path:
    """同名文件已经存在时加序号，不覆盖用户的东西。"""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def fetch_mv_file(
    client,
    mv,
    target: Path,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    skip_existing: bool = True,
) -> str:
    """把一支 MV 完整下载到 ``target``，返回最终路径。

    网易云的视频 CDN 会中途掐连接：先写 ``.part``，断了用 ``Range`` 从
    已下载的位置续传，同时重新取播放地址。最多续 ``ATTEMPTS`` 次。

    ``progress`` 收到 ``-1`` 表示总长未知、``0–99`` 表示百分比；
    ``cancelled`` 返回真时抛 :class:`DownloadCancelled` 并清理半截文件。
    ``skip_existing`` 为真时，目标已存在且非空就直接返回 —— 最终名只在
    下载完整后才出现，所以存在即完整，不会把半截文件当成已完成。
    """
    if skip_existing and target.is_file() and target.stat().st_size > 0:
        return str(target)
    url, _fee = client.mv_url(mv.id)
    part = target.with_name(target.name + ".part")
    attempt = 0
    if progress is not None:
        progress(-1)
    while True:
        written = part.stat().st_size if part.is_file() else 0
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
        }
        if written:
            headers["Range"] = f"bytes={written}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = int(getattr(response, "status", 200) or 200)
                length = int(response.headers.get("Content-Length") or 0)
                if status == 206:
                    parsed = _parse_content_range(
                        response.headers.get("Content-Range") or "")
                    if parsed is None:
                        # 没有 Content-Range 就没法确认这一段属于哪个位置，
                        # 拼出来的文件可能是错的
                        raise OSError("续传响应缺少有效的 Content-Range")
                    start, total = parsed
                    if start != written:
                        raise OSError("续传响应的起点与已下载位置不一致")
                    base = written
                    mode = "ab"
                else:
                    if length <= 0:
                        # 200 且没有长度：读到 EOF 也无法判断是否完整
                        raise OSError("服务器没有提供文件长度，无法校验完整性")
                    base = 0
                    total = length
                    mode = "wb"
                if total > MAX_MV_BYTES:
                    raise OSError("视频超过大小上限，已中止")
                got = 0
                with open(part, mode) as handle:
                    while True:
                        if cancelled is not None and cancelled():
                            raise DownloadCancelled("已取消下载")
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if base + got > total:
                            raise OSError("下载数据超过声明的长度")
                        handle.write(chunk)
                        if progress is not None:
                            progress(min(99, (base + got) * 100 // total))
                if base + got != total:
                    # 连接提前关闭时 read() 只返回 b''，不抛异常；
                    # 不核对长度就会把半截文件当成下载完成
                    raise OSError("下载不完整")
            os.replace(part, target)
            return str(target)
        except DownloadCancelled:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        except Exception:  # noqa: BLE001 - 网络异常统一重试
            attempt += 1
            if attempt > ATTEMPTS:
                try:
                    part.unlink()
                except OSError:
                    pass
                raise
            time.sleep(1.5 * attempt)
            # 旧地址可能已随断流失效，重新取一次再续传
            url, _fee = client.mv_url(mv.id)


def download_all(
    client,
    mvs,
    directory: Path | str,
    *,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """顺序下载一批 MV，返回 ``(新下载数, 跳过数)``。

    目标目录里已有同名且非空的文件就跳过，不生成「(2)」副本 —— 和歌曲
    下载的查重行为保持一致。
    """
    items = list(mvs)
    folder = Path(directory)
    downloaded = 0
    skipped = 0
    for index, mv in enumerate(items):
        if cancelled is not None and cancelled():
            raise DownloadCancelled("已取消下载")
        target = folder / mv_filename(mv)
        if target.is_file() and target.stat().st_size > 0:
            skipped += 1
            if progress is not None:
                progress(f"跳过已存在：{mv.name}")
            continue
        if progress is not None:
            progress(f"正在下载 {index + 1}/{len(items)}：{mv.name}")
        fetch_mv_file(client, mv, target, cancelled=cancelled)
        downloaded += 1
    return downloaded, skipped


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    mv = type("M", (), {"id": 9, "name": 'a/b:c*?"<>|', "artist": "某歌手"})()
    check("文件名清理", mv_filename(mv), "某歌手 - a_b_c______.mp4")
    bare = type("M", (), {"id": 7, "name": "", "artist": ""})()
    check("空名退回 id", mv_filename(bare), "mv-7.mp4")

    payload = bytes(range(256)) * 2048  # 512 KiB
    state = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["n"] += 1
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes="):
                start = int(rng[6:].split("-", 1)[0])
                body = payload[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if state.get("no_length"):
                # 200 且没有 Content-Length：客户端必须拒绝，而不是把
                # 读到 EOF 的内容当成完整文件
                state["no_length"] = False
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True
                return
            half = len(payload) // 2
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload[:half])
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/a.mp4"
    client = type("C", (), {"mv_url": lambda self, mv_id: (url, 0)})()

    with tempfile.TemporaryDirectory() as root:
        target = Path(root) / "out.mp4"
        got = fetch_mv_file(client, mv, target)
        check("下载路径", got, str(target))
        check("内容完整", target.read_bytes(), payload)
        check("断流后重连", state["n"] >= 2, True)

        before = state["n"]
        fetch_mv_file(client, mv, target)
        check("已存在时不重复下载", state["n"], before)

        named = Path(root) / mv_filename(mv)
        named.write_bytes(payload)
        check("批量下载跳过已存在", download_all(client, [mv], root), (0, 1))

        state["no_length"] = True
        again = Path(root) / "again.mp4"
        fetch_mv_file(client, mv, again)
        check("无长度响应被拒绝并重试", again.read_bytes(), payload)

    server.shutdown()

    if failures:
        print(f"MV 下载自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("MV 下载自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
