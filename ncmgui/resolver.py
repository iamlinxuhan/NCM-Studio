"""把下载目标展开成歌曲清单，并对照输出目录做查重。

**为什么需要这一层：** ``ncmctl download`` 自己不会跳过已经存在的文件。目标
文件名重名时它会接着写成 ``歌曲(1).flac``、``歌曲(2).flac``
（见 ncmctl 源码 ``internal/ncmctl/download.go`` 的「避免文件重名」循环）。
所以「已经有同名歌曲就跳过」这件事必须在调用 download **之前**做完：

1. 把每个目标（歌曲 ID / 歌曲 / 专辑 / 歌手 / 歌单链接）展开成歌曲 ID 列表；
2. 用 ncmctl 的命名规则推算出每首歌的文件名；
3. 拿去和输出目录里的现有文件比对，把已经存在的剔掉；
4. 只把剩下的歌曲 ID 交给 ``ncmctl download``。

**命名规则**（ncmctl ``download.go``）::

    <艺术家1,艺术家2> - <歌曲名>.<扩展名>

艺术家与歌曲名都经过 ``utils.Filename(x, "_")``：先 ``TrimSpace``，再把
``\\/:*?"<>|`` 替换成下划线。本模块的 :func:`filename_safe` 严格镜像它 ——
两侧算法只要有一点出入，查重结果就是错的。

**只比对主干名，不比对扩展名。** 同一首歌会因实际拿到的音质落成 ``.flac``
或 ``.mp3``，把扩展名写死会导致漏判。

本模块不依赖 Qt，可以单独自检：

    .venv/bin/python -m ncmgui.resolver
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import ncmctl
from .player import AudioSource, format_clock

#: 与 ncmctl ``internal/ncmctl/utils.go`` 的 urlPattern 保持一致
URL_RE = re.compile(r"/(song|artist|album|playlist)\?id=(\d+)")

#: 与 ncmctl ``pkg/utils/utils.go`` 的 filenameRegexp 保持一致
FILENAME_RE = re.compile(r'[\\/:*?"<>|]')

#: ncmctl 给重名文件追加的 ``(1)`` ``(2)`` 后缀
VARIANT_RE = re.compile(r"\(\d+\)$")

#: 单次 SongDetail / ArtistSongs 请求的条数（与 ncmctl 一致）
DETAIL_BATCH = 500

#: 歌手作品最多翻多少页，避免接口返回异常时无限循环
ARTIST_MAX_PAGES = 40

#: 「我喜欢的音乐」歌单链接格式
PLAYLIST_URL = "https://music.163.com/playlist?id={}"


class ResolveError(RuntimeError):
    """解析目标或调用 API 失败。消息直接面向用户，不夹带英文堆栈。"""


class SongUrlError(ResolveError):
    """取播放地址失败。

    ``SongDownloadUrlV1`` 把失败原因放在 ``data.code`` 里，顶层 ``code`` 仍是 200，
    所以 :meth:`Resolver._curl` 不会替我们拦下来 —— 这个异常就是那一层的翻译。
    """

    #: 网易云接口的失败码；拿不到时为 0
    code: int

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        """-103 是限流，过一会儿重试有意义。"""
        return self.code == -103

    @property
    def needs_login(self) -> bool:
        """301 表示登录失效。"""
        return self.code == 301


# --------------------------------------------------------------------------- #
# 命名规则（必须与 ncmctl 完全一致）
# --------------------------------------------------------------------------- #

def filename_safe(text: str) -> str:
    """镜像 ncmctl 的 ``utils.Filename(x, "_")``。

    顺序有意义：ncmctl 先 ``strings.TrimSpace`` 再替换非法字符，所以
    ``"a/b "`` 会变成 ``"a_b"`` 而不是 ``"a_b_"``。
    """
    return FILENAME_RE.sub("_", (text or "").strip())


def song_base(name: str, artists: Iterable[str]) -> str:
    """推算出这首歌的文件主干名（不含扩展名）。"""
    joined = ",".join(filename_safe(a) for a in artists)
    return f"{joined} - {filename_safe(name)}"


def parse_target(source: str) -> tuple[str, int]:
    """把一条目标解析成 ``(类型, ID)``。

    类型取值 ``song`` / ``album`` / ``artist`` / ``playlist``，与 ncmctl 一致：
    纯数字按歌曲 ID 处理，其余必须是 ``music.163.com`` 的链接。
    """
    source = (source or "").strip()
    if not source:
        raise ResolveError("目标为空")

    if source.isdigit():
        return "song", int(source)

    if "music.163.com" not in source:
        raise ResolveError("既不是数字 ID，也不是 music.163.com 的链接")

    match = URL_RE.search(source)
    if not match:
        raise ResolveError("链接里找不到 song / album / artist / playlist 的 id")
    try:
        return match.group(1), int(match.group(2))
    except ValueError as exc:  # 正则已限定 \d+，这里只是兜底
        raise ResolveError(f"id 不是数字：{exc}") from exc


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

#: 网易云 ``fee`` 字段的含义（见 ncmctl 的 ``api/weapi/song.go``）：
#: 0 免费、1 二元购买单曲、4 购买专辑、8 低音质免费。
#: 站在「下载无损 / Hi-Res」的角度，非 0 的几种都意味着没有会员（或没买专辑）
#: 时拿不到所选音质，所以统一在界面上标出来。未知的非 0 取值一律按 VIP 处理。
_FEE_LABELS = {1: "VIP", 4: "付费专辑", 8: "VIP音质"}

#: 明细末尾图例用的解释文案，键与 ``_FEE_LABELS`` 的取值一致
_FEE_HINTS = {
    "VIP": "需会员才能听 / 下载所选音质",
    "付费专辑": "需先购买专辑",
    "VIP音质": "非会员只能听低音质",
}


@dataclass(frozen=True)
class Song:
    """解析出来的一首歌。``base`` 就是 ncmctl 会用的文件名主干。"""

    id: int
    name: str
    artists: tuple[str, ...] = ()
    #: 付费类型，含义见 ``_FEE_LABELS``；0 表示免费
    fee: int = 0
    #: 专辑名。列表页要显示，下载用不到；默认空串保证老调用点不受影响
    album: str = ""
    #: 时长，**毫秒**（接口的 ``dt`` 就是这个单位）
    duration: int = 0
    #: 专辑封面地址（接口 ``al.picUrl``）。列表页与播放页用来显示封面
    pic_url: str = ""
    #: 本地文件路径。只有「本地音乐」队列会填，流媒体歌曲为空串
    path: str = ""
    #: 本地文件的音质描述，如「无损 44.1kHz/16bit」「极高 320 kbps」。
    #: 流媒体歌曲为空串 —— 那边的音质由播放时选的档位决定，不是文件属性
    quality: str = ""
    #: 歌曲来源。空串表示网易云；``local`` 表示本地文件；流媒体服务器填
    #: ``subsonic`` / ``jellyfin`` 等。3.0 之前保存的队列没有这个字段，
    #: 反序列化时按空串处理，仍然是网易云。
    source: str = ""
    #: 流媒体服务器里的原始 ID（字符串）。网易云与本地文件为空
    stream_id: str = ""

    @property
    def base(self) -> str:
        return song_base(self.name, self.artists)

    @property
    def duration_text(self) -> str:
        """``03:42`` 这样的时长；接口没给时是 ``--:--``。"""
        return format_clock(self.duration / 1000 if self.duration else None)

    @property
    def search_text(self) -> str:
        """列表页搜索用的合并文本：歌名 + 歌手 + 专辑。"""
        return " ".join(part for part in (self.name, *self.artists, self.album) if part)

    @property
    def display(self) -> str:
        joined = ", ".join(self.artists)
        return f"{joined} - {self.name}" if joined else self.name

    @property
    def fee_label(self) -> str:
        """付费类型的短标记，免费歌曲返回空串。"""
        if not self.fee:
            return ""
        return _FEE_LABELS.get(self.fee, "VIP")

    @property
    def fee_tag(self) -> str:
        """列表里显示的短标记，与 SPlayer 一致：``VIP`` / ``EP``。

        ``fee=8`` 是「低音质免费」，完整高音质才需要会员，不适合在歌名后面
        占位置；下载页仍用更详细的 :attr:`fee_label`。
        """
        if self.fee == 1:
            return "VIP"
        if self.fee == 4:
            return "EP"
        return ""

    @property
    def is_paid(self) -> bool:
        return bool(self.fee)

    @property
    def marked(self) -> str:
        """带付费标记的展示名，例如 ``[VIP] 周杰伦 - 晴天``。"""
        label = self.fee_label
        return f"[{label}] {self.display}" if label else self.display


@dataclass
class Plan:
    """一次「解析 + 查重」的结果。"""

    songs: list[Song] = field(default_factory=list)
    missing: list[Song] = field(default_factory=list)
    present: list[Song] = field(default_factory=list)
    output: Path = field(default_factory=Path)
    warnings: list[str] = field(default_factory=list)
    label: str = ""
    #: 产生这份计划的原始目标，用来判断「预检」的结果是否还对得上当前输入
    sources: tuple[str, ...] = ()
    #: 生成计划时账号是不是会员；``None`` 表示没查出来。只影响文案语气
    vip: bool | None = None

    @property
    def total(self) -> int:
        return len(self.songs)

    @property
    def paid(self) -> list[Song]:
        """需要会员或购买的歌曲。"""
        return [song for song in self.songs if song.is_paid]

    def matches(self, targets: Sequence[str], output: str | os.PathLike[str]) -> bool:
        """这份计划是否仍然适用于给定的目标与输出目录。"""
        return (
            tuple(self.sources) == tuple(targets)
            and Path(output).expanduser() == self.output
        )

    def report(self) -> str:
        """一行式摘要，给界面上的标签用。"""
        if not self.songs:
            return "没有解析出任何歌曲"
        parts = [f"共 {self.total} 首"]
        if self.present:
            parts.append(f"已存在 {len(self.present)} 首（跳过）")
        parts.append(f"待下载 {len(self.missing)} 首")
        if self.paid_missing:
            note = f"待下载中 {len(self.paid_missing)} 首需会员"
            if self.vip is False:
                note += "（当前不是会员，多半会失败）"
            parts.append(note)
        return " · ".join(parts)

    @property
    def paid_missing(self) -> list[Song]:
        """待下载里需要会员/购买的那些 —— 最可能失败的就是它们。"""
        return [song for song in self.missing if song.is_paid]

    def details(self, limit: int = 120) -> str:
        """明细文本，放进只读文本框。缺失列表太长时只列前面一段。"""
        lines: list[str] = []
        if self.label:
            lines.append(f"{self.label}（{self.total} 首）")
        lines.append(f"输出目录：{self.output}")

        if self.present:
            lines.append("")
            lines.append(f"▸ 已存在，跳过下载（{len(self.present)} 首）")
            lines.extend(f"  · {song.marked}" for song in self.present[:limit])

        if self.missing:
            lines.append("")
            lines.append(f"▸ 将要下载（{len(self.missing)} 首）")
            lines.extend(f"  · {song.marked}" for song in self.missing[:limit])
            if len(self.missing) > limit:
                lines.append(f"  … 还有 {len(self.missing) - limit} 首")

        if self.paid:
            # 只解释这份计划里真的出现过的标记
            shown = {song.fee_label for song in self.paid}
            lines.append("")
            lines.append(
                "标记含义："
                + " · ".join(
                    f"[{label}] {hint}"
                    for label, hint in _FEE_HINTS.items()
                    if label in shown
                )
            )
            if self.vip is True:
                lines.append("当前账号是会员，带标记的曲目通常能正常下载。")
            elif self.vip is False:
                lines.append("注意：当前账号不是会员，带标记的曲目很可能拉不到所选音质。")

        if self.warnings:
            lines.append("")
            lines.append(f"▸ 被跳过的目标（{len(self.warnings)} 条）")
            lines.extend(f"  ! {item}" for item in self.warnings[:limit])
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 下载进度的付费标注
# --------------------------------------------------------------------------- #

#: 认出一行是不是 ncmctl 的进度条。
#:
#: pb 打出来的是 ``<标题，空格补齐>[<进度条>] <已下载>/<总量> <百分比>%``。
#: 标题自己可能就带方括号（歌名如 ``青花瓷 [Live]``），所以标题用贪婪匹配，
#: 让它一直吃到**最后一个** ``[`` 为止。
_PROGRESS_RE = re.compile(r"^(?P<title>.+)\[(?P<bar>[^\]]*)\]\s*\S.*%$")


def _squash(text: str) -> str:
    """去掉全部空白字符。

    同一首歌，ncmctl 写文件名时歌手之间是 ``", "``，进度条里却是 ``","``
    （两边各用了一套拼接）。比对之前先把这个差异抹平。
    """
    return "".join(text.split())


class FeeTagger:
    """给 ncmctl 的下载进度行补上付费标记。

    ncmctl 的进度条只打文件名，它并不知道哪首需要会员。而查重阶段我们已经把
    每首歌的 ``fee`` 拿在手上了，这里做一次回匹配，让 ``[VIP]`` **从头到尾
    跟着那一行进度走**，而不是开跑前罗列一遍就完事 —— 几百首歌的清单滚两屏
    就没了，等于没标。

    匹配要容忍两处差异：

    * **空白** —— 见 :func:`_squash`
    * **截断** —— 进度条宽度有限，超出的标题会被 pb 砍成 ``前缀...``，
      所以还要拿掉尾部的 ``...`` 再做一次前缀匹配

    实例本身就是可调用对象，直接塞给 :meth:`ConsolePanel.set_tagger` 即可。
    """

    def __init__(self, songs: Iterable[Song]) -> None:
        #: 规范化歌名 → 标记。只收付费歌：命中就加标记，没命中就是免费。
        self._index: dict[str, str] = {}
        for song in songs:
            label = song.fee_label
            if label:
                self._index.setdefault(_squash(song.base), label)
        #: 进度行每秒刷新好几次，同一行的标题反复查太浪费，缓存住
        self._cache: dict[str, str | None] = {}

    def __call__(self, line: str) -> str | None:
        """是付费歌曲的进度行就返回加了标记的整行，否则返回 ``None``。"""
        match = _PROGRESS_RE.match(line)
        if match is None:
            return None
        label = self._lookup(match.group("title").rstrip())
        return f"[{label}]{line}" if label else None

    def _lookup(self, title: str) -> str | None:
        key = _squash(title)
        if key in self._cache:
            return self._cache[key]

        label = self._index.get(key)
        if label is None and key.endswith("..."):
            # pb 把放不下的标题砍成了「前缀 + ...」
            stem = key[:-3]
            label = next(
                (lab for base, lab in self._index.items() if base.startswith(stem)),
                None,
            )
        self._cache[key] = label
        return label


# --------------------------------------------------------------------------- #
# 输出目录扫描
# --------------------------------------------------------------------------- #

def order_songs(ids: Sequence[int], songs: Iterable[Song]) -> list[Song]:
    """按给定的 ID 顺序重排歌曲清单。

    ``SongDetail`` 是分批请求的，接口不保证返回顺序与请求顺序一致；而列表页的
    序号、上一首/下一首都依赖歌单的原始顺序。少了的 id 直接跳过（下架/无版权）。
    """
    by_id = {song.id: song for song in songs}
    return [by_id[song_id] for song_id in ids if song_id in by_id]


#: ``SongDownloadUrlV1`` 的 ``data.code`` 含义。与网易云的约定一致
_URL_ERRORS = {
    404: "歌曲不存在或已下架（版权受限）",
    -103: "请求过于频繁，请稍后再试",
    -105: "需要 VIP 会员或购买后才能播放这首歌",
    -110: "当前账号或网络无权播放（可能登录失效或受地区限制）",
    301: "登录已失效，请重新登录",
}


def parse_player_url(payload: dict) -> AudioSource:
    """把 ``SongPlayerV1`` 的响应翻译成播放地址。

    这个接口返回的是 ``data: [ {...} ]`` 数组（与下载接口的 ``data: {...}``
    不同）。失败码同样藏在元素自己的 ``code`` 里，顶层仍是 200。

    ``freeTrialInfo`` 即使是完整歌曲也可能出现，但只有 ``end > start`` 才
    真的是试听片段；不能只看字段是否存在。
    """
    items = payload.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise SongUrlError(0, "接口没有返回播放地址（可能已下架或没有版权）")

    data = items[0]
    raw_code = data.get("code")
    if raw_code is None:
        raw_code = payload.get("code")
    try:
        code = int(raw_code or 0)
    except (TypeError, ValueError):
        code = 0

    if code != 200:
        message = _URL_ERRORS.get(code) or str(
            data.get("message") or f"接口返回 code={code}"
        )
        raise SongUrlError(code, message)

    url = str(data.get("url") or "").strip()
    if not url:
        # 没有地址时 ``fee`` 能区分「要会员」和「没版权」；SPlayer 也是这样
        # 分类的（fee=1 单曲付费、fee=4 付费专辑）。给出可操作的提示，
        # 比笼统的「可能已下架」有用。
        raw_fee = data.get("fee")
        try:
            fee = int(raw_fee or 0)
        except (TypeError, ValueError):
            fee = 0
        if fee in (1, 4):
            raise SongUrlError(
                -105,
                "需要 VIP 会员或购买后才能播放这首歌"
                if fee == 1 else "需要先购买这张专辑才能播放",
            )
        raise SongUrlError(code, "接口没有返回播放地址（可能已下架或没有版权）")

    try:
        expi = int(data.get("expi") or 600)
    except (TypeError, ValueError):
        expi = 600

    info = data.get("freeTrialInfo")
    trial = False
    if isinstance(info, dict):
        try:
            trial = int(info.get("end") or 0) > int(info.get("start") or 0)
        except (TypeError, ValueError):
            trial = False

    return AudioSource(
        url=url,
        level=str(data.get("level") or ""),
        expi=expi,
        trial=trial,
    )


def parse_song_url(payload: dict) -> AudioSource:
    """把 ``SongDownloadUrlV1`` 的响应翻译成播放地址。

    **失败码在 ``data.code`` 里**，顶层 ``code`` 通常仍是 200 —— 只看顶层会把
    「无版权」「要会员」当成成功，然后拿着空 URL 去调 mpv。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SongUrlError(0, "接口没有返回播放地址（可能已下架或没有版权）")

    raw_code = data.get("code")
    if raw_code is None:
        raw_code = payload.get("code")
    try:
        code = int(raw_code or 0)
    except (TypeError, ValueError):
        code = 0

    if code != 200:
        message = _URL_ERRORS.get(code) or str(
            data.get("message") or f"接口返回 code={code}"
        )
        raise SongUrlError(code, message)

    url = str(data.get("url") or "").strip()
    if not url:
        raise SongUrlError(code, "接口没有返回播放地址（可能已下架或没有版权）")

    try:
        expi = int(data.get("expi") or 600)
    except (TypeError, ValueError):
        expi = 600

    return AudioSource(
        url=url,
        level=str(data.get("level") or ""),
        expi=expi,
        # freeTrialInfo 非空 = 这是 30 秒试听片段
        trial=bool(data.get("freeTrialInfo")),
    )


