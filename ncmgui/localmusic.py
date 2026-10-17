"""本地音乐：把下载目录或用户选中的音频文件变成播放队列。

ncmctl 下载时按 ``艺术家 - 歌名.flac`` 命名（见
:func:`ncmgui.resolver.song_base`），所以文件名里就带着歌名和歌手，不需要再读
标签。读标签虽然更准，但扫一个几千首的目录会慢得多，而这里只需要「列表里显示
得像首歌」。

只有音质那一列必须碰文件：FLAC 直接解析开头的 STREAMINFO，其余格式交给
mutagen 读文件头（不读封面、不读完整标签）。读不出来时退回按扩展名判断，
不会因为一个坏文件让整次扫描失败。

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

#: 无损格式。``.m4a`` 既可能是 ALAC（无损）也可能是 AAC（有损），
#: 要另看 mutagen 报的 codec，所以不在这个集合里。
LOSSLESS_SUFFIXES = {
    ".flac",
    ".wav",
    ".aif",
    ".aiff",
    ".ape",
    ".wv",
    ".alac",
}


def _rate_text(rate: int) -> str:
    """44100 → ``44.1kHz``，96000 → ``96kHz``。"""
    return f"{rate / 1000:g}kHz"


def bitrate_text(bitrate: int) -> str:
    """有损音频的码率描述，前缀沿用下载页的档位名。

    ``320 kbps`` 是「极高」、``192`` 是「较高」、``128`` 是「标准」。
    VBR 文件报告的是平均码率，落在哪个区间就按哪个档位显示。
    """
    kbps = max(1, round(int(bitrate) / 1000))
    if kbps >= 320:
        tier = "极高"
    elif kbps >= 192:
        tier = "较高"
    elif kbps >= 128:
        tier = "标准"
    else:
        tier = ""
    return f"{tier} {kbps} kbps".strip()


def _flac_streaminfo(path: Path) -> tuple[int, int] | None:
    """读 FLAC 文件开头的 STREAMINFO，返回 ``(采样率, 位深)``。

    只读前几十个字节。用 mutagen 也能读，但它会把全部元数据块（包括内嵌
    封面）都加载进来 —— 扫一个有几百张封面的目录时，那些字节完全用不上。
    """
    try:
        with path.open("rb") as handle:
            if handle.read(4) != b"fLaC":
                return None
            block = handle.read(4)
            if len(block) < 4:
                return None
            # STREAMINFO 必须是第一个元数据块：首字节高 1 位是「最后一块」
            # 标记，低 7 位是块类型（0 = STREAMINFO），后 3 字节是长度
            if block[0] & 0x7F != 0:
                return None
            data = handle.read(34)
            if len(data) < 34:
                return None
            # 前 10 字节是最小/最大块大小与帧大小；后 8 字节按位打包：
            # 采样率 20 位、声道数 3 位、位深 5 位、总采样数 36 位
            packed = int.from_bytes(data[10:18], "big")
            rate = (packed >> 44) & 0xFFFFF
            bits = ((packed >> 36) & 0x1F) + 1
            if not rate or not bits:
                return None
            return rate, bits
    except OSError:
        return None


def describe_quality(path: str | os.PathLike[str]) -> str:
    """一个音频文件的音质，用来显示在本地音乐列表的「音乐质量」列。

    无损给 ``无损 44.1kHz/16bit``（超过 48kHz 或 16bit 标 ``Hi-Res``），
    有损给 ``极高 320 kbps`` 这样的码率。读不到文件头时退回扩展名，
    至少让用户知道这是什么格式；再不行就是空串。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    rate = 0
    bits = 0
    bitrate = 0
    codec = ""

    if suffix == ".flac":
        info = _flac_streaminfo(path)
        if info is not None:
            rate, bits = info

    if not (rate and bits):
        try:
            # 懒加载：没装 mutagen 时仍能扫描目录，只是音质列退化成扩展名
            from mutagen import File as MutagenFile

            audio = MutagenFile(path)
            info = getattr(audio, "info", None)
            if info is not None:
                rate = int(getattr(info, "sample_rate", 0) or 0)
                bits = int(getattr(info, "bits_per_sample", 0) or 0)
                bitrate = int(getattr(info, "bitrate", 0) or 0)
                codec = str(getattr(info, "codec", "") or "").lower()
        except Exception:
            # 文件读不了、格式不认识、mutagen 不在 —— 都不该让扫描挂掉
            pass

    lossless = suffix in LOSSLESS_SUFFIXES or (
        suffix in {".m4a", ".mp4"} and codec == "alac"
    )
    if lossless:
        if rate and bits:
            tag = "Hi-Res" if rate > 48000 or bits > 16 else "无损"
            return f"{tag} {_rate_text(rate)}/{bits}bit"
        return "无损"

    if bitrate:
        return bitrate_text(bitrate)

    # 没有可用文件头：至少把格式写出来，别让这一列空着
    return suffix.lstrip(".").upper()


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
        quality=describe_quality(path),
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

    check("320 kbps 档位", bitrate_text(320000), "极高 320 kbps")
    check("192 kbps 档位", bitrate_text(192000), "较高 192 kbps")
    check("128 kbps 档位", bitrate_text(128000), "标准 128 kbps")
    check("低码率不硬套档位", bitrate_text(96000), "96 kbps")

    def flac_header(rate: int, bits: int, channels: int = 2) -> bytes:
        """拼一个只含 STREAMINFO 的 FLAC 文件头，够 describe_quality 读。"""
        packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
        streaminfo = b"\0" * 10 + packed.to_bytes(8, "big") + b"\0" * 16
        return b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + streaminfo

    with tempfile.TemporaryDirectory(prefix="ncmgui-local-") as tmp:
        root = Path(tmp)
        (root / "A - 一.flac").write_bytes(flac_header(44100, 16))
        (root / "A - 二.mp3").write_bytes(b"x")
        (root / "cover.jpg").write_bytes(b"x")
        (root / ".hidden").mkdir()
        (root / ".hidden" / "A - 三.flac").write_bytes(b"x")
        nested = root / "子目录"
        nested.mkdir()
        (nested / "B - 四.ogg").write_bytes(b"x")
        (root / "A - 高解析.flac").write_bytes(flac_header(96000, 24))

        scanned = scan(root)
        check("只收音频（跳过图片）", len(scanned), 4)
        check("跳过隐藏目录", [s.name for s in scanned if s.name == "三"], [])
        check("递归子目录", sorted(s.name for s in scanned),
              ["一", "二", "四", "高解析"])
        check("ID 连续且为负", [s.id for s in scanned], [-1, -2, -3, -4])
        check("按路径排序", [s.name for s in scanned],
              ["一", "二", "高解析", "四"])
        check("不存在的目录", scan(root / "没有"), [])
        check("上限生效", len(scan(root, limit=2)), 2)

        by_name = {s.name: s for s in scanned}
        check("FLAC 音质", by_name["一"].quality, "无损 44.1kHz/16bit")
        check("Hi-Res 音质", by_name["高解析"].quality, "Hi-Res 96kHz/24bit")
        check("坏文件退回扩展名", by_name["二"].quality, "MP3")
        check("song_from_path 带上音质",
              song_from_path(root / "A - 一.flac").quality, "无损 44.1kHz/16bit")

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
