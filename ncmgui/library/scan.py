"""增量扫描：路径 + 大小 + mtime 没变就不读标签。

首次扫描几千首文件会花几秒到几十秒，放后台线程；之后每次只 stat 一遍，
没改动过的文件直接跳过。扫描过程中每 200 个文件提交一次，界面可以同时读取。

文件名仍然按 ncmctl 的 ``艺术家 - 歌名`` 规则兜底；有可用标签时优先标签。

自检::

    .venv/bin/python -m ncmgui.library.scan
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..lyrics import AUDIO_SUFFIXES
from ..localmusic import describe_quality
from .db import LibraryDB

#: 单次扫描的文件数上限。曲库再大也应当分目录管理；这个上限只防失控。
SCAN_LIMIT = 50000

#: 每多少个文件提交一次事务。太小会频繁 fsync，太大则界面长时间看不到进度。
COMMIT_EVERY = 200


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    cancelled: bool = False
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def _first_tag(tags, keys: Sequence[str]) -> str:
    """mutagen 的标签值可能是字符串、列表或数字。取第一个非空项。

    ``tags.get()`` 对缺失的键返回 ``None``，必须先跳过再 ``str()``；否则
    缺失的标签会被写成字符串 ``"None"``，曲库里所有字段都变成 None。
    """
    if tags is None:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, TypeError):
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _name_from_path(path: Path) -> tuple[str, tuple[str, ...], str]:
    """标签缺失时退回文件名。规则与 :mod:`ncmgui.localmusic` 一致。"""
    stem = path.stem.strip() or path.name
    artist_part, separator, title = stem.partition(" - ")
    if separator:
        artists = tuple(
            part.strip() for part in artist_part.split(",") if part.strip()
        )
        name = title.strip() or stem
    else:
        artists = ()
        name = stem
    return name, artists, path.parent.name


def read_tags(path: Path) -> tuple[str, tuple[str, ...], str, int]:
    """读歌名/歌手/专辑/时长；坏文件不会抛异常。"""
    name, artists, album = _name_from_path(path)
    duration = 0
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is not None:
            tags = getattr(audio, "tags", None)
            name = _first_tag(tags, ("title",)) or name
            artist = _first_tag(tags, ("artist", "albumartist"))
            if artist:
                artists = tuple(
                    part.strip() for part in artist.replace("/", ",").split(",")
                    if part.strip()
                )
            album = _first_tag(tags, ("album",)) or album
            info = getattr(audio, "info", None)
            if info is not None:
                length = float(getattr(info, "length", 0) or 0)
                duration = int(length * 1000)
    except Exception:
        # 标签损坏/格式不认识都退回文件名，不让整次扫描失败
        pass
    return name, artists, album, duration


def _iter_audio(root: Path, cancel: Callable[[], bool] | None,
                errors: list[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        if cancel is not None and cancel():
            return
        dirnames[:] = sorted(
            name for name in dirnames if not name.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            yield path


def _under(path: str, roots: Sequence[Path]) -> bool:
    try:
        target = Path(path)
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def scan(
    db: LibraryDB,
    directories: Sequence[str | os.PathLike[str]],
    *,
    limit: int = SCAN_LIMIT,
    progress: Callable[[int, int, int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ScanResult:
    """扫描若干目录并更新索引。

    ``progress(done, total, added, updated)`` 在扫描过程中周期性调用；UI 负责
    把它送回主线程。``cancel()`` 返回 True 时提前结束，已写入的条目保留。
    """
    started = time.monotonic()
    result = ScanResult()
    roots: list[Path] = []
    for raw in directories:
        root = Path(raw).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        if root.is_dir() and root not in roots:
            roots.append(root)
    if not roots:
        result.seconds = time.monotonic() - started
        return result

    existing = {
        str(row["path"]): row
        for row in db.query("SELECT * FROM tracks")
    }
    seen: set[str] = set()
    files: list[Path] = []
    for root in roots:
        for path in _iter_audio(root, cancel, result.errors):
            if cancel is not None and cancel():
                result.cancelled = True
                break
            files.append(path)
            if len(files) >= limit:
                break
        if result.cancelled or len(files) >= limit:
            break

    if cancel is not None and cancel():
        # 可能在 os.walk 还没吐出第一个文件时就取消了
        result.cancelled = True

    if result.cancelled:
        # 枚举阶段就取消了：一首都没处理，绝不能走后面的「清理失踪文件」
        result.seconds = time.monotonic() - started
        return result

    total = len(files)
    now = int(time.time())

    def apply(writes: list[tuple]) -> None:
        """短事务写一批。标签读取在锁外，界面查询不会被长扫描卡住。"""
        with db.batch() as conn:
            for item in writes:
                if item[0] == "insert":
                    _, resolved, name, artists_text, album, duration, quality, size, mtime = item
                    conn.execute(
                        "INSERT INTO tracks(id, path, name, artists, album,"
                        " duration_ms, quality, size, mtime, added_at, updated_at)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (db.next_track_id(), resolved, name, artists_text, album,
                         duration, quality, size, mtime, now, now),
                    )
                else:
                    _, track_id, name, artists_text, album, duration, quality, size, mtime = item
                    conn.execute(
                        "UPDATE tracks SET name = ?, artists = ?, album = ?,"
                        " duration_ms = ?, quality = ?, size = ?, mtime = ?,"
                        " updated_at = ? WHERE id = ?",
                        (name, artists_text, album, duration, quality,
                         size, mtime, now, track_id),
                    )

    try:
        writes: list[tuple] = []
        for done, path in enumerate(files, start=1):
            if cancel is not None and cancel():
                result.cancelled = True
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            resolved = str(path)
            seen.add(resolved)
            result.scanned += 1
            mtime = stat.st_mtime_ns / 1e9
            row = existing.get(resolved)
            # 旧版本把缺失的标签写成了字符串 "None"；这类行即使文件没变也要
            # 重读一次，否则增量扫描永远不会修复它。
            polluted = row is not None and (
                row["name"] == "None"
                or row["artists"] == "None"
                or row["album"] == "None"
            )
            unchanged = (
                row is not None
                and not polluted
                and int(row["size"]) == int(stat.st_size)
                and abs(float(row["mtime"]) - mtime) < 0.001
            )
            if unchanged:
                continue
            name, artists, album, duration = read_tags(path)
            quality = describe_quality(path)
            artists_text = ", ".join(artists)
            size = int(stat.st_size)
            if row is None:
                writes.append(("insert", resolved, name, artists_text, album,
                               duration, quality, size, mtime))
                result.added += 1
            else:
                writes.append(("update", int(row["id"]), name, artists_text,
                               album, duration, quality, size, mtime))
                result.updated += 1

            if len(writes) >= COMMIT_EVERY:
                apply(writes)
                writes.clear()
            if progress is not None and (done % 50 == 0 or done == total):
                progress(done, total, result.added, result.updated)

        if writes:
            apply(writes)
            writes.clear()

        if not result.cancelled and (cancel is None or not cancel()):
            # 只清理本次扫描目录下的失踪文件；用户的其他收藏不受影响
            stale = [
                row["id"] for key, row in existing.items()
                if key not in seen and _under(key, roots)
            ]
            with db.batch() as conn:
                for offset in range(0, len(stale), 500):
                    chunk = stale[offset:offset + 500]
                    marks = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"DELETE FROM tracks WHERE id IN ({marks})", tuple(chunk)
                    )
            result.removed = len(stale)
    finally:
        result.seconds = time.monotonic() - started
        if progress is not None:
            progress(result.scanned, total, result.added, result.updated)
    return result


def forget_directory(db: LibraryDB, directory: str | os.PathLike[str]) -> int:
    """把某个目录下的曲目从索引里删掉，返回删除条数。

    用户从「曲库目录」里移除一个文件夹时调用。用前缀比较而不是 ``LIKE``，
    目录名里的 ``%`` / ``_`` 不会被当成通配符。
    """
    root = Path(directory).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    prefix = str(root) + os.sep
    cursor = db.execute(
        "DELETE FROM tracks WHERE substr(path, 1, ?) = ?",
        (len(prefix), prefix),
    )
    return int(cursor.rowcount or 0)


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from .. import ensure_utf8_stdout
    from .db import LibraryDB

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    check("缺失标签不写成 None", _first_tag({"title": None}, ("title",)), "")
    check("列表标签取首项", _first_tag({"title": ["甲", "乙"]}, ("title",)), "甲")

    with tempfile.TemporaryDirectory() as root:
        music = Path(root) / "music"
        music.mkdir()
        first = music / "甲 - 第一首.mp3"
        second = music / "乙 - 第二首.flac"
        first.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")
        second.write_bytes(b"fLaC" + b"\x00" * 64)
        (music / ".hidden.mp3").write_bytes(b"x")
        (music / "note.txt").write_text("x", encoding="utf-8")

        db = LibraryDB(Path(root) / "library.db")
        result = scan(db, [music])
        check("首次扫描数量", result.added, 2)
        check("忽略隐藏/非音频", result.scanned, 2)
        check("索引数量", db.query_one(
            "SELECT COUNT(*) AS n FROM tracks")["n"], 2)
        row = db.query_one("SELECT * FROM tracks WHERE path = ?", (str(first),))
        check("文件名拆歌手", row["artists"], "甲")
        check("文件名拆歌名", row["name"], "第一首")
        check("本地 ID 为负", row["id"] < 0, True)

        second_result = scan(db, [music])
        check("第二次不重复添加", second_result.added, 0)
        check("第二次不重复更新", second_result.updated, 0)
        check("第二次仍在索引", db.query_one(
            "SELECT COUNT(*) AS n FROM tracks")["n"], 2)

        first.write_bytes(first.read_bytes() + b"changed")
        changed = scan(db, [music])
        check("大小变化触发更新", changed.updated, 1)

        first.unlink()
        removed = scan(db, [music])
        check("失踪文件被清理", removed.removed, 1)
        check("剩余一首", db.query_one(
            "SELECT COUNT(*) AS n FROM tracks")["n"], 1)

        nested = music / "子目录"
        nested.mkdir()
        (nested / "丙 - 第三首.mp3").write_bytes(b"x")
        scan(db, [music])
        check("新增子目录曲目", db.query_one(
            "SELECT COUNT(*) AS n FROM tracks")["n"], 2)
        check("移除目录只删该目录", forget_directory(db, nested), 1)
        check("移除后剩一首", db.query_one(
            "SELECT COUNT(*) AS n FROM tracks")["n"], 1)

        cancelled = scan(db, [music], cancel=lambda: True)
        check("取消标记", cancelled.cancelled, True)
        db.close()

    if failures:
        print(f"曲库扫描自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("曲库扫描自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
