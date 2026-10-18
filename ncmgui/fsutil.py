"""原子写文件与原子编辑音频。

**为什么需要这一层：** 下载后的歌词、封面、配置和音频标签都可能写到一半时
程序被关掉、进程被系统杀掉或磁盘写满。直接 ``write_text`` / ``write_bytes``
会把目标文件截断成半截；mutagen 在原来的音频文件上就地改标签，异常时甚至可能
把整首音频写坏。用户看到的不是一个报错，而是一首再也播不了的文件。

做法都是同一条：**同目录临时文件 → 写完 fsync → os.replace**。``os.replace``
在同一文件系统内是原子的，所以目标文件要么是旧内容，要么是新内容，不存在中间
状态。音频标签还要多一步：临时文件必须保留原后缀，mutagen 靠后缀识别容器。

本模块不依赖 Qt，可以单独自检：

    .venv/bin/python -m ncmgui.fsutil
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

#: 新建文件的默认权限。已有文件会保留原来的 mode。
_DEFAULT_FILE_MODE = 0o644


def _fsync_file(path: Path) -> None:
    """把文件内容刷到磁盘。

    Windows 的 ``_commit``（``os.fsync`` 的实现）要求句柄可写，用 ``O_RDONLY``
    打开会直接抛 ``OSError(9, 'Bad file descriptor')``，所以统一用 ``O_RDWR``。
    这里刷的始终是我们自己的临时文件，本来就是可写的。
    """
    fd = os.open(path, os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *, mode: int | None = None) -> None:
    """原子地把 ``data`` 写到 ``path``。

    目标已存在时保留它的权限位；新文件用 0644（敏感文件由调用方显式传 ``mode``，
    例如配置用 0600）。临时文件与目标同目录，保证 ``os.replace`` 不跨文件系统。
    """
    dest = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        if mode is None:
            try:
                mode = dest.stat().st_mode & 0o777
            except OSError:
                mode = _DEFAULT_FILE_MODE
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """``atomic_write_bytes`` 的文本版本。"""
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_edit(path: str | os.PathLike[str], edit: Callable[[Path], T]) -> T:
    """复制一份同后缀临时文件，交给 ``edit`` 改，成功后再原子替换原文件。

    ``edit`` 接收临时文件路径，可以是 mutagen 的 ``FLAC(tmp)`` / ``MP3(tmp)``。
    **必须保留原后缀**：mutagen 按后缀决定用哪个容器解析器，``.tmp`` 会被当成
    未知格式。任何异常都会删掉临时文件，原文件字节不动。

    返回 ``edit`` 的返回值，方便调用方拿到 mutagen 对象上的信息。
    """
    source = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{source.stem}.", suffix=source.suffix, dir=str(source.parent)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        # copy2 会把源文件的权限位一并带过来：源若是只读（0444），临时副本
        # 也会只读，mutagen 和后面的 fsync 都写不了。编辑期间先放开写权限，
        # 最终权限在替换前从源文件恢复。
        os.chmod(tmp, 0o600)
        result = edit(tmp)
        _fsync_file(tmp)
        try:
            mode = source.stat().st_mode & 0o777
        except OSError:
            mode = _DEFAULT_FILE_MODE
        os.chmod(tmp, mode)
        os.replace(tmp, source)
        return result
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _selftest() -> int:
    """不联网、不需要 Qt，验证原子写与失败回滚。"""
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}\n    期望 {want!r}\n    实际 {got!r}")

    with tempfile.TemporaryDirectory(prefix="ncmgui-fsutil-") as tmp:
        root = Path(tmp)
        target = root / "歌词.lrc"
        target.write_text("旧内容", encoding="utf-8")
        target.chmod(0o640)

        atomic_write_text(target, "新内容")
        check("文本原子写内容", target.read_text("utf-8"), "新内容")
        check("已有文件保留权限", target.stat().st_mode & 0o777, 0o640)
        check("没有留下临时文件", sorted(p.name for p in root.iterdir()), ["歌词.lrc"])

        binary = root / "封面.jpg"
        atomic_write_bytes(binary, b"\xff\xd8\xff")
        check("字节原子写内容", binary.read_bytes(), b"\xff\xd8\xff")

        audio = root / "歌.flac"
        audio.write_bytes(b"ORIGINAL")
        audio.chmod(0o600)

        def boom(path: Path) -> None:
            path.write_bytes(b"HALF")
            raise RuntimeError("模拟写标签失败")

        try:
            atomic_edit(audio, boom)
        except RuntimeError:
            pass
        else:
            failures.append("atomic_edit 应当把 edit 的异常继续抛出")
        check("失败后原文件字节不变", audio.read_bytes(), b"ORIGINAL")
        check("失败后没有残留临时文件", sorted(p.name for p in root.iterdir()),
              sorted(["歌.flac", "封面.jpg", "歌词.lrc"]))

        result = atomic_edit(audio, lambda p: (p.write_bytes(b"EDITED"), "ok")[1])
        check("成功后返回 edit 的结果", result, "ok")
        check("成功后内容替换", audio.read_bytes(), b"EDITED")
        check("成功后保留权限", audio.stat().st_mode & 0o777, 0o600)
        check("成功后没有临时文件", sorted(p.name for p in root.iterdir()),
              sorted(["歌.flac", "封面.jpg", "歌词.lrc"]))

        # 临时文件必须带原后缀：mutagen 靠后缀识别容器
        seen: list[str] = []

        def remember(path: Path) -> None:
            seen.append(path.suffix)

        atomic_edit(audio, remember)
        check("临时文件保留原后缀", seen, [".flac"])

        # 只读源文件：临时副本必须先放开写权限才能改，改完再恢复原权限。
        # Windows 的 os.fsync 还要求句柄可写，这里一并覆盖到。
        audio.chmod(0o444)
        atomic_edit(audio, lambda p: p.write_bytes(b"READONLY-EDITED"))
        check("只读文件也能原子编辑", audio.read_bytes(), b"READONLY-EDITED")
        check("只读权限保持不变", audio.stat().st_mode & 0o777, 0o444)
        # 临时目录清理时要能删掉它（Windows 上只读文件删不掉）
        audio.chmod(0o600)

    if failures:
        print(f"原子写自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("原子写自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
