"""本地音乐：把下载目录或用户选中的音频文件变成播放队列。

ncmctl 下载时按 ``艺术家 - 歌名.flac`` 命名（见
:func:`ncmgui.resolver.song_base`），所以文件名里就带着歌名和歌手，不需要再读
标签。读标签虽然更准，但扫一个几千首的目录会慢得多，而这里只需要「列表里显示
得像首歌」。

本地歌曲的 ``id`` 用负数：网易云的 ID 都是正数，负数不会和它们撞上，红心表格
的高亮判断也就不会误伤。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

from .lyrics import AUDIO_SUFFIXES
from .resolver import Song

#: 一次最多扫多少首。目录里混着几千个文件时，界面不该为了一个列表卡住。
SCAN_LIMIT = 5000


def song_from_path(path: Path, index: int = 0) -> Song:
    """一个音频文件 → :class:`~ncmgui.resolver.Song`。

    主干名按 ncmctl 的规则拆：``艺术家1,艺术家2 - 歌名``。没有 `` - `` 就把整个
    主干当歌名 —— 用户自己的文件不一定按这个规律命名。
    """
    path = Path(path)
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

    return Song(
        id=-(int(index) + 1),
        name=name,
        artists=artists,
        album=path.parent.name,
        duration=0,
        path=str(path),
    )


def songs_from_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Song]:
    """把用户选中的文件列表变成队列，保持选择顺序并去掉重复项。"""
    seen: set[str] = set()
    songs: list[Song] = []
    for raw in paths:
        path = Path(raw)
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        songs.append(song_from_path(path, len(songs)))
    return songs


def scan(directory: str | os.PathLike[str], limit: int = SCAN_LIMIT) -> list[Song]:
    """递归扫一个目录里的音频文件，按文件名排序。

    ``limit`` 是硬上限：超大的音乐库不在这个功能的预期里，但也不该让它把
    界面拖死。目录不存在时返回空列表。
    """
    root = Path(directory).expanduser()
    if not root.is_dir():
        return []

    found: list[Path] = []
    try:
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            found.append(path)
            if len(found) >= limit:
                break
    except OSError:
        pass

    found.sort(key=lambda item: str(item).casefold())
    return [song_from_path(path, row) for row, path in enumerate(found)]


def default_directory(cfg) -> Path:
    """本地音乐默认打开哪里。

    用户上次选过（``player.local_dir``）就用它；否则用程序下载目录
    （``download.output``）—— 这也是用户说「默认就使用程序下载目录」的意思。
    """
    saved = str(cfg.get("player.local_dir", "") or "").strip()
    if saved:
        path = Path(saved).expanduser()
        if path.is_dir():
            return path
    return Path(str(cfg.get("download.output", ""))).expanduser()


def remember_directory(cfg, path: str | os.PathLike[str]) -> None:
    """记住用户这次选的目录，下次打开文件对话框直接落在这里。"""
    directory = Path(path).expanduser()
    if directory.is_file():
        directory = directory.parent
    if directory.is_dir():
        cfg.set("player.local_dir", str(directory))


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    song = song_from_path(Path("/music/陈奕迅 - 孤勇者.flac"))
    check("拆出歌名", song.name, "孤勇者")
    check("拆出歌手", song.artists, ("陈奕迅",))
    check("负数 ID 避开网易云", song.id, -1)
    check("保留本地路径", song.path, "/music/陈奕迅 - 孤勇者.flac")
    check("专辑取目录名", song.album, "music")
    check("展示名", song.display, "陈奕迅 - 孤勇者")

    multi = song_from_path(Path("/m/A,B - 歌.mp3"), 4)
    check("多个歌手按逗号拆", multi.artists, ("A", "B"))
    check("序号进负数 ID", multi.id, -5)

    plain = song_from_path(Path("/m/只有歌名.wav"))
    check("没有分隔符时整串是歌名", plain.name, "只有歌名")
    check("没有分隔符时歌手为空", plain.artists, ())

    with tempfile.TemporaryDirectory(prefix="ncmgui-local-") as tmp:
        root = Path(tmp)
        (root / "A - 一.flac").write_bytes(b"x")
        (root / "A - 二.mp3").write_bytes(b"x")
        (root / "cover.jpg").write_bytes(b"x")
        (root / ".hidden").mkdir()
        (root / ".hidden" / "A - 三.flac").write_bytes(b"x")
        nested = root / "子目录"
        nested.mkdir()
        (nested / "B - 四.ogg").write_bytes(b"x")

        scanned = scan(root)
        check("只收音频（跳过图片）", len(scanned), 3)
        check("跳过隐藏目录", [s.name for s in scanned if s.name == "三"], [])
        check("递归子目录", sorted(s.name for s in scanned), ["一", "二", "四"])
        check("ID 连续且为负", [s.id for s in scanned], [-1, -2, -3])
        check("按路径排序", [s.name for s in scanned], ["一", "二", "四"])
        check("不存在的目录", scan(root / "没有"), [])
        check("上限生效", len(scan(root, limit=2)), 2)

        picked = songs_from_paths([root / "A - 一.flac", root / "A - 一.flac",
                                   root / "nope.flac"])
        check("去重且跳过不存在的", [s.name for s in picked], ["一"])

    if failures:
        print(f"本地音乐自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("本地音乐自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
