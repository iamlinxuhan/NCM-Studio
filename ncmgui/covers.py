"""专辑封面：下载后落成同名图片，或写进音频标签。

**为什么需要这一层：** ``ncmctl download`` 只下载音频，封面、歌手、歌名一个标签
都不写（帮助里那句 ``--tag`` 是空实现）。用户拿到的就是一个裸音频文件，本地音乐
列表里也只能看到文件名。封面地址本来就在解析结果里（``Song.pic_url``），下载完
之后顺手补一轮就行。

两种模式（值就是下载页下拉框的 ``userData``）：

``file``
    在音频旁边放一张同名图片：``艺术家 - 歌名.flac`` → ``艺术家 - 歌名.jpg``。
    **默认**，不改动音频文件本身。本地播放时优先读它。
``embed``
    把封面写进音频标签（flac 的 picture、mp3 的 APIC、m4a 的 covr……）。
    这个模式会**改写用户文件**。

另外提供 :func:`read_artwork`，本地音乐播放时按「同名图片 → 内嵌封面」的顺序找图，
不需要联网。

本模块不依赖 Qt，可以单独自检：

    .venv/bin/python -m ncmgui.covers
"""

from __future__ import annotations

import base64
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .lyrics import index_audio, targets_for

MODE_FILE = "file"
MODE_EMBED = "embed"
MODE_NONE = "none"

#: ``(显示名, 值)``，直接喂给 :meth:`~ncmgui.widgets.form.FormCard.add_combo`。
MODES: list[tuple[str, str]] = [
    ("保存为同名图片", MODE_FILE),
    ("写入音频标签（内嵌）", MODE_EMBED),
    ("不下载封面", MODE_NONE),
]

#: 认得出是同名封面的扩展名，按优先级排列。
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


class CoverError(Exception):
    """封面处理失败，消息可以直接给人看。"""


def mode_label(mode: str) -> str:
    """模式值 → 界面上的说法。"""
    for name, value in MODES:
        if value == mode:
            return name
    return mode


def image_kind(data: bytes) -> tuple[str, str] | None:
    """按文件头判断图片类型，返回 ``(扩展名, MIME)``；不是图片返回 ``None``。

    服务端有时会在失败时返回一段 HTML 或 JSON（状态码仍是 200），直接按字节
    存成 ``.jpg`` 会得到一张打不开的图。所以落盘前先认文件头。
    """
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data[:2] == b"BM":
        return ".bmp", "image/bmp"
    return None


def find_cover(audio: Path) -> Path | None:
    """音频旁边已有的同名封面（任意支持的扩展名）。"""
    for suffix in IMAGE_SUFFIXES:
        candidate = Path(audio).with_suffix(suffix)
        try:
            if candidate.is_file() and candidate.stat().st_size:
                return candidate
        except OSError:
            continue
    return None


def read_embedded(path: Path) -> bytes | None:
    """读出音频文件里内嵌的封面；没有、或格式认不出，返回 ``None``。"""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return None

    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.mp3 import MP3

            tags = MP3(path).tags
            frames = tags.getall("APIC") if tags else []
            return bytes(frames[0].data) if frames else None

        if suffix == ".flac":
            from mutagen.flac import FLAC

            pictures = FLAC(path).pictures
            return bytes(pictures[0].data) if pictures else None

        if suffix in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            covers = (MP4(path).tags or {}).get("covr")
            return bytes(covers[0]) if covers else None

        if suffix in (".ogg", ".opus"):
            from mutagen.flac import Picture

            if suffix == ".opus":
                from mutagen.oggopus import OggOpus

                audio = OggOpus(path)
            else:
                from mutagen.oggvorbis import OggVorbis

                audio = OggVorbis(path)
            value = audio.get("metadata_block_picture")
            if not value:
                return None
            return bytes(Picture(base64.b64decode(value[0])).data)
    except Exception:  # noqa: BLE001 - 读不出来就按「没有封面」处理
        return None
    return None