def existing_bases(directory: str | os.PathLike[str]) -> set[str]:
    """列出目录下所有文件名的主干（含 ncmctl 的 ``(N)`` 变体）。

    加进去的既有原样主干，也有去掉 ``(N)`` 后缀的那份：ncmctl 第二次下载
    同一首歌会落成 ``X(1).flac``，比对 ``X`` 时应当算「已存在」；而一首真的
    叫 ``X(1)`` 的歌也应该能被 ``X(1)`` 匹配上。两个都放，只可能更保守。
    """
    known: set[str] = set()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return known

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        stem = Path(entry.name).stem.strip()
        if not stem:
            continue
        known.add(stem)
        stripped = VARIANT_RE.sub("", stem).strip()
        if stripped:
            known.add(stripped)
    return known


# --------------------------------------------------------------------------- #
# 目标解析器
# --------------------------------------------------------------------------- #

def _duration_seconds(text: str, default: float = 30.0) -> float:
    """把 ``30s`` / ``1m`` / ``500ms`` 这类写法换成秒。"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*", text or "")
    if not match:
        return default
    value = float(match.group(1))
    unit = match.group(2) or "s"
    return value * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]


class Resolver:
    """通过 ``ncmctl curl`` 调用网易云接口，把目标展开成歌曲清单。

    :param binary: ncmctl 可执行文件路径
    :param opts: 全局参数（``--home`` / ``-c`` / ``--debug``）
    :param timeout: 单次 API 调用的超时，写成 ncmctl 认识的形式（如 ``30s``）
    :param progress: 进度回调，接收一句人类可读的说明
    """

    def __init__(
        self,
        binary: str | None,
        opts: ncmctl.GlobalOpts | None = None,
        timeout: str = "30s",
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.binary = binary or ""
        self.opts = opts or ncmctl.GlobalOpts()
        self.timeout = timeout
        self.progress = progress
        self._account: tuple[int, int] | None = None

    # ------------------------------------------------------------- 底层调用

    def notify(self, message: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(message)
        except RuntimeError:
            # 界面已经被销毁（例如解析途中用户关掉了窗口）
            pass

    def _curl(self, method: str, data: dict) -> dict:
        """调用一个 weapi 方法并把响应解析成 dict。

        响应用 ``-o`` 落到临时文件再读，不走 stdout —— ncmctl 会把
        「generate file path: ...」这类日志和 JSON 混在一起打印。
        """
        if not self.binary:
            raise ResolveError("找不到 ncmctl 可执行文件，请到「设置」页指定路径")

        # API 超时之外再留一段余量，用于进程启动与网络重试
        hard_timeout = _duration_seconds(self.timeout) * 3 + 20

        with tempfile.TemporaryDirectory(prefix="ncmgui-resolve-") as tmp:
            dest = Path(tmp) / "response.json"
            argv = ncmctl.build_curl(
                method,
                kind="weapi",
                data=json.dumps(data, ensure_ascii=False),
                output=str(dest),
                timeout=self.timeout,
                opts=self.opts,
            )
            try:
                proc = subprocess.run(
                    [self.binary, *argv],
                    capture_output=True,
                    text=True,
                    timeout=hard_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise ResolveError(f"{method} 超时（{self.timeout} 内没有响应）") from exc
            except OSError as exc:
                raise ResolveError(f"无法启动 ncmctl：{exc}") from exc

            if not dest.is_file():
                raise ResolveError(f"{method} 没有返回结果：{_last_line(proc)}")

            try:
                payload = json.loads(dest.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ResolveError(f"{method} 返回的不是合法 JSON：{exc}") from exc

        if not isinstance(payload, dict):
            raise ResolveError(f"{method} 返回了意料之外的结构")

        code = payload.get("code")
        if code not in (None, 200):
            detail = payload.get("message") or payload.get("msg") or ""
            hint = "（登录可能已失效，请重新登录）" if code == 301 else ""
            raise ResolveError(f"{method} 失败：code={code} {detail}{hint}".strip())
        return payload

    # ------------------------------------------------------------- 账号相关

    def account(self) -> tuple[int, int]:
        """当前登录账号的 ``(UID, vipType)``；``vipType`` 非 0 表示有会员。

        一次解析里会员状态会被问好几次，所以缓存一份。
        """
        if self._account is None:
            payload = self._curl("GetUserInfo", {})
            profile = payload.get("profile") or {}
            account = payload.get("account") or {}
            uid = profile.get("userId") or account.get("id")
            if not uid:
                raise ResolveError("拿不到用户 ID，登录可能已失效")
            vip_type = profile.get("vipType") or account.get("vipType") or 0
            self._account = (int(uid), int(vip_type))
        return self._account

    def user_id(self) -> int:
        """当前登录账号的 UID。"""
        return self.account()[0]

    def is_vip(self) -> bool | None:
        """当前账号是不是会员。查不出来返回 ``None``（界面按「不确定」处理）。"""
        try:
            return self.account()[1] != 0
        except ResolveError:
            # 少吓唬人：状态不明时不要断言「你下不了」
            return None

    def liked_playlist(self) -> tuple[int, str]:
        """返回 ``(歌单 ID, 歌单名)`` —— 即「我喜欢的音乐」。

        歌单 ID 不一定等于 UID：新版账号会另发一个歌单，用 ``specialType == 5``
        标记。老账号的歌单 ID 才是 UID，所以两个都试。
        """
        uid = self.user_id()
        self.notify("已取得账号信息，正在查找「我喜欢的音乐」…")

        payload = self._curl("Playlist", {"uid": str(uid), "offset": "0", "limit": "1000"})
        for item in payload.get("playlist") or []:
            if item.get("specialType") == 5:
                return int(item["id"]), item.get("name") or "我喜欢的音乐"

        # 老账号：歌单 ID 就是 UID
        return uid, "我喜欢的音乐"

    def liked_songs(self) -> list[Song]:
        """取「我喜欢的音乐」的完整歌曲清单，保持歌单顺序。"""
        playlist_id, name = self.liked_playlist()
        self.notify(f"正在读取「{name}」…")

        ids = self.expand("playlist", playlist_id)
        self.notify(f"「{name}」共 {len(ids)} 首，正在获取歌曲信息…")

        songs = order_songs(ids, self.song_details(ids))
        if not songs:
            raise ResolveError("红心歌单里没有可用的歌曲（曲目可能都已下架）")
        return songs

    def song_url(self, song_id: int, level: str = "exhigh") -> AudioSource:
        """取一首歌的播放直链。

        ``level`` 取值与下载页一致：standard / higher / exhigh / lossless / hires。
        非会员请求高音质时，接口通常退回一个能给的档位（``AudioSource.level``
        是**实际**拿到的），拿不到就是 :class:`SongUrlError`。

        用 ``SongPlayerV1`` 而不是 ``SongDownloadUrlV1``：后者是下载接口，
        播放时调用会消耗每日下载额度，语义也不对。下载页仍然走下载接口。
        """
        payload = self._curl(
            "SongPlayerV1", {"ids": [int(song_id)], "level": level}
        )
        return parse_player_url(payload)

    # ------------------------------------------------------------- 目标展开

    def expand(self, kind: str, target_id: int) -> list[int]:
        """把一个目标展开成歌曲 ID 列表。"""
        if kind == "song":
            return [target_id]

        if kind == "playlist":
            payload = self._curl(
                "PlaylistDetail", {"id": str(target_id), "n": "0", "s": "0"}
            )
            playlist = payload.get("playlist") or {}
            track_ids = playlist.get("trackIds") or []
            if not track_ids:
                raise ResolveError(
                    f"歌单 {target_id} 里没有歌曲"
                    "（私密歌单需要登录，或该歌单已失效）"
                )
            self.notify(f"歌单「{playlist.get('name') or target_id}」共 {len(track_ids)} 首")
            return [int(t["id"]) for t in track_ids if t.get("id")]

        if kind == "album":
            payload = self._curl("Album", {"id": str(target_id)})
            songs = payload.get("songs") or []
            if not songs:
                raise ResolveError(f"专辑 {target_id} 里没有歌曲")
            self.notify(f"专辑「{payload.get('album', {}).get('name') or target_id}」"
                         f"共 {len(songs)} 首")
            return [int(s["id"]) for s in songs if s.get("id")]

        if kind == "artist":
            return self._artist_songs(target_id)

        raise ResolveError(f"不支持的目标类型：{kind}")

    def _artist_songs(self, artist_id: int, page_size: int = DETAIL_BATCH) -> list[int]:
        """翻页取某位歌手的全部作品。分页参数与 ncmctl 保持一致。"""
        ids: list[int] = []
        seen: set[int] = set()

        for page in range(1, ARTIST_MAX_PAGES + 1):
            payload = self._curl(
                "ArtistSongs",
                {
                    "id": artist_id,
                    "private_cloud": "true",
                    "work_type": 1,
                    "order": "hot",
                    # offset 是「跳过多少首」，不是页码。以前直接传 page，
                    # 第 2 页起仍在取第一页，歌手作品一多就死循环/重复。
                    "offset": (page - 1) * page_size,
                    "limit": page_size,
                },
            )
            songs = payload.get("songs") or []
            for item in songs:
                song_id = item.get("id")
                if song_id and song_id not in seen:
                    seen.add(song_id)
                    ids.append(int(song_id))

            self.notify(f"歌手 {artist_id}：已取到 {len(ids)} 首")
            if not payload.get("more") or not songs:
                break

        if not ids:
            raise ResolveError(f"歌手 {artist_id} 没有取到任何作品")
        return ids

    def lyric_v1(self, song_id: int) -> dict:
        """``LyricV1``：比旧 ``Lyric`` 多一份 ``yrc``（逐字时间）。"""
        return self._curl("LyricV1", {"id": int(song_id)})

    def lyric(self, song_id: int) -> dict:
        """取一首歌的歌词，优先 ``LyricV1``，拿不到逐字数据再退旧 ``Lyric``。

        ``LyricV1`` 是较新的接口，旧版 ncmctl 可能没有；接口不认、网络失败或
        返回的三份歌词全空时都回退，不让“逐字”这个增强把原来的歌词功能弄坏。
        整理响应的是 :mod:`ncmgui.lyrics` —— 这一层只负责把请求发出去。
        """
        try:
            payload = self.lyric_v1(song_id)
        except ResolveError:
            payload = None
        if _has_lyric(payload):
            return payload  # type: ignore[return-value]
        return self._curl("Lyric", {"id": int(song_id)})

    def song_details(self, ids: Sequence[int], batch: int = DETAIL_BATCH) -> list[Song]:
        """批量取歌曲详情，得到文件名推算所需的歌名、艺术家与付费类型。"""
        songs: list[Song] = []
        total = len(ids)

        for start in range(0, total, batch):
            chunk = ids[start : start + batch]
            payload = self._curl(
                "SongDetail", {"c": [{"id": str(i), "v": 0} for i in chunk]}
            )
            for item in payload.get("songs") or []:
                try:
                    song_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                album = item.get("al") or {}
                try:
                    duration = int(item.get("dt") or 0)
                except (TypeError, ValueError):
                    duration = 0
                songs.append(
                    Song(
                        id=song_id,
                        name=item.get("name") or "",
                        artists=tuple(
                            (artist.get("name") or "") for artist in item.get("ar") or []
                        ),
                        fee=int(item.get("fee") or 0),
                        album=str(album.get("name") or "") if isinstance(album, dict) else "",
                        duration=duration,
                        pic_url=(
                            str(album.get("picUrl") or "")
                            if isinstance(album, dict)
                            else ""
                        ),
                    )
                )
            if total > batch:
                self.notify(f"已获取 {min(start + batch, total)}/{total} 首歌曲信息")

        if not songs:
            raise ResolveError("接口没有返回任何歌曲详情（曲目可能都已下架或没有版权）")
        return songs

    # ------------------------------------------------------------- 对外入口

    def resolve(self, targets: Sequence[str]) -> tuple[list[Song], list[str]]:
        """展开全部目标。个别目标解析失败只记警告，不中断整体。"""
        ids: list[int] = []
        seen: set[int] = set()
        warnings: list[str] = []

        for raw in targets:
            try:
                kind, target_id = parse_target(raw)
                found = self.expand(kind, target_id)
            except ResolveError as exc:
                warnings.append(f"{raw} —— {exc}")
                continue

            added = 0
            for song_id in found:
                if song_id not in seen:
                    seen.add(song_id)
                    ids.append(song_id)
                    added += 1
            if added != len(found):
                self.notify(f"{raw}：去重后新增 {added} 首")

        if not ids:
            detail = "；".join(warnings) if warnings else "请检查填写的 ID 或链接"
            raise ResolveError(f"没有解析出任何歌曲：{detail}")

        return self.song_details(ids), warnings

    def plan(self, targets: Sequence[str], output: str | os.PathLike[str]) -> Plan:
        """解析目标并对照输出目录查重，得到一份可执行的下载计划。"""
        out = Path(output).expanduser()
        songs, warnings = self.resolve(targets)

        self.notify(f"正在比对输出目录 {out} …")
        known = existing_bases(out)

        missing = [song for song in songs if song.base not in known]
        present = [song for song in songs if song.base in known]

        return Plan(
            songs=songs,
            missing=missing,
            present=present,
            output=out,
            warnings=list(warnings),
            sources=tuple(targets),
            # 有了它，「N 首需会员」才能说清是「能下」还是「悬」
            vip=self.is_vip(),
        )


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _last_line(proc: subprocess.CompletedProcess) -> str:
    """从 ncmctl 的输出里挑一句最能说明问题的。"""
    for stream in (proc.stderr, proc.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"退出码 {proc.returncode}"


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _has_lyric(payload: object) -> bool:
    """响应里有没有任何一份非空歌词（不判断占位串，那是 lyrics 模块的事）。

    不导入 :mod:`ncmgui.lyrics`：后者反向依赖本模块的 ``VARIANT_RE``，互相
    import 会成环。
    """
    if not isinstance(payload, dict):
        return False
    for key in ("lrc", "yrc", "tlyric"):
        node = payload.get(key)
        if isinstance(node, dict):
            text = node.get("lyric") or ""
        elif isinstance(node, str):
            text = node
        else:
            text = ""
        if str(text).strip():
            return True
    return False


def _selftest() -> int:
    """不需要网络，只验证命名规则、目标解析与查重逻辑。"""
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}\n    期望 {want!r}\n    实际 {got!r}")

    # ---- 命名规则：必须和 ncmctl 的 Filename(x, "_") 一致
    check("filename_safe 替换非法字符", filename_safe('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
    check("filename_safe 先 trim 再替换", filename_safe("a/b "), "a_b")
    check("filename_safe 保留中文", filename_safe("孤勇者"), "孤勇者")
    check("song_base 单艺术家", song_base("孤勇者", ["陈奕迅"]), "陈奕迅 - 孤勇者")
    check("song_base 多艺术家", song_base("X", ["A", "B"]), "A,B - X")
    check("song_base 无艺术家", song_base("X", []), " - X")

    # ---- 下载进度的付费标注
    tagger = FeeTagger([
        Song(1, "起风了", ("周深",), fee=1),
        Song(2, "原恒星", ("WOVOP", "薛钦", "洛天依"), fee=8),
        Song(3, "宇宙一隅的我", ("长长长安pwp", "洛天依", "星尘"), fee=8),
        Song(4, "Canon in D", ("Johann Pachelbel", "Chris Snelling"), fee=1),
        Song(5, "青花瓷 [Live]", ("李文豹",), fee=1),
        Song(6, "免费歌", ("甲",), fee=0),
    ])
    for name, line, want in (
        (
            "标注：精确命中",
            "周深 - 起风了                    [-----------↗           ] 36.42 MiB/37.43 MiB  97.29%",
            True,
        ),
        (
            "标注：歌手分隔符的空格差异不算数",
            "WOVOP, 薛钦, 洛天依 - 原恒星     [--------] 10.99 MiB/24.29 MiB  45.26%",
            True,
        ),
        (
            "标注：标题被 pb 截成「前缀...」",
            "长长长安pwp,洛天依,星尘 - 宇宙一... [-----] 25.25 MiB/30.07 MiB  83.96%",
            True,
        ),
        (
            "标注：截断到只剩「歌手 - ...」",
            "Johann Pachelbel, Chris Snelling - ... [----] 16.40 MiB/16.40 MiB 100.00%",
            True,
        ),
        (
            "标注：歌名自己带方括号也不会错位",
            "李文豹 - 青花瓷 [Live]           [--------] 1.00 MiB/2.00 MiB  50.00%",
            True,
        ),
        (
            "标注：免费歌不加标记",
            "甲 - 免费歌                      [--------] 1.00 MiB/2.00 MiB  50.00%",
            False,
        ),
        ("标注：普通日志行不受影响", "report total: 172 success: 172 failed: 0", False),
    ):
        got = tagger(line)
        if want:
            check(name, got is not None and got.startswith("[") and line in got, True)
        else:
            check(name, got is None, True)

    # ---- 目标解析
    check("解析纯数字", parse_target("2161154646"), ("song", 2161154646))
    check(
        "解析歌曲链接",
        parse_target("https://music.163.com/song?id=1820944399&x=1"),
        ("song", 1820944399),
    )
    check(
        "解析歌单链接",
        parse_target("https://music.163.com/#/playlist?id=593617579"),
        ("playlist", 593617579),
    )
    check("解析专辑链接", parse_target("https://music.163.com/album?id=32311"), ("album", 32311))
    check("解析歌手链接", parse_target("https://music.163.com/artist?id=6452"), ("artist", 6452))
    for bad in ("", "随便一段话", "https://music.163.com/song", "https://example.com/song?id=1"):
        try:
            parse_target(bad)
        except ResolveError:
            pass
        else:
            failures.append(f"parse_target({bad!r}) 应当抛出 ResolveError")

    # ---- 输出目录查重
    with tempfile.TemporaryDirectory(prefix="ncmgui-selftest-") as tmp:
        root = Path(tmp)
        (root / "陈奕迅 - 孤勇者.flac").write_bytes(b"")
        (root / "A,B - X(1).mp3").write_bytes(b"")          # ncmctl 的重名变体
        (root / "无艺术家.mp3").write_bytes(b"")
        (root / "真的 - 叫(1).flac").write_bytes(b"")        # 歌名本身带 (1)
        (root / "子目录").mkdir()

        known = existing_bases(root)
        check("扫描到普通文件", "陈奕迅 - 孤勇者" in known, True)
        check("扫描到 (N) 变体", "A,B - X" in known, True)
        check("忽略子目录", "子目录" not in known, True)
        check("歌名自带 (1) 保留原样", "真的 - 叫(1)" in known, True)

        songs = [
            Song(1, "孤勇者", ("陈奕迅",)),            # 已存在
            Song(2, "X", ("A", "B")),                  # 已存在（(1) 变体）
            Song(3, "Y", ("A", "B"), fee=1),           # 待下载，VIP
            Song(4, "叫(1)", ("真的",)),                # 已存在（原名匹配）
        ]
        missing = [s for s in songs if s.base not in known]
        present = [s for s in songs if s.base in known]
        check("待下载列表", [s.id for s in missing], [3])
        check("已存在列表", sorted(s.id for s in present), [1, 2, 4])

        plan = Plan(
            songs=songs, missing=missing, present=present, output=root,
            warnings=["https://x —— 解析失败"], label="测试",
        )
        check("Plan.total", plan.total, 4)
        check("Plan.report 含关键数字", "待下载 1 首" in plan.report(), True)
        check("Plan.details 含歌名", "A, B - Y" in plan.details(), True)

    # ---- 付费标记
    free, vip, album, low = (
        Song(1, "免费歌", ("甲",), fee=0),
        Song(2, "会员歌", ("乙",), fee=1),
        Song(3, "专辑歌", ("丙",), fee=4),
        Song(4, "试听歌", ("丁",), fee=8),
    )
    check("fee=0 不算付费", free.is_paid, False)
    check("free 无标记", free.fee_label, "")
    check("free.marked 原样", free.marked, "甲 - 免费歌")
    check("fee=1 → VIP", vip.fee_label, "VIP")
    check("fee=1 列表短标记", vip.fee_tag, "VIP")
    check("fee=4 列表短标记", album.fee_tag, "EP")
    check("fee=8 列表不占标记", low.fee_tag, "")
    check("fee=4 → 付费专辑", album.fee_label, "付费专辑")
    check("fee=8 → VIP音质", low.fee_label, "VIP音质")
    check("未知非零 fee 兜底为 VIP", Song(5, "x", fee=99).fee_label, "VIP")
    check("marked 带方括号标记", vip.marked, "[VIP] 乙 - 会员歌")

    paid_plan = Plan(
        songs=[free, vip, album, low],
        missing=[vip, album],
        present=[free, low],
        warnings=[],
    )
    check("Plan.paid 只收付费的", [s.id for s in paid_plan.paid], [2, 3, 4])
    check("Plan.paid_missing 只看待下载",
          [s.id for s in paid_plan.paid_missing], [2, 3])
    check("report 提示待下载里几首要会员",
          "待下载中 2 首需会员" in paid_plan.report(), True)
    check("已存在的不算进「需会员」那句",
          Plan(songs=[free, low], missing=[], present=[free, low]).report().count("需会员"), 0)
    check("非会员时语气变重",
          "当前不是会员，多半会失败" in Plan(
              songs=[free, vip], missing=[vip], present=[free], vip=False
          ).report(), True)
    check("是会员时不吓唬人",
          "多半会失败" not in Plan(
              songs=[free, vip], missing=[vip], present=[free], vip=True
          ).report(), True)
    text = paid_plan.details()
    check("明细里待下载带标记", "[VIP] 乙 - 会员歌" in text, True)
    check("明细里已存在也带标记", "[VIP音质] 丁 - 试听歌" in text, True)
    # 这份计划里三种标记都有，图例就该三条
    legend = text.split("标记含义：")[1]
    check("图例覆盖出现过的三种标记",
          all(mark in legend for mark in ("[VIP]", "[付费专辑]", "[VIP音质]")), True)
    # 只有 VIP 的计划，图例不该捎带上没出现过的类型
    vip_only = Plan(songs=[free, vip], missing=[vip]).details().split("标记含义：")[1]
    check("图例不列没出现过的标记",
          "[VIP]" in vip_only and "付费专辑" not in vip_only, True)
    check("全免费时不出现图例", "标记含义" not in Plan(songs=[free]).details(), True)
    check("全免费时 report 不提会员", "需会员" not in Plan(songs=[free]).report(), True)

    # ---- 列表页需要的附加字段（都带默认值，老调用点不受影响）
    plain = Song(7, "晴天", ("周杰伦",))
    check("album 默认空串", plain.album, "")
    check("duration 默认 0", plain.duration, 0)
    check("没有时长显示 --:--", plain.duration_text, "--:--")
    check("时长格式化", Song(8, "x", duration=222000).duration_text, "03:42")
    check("超过一小时", Song(9, "x", duration=3661000).duration_text, "1:01:01")
    check(
        "搜索文本含歌名歌手专辑",
        Song(10, "晴天", ("周杰伦",), album="叶惠美").search_text,
        "晴天 周杰伦 叶惠美",
    )

    # SongDetail 不保证顺序；列表序号和上一首/下一首都按歌单顺序来
    a, b, c = Song(1, "a"), Song(2, "b"), Song(3, "c")
    check("按歌单顺序重排", [s.id for s in order_songs([3, 1, 2], [a, b, c])], [3, 1, 2])
    check("缺失的 id 跳过", [s.id for s in order_songs([1, 99, 2], [a, b])], [1, 2])
    check("重复 id 保留两次", [s.id for s in order_songs([1, 1], [a])], [1, 1])
    check("空输入", order_songs([], []), [])

    # ---- 播放直链：失败码在 data.code 里，顶层 code 仍是 200
    stream = parse_song_url(
        {
            "code": 200,
            "data": {
                "code": 200,
                "url": "https://m801.music.126.net/x.flac",
                "level": "exhigh",
                "expi": 1200,
            },
        }
    )
    check("直链 URL", stream.url, "https://m801.music.126.net/x.flac")
    check("直链音质", stream.level, "exhigh")
    check("直链有效期", stream.expi, 1200)
    check("不是试听", stream.trial, False)

    trial = parse_song_url(
        {"code": 200, "data": {"code": 200, "url": "u", "freeTrialInfo": {"start": 0}}}
    )
    check("freeTrialInfo 标记为试听", trial.trial, True)
    check("expi 缺省值", trial.expi, 600)

    for code, needle in (
        (404, "下架"),
        (-103, "频繁"),
        (-105, "VIP"),
        (-110, "无权"),
        (301, "登录"),
    ):
        try:
            parse_song_url({"code": 200, "data": {"code": code, "url": ""}})
        except SongUrlError as exc:
            check(f"code={code} 的提示", needle in str(exc), True)
            check(f"code={code} 保留原始码", exc.code, code)
        else:
            failures.append(f"data.code={code} 应当抛 SongUrlError")

    for code in (-103, 404):
        try:
            parse_song_url({"code": 200, "data": {"code": code, "url": ""}})
        except SongUrlError as exc:
            check(f"code={code} 是否可重试", exc.retryable, code == -103)

    try:
        parse_song_url({"code": 200, "data": {"code": 200, "url": ""}})
    except SongUrlError as exc:
        check("空 URL 也报错", "播放地址" in str(exc), True)
    else:
        failures.append("空 URL 应当抛 SongUrlError")

    try:
        parse_song_url({"code": 200, "data": None})
    except SongUrlError:
        pass
    else:
        failures.append("data 不是对象时应当抛 SongUrlError")

    # ---- song_url 的请求形状：ids 是数组，level 原样传
    class _FakeResolver(Resolver):
        def __init__(self) -> None:  # 跳过父类的 ncmctl 依赖
            self.calls: list[tuple[str, dict]] = []

        def _curl(self, method: str, data: dict) -> dict:
            self.calls.append((method, data))
            return {
                "code": 200,
                "data": [{"code": 200, "url": "u", "level": "standard", "expi": 1200}],
            }

    fake = _FakeResolver()
    result = fake.song_url(123, "lossless")
    check("song_url 调用的方法", fake.calls[0][0], "SongPlayerV1")
    check("song_url 的 ids 是整数数组", fake.calls[0][1]["ids"], [123])
    check("song_url 透传音质", fake.calls[0][1]["level"], "lossless")
    check("song_url 返回 URL", result.url, "u")
    check("song_url 返回过期时间", result.expi, 1200)

    # ---- 播放地址解析：数组空/元素失败/试听判定
    for name, payload in (
        ("data 不是数组", {"code": 200, "data": {}}),
        ("data 为空数组", {"code": 200, "data": []}),
        ("元素不是对象", {"code": 200, "data": ["x"]}),
    ):
        try:
            parse_player_url(payload)
        except SongUrlError:
            pass
        else:
            failures.append(f"{name} 时应当抛 SongUrlError")
    try:
        parse_player_url({"code": 200, "data": [{"code": 404, "url": ""}]})
    except SongUrlError as exc:
        check("播放地址失败码翻译", exc.code, 404)
    else:
        failures.append("元素 code=404 时应当抛 SongUrlError")
    # 没有 URL 时 fee 决定提示文案：1=会员单曲，4=付费专辑
    for fee, needle in ((1, "VIP"), (4, "专辑")):
        try:
            parse_player_url(
                {"code": 200, "data": [{"code": 200, "url": "", "fee": fee}]}
            )
        except SongUrlError as exc:
            check(f"无地址 fee={fee} 提示", needle in str(exc), True)
        else:
            failures.append(f"无地址 fee={fee} 时应当抛 SongUrlError")
    check(
        "freeTrialInfo 的 0..0 不算试听",
        parse_player_url(
            {"code": 200, "data": [{"code": 200, "url": "u",
                                    "freeTrialInfo": {"start": 0, "end": 0}}]}
        ).trial,
        False,
    )
    check(
        "freeTrialInfo 有区间才算试听",
        parse_player_url(
            {"code": 200, "data": [{"code": 200, "url": "u",
                                    "freeTrialInfo": {"start": 0, "end": 30000}}]}
        ).trial,
        True,
    )

    # ---- 歌词：LyricV1 优先，拿不到逐字数据时回退旧 Lyric
    class _FakeLyric(Resolver):
        def __init__(self, v1_result=None, v1_error=False) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.v1_result = v1_result
            self.v1_error = v1_error

        def _curl(self, method: str, data: dict) -> dict:
            self.calls.append((method, data))
            if method == "LyricV1":
                if self.v1_error:
                    raise ResolveError("接口不支持")
                return self.v1_result  # type: ignore[return-value]
            return {"lrc": {"lyric": "[00:01.00]旧接口"}}

    v1_payload = {
        "lrc": {"lyric": "[00:01.00]逐字歌"},
        "yrc": {"lyric": "[1000,2000](1000,500,0)逐(1500,500,0)字"},
        "tlyric": {"lyric": "[00:01.00]译文"},
    }
    fake = _FakeLyric(v1_payload)
    got = fake.lyric(42)
    check("歌词优先调 LyricV1", [call[0] for call in fake.calls], ["LyricV1"])
    check("歌词 id 是整数", fake.calls[0][1]["id"], 42)
    check("LyricV1 有数据时直接用", got is v1_payload, True)

    fake = _FakeLyric({"lrc": {"lyric": ""}, "yrc": {"lyric": ""}, "tlyric": {"lyric": ""}})
    got = fake.lyric(43)
    check("LyricV1 全空时回退旧接口",
          [call[0] for call in fake.calls], ["LyricV1", "Lyric"])
    check("回退后拿到旧接口歌词", got["lrc"]["lyric"], "[00:01.00]旧接口")

    fake = _FakeLyric(v1_error=True)
    got = fake.lyric(44)
    check("LyricV1 报错时回退旧接口",
          [call[0] for call in fake.calls], ["LyricV1", "Lyric"])
    check("报错回退后仍拿到歌词", got["lrc"]["lyric"], "[00:01.00]旧接口")

    check("_has_lyric 认字符串字段",
          _has_lyric({"lrc": "[00:01.00]x"}), True)
    check("_has_lyric 忽略空字段",
          _has_lyric({"lrc": {"lyric": "  "}, "yrc": {"lyric": ""}}), False)

    # ---- 目录不存在时不应抛错
    check("不存在的目录返回空集合", existing_bases("/nonexistent/nope"), set())

    # ---- 时长解析
    check("时长 30s", _duration_seconds("30s"), 30.0)
    check("时长 500ms", _duration_seconds("500ms"), 0.5)
    check("时长非法回退", _duration_seconds("随便"), 30.0)

    # ---- curl 参数：响应写文件，避免和日志混在 stdout 里
    argv = ncmctl.build_curl("GetUserInfo", "weapi", "{}", "/tmp/r.json", "15s")
    check("curl 带 --output", argv[argv.index("--output") + 1], "/tmp/r.json")
    check("curl 方法名在最后", argv[-1], "GetUserInfo")

    # ---- 取歌词的入口：没有 ncmctl 时应当报明确的错，而不是抛个别的异常出去
    try:
        Resolver("").lyric(1)
    except ResolveError:
        pass
    else:
        failures.append("没有 ncmctl 时 Resolver.lyric 应当抛 ResolveError")

    if failures:
        print(f"解析器自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("解析器自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
