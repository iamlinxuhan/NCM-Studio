"""歌词：从接口取回、合并翻译，再落成 ``.lrc`` 或写进音频标签。

**为什么需要这一层：** ncmctl 只下音频，不碰歌词 —— 它的 ``--tag`` 帮助文本写着
"reserved for compatibility; download tag writing is not implemented"，实测下完的
flac 里 ``format_tags`` 是空的，连 artist/title 都没有，歌词自然更没有。用户拿到的
就是一个裸音频文件，歌名歌手全靠文件名。

歌词本身拿得到，而且一次能拿三份。``ncmctl curl Lyric`` 的响应里：

* ``lrc.lyric`` —— 原文，LRC 文本，时间戳带**毫秒**（``[00:44.870]``）
* ``tlyric.lyric`` —— 中文翻译，**自带时间轴**，混着 ``[by:...]`` 这类无时间戳的元数据行
* ``romalrc.lyric`` —— 罗马音，多数歌是空的

**写出去的时间戳一律转成两位小数**（``[mm:ss.xx]``）。LRC 的小数位本该是百分秒，
毫秒是 NetEase 的扩展写法；Elisa 25.04 的解析器把三位当百分秒再 ×10，一句歌词能晚
出 9 秒，而 Debian trixie 装的正是这个版本（详见 :func:`canonical_timestamps`）。

三种模式（值就是界面下拉框的 ``userData``）：

``none``
    不下载歌词。
``lrc``
    在音频文件旁边写一个同名的 ``.lrc``。**默认**，不动音频文件本身。
``embed``
    把歌词写进音频文件的标签（flac 的 Vorbis 注释 / mp3 的 ID3 USLT …）。
    这是唯一会**改写用户文件**的模式。

**"没有歌词"有好几种形态。** 接口在查无歌词时返回 ``[00:00.00]暂无歌词``，在纯音乐上
返回 ``[00:05.00]纯音乐，请欣赏`` —— 两者都是**只有一行占位文字**。照着写出去，用户
的曲库里就会多出一堆内容为"暂无歌词"的 .lrc。所以判定用的是"去掉标签后还剩多少字"，
而不是比对某几个固定字符串（见 :func:`has_content`）。

本模块不依赖 Qt，可以单独自检：

    .venv/bin/python -m ncmgui.lyrics
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .resolver import VARIANT_RE

# --------------------------------------------------------------------------- #
# 模式
# --------------------------------------------------------------------------- #

MODE_NONE = "none"
MODE_LRC = "lrc"
MODE_EMBED = "embed"

#: ``(显示名, 值)``，直接喂给 :meth:`~ncmgui.widgets.form.FormCard.add_combo`。
#: 默认那一项由调用方通过 ``download.lyric`` 配置项决定，见 config.py。
MODES: list[tuple[str, str]] = [
    ("独立 .lrc 文件", MODE_LRC),
    ("写入音频标签（内嵌）", MODE_EMBED),
    ("不下载歌词", MODE_NONE),
]

#: 认得出是音频的扩展名。只影响"能不能被找到"，写入支持与否另有一套（见 ``_FIELD``）。
AUDIO_SUFFIXES = frozenset(
    {".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus", ".wav", ".ape", ".wv", ".aac", ".wma"}
)

#: 歌词默认写进哪个字段：Vorbis 注释（flac/ogg/opus）与 MP4 atom 都是"容器 + 字段名"的结构
_FIELD = {
    ".flac": "lyrics",
    ".ogg": "lyrics",
    ".opus": "lyrics",
    ".m4a": "\xa9lyr",
    ".mp4": "\xa9lyr",
}

# --------------------------------------------------------------------------- #
# LRC 文本处理
# --------------------------------------------------------------------------- #

#: ``[mm:ss]`` / ``[mm:ss.xx]`` / ``[mm:ss.xxx]``；毫秒位也见过用冒号的写法，一并认下。
#:
#: 小数位**不设上限**（``\d+``）。位数只是精度，不是另一种格式：``.5`` 与 ``.500``
#: 都是半秒。写死成 ``\d{1,3}`` 的话，接口哪天多给一位，这些行就会**认不出时间戳**
#: —— 不报错、不中断，只是译文再也对不上、原样写出去，正是最难发现的那种坏法。
#:
#: 末尾那个 ``(-\d+)?`` 是实战里撞见的：接口给过 ``[00:00.00-1]``（本机曲库
#: ``Meditacion Profunda - Aumenta La Productividad.flac`` 里就是），后面挂了个
#: 看不懂的编号。认下它、**原样留着** —— 对齐只看前面的时间，而看不懂的东西不该
#: 顺手删掉（万一哪家播放器认它呢）。
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d+))?(-\d+)?\]")

#: 长得像时间戳的标签：``[`` 后面跟着数字和分隔符。用来揪出"接口换了写法"——
#: 见 :func:`unknown_timestamps`。只认这种形状，``[123]`` 这类方括号正文不会误报。
_SUSPECT_RE = re.compile(r"\[\s*\d{1,4}\s*[:.]")

#: 行首一个方括号标签（不含嵌套），用来逐段剥掉 ``[00:01.00][00:05.00]词`` 这类行首连排
_LEADING_TAG_RE = re.compile(r"\[[^\[\]]*\]")

#: ``[by:xxx]`` ``[ti:xxx]`` ``[offset:0]`` 这类 LRC 元数据标签
_META_RE = re.compile(r"\[[a-zA-Z#]+:[^\]]*\]")

#: 占位串里会出现的词。做成"关键词 + 长度上限"而不是整句比对 ——
#: 上游换个说法（"暂不提供歌词"之类）时，只比对整句会直接漏过去。
_PLACEHOLDER_HINTS = ("暂无歌词", "暂无", "纯音乐", "instrumental", "no lyric", "no lyrics")

#: 去掉时间戳与元数据标签后，正文短于这个长度才允许按占位串判定。
#: 一整首歌的歌词不可能只有这么几个字。
_PLACEHOLDER_MAX_CHARS = 20


def mode_label(mode: str) -> str:
    """模式值 → 界面上的说法，用来在控制台里交代这次在做什么。"""
    for name, value in MODES:
        if value == mode:
            return name
    return mode


class LyricsError(RuntimeError):
    """歌词处理的失败。消息直接面向用户，不夹带英文堆栈。"""


def has_content(text: str) -> bool:
    """这段 LRC 里到底有没有歌词。

    判据是"剥掉时间戳和元数据标签之后还剩多少字"，而不是比对固定字符串 ——
    没有歌词时接口返回的是一行占位文字（``暂无歌词`` / ``纯音乐，请欣赏``），
    这种东西写进 .lrc 只会让用户的曲库里多出一堆垃圾。
    """
    body = _META_RE.sub("", _TIMESTAMP_RE.sub("", text or ""))
    body = "".join(body.split())
    if not body:
        return False
    if len(body) > _PLACEHOLDER_MAX_CHARS:
        return True
    lowered = body.lower()
    return not any(hint in lowered for hint in _PLACEHOLDER_HINTS)


def _centiseconds(match: re.Match[str]) -> int:
    """把一条时间戳归一化成厘秒。

    精度不统一是常态：原文常见 ``[00:00.000]``，翻译常见 ``[00:32.500]``，
    也有 ``[00:01.0]``。不归一化就永远对不上，翻译会整段丢失。

    小数位是**秒的小数部分**：位数只表示精度，不改变含义，所以按位数换算而不是
    逐个位数分支 —— ``.5`` = ``.50`` = ``.500`` = 半秒。这么写还有一个好处：
    接口哪天给到四位、五位，这里照样算得对，不必回来加分支。

    一律**截断**取整：毫秒截到厘秒最多偏早 9 毫秒，比任何播放器的刷新粒度都小，
    而且是"早"这个安全方向（宁可早一点显示，不要晚一点）。
    """
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    frac = match.group(3) or ""
    # 位数只表示精度：int(frac) / 10**len(frac) 就是秒的小数部分，乘 100 得厘秒
    hundredths = int(frac) * 100 // 10 ** len(frac) if frac else 0
    return minutes * 6000 + seconds * 100 + hundredths


def _format_timestamp(match: re.Match[str]) -> str:
    """一条时间戳 → ``[mm:ss.xx]``。

    绕一圈走归一化后的厘秒再回写，这样写出去的写法与对齐用的键永远一致。
    """
    total = _centiseconds(match)
    # 时间戳后面跟着的编号（``[00:00.00-1]`` 里的 ``-1``）不是我们写得出来的东西，
    # 看不懂就原样带回去，不做删改
    suffix = match.group(4) or ""
    return f"[{total // 6000:02d}:{total // 100 % 60:02d}.{total % 100:02d}{suffix}]"


def canonical_timestamps(text: str) -> str:
    """把文本里所有时间戳统一写成两位小数（``[mm:ss.xx]``）。

    LRC 的小数位本来指百分秒，而接口给的是毫秒（``[00:44.870]``）。多数播放器
    认三位，但 **Elisa 25.04 的解析器把三位小数当成百分秒累加再 ×10** ——
    ``[00:44.870]`` 被算成 52.7 秒而不是 44.87 秒，整整晚 7.8 秒；误差随小数部分
    在 0～9 秒之间乱跳（本机曲库实测：8366 个时间戳里 4693 个踩中，中位晚 4.6 秒）。
    表现出来就是"这一句都唱完了，歌词还没翻过去"。

    上游 2025-05-26 的 ``c0246707``（"support milliseconds of any length"）才修好，
    而 Debian trixie 装的 Elisa 25.04.2 没有这个补丁 —— 不该指望用户为看歌词去升级
    播放器。

    两位小数是 LRC 的标准写法，新老播放器都算得对。代价是毫秒被截到厘秒：最多偏早
    9 毫秒，比任何播放器的刷新粒度都小，而且是"早"这个安全方向。
    """
    return _TIMESTAMP_RE.sub(_format_timestamp, text)


def outdated(text: str) -> bool:
    """这份已有的歌词是不是旧写法，值得重写一遍。

    光看"有没有歌词"会把旧写法的文件永远放过去：1.1.0 之前写出去的歌词带着毫秒
    时间戳，正是 Elisa 25.04 会算错九秒的那种。拿"写出去应该长什么样"当判据，
    重跑同一批就能**就地补上**，不用把文件删了重下。

    :func:`canonical_timestamps` 是幂等的，所以这个判断稳定：改过的文件不会一直
    被判成过时。
    """
    return bool(text) and canonical_timestamps(text) != text


def unknown_timestamps(text: str) -> list[str]:
    """挑出"长得像时间戳、但认不出来"的标签。

    这是防接口改写法的那道哨兵。解析已经放宽到任意位数的小数，但玩法不止一种：
    万一哪天变成 ``[00:44,870]``（逗号）、``[44.870]``（没有分钟）或别的花样，
    那一行就会被当成**没有时间戳的正文** —— 译文对不上、时间戳不被归一化，
    结果是写出一份时间轴错乱的歌词，而全程**一句错都不报**。这是最难查的坏法，
    所以宁可吵一点：认不出来就明说。

    只看行首连续的那串方括号 —— 正文里出现 ``[...]``（``词[1]``）是歌词内容，
    不是标签。元数据标签（``[by:...]`` / ``[offset:...]``）本来就没有时间戳，也不算。

    认得出的时间戳为空列表；返回的都是原样的标签文本，直接给用户看。
    """
    found: list[str] = []
    for line in (text or "").splitlines():
        rest = line.lstrip()
        while rest.startswith("["):
            match = _LEADING_TAG_RE.match(rest)
            if match is None:
                break
            tag = match.group(0)
            if (
                not _TIMESTAMP_RE.fullmatch(tag)
                and not _META_RE.fullmatch(tag)
                and _SUSPECT_RE.match(tag)
            ):
                found.append(tag)
            rest = rest[match.end():].lstrip()
    return found


def merge_translation(original: str, translation: str) -> str:
    """把翻译逐行插到原文对应行的下面。

    对齐靠**归一化后的时间戳**。翻译里 ``[by:MMMMad-man]`` 这种没有时间戳的
    元数据行直接丢掉 —— 它们插不进原文的任意一行，留着只会在歌词中间冒出一句
    "by:MMMMad-man"。

    原文一行都没时间戳（纯文本歌词）时原样返回：没有坐标就谈不上对齐。
    """
    if not (translation or "").strip() or not has_content(translation):
        return original or ""

    pairs = _timestamped(translation)
    if not pairs:
        return original or ""

    lines = (original or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not any(_TIMESTAMP_RE.search(line) for line in lines):
        return original or ""

    used: set[int] = set()
    out: list[str] = []
    for line in lines:
        out.append(line)
        match = _TIMESTAMP_RE.search(line)
        if match is None:
            continue
        key = _centiseconds(match)
        # 同一时间戳有多条原文行时（重复副歌）只插一次，否则译文会重复好几遍
        if key in used:
            continue
        translated = pairs.get(key)
        if translated:
            used.add(key)
            out.append(f"{match.group(0)}{translated}")
    return "\n".join(out)


def blank_flac() -> bytes:
    """最小的合法 flac（只有 STREAMINFO，没有音频帧）。

    给自检用的素材：mutagen 从零造不出 FLAC —— ``FLAC()`` 没 load 过就没有
    ``metadata_blocks``，``.info`` 直接 AttributeError。要验"写进标签再读回来"
    就只能自己拼一个。

    露在外面是因为打包后的 ``--self-test`` 也要用它：mutagen 是懒加载的，
    打包少收一个子模块在模块清单上看不出来，只能真写一次才知道。
    """
    # STREAMINFO 尾部是 20 位采样率 + 3 位声道数 + 5 位位深 + 36 位总采样数，压成 8 字节
    packed = (44100 << 44) | (1 << 41) | (15 << 36)
    streaminfo = (
        (4096).to_bytes(2, "big")     # 最小块大小
        + (4096).to_bytes(2, "big")   # 最大块大小
        + b"\x00" * 6                 # 最小 / 最大帧大小
        + packed.to_bytes(8, "big")
        + b"\x00" * 16                # MD5，全 0 表示未计算
    )
    # 首字节 0x80：最高位 1 = 最后一个元数据块，低 7 位 0 = STREAMINFO
    return b"fLaC" + b"\x80" + len(streaminfo).to_bytes(3, "big") + streaminfo


#: 一个合法的 MPEG-1 Layer III 帧（128 kbps / 44.1 kHz / 立体声，417 字节）：
#: 4 字节帧头 + 413 字节静音数据。
_SILENT_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def blank_mp3() -> bytes:
    """最小的合法 mp3。

    两帧是下限：mutagen 的 MPEGInfo 要连着认出足够多的帧才肯认这是 MPEG 流，
    只有一帧会直接 ``HeaderNotFoundError: can't sync to MPEG frame``。
    """
    return _SILENT_MP3_FRAME * 2


def _timestamped(text: str) -> dict[int, str]:
    """LRC → ``{厘秒: 正文}``。没有时间戳的行（元数据）与空正文都丢掉。"""
    found: dict[int, str] = {}
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _TIMESTAMP_RE.search(raw)
        if match is None:
            continue
        body = _TIMESTAMP_RE.sub("", raw).strip()
        if body:
            found.setdefault(_centiseconds(match), body)
    return found


def normalize(text: str) -> str:
    """统一换行、去掉行尾空白与首尾空行，并以一个换行结尾。"""
    lines = [
        line.rstrip()
        for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


# --------------------------------------------------------------------------- #
# 接口响应
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Lyric:
    """一次 ``Lyric`` 接口调用拿到的歌词。"""

    original: str = ""
    translation: str = ""

    @property
    def is_empty(self) -> bool:
        return not has_content(self.original) and not has_content(self.translation)

    def text(self) -> str:
        """最终要写出去的文本；这首歌没有歌词时返回空串。

        这里是**唯一的出口**：``.lrc`` 与写标签两条路径都从这里拿文本，所以时间戳
        的写法也统一在这一步落实（见 :func:`canonical_timestamps`）。
        """
        if has_content(self.original):
            body = merge_translation(self.original, self.translation)
        elif has_content(self.translation):
            # 原文是占位串但翻译有内容：极少见，但宁可写翻译也别什么都不写
            body = self.translation
        else:
            return ""
        return canonical_timestamps(normalize(body))


def parse(payload: object) -> Lyric:
    """把 ``ncmctl curl Lyric`` 的响应整理成 :class:`Lyric`。

    两个字段都是 ``{"version": .., "lyric": "..."}`` 的形状，但也容忍直接给字符串
    （接口换写法时不至于整个功能哑掉）。
    """
    return Lyric(
        original=_lyric_field(payload, "lrc"),
        translation=_lyric_field(payload, "tlyric"),
    )


def _lyric_field(payload: object, key: str) -> str:
    node = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(node, dict):
        return str(node.get("lyric") or "")
    if isinstance(node, str):
        return node
    return ""


# --------------------------------------------------------------------------- #
# 找到歌词该写到哪个文件上
# --------------------------------------------------------------------------- #

def lrc_path(audio: Path) -> Path:
    """音频文件对应的 ``.lrc`` 路径：同名换后缀，放在它旁边。"""
    return audio.with_suffix(".lrc")


def _read_text(path: Path) -> str:
    """读一个小文本文件；不存在、读不动、内容是空的，一律当空串。"""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def index_audio(directory: str | os.PathLike[str]) -> dict[str, list[Path]]:
    """扫输出目录，建「文件名主干 → 实际音频文件」的索引。

    **不能拿主干名去拼扩展名。** ncmctl 落成 ``.flac`` 还是 ``.mp3`` 取决于实际
    拿到的音质，同一首歌重名时还会变成 ``X(1).flac`` —— 三种都要能对上，所以是
    扫目录反查，而不是猜。

    带 ``(N)`` 变体的文件会同时登记在原样主干和去掉 ``(N)`` 的主干下，与
    :func:`~ncmgui.resolver.existing_bases` 的取舍保持一致。
    """
    index: dict[str, list[Path]] = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return index

    root = Path(directory)
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        path = Path(entry.name)
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        stem = path.stem.strip()
        if not stem:
            continue

        full = root / entry.name
        _remember(index, stem, full)
        stripped = VARIANT_RE.sub("", stem).strip()
        if stripped and stripped != stem:
            _remember(index, stripped, full)
    return index


def _remember(index: dict[str, list[Path]], key: str, path: Path) -> None:
    bucket = index.setdefault(key, [])
    if path not in bucket:
        bucket.append(path)


def targets_for(base: str, index: dict[str, list[Path]]) -> list[Path]:
    """某个文件名主干对应的全部音频文件。"""
    return list(index.get(base, ()))


# --------------------------------------------------------------------------- #
# 写进音频标签（可选功能，缺 mutagen 时给一句能照做的话）
# --------------------------------------------------------------------------- #

def ensure_embed_support() -> None:
    """``embed`` 模式的前置检查。"""
    try:
        import mutagen  # noqa: F401
    except ImportError as exc:
        raise LyricsError(
            "「写入音频标签」需要 mutagen 这个库。"
            "装一下就好：pip install mutagen；"
            "或者把「歌词」改成「独立 .lrc 文件」—— 那个不需要额外依赖。"
        ) from exc


def read_embedded(path: Path) -> str:
    """读出音频文件里已有的歌词；没有、或格式认不出，都返回空串。"""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        return ""

    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.mp3 import MP3

            frames = (MP3(path).tags or {}).getall("USLT")
            return str(frames[0].text or "") if frames else ""
        field = _FIELD.get(suffix)
        if field is None:
            return ""
        value = _open(path, suffix).get(field)
    except Exception:  # noqa: BLE001 - 读不出来就按「没有歌词」处理，不值得中断整批
        return ""

    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def embed(path: Path, text: str) -> None:
    """把歌词写进音频标签。

    只支持 :data:`_FIELD` 里列出的容器加 mp3；其余格式明确报错，而不是"写了个
    看似成功但读不回来的标签"。
    """
    ensure_embed_support()
    suffix = path.suffix.lower()

    if suffix == ".mp3":
        from mutagen.id3 import USLT
        from mutagen.mp3 import MP3

        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        # 先清掉旧的：USLT 可以有多条，直接 add 会留下两份歌词，播放器显示哪份看运气
        audio.tags.delall("USLT")
        audio.tags.add(USLT(encoding=3, lang="XXX", desc="", text=text))
        audio.save(v2_version=3)
        return

    field = _FIELD.get(suffix)
    if field is None:
        raise LyricsError(
            f"暂不支持把歌词写进 {suffix or '这种'} 文件，"
            "只支持 flac / mp3 / m4a / ogg / opus"
        )
    audio = _open(path, suffix)
    audio[field] = [text]
    audio.save()


def _open(path: Path, suffix: str):
    """按扩展名打开音频文件（返回 mutagen 对象）。"""
    if suffix == ".flac":
        from mutagen.flac import FLAC

        return FLAC(path)
    if suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4

        return MP4(path)
    if suffix == ".opus":
        from mutagen.oggopus import OggOpus

        return OggOpus(path)
    from mutagen.oggvorbis import OggVorbis

    return OggVorbis(path)


# --------------------------------------------------------------------------- #
# 逐首处理
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Outcome:
    """一首歌的处理结果。``status`` 取值见 :class:`Report`。"""

    status: str
    base: str
    detail: str = ""
    #: 歌词里"像时间戳但认不出来"的标签，见 :func:`unknown_timestamps`。
    #: 非空等于说接口换了写法，得让用户看见 —— 见 :meth:`Report.record`
    odd: tuple[str, ...] = ()


@dataclass
class Report:
    """一整批的结果。"""

    total: int = 0
    #: 真的写下去了（新建 .lrc / 写进标签）
    written: int = 0
    #: 已经有歌词，跳过
    skipped: int = 0
    #: 接口说这首歌没有歌词（查无歌词或纯音乐）
    empty: int = 0
    #: 输出目录里找不到对应的音频文件
    missing: int = 0
    #: 出错了（接口失败、写文件失败、格式不支持……）
    failed: int = 0
    #: 歌词里有认不出来的时间戳写法的歌数（接口换了格式的征兆）
    odd: int = 0
    problems: list[str] = field(default_factory=list)

    def record(self, outcome: Outcome) -> None:
        if outcome.status == "ok":
            self.written += 1
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

        # 格式变了就是全库一起变，逐首列出来只会把真正的失败挤出 notes() 的限额，
        # 所以只留第一条（带上实际认不出的那个标签），其余靠 summary() 里的计数交代
        if outcome.odd:
            self.odd += 1
            if self.odd == 1:
                self.problems.append(odd_note(outcome))

    def notes(self, limit: int = 20) -> list[str]:
        """值得逐条打出来的行（跳过不算，几百首歌会刷屏）。"""
        lines = self.problems[:limit]
        if len(self.problems) > limit:
            lines.append(f"… 还有 {len(self.problems) - limit} 条，见导出日志")
        return lines

    def summary(self) -> str:
        parts: list[str] = []
        if self.written:
            parts.append(f"写入 {self.written} 首")
        if self.skipped:
            parts.append(f"已有歌词跳过 {self.skipped} 首")
        if self.empty:
            parts.append(f"没有歌词 {self.empty} 首")
        if self.missing:
            parts.append(f"找不到文件 {self.missing} 首")
        if self.failed:
            parts.append(f"失败 {self.failed} 首")
        if self.odd:
            parts.append(f"{self.odd} 首有认不出的时间戳")
        return " · ".join(parts) if parts else "没有需要处理的歌词"


def odd_note(outcome: Outcome) -> str:
    """接口换了时间戳写法时，那句给人看的话。

    措辞刻意说得具体（把认不出的那个标签原样摆出来）：看到 ``[00:44,870]`` 比看到
    "格式异常"有用得多，用户能一眼判断是不是真出了问题，也不至于把正常文件当坏了。
    """
    return (
        f"{outcome.base}：认不出的时间戳 {outcome.odd[0]}"
        " —— 接口可能改了歌词格式，这一句的时间轴对不上"
    )


def apply(base: str, targets: Sequence[Path], lyric: Lyric, mode: str) -> Outcome:
    """把一首歌的歌词写到它对应的文件上。"""
    if not targets:
        return Outcome("missing", base, f"{base}：输出目录里没找到对应的音频文件")

    text = lyric.text() if isinstance(lyric, Lyric) else str(lyric or "")
    if not text.strip():
        return Outcome("empty", base, f"{base}：接口没有给出歌词（查无歌词或纯音乐）")

    # 这里是**唯一**能发现"接口换了时间戳写法"的地方。解析已经放宽到任意位数的小数，
    # 但花样是无穷的：逗号分隔、没有分钟位……认不出来的那些会以"正文"的身份原样混进
    # 输出，译文对不上、时间轴错乱，而全程不报错。所以要主动查一遍，吵一点也比静默坏掉强
    odd = tuple(unknown_timestamps(text))

    if mode == MODE_LRC:
        outcome = _write_lrc(base, targets, text)
    elif mode == MODE_EMBED:
        outcome = _write_embed(base, targets, text)
    else:
        outcome = Outcome("skipped", base)

    return replace(outcome, odd=odd)


def _write_lrc(base: str, targets: Sequence[Path], text: str) -> Outcome:
    written = 0
    for audio in targets:
        dest = lrc_path(audio)
        try:
            existing = _read_text(dest)
            if existing and not outdated(existing):
                continue
            dest.write_text(text, encoding="utf-8")
            written += 1
        except OSError as exc:
            return Outcome("failed", base, f"{base}：写 {dest.name} 失败 —— {exc}")
    if not written:
        return Outcome("skipped", base)
    return Outcome("ok", base)


def _write_embed(base: str, targets: Sequence[Path], text: str) -> Outcome:
    written = 0
    for audio in targets:
        try:
            existing = read_embedded(audio)
            if existing and not outdated(existing):
                continue
            embed(audio, text)
            written += 1
        except LyricsError as exc:
            return Outcome("failed", base, f"{base}：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单个文件写坏了不该拖垮整批
            return Outcome("failed", base, f"{base}：写 {audio.name} 失败 —— {exc}")
    if not written:
        return Outcome("skipped", base)
    return Outcome("ok", base)


def satisfied(targets: Sequence[Path], mode: str) -> bool:
    """这些文件是不是已经有歌词了，而且写法就是现在这份。

    用来**省掉一次接口调用** —— 重复跑同一批（最常见的就是补下载之后再来一遍）
    时，几百首的曲库不该为了"每首都问一次然后全部跳过"而白跑几百个请求。

    "有歌词"要连写法一起看（见 :func:`outdated`）：只认"有没有"的话，旧写法写下去
    的文件会因为自己存在而被永久跳过，改正过的写法再也没机会落上去。
    """
    if not targets:
        return False
    if mode == MODE_LRC:
        for audio in targets:
            text = _read_text(lrc_path(audio))
            if not text or outdated(text):
                return False
        return True
    if mode == MODE_EMBED:
        for audio in targets:
            text = read_embedded(audio)
            if not text or outdated(text):
                return False
        return True
    return False


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #

def collect(
    pairs: Iterable[tuple[str, int]],
    fetch: Callable[[int], object],
    directory: str | os.PathLike[str],
    mode: str,
    parallel: int = 4,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> Report:
    """给一批歌抓歌词并落盘。

    :param pairs: ``(文件名主干, 歌曲 ID)``。主干来自
        :attr:`~ncmgui.resolver.Song.base`，就是 ncmctl 实际用的那个文件名。
    :param fetch: 接收歌曲 ID、返回接口响应（喂给 :func:`parse`）的可调用对象。
        注入进来是为了让本模块不依赖 :class:`~ncmgui.resolver.Resolver`，
        自检时可以塞个假的。
    :param on_progress: ``(已完成, 总数, 当前主干)``，用来刷一行计数
    :param on_note: 值得单独打一行的说明（失败、找不到文件……）

    回调是在**工作线程**里调用的，界面那边务必只做「发信号」这种线程安全的动作。
    """
    items = list(pairs)
    report = Report(total=len(items))
    if mode not in (MODE_LRC, MODE_EMBED) or not items:
        return report

    if mode == MODE_EMBED:
        ensure_embed_support()

    index = index_audio(directory)
    lock = threading.Lock()
    done = 0

    def work(pair: tuple[str, int]) -> None:
        nonlocal done
        base, song_id = pair
        targets = targets_for(base, index)

        if satisfied(targets, mode):
            outcome = Outcome("skipped", base)
        else:
            try:
                lyric = parse(fetch(song_id))
            except Exception as exc:  # noqa: BLE001 - 单首失败不该中断整批
                outcome = Outcome(
                    "failed", base, f"{base}：{str(exc).strip() or exc.__class__.__name__}"
                )
            else:
                outcome = apply(base, targets, lyric, mode)

        with lock:
            done += 1
            position = done
            report.record(outcome)
            # 格式变了是全库一起变，逐首报只会把面板刷满；这里只放第一条出声，
            # 其余交给 summary() 的计数（``report.odd`` 刚在 record 里加过）
            odd_line = odd_note(outcome) if outcome.odd and report.odd == 1 else ""

        if on_progress is not None:
            on_progress(position, len(items), base)
        if on_note is not None:
            if outcome.detail:
                on_note(outcome.detail)
            if odd_line:
                on_note(odd_line)

    workers = max(1, min(int(parallel), len(items)))
    if workers == 1:
        for item in items:
            work(item)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ncmgui-lyric") as pool:
            for _ in pool.map(work, items):
                pass
    return report


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    """不联网、不需要 Qt、不需要登录，只验证歌词解析与落盘。"""
    import tempfile

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}\n    期望 {want!r}\n    实际 {got!r}")

    # ---- 「到底有没有歌词」的判定
    check("空文本没有歌词", has_content(""), False)
    check("只有空白没有歌词", has_content("\n  \n"), False)
    check("查无歌词占位串不算歌词", has_content("[00:00.00]暂无歌词"), False)
    check("纯音乐占位串不算歌词", has_content("[00:05.00]纯音乐，请欣赏"), False)
    check("占位串换个说法也算", has_content("[00:00.00]暂无"), False)
    check("真的歌词算歌词", has_content("[00:01.00]那一天的风"), True)
    check("只有元数据标签不算歌词", has_content("[by:MMMMad-man]\n[ti:x]"), False)
    check(
        "短但确实是歌词的不误判",
        has_content("[00:00.000] 作词 : 黒うさP\n[00:01.000] 作曲 : 黒うさP"),
        True,
    )

    # ---- 翻译合并
    original = "[00:00.000] 作词 : 黒うさP\n[00:01.000]千本桜\n[00:02.00]夜ニ紛レ"
    translation = "[by:MMMMad-man]\n[00:02.00]夜中纷乱\n[00:01.000]千本樱"
    check(
        "翻译按时间戳插在原文下一行、元数据行被丢掉",
        merge_translation(original, translation),
        "[00:00.000] 作词 : 黒うさP\n[00:01.000]千本桜\n[00:01.000]千本樱\n"
        "[00:02.00]夜ニ紛レ\n[00:02.00]夜中纷乱",
    )
    # 时间戳对不上就不插 —— 强行按顺序配会把译文错位到别的句子上，
    # 比"这句没有翻译"难发现得多
    check(
        "时间戳对不上的译文宁可丢掉也不错位",
        merge_translation("[00:01.00]原文", "[00:09.00]译文"),
        "[00:01.00]原文",
    )
    check("没有翻译时原文原样返回", merge_translation(original, ""), original)
    check(
        "翻译全是占位串时不合并",
        merge_translation("[00:01.00]真的歌词", "[00:01.00]暂无歌词"),
        "[00:01.00]真的歌词",
    )
    check(
        "原文没有时间戳时无法对齐，原样返回",
        merge_translation("纯文本歌词", "[00:01.00]translated"),
        "纯文本歌词",
    )
    check(
        "同一时间戳重复出现时译文只插一次",
        merge_translation("[00:01.00]副歌\n[00:01.00]副歌", "[00:01.00]chorus"),
        "[00:01.00]副歌\n[00:01.00]chorus\n[00:01.00]副歌",
    )
    check(
        "毫秒位精度不同也对得上",
        merge_translation("[00:01.00]a", "[00:01.000]b"),
        "[00:01.00]a\n[00:01.00]b",
    )

    # ---- 时间戳统一成两位小数。Elisa 25.04 把三位小数当百分秒算，能差出 9 秒，
    #      所以"写出去的文本里不许再有三位小数"是个必须守住的约束
    check("三位小数截到厘秒", canonical_timestamps("[00:44.870]詞"), "[00:44.87]詞")
    check("两位小数原样", canonical_timestamps("[01:23.45]詞"), "[01:23.45]詞")
    check("一位小数补零", canonical_timestamps("[01:23.4]詞"), "[01:23.40]詞")
    check("没有小数位补 00", canonical_timestamps("[01:23]詞"), "[01:23.00]詞")
    check("小数位用冒号的写法也归到点",
          canonical_timestamps("[00:01:500]詞"), "[00:01.50]詞")
    check("一行多个时间戳都改写",
          canonical_timestamps("[00:12.000][00:15.300]副歌"),
          "[00:12.00][00:15.30]副歌")
    check("分钟位超过两位不被截断",
          canonical_timestamps("[123:45.678]詞"), "[123:45.67]詞")
    check(
        "元数据标签一个都不动",
        canonical_timestamps("[by:MMMMad-man]\n[offset:+350]\n[length:04:07.46]"),
        "[by:MMMMad-man]\n[offset:+350]\n[length:04:07.46]",
    )
    check(
        "text() 里原文与译文的时间戳都变成两位小数",
        Lyric("[00:01.000]原文", "[00:01.000]译文").text(),
        "[00:01.00]原文\n[00:01.00]译文\n",
    )
    check(
        "只有翻译时也一样归一到两位小数",
        Lyric("[00:00.000]暂无歌词", "[00:01.000]译文").text(),
        "[00:01.00]译文\n",
    )
    check("带毫秒的旧歌词算过时", outdated("[00:01.000]第一句"), True)
    check("两位小数的不算过时", outdated("[00:01.00]第一句"), False)
    check("没有时间戳（纯文本歌词）不算过时", outdated("随便一段歌词"), False)
    check("空的不算过时（那是「还没有」而不是「过时」）", outdated(""), False)

    # ---- 接口哪天改了写法：解析要照样认得出，认不出的要吭声。
    #      这些用例是"防将来"的：上游多给或少给一位小数、换个分隔符，
    #      都不该变成一份时间轴错乱却全程不报错的歌词
    check(
        "接口多给一位小数也认，一样截到厘秒",
        canonical_timestamps("[00:44.8700]詞"),
        "[00:44.87]詞",
    )
    check(
        "小数位长度只表示精度，不改变含义（.5 就是 .500）",
        (canonical_timestamps("[00:01.5]詞"), canonical_timestamps("[00:01.500]詞")),
        ("[00:01.50]詞", "[00:01.50]詞"),
    )
    check(
        "四位小数时译文照样对得上（精度一改就整段丢失才是要防的事）",
        Lyric("[00:01.0000]原文", "[00:01.0000]译文").text(),
        "[00:01.00]原文\n[00:01.00]译文\n",
    )
    check("认得出的写法不算可疑（四位也认得出）", unknown_timestamps("[00:44.8700]詞"), [])
    # ``[00:00.00-1]`` 是接口真的给过的东西（曲库里那首 Meditacion Profunda 就是），
    # 末尾挂了个看不懂的编号。认下来对齐，但别擅自删掉
    check("时间戳后面挂编号也认得出", unknown_timestamps("[00:00.00-1] 作词 : X"), [])
    check("挂了编号的照旧归一到两位小数、编号原样留着",
          canonical_timestamps("[00:44.870-1]詞"), "[00:44.87-1]詞")
    check("挂了编号的不算旧写法（不该为它反复重写文件）",
          outdated("[00:00.00-1] 作词 : X"), False)
    check("挂了编号的也能和译文对齐",
          merge_translation("[00:01.00-1]a", "[00:01.00]b"),
          "[00:01.00-1]a\n[00:01.00-1]b")
    check("正常歌词里没有可疑标签", unknown_timestamps("[00:44.87]詞"), [])
    check(
        "元数据标签不算可疑（它们本来就没有时间戳）",
        unknown_timestamps("[by:MMMMad-man]\n[offset:+350]\n[length:04:07.46]"),
        [],
    )
    check("正文里的方括号不算可疑", unknown_timestamps("[00:01.00]词[1]句"), [])
    check("没有标签的普通行不算可疑", unknown_timestamps("随便一段歌词"), [])
    # 下面几条是"接口换了花样"的模拟：解析认不出它们，必须被挑出来 ——
    # 挑不出来就等于悄悄交付一份时间轴错乱的歌词
    check("逗号当分隔符会被挑出来", unknown_timestamps("[00:44,870]詞"), ["[00:44,870]"])
    check("没有分钟位会被挑出来", unknown_timestamps("[44.870]詞"), ["[44.870]"])
    check(
        "行首连排的可疑标签都会挑出来",
        unknown_timestamps("[00:44,870][01:02,500]詞"),
        ["[00:44,870]", "[01:02,500]"],
    )
    check(
        "可疑标签混在正常行里也挑得出来",
        unknown_timestamps("[00:01.00]正常\n[00:44,870]可疑"),
        ["[00:44,870]"],
    )

    # ---- 接口响应
    lyric = parse(
        {
            "lrc": {"version": 1, "lyric": "[00:01.00]原文"},
            "tlyric": {"version": 1, "lyric": "[00:01.00]译文"},
        }
    )
    check("parse 取到原文", lyric.original, "[00:01.00]原文")
    check("parse 取到翻译", lyric.translation, "[00:01.00]译文")
    check("text() 合并了两者", "[00:01.00]译文" in lyric.text(), True)
    check(
        "接口换写法（直接给字符串）也能认",
        parse({"lrc": "[00:01.00]原文"}).original,
        "[00:01.00]原文",
    )
    check("纯音乐 → text() 为空", parse({"lrc": {"lyric": "[00:05.00]纯音乐，请欣赏"}}).text(), "")
    check("响应缺字段不炸", parse({}).text(), "")
    check("响应是 None 也不炸", parse(None).text(), "")

    # ---- normalize
    check("normalize 统一换行并补结尾", normalize("a\r\nb\r\n"), "a\nb\n")
    check("normalize 去掉首尾空行", normalize("\n\na\n\n"), "a\n")
    check("normalize 空文本", normalize("   \n"), "")

    # ---- 文件名匹配
    with tempfile.TemporaryDirectory(prefix="ncmgui-lyrics-") as tmp:
        root = Path(tmp)
        (root / "陈奕迅 - 孤勇者.flac").write_bytes(b"x")
        (root / "A,B - X(1).mp3").write_bytes(b"x")       # ncmctl 的重名变体
        (root / "无艺术家 - 歌.mp3").write_bytes(b"x")
        (root / "陈奕迅 - 孤勇者.lrc").write_text("旧歌词", encoding="utf-8")  # 不是音频
        (root / "子目录").mkdir()

        index = index_audio(root)
        check("按主干找到 flac", [p.name for p in targets_for("陈奕迅 - 孤勇者", index)],
              ["陈奕迅 - 孤勇者.flac"])
        check(".lrc 不算音频（否则会找错文件）", ".lrc" in index, False)
        check("(N) 变体登记在原名下", [p.name for p in targets_for("A,B - X", index)],
              ["A,B - X(1).mp3"])
        check("(N) 变体也登记在带后缀的原名下",
              [p.name for p in targets_for("A,B - X(1)", index)], ["A,B - X(1).mp3"])
        check("找不到的主干返回空", targets_for("没有这首", index), [])
        check("不存在的目录不抛错", index_audio("/nonexistent/nope"), {})
        check("lrc_path 同名换后缀",
              lrc_path(Path("/x/陈奕迅 - 孤勇者.flac")).name, "陈奕迅 - 孤勇者.lrc")

    # ---- 写 .lrc
    with tempfile.TemporaryDirectory(prefix="ncmgui-lyrics-") as tmp:
        root = Path(tmp)
        audio = root / "甲 - 歌.flac"
        audio.write_bytes(b"x")
        text = "[00:01.00]第一句\n[00:02.00]第二句\n"

        outcome = apply("甲 - 歌", [audio], Lyric("[00:01.00]第一句\n[00:02.00]第二句"), MODE_LRC)
        check("写入成功状态", outcome.status, "ok")
        lrc = root / "甲 - 歌.lrc"
        check(".lrc 落盘", lrc.is_file(), True)
        check(".lrc 编码与内容", lrc.read_text("utf-8"), text)
        check("音频文件没被动过", audio.read_bytes(), b"x")

        outcome = apply("甲 - 歌", [audio], Lyric("[00:01.00]第一句\n[00:02.00]第二句"), MODE_LRC)
        check("已有 .lrc 时跳过（幂等）", outcome.status, "skipped")
        check("跳过时不改写内容", lrc.read_text("utf-8"), text)
        check("satisfied 认得出已经写过", satisfied([audio], MODE_LRC), True)

        # 旧写法（带毫秒）写下去的文件得能就地改过来 —— 否则改正过的写法再没机会落上去
        old_text = "[00:01.000]第一句\n[00:02.000]第二句\n"
        lrc.write_text(old_text, encoding="utf-8")
        check("旧写法的 .lrc 不算已写过", satisfied([audio], MODE_LRC), False)
        outcome = apply("甲 - 歌", [audio], Lyric(old_text), MODE_LRC)
        check("旧写法的 .lrc 会被重写", outcome.status, "ok")
        check("重写后是两位小数", lrc.read_text("utf-8"), text)
        check("重写后恢复幂等", satisfied([audio], MODE_LRC), True)

        check("找不到文件时是 missing",
              apply("甲 - 歌", [], Lyric("[00:01.00]x"), MODE_LRC).status, "missing")
        check("没有歌词时是 empty",
              apply("甲 - 歌", [audio], Lyric("[00:00.00]暂无歌词"), MODE_LRC).status, "empty")

        # 主干对不上：目录里只有 flac，但用户下的是 mp3 —— 这里靠的目标是空的
        check("空 lrc 文件不算已写过",
              satisfied([root / "乙 - 没写过.flac"], MODE_LRC), False)

    # ---- 写标签（需要 mutagen）
    try:
        import mutagen  # noqa: F401
    except ImportError:
        print("（跳过音频标签往返测试：本机没装 mutagen）")
    else:
        from mutagen.flac import FLAC

        with tempfile.TemporaryDirectory(prefix="ncmgui-lyrics-") as tmp:
            root = Path(tmp)
            flac = root / "甲 - 歌.flac"
            flac.write_bytes(blank_flac())
            payload = "[00:01.00]第一句\n[00:02.00]译文\n"

            check("新文件里读不到歌词", read_embedded(flac), "")
            check("satisfied 对空标签说没有", satisfied([flac], MODE_EMBED), False)
            outcome = apply("甲 - 歌", [flac], Lyric("[00:01.00]第一句\n[00:02.00]译文"), MODE_EMBED)
            check("写标签成功状态", outcome.status, "ok")
            check("标签里读回同一份歌词", read_embedded(flac), payload)
            check("satisfied 认得出已经写过", satisfied([flac], MODE_EMBED), True)
            check("再写一次是跳过",
                  apply("甲 - 歌", [flac], Lyric("[00:01.00]第一句"), MODE_EMBED).status,
                  "skipped")
            check("音频流没被动过（还是能解析）", FLAC(flac).info.sample_rate, 44100)

            # 标签里的旧写法同样要能就地补上
            old_text = "[00:01.000]第一句\n[00:02.000]译文\n"
            embed(flac, old_text)
            check("旧写法的标签不算已写过", satisfied([flac], MODE_EMBED), False)
            outcome = apply("甲 - 歌", [flac], Lyric(old_text), MODE_EMBED)
            check("旧写法的标签被重写", outcome.status, "ok")
            check("重写后是两位小数", read_embedded(flac), payload)
            check("重写后恢复幂等", satisfied([flac], MODE_EMBED), True)

            mp3 = root / "乙 - 歌.mp3"
            mp3.write_bytes(blank_mp3())
            check("mp3 里读不到歌词", read_embedded(mp3), "")
            outcome = apply("乙 - 歌", [mp3], Lyric("[00:01.00]only"), MODE_EMBED)
            check("mp3 写标签成功状态", outcome.status, "ok")
            check("mp3 标签读回同一份歌词", read_embedded(mp3), "[00:01.00]only\n")

            wav = root / "丙 - 歌.wav"
            wav.write_bytes(b"RIFF")
            outcome = apply("丙 - 歌", [wav], Lyric("[00:01.00]x"), MODE_EMBED)
            check("不支持的格式明确失败而不是静默", outcome.status, "failed")
            check("失败信息里点名了格式", "flac" in outcome.detail, True)

    # ---- 批量
    class _Resolver:
        """假的取词器：偶数 ID 返回真歌词，奇数 ID 返回纯音乐。"""

        def __init__(self) -> None:
            self.calls: list[int] = []
            self.lock = threading.Lock()

        def lyric(self, song_id: int) -> dict:
            with self.lock:
                self.calls.append(song_id)
            if song_id % 2:
                return {"lrc": {"lyric": "[00:05.00]纯音乐，请欣赏"}}
            return {"lrc": {"lyric": "[00:01.00]真的歌词"}}

    with tempfile.TemporaryDirectory(prefix="ncmgui-lyrics-") as tmp:
        root = Path(tmp)
        (root / "甲 - 一.flac").write_bytes(b"x")
        (root / "乙 - 二.mp3").write_bytes(b"x")

        fake = _Resolver()
        seen: list[str] = []
        report = collect(
            [("甲 - 一", 1), ("乙 - 二", 2)],
            fake.lyric,
            root,
            MODE_LRC,
            parallel=2,
            on_note=seen.append,
        )
        check("批量：纯音乐记 empty", report.empty, 1)
        check("批量：真歌词记 written", report.written, 1)
        check("批量：总数", report.total, 2)
        check("批量：只有真有歌词的那个落了盘", (root / "乙 - 二.lrc").is_file(), True)
        check("批量：纯音乐那个没落盘", (root / "甲 - 一.lrc").exists(), False)
        check("批量：empty 会打一行说明", len(seen), 1)
        check("批量：汇总里有数字", "写入 1 首" in report.summary(), True)

        # 再跑一遍：两个都该跳过，而且一次接口都不该调
        before = len(fake.calls)
        report = collect([("甲 - 一", 1), ("乙 - 二", 2)], fake.lyric, root, MODE_LRC, parallel=2)
        check("重复跑：有歌词的跳过", report.skipped, 1)
        check("重复跑：纯音乐的仍要问接口", len(fake.calls) - before, 1)

        check("批量：接口全挂时算 failed 而不是崩",
              collect([("甲 - 一", 1)], _boom, root, MODE_LRC, parallel=1).failed, 1)
        check("批量：主干对不上时算 missing",
              collect([("丙 - 三", 2)], fake.lyric, root, MODE_LRC, parallel=1).missing, 1)
        check("批量：mode=none 不写也不请求",
              collect([("乙 - 二", 2)], fake.lyric, root, MODE_NONE).written, 0)
        check("批量：空输入直接返回",
              collect([], fake.lyric, root, MODE_LRC).total, 0)

        report = collect([("甲 - 一", 1)], fake.lyric, root, MODE_LRC, parallel=1)
        check("problems 超过上限时截断并提示",
              len(Report(problems=[f"x{i}" for i in range(50)]).notes(3)), 4)

        # ---- 接口哪天换了时间戳写法：歌词照写（认不出的只是那几行，整首丢掉更亏），
        #      但必须**出声**。挑不出来的话，用户拿到的是时间轴错乱却没有任何提示的歌词
        def _odd_lyric(_song_id: int) -> dict:
            return {"lrc": {"lyric": "[00:01,000]第一句\n[00:02,500]第二句"}}

        (root / "丁 - 换写法.flac").write_bytes(b"x")
        seen_odd: list[str] = []
        report = collect(
            [("丁 - 换写法", 9)], _odd_lyric, root, MODE_LRC, parallel=1,
            on_note=seen_odd.append,
        )
        check("换写法：歌词照样写下去", report.written, 1)
        check("换写法：计进 odd", report.odd, 1)
        check("换写法：汇总里点出来", "1 首有认不出的时间戳" in report.summary(), True)
        check("换写法：面板上出一行提示", len(seen_odd), 1)
        check("换写法：提示里带上原样的标签（看得到才知道是什么）",
              "[00:01,000]" in seen_odd[0], True)
        # 只出一次声。每首歌都念一遍会刷屏，而格式改变来就是全库一起变
        check("换写法：第二次跑不再唠叨（已经写过了）",
              collect([("丁 - 换写法", 9)], _odd_lyric, root, MODE_LRC, parallel=1).odd, 0)

    # ---- 进度回调
    with tempfile.TemporaryDirectory(prefix="ncmgui-lyrics-") as tmp:
        root = Path(tmp)
        (root / "甲 - 一.flac").write_bytes(b"x")
        steps: list[tuple[int, int, str]] = []
        collect(
            [("甲 - 一", 1)],
            _Resolver().lyric,
            root,
            MODE_LRC,
            parallel=1,
            on_progress=lambda a, b, c: steps.append((a, b, c)),
        )
        check("进度回调报告 (1, 1, 主干)", steps, [(1, 1, "甲 - 一")])

    if failures:
        print(f"歌词自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("歌词自检全部通过")
    return 0


def _boom(_song_id: int) -> dict:
    raise LyricsError("接口调用失败")


if __name__ == "__main__":
    raise SystemExit(_selftest())