def read_artwork(path: Path | str) -> bytes | None:
    """本地播放用的封面：先找同名图片，再找内嵌封面。

    先看旁边的文件是有意的：用户用本程序补过封面之后，那张图比音频里可能存在的
    旧封面更接近当前曲库。
    """
    audio = Path(path)
    sibling = find_cover(audio)
    if sibling is not None:
        try:
            data = sibling.read_bytes()
        except OSError:
            data = b""
        if data:
            return data
    return read_embedded(audio)


def embed(path: Path, data: bytes) -> None:
    """把封面写进音频标签；不支持的容器明确报错。"""
    kind = image_kind(data)
    if kind is None:
        raise CoverError("拿到的不是可识别的图片")
    mime = kind[1]
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".mp3":
        from mutagen.id3 import APIC
        from mutagen.mp3 import MP3

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        # 和歌词一样，先清掉旧的，免得一个文件里叠好几张封面
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        audio.save(v2_version=3)
        return

    if suffix == ".flac":
        from mutagen.flac import FLAC, Picture

        audio = FLAC(path)
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = data
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
        return

    if suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover

        fmt = MP4Cover.FORMAT_PNG if kind[0] == ".png" else MP4Cover.FORMAT_JPEG
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        audio.save()
        return

    if suffix in (".ogg", ".opus"):
        from mutagen.flac import Picture

        if suffix == ".opus":
            from mutagen.oggopus import OggOpus

            audio = OggOpus(path)
        else:
            from mutagen.oggvorbis import OggVorbis

            audio = OggVorbis(path)
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = data
        audio["metadata_block_picture"] = [
            base64.b64encode(picture.write()).decode("ascii")
        ]
        audio.save()
        return

    raise CoverError(
        f"暂不支持把封面写进 {suffix or '这种'} 文件，"
        "只支持 flac / mp3 / m4a / ogg / opus"
    )


def satisfied(targets: Sequence[Path], mode: str) -> bool:
    """这些文件是不是已经有封面了。

    用来省掉一次网络请求：补下载时整个目录都会被重新查一遍，几百首里绝大多数
    早就补过了。
    """
    if not targets:
        return False
    if mode == MODE_FILE:
        return all(find_cover(audio) is not None for audio in targets)
    if mode == MODE_EMBED:
        return all(read_embedded(audio) is not None for audio in targets)
    return False


# --------------------------------------------------------------------------- #
# 逐首处理
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Outcome:
    """一首歌的处理结果。``status`` 取值见 :class:`Report`。"""

    status: str
    base: str
    detail: str = ""


@dataclass
class Report:
    """一整批的结果。"""

    total: int = 0
    #: 真的写下去了（新建图片 / 写进标签）
    saved: int = 0
    #: 已经有封面，跳过
    skipped: int = 0
    #: 接口没给封面地址
    empty: int = 0
    #: 输出目录里找不到对应的音频文件
    missing: int = 0
    #: 出错了（下载失败、格式不支持……）
    failed: int = 0
    problems: list[str] = field(default_factory=list)

    def record(self, outcome: Outcome) -> None:
        if outcome.status == "ok":
            self.saved += 1
        elif outcome.status == "skipped":
            self.skipped += 1
        elif outcome.status == "empty":
            self.empty += 1
        elif outcome.status == "missing":
            self.missing += 1
        else:
            self.failed += 1

        if outcome.status in ("failed", "missing", "empty") and outcome.detail:
            self.problems.append(outcome.detail)

    def notes(self, limit: int = 20) -> list[str]:
        lines = self.problems[:limit]
        if len(self.problems) > limit:
            lines.append(f"… 还有 {len(self.problems) - limit} 条，见导出日志")
        return lines

    def summary(self) -> str:
        parts: list[str] = []
        if self.saved:
            parts.append(f"保存 {self.saved} 首")
        if self.skipped:
            parts.append(f"已有封面跳过 {self.skipped} 首")
        if self.empty:
            parts.append(f"没有封面地址 {self.empty} 首")
        if self.missing:
            parts.append(f"找不到文件 {self.missing} 首")
        if self.failed:
            parts.append(f"失败 {self.failed} 首")
        return " · ".join(parts) if parts else "没有需要处理的封面"


def apply(base: str, targets: Sequence[Path], data: bytes, mode: str) -> Outcome:
    """把一张封面写到这首歌对应的全部音频文件上。"""
    if not targets:
        return Outcome("missing", base, f"{base}：输出目录里没找到对应的音频文件")
    if mode == MODE_NONE:
        return Outcome("skipped", base)
    kind = image_kind(data)
    if kind is None:
        return Outcome("failed", base, f"{base}：接口返回的不是图片")

    written = 0
    for audio in targets:
        try:
            if mode == MODE_FILE:
                if find_cover(audio) is not None:
                    continue
                dest = Path(audio).with_suffix(kind[0])
                dest.write_bytes(data)
            else:
                if read_embedded(audio) is not None:
                    continue
                embed(audio, data)
            written += 1
        except CoverError as exc:
            return Outcome("failed", base, f"{base}：{exc}")
        except OSError as exc:
            return Outcome("failed", base, f"{base}：写 {Path(audio).name} 的封面失败 —— {exc}")
        except Exception as exc:  # noqa: BLE001 - 单个文件坏了不该拖垮整批
            return Outcome("failed", base, f"{base}：写 {Path(audio).name} 的封面失败 —— {exc}")

    return Outcome("ok" if written else "skipped", base)


def collect(
    pairs: Iterable[tuple[str, str]],
    fetch: Callable[[str], bytes],
    directory: str | os.PathLike[str],
    mode: str,
    parallel: int = 4,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> Report:
    """给一批歌抓封面并落盘。

    :param pairs: ``(文件名主干, 封面地址)``。主干来自
        :attr:`~ncmgui.resolver.Song.base`，就是 ncmctl 实际用的那个文件名。
    :param fetch: 接收 URL、返回图片字节的可调用对象。注入进来是为了让本模块
        不依赖 Qt 和具体的 HTTP 实现，自检时可以塞个假的。
    :param on_progress: ``(已完成, 总数, 当前主干)``
    :param on_note: 值得单独打一行的说明（失败、找不到文件……）

    回调是在**工作线程**里调用的，界面那边务必只做「发信号」这种线程安全的动作。
    """
    items = list(pairs)
    report = Report(total=len(items))
    if mode not in (MODE_FILE, MODE_EMBED) or not items:
        return report

    index = index_audio(directory)
    lock = threading.Lock()
    done = 0

    def work(pair: tuple[str, str]) -> None:
        nonlocal done
        base, url = pair
        targets = targets_for(base, index)

        if not targets:
            outcome = Outcome("missing", base, f"{base}：输出目录里没找到对应的音频文件")
        elif satisfied(targets, mode):
            outcome = Outcome("skipped", base)
        elif not (url or "").strip():
            outcome = Outcome("empty", base, f"{base}：接口没有给出封面地址")
        else:
            try:
                data = fetch(url)
            except Exception as exc:  # noqa: BLE001 - 单首失败不该中断整批
                outcome = Outcome(
                    "failed", base, f"{base}：{str(exc).strip() or exc.__class__.__name__}"
                )
            else:
                outcome = apply(base, targets, data, mode)

        with lock:
            done += 1
            position = done
            report.record(outcome)

        if on_progress is not None:
            on_progress(position, len(items), base)
        if on_note is not None and outcome.detail:
            on_note(outcome.detail)

    workers = max(1, min(int(parallel), len(items)))
    if workers == 1:
        for item in items:
            work(item)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ncmgui-cover") as pool:
            for _ in pool.map(work, items):
                pass
    return report


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

#: 1×1 的 PNG，够 mutagen 和文件头识别用了
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _selftest() -> int:
    import tempfile

    from . import ensure_utf8_stdout
    from .lyrics import blank_flac, blank_mp3

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    check("识别 PNG", image_kind(_PNG), (".png", "image/png"))
    check("识别 JPEG", image_kind(b"\xff\xd8\xff\xe0" + b"x" * 8), (".jpg", "image/jpeg"))
    check("识别 WEBP", image_kind(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x"), (".webp", "image/webp"))
    check("HTML 不算图片", image_kind(b"<!DOCTYPE html>"), None)
    check("空内容不算图片", image_kind(b""), None)
    check("模式名", mode_label(MODE_FILE), "保存为同名图片")

    with tempfile.TemporaryDirectory(prefix="ncmgui-cover-") as tmp:
        root = Path(tmp)
        audio = root / "陈奕迅 - 孤勇者.flac"
        audio.write_bytes(blank_flac())

        check("没有封面时 satisfied 为假", satisfied([audio], MODE_FILE), False)

        # 同名图片
        outcome = apply("陈奕迅 - 孤勇者", [audio], _PNG, MODE_FILE)
        check("保存同名图片", outcome.status, "ok")
        check("图片扩展名跟着内容走", (root / "陈奕迅 - 孤勇者.png").is_file(), True)
        check("保存后 satisfied 为真", satisfied([audio], MODE_FILE), True)
        check("read_artwork 读到同名图片", read_artwork(audio), _PNG)
        check(
            "再跑一次不会覆盖",
            apply("陈奕迅 - 孤勇者", [audio], _PNG, MODE_FILE).status,
            "skipped",
        )

        # 内嵌封面
        embedded = root / "A - B.flac"
        embedded.write_bytes(blank_flac())
        check("内嵌前没有封面", read_embedded(embedded), None)
        check("写进 flac", apply("A - B", [embedded], _PNG, MODE_EMBED).status, "ok")
        check("flac 封面能读回", read_embedded(embedded), _PNG)
        check("内嵌后 satisfied 为真", satisfied([embedded], MODE_EMBED), True)
        check("read_artwork 读到内嵌封面", read_artwork(embedded), _PNG)

        mp3 = root / "A - C.mp3"
        mp3.write_bytes(blank_mp3())
        check("写进 mp3", apply("A - C", [mp3], _PNG, MODE_EMBED).status, "ok")
        check("mp3 封面能读回", read_embedded(mp3), _PNG)

        check(
            "找不到音频时明确报 missing",
            apply("不存在", [], _PNG, MODE_FILE).status,
            "missing",
        )
        check(
            "坏数据明确报 failed",
            apply("陈奕迅 - 孤勇者", [audio], b"not an image", MODE_FILE).status,
            "failed",
        )

        # collect：第二遍因为已有封面，不应再发请求。
        # 换一个干净的主干，才验得到「第一次真的去下载了」
        fresh = root / "D - E.flac"
        fresh.write_bytes(blank_flac())
        calls: list[str] = []

        def fake_fetch(url: str) -> bytes:
            calls.append(url)
            return _PNG

        first = collect([("D - E", "https://example.com/a.jpg")], fake_fetch, root, MODE_FILE)
        check("collect 保存一首", (first.saved, first.skipped, first.failed), (1, 0, 0))
        check("collect 发了一次请求", len(calls), 1)
        second = collect([("D - E", "https://example.com/a.jpg")], fake_fetch, root, MODE_FILE)
        check("第二遍跳过", (second.saved, second.skipped), (0, 1))
        check("已有封面不再请求", len(calls), 1)

        missing = collect([("没这首歌", "https://example.com/a.jpg")], fake_fetch, root, MODE_FILE)
        check("collect 找不到文件", missing.missing, 1)
        blank = root / "F - G.flac"
        blank.write_bytes(blank_flac())
        empty = collect([("F - G", "")], fake_fetch, root, MODE_EMBED)
        check("collect 没有封面地址", empty.empty, 1)

    if failures:
        print(f"封面自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("封面自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
