"""网易云响应到本程序模型的归一化。

接口字段随客户端版本漂移：同一首歌在云搜索里是 ``al``/``ar``/``dt``，在云盘
里可能是 ``simpleSong``，在旧接口里又是 ``album``/``artists``/``duration``。
页面不应该到处写 ``or`` 链，所以集中在这里做一次，缺字段就退回空值。

只做只读转换，不发请求。自检用几段真实响应的缩样，不联网::

    .venv/bin/python -m ncmgui.api.models
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..resolver import Song


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Artist:
    id: int
    name: str
    pic_url: str = ""
    alias: tuple[str, ...] = ()
    album_size: int = 0
    fans: int = 0
    brief: str = ""
    #: 当前账号是否已关注（未登录时接口不给，保持 False）
    followed: bool = False

    @property
    def subtitle(self) -> str:
        return " / ".join(self.alias) if self.alias else f"{self.album_size} 张专辑"


@dataclass(frozen=True)
class Album:
    id: int
    name: str
    pic_url: str = ""
    artists: tuple[Artist, ...] = ()
    publish_time: int = 0
    size: int = 0
    description: str = ""
    company: str = ""
    #: 当前账号是否已收藏
    subscribed: bool = False

    @property
    def artist_text(self) -> str:
        return " / ".join(artist.name for artist in self.artists)


@dataclass(frozen=True)
class Playlist:
    id: int
    name: str
    cover_url: str = ""
    creator: str = ""
    creator_id: int = 0
    track_count: int = 0
    play_count: int = 0
    description: str = ""
    subscribed: bool = False
    special_type: int = 0
    update_time: int = 0

    @property
    def own(self) -> bool:
        """``special_type=5`` 是「我喜欢的音乐」，不能删除/改名。"""
        return self.special_type == 5

    @property
    def subtitle(self) -> str:
        if self.creator:
            return f"{self.creator} · {self.track_count} 首"
        return f"{self.track_count} 首"


@dataclass(frozen=True)
class Comment:
    id: int
    user: str
    content: str
    user_id: int = 0
    avatar: str = ""
    #: 点赞数。网页端叫 ``likedCount``；``liked_by_me`` 才是「我点过赞」
    liked: int = 0
    time: int = 0
    reply: str = ""
    ip: str = ""
    liked_by_me: bool = False
    reply_count: int = 0
    thread_id: str = ""
    #: 被回复的评论 ID；0 表示直接评论
    parent_id: int = 0
    #: 楼中楼回复里被回复者，例如「回复 张三：」里的张三
    reply_user: str = ""

    @property
    def time_text(self) -> str:
        if not self.time:
            return ""
        from datetime import datetime

        return datetime.fromtimestamp(self.time / 1000).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class DislikeReason:
    """漫游「不感兴趣」菜单里的一项。

    ``type`` 是 ``fixed``（推荐重复 / 推荐不感兴趣）或 ``artist``
    （屏蔽歌手：某某）。屏蔽歌手时 ``artist_id`` 才有值。
    """

    reason: str
    kind: str = "fixed"
    artist_id: int = 0

    @property
    def is_artist(self) -> bool:
        return self.kind == "artist" and self.artist_id > 0


def dislike_reason_from_api(item: dict | None) -> DislikeReason | None:
    item = _dict(item)
    reason = _text(item.get("reason")).strip()
    if not reason:
        return None
    kind = _text(item.get("type")).strip() or "fixed"
    return DislikeReason(
        reason=reason,
        kind=kind,
        artist_id=_int(item.get("id")) if kind == "artist" else 0,
    )


@dataclass(frozen=True)
class CloudSong:
    song: Song
    file_name: str = ""
    size: int = 0
    upload_time: int = 0

    @property
    def subtitle(self) -> str:
        return self.file_name or self.song.display


@dataclass(frozen=True)
class Podcast:
    """播客专辑。网页端叫 voicelist / djradio，两者共用 ``djradio`` 的 ID。"""

    id: int
    name: str
    cover_url: str = ""
    creator: str = ""
    description: str = ""
    program_count: int = 0
    sub_count: int = 0
    play_count: int = 0
    subscribed: bool = False

    @property
    def subtitle(self) -> str:
        parts = []
        if self.creator:
            parts.append(self.creator)
        if self.program_count:
            parts.append(f"{self.program_count} 期")
        return " · ".join(parts)


@dataclass(frozen=True)
class Program:
    """播客里的一期节目。``song`` 是它对应的声音（可直接交给播放器）。"""

    song: Song
    radio_id: int = 0
    radio_name: str = ""
    cover_url: str = ""
    description: str = ""
    create_time: int = 0
    serial: int = 0
    duration: int = 0

    @property
    def subtitle(self) -> str:
        parts = []
        if self.radio_name:
            parts.append(self.radio_name)
        if self.duration:
            minutes, seconds = divmod(self.duration // 1000, 60)
            parts.append(f"{minutes}:{seconds:02d}")
        return " · ".join(parts)


@dataclass(frozen=True)
class SearchPage:
    kind: str
    items: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    limit: int = 30

    @property
    def has_more(self) -> bool:
        return self.page * self.limit < self.total


@dataclass(frozen=True)
class Chart:
    """排行榜里的一张榜。``id`` 就是它对应的歌单 ID。"""

    id: int
    name: str
    cover_url: str = ""
    #: 「每周五更新」「刚刚更新」这类文案，网页端直接显示
    update_frequency: str = ""
    #: 榜单前三首的 (歌名, 歌手)。只有「榜单推荐 / 官方榜」会带预览
    preview: tuple[tuple[str, str], ...] = ()

    @property
    def subtitle(self) -> str:
        parts = []
        if self.update_frequency:
            parts.append(self.update_frequency)
        if self.preview:
            parts.append(" / ".join(name for name, _artist in self.preview[:3]))
        return " · ".join(parts)


@dataclass(frozen=True)
class ChartGroup:
    """榜单分组，例如「官方榜」「曲风榜」。"""

    name: str
    charts: tuple[Chart, ...] = ()


@dataclass(frozen=True)
class Mv:
    """音乐视频。``id`` 是 MV 自己的 ID，不是歌曲 ID。"""

    id: int
    name: str
    artist: str = ""
    cover_url: str = ""
    play_count: int = 0
    #: 时长，**毫秒**
    duration: int = 0
    description: str = ""
    publish_time: str = ""

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(self.duration // 1000, 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def subtitle(self) -> str:
        parts = [part for part in (self.artist, self.duration_text) if part]
        return " · ".join(parts)


@dataclass(frozen=True)
class UserProfile:
    """用户主页的头部资料。"""

    id: int
    name: str
    avatar: str = ""
    signature: str = ""
    follows: int = 0
    followeds: int = 0
    level: int = 0
    listen_songs: int = 0
    vip: bool = False
    #: 当前账号是否已关注 TA
    followed: bool = False
    #: 对方是否公开听歌记录；不公开时「听歌排行」显示提示
    record_visible: bool = True

    @property
    def subtitle(self) -> str:
        parts = []
        if self.level:
            parts.append(f"LV.{self.level}")
        if self.vip:
            parts.append("VIP")
        parts.append(f"关注 {self.follows}")
        parts.append(f"粉丝 {self.followeds}")
        if self.listen_songs:
            parts.append(f"累计听歌 {self.listen_songs} 首")
        return " · ".join(parts)


@dataclass(frozen=True)
class UserNote:
    """用户主页「笔记」里的一条动态。

    网易云把这叫 event（type=18），正文在 ``json.msg`` 里，可能带一首歌或
    若干张图片；没有正文的纯转发也会出现在列表里。
    """

    id: int
    text: str
    time: int = 0                      # 毫秒时间戳
    song: Song | None = None
    images: tuple[str, ...] = ()
    liked: int = 0
    comments: int = 0


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

def artist_from_api(item: dict | None) -> Artist:
    item = _dict(item)
    aliases = tuple(_text(alias) for alias in _list(item.get("alias")) if alias)
    return Artist(
        id=_int(item.get("id") or item.get("artistId")),
        name=_text(item.get("name") or item.get("artistName")),
        pic_url=_text(
            item.get("picUrl") or item.get("img1v1Url") or item.get("avatar")
        ),
        alias=aliases,
        album_size=_int(item.get("albumSize")),
        fans=_int(item.get("fans") or item.get("fansCount")),
        brief=_text(item.get("briefDesc") or item.get("briefDesc")),
        followed=bool(item.get("followed")),
    )


def album_from_api(item: dict | None) -> Album:
    item = _dict(item)
    artists = tuple(
        artist_from_api(artist)
        for artist in (_list(item.get("artists")) or _list(item.get("ar")))
        if isinstance(artist, dict)
    )
    return Album(
        id=_int(item.get("id") or item.get("albumId")),
        name=_text(item.get("name") or item.get("albumName")),
        pic_url=_text(item.get("picUrl") or item.get("coverImgUrl")),
        artists=artists,
        publish_time=_int(item.get("publishTime")),
        size=_int(item.get("size")),
        description=_text(item.get("description") or item.get("briefDesc")),
        company=_text(item.get("company")),
        subscribed=bool(item.get("isSub") or item.get("subscribed")),
    )


def playlist_from_api(item: dict | None) -> Playlist:
    item = _dict(item)
    creator = _dict(item.get("creator"))
    return Playlist(
        id=_int(item.get("id")),
        name=_text(item.get("name")),
        cover_url=_text(item.get("coverImgUrl") or item.get("picUrl")),
        creator=_text(creator.get("nickname") or item.get("creatorName")),
        creator_id=_int(creator.get("userId") or item.get("userId")),
        track_count=_int(item.get("trackCount") or item.get("songCount")),
        play_count=_int(item.get("playCount") or item.get("playcount")),
        description=_text(item.get("description")),
        subscribed=bool(item.get("subscribed")),
        special_type=_int(item.get("specialType")),
        update_time=_int(item.get("updateTime")),
    )


def comment_from_api(item: dict | None) -> Comment:
    item = _dict(item)
    user = _dict(item.get("user"))
    replies = _list(item.get("beReplied"))
    reply = ""
    reply_user = ""
    if replies and isinstance(replies[0], dict):
        reply = _text(replies[0].get("content"))
        reply_user = _text(_dict(replies[0].get("user")).get("nickname"))
    thread = _dict(item.get("commentThread"))
    # ``ipLocation`` 是个对象（ip/location/userId），旧接口也可能直接给字符串；
    # 直接 str() 会在界面上显示成一整段字典
    ip_location = item.get("ipLocation")
    if isinstance(ip_location, dict):
        ip_text = _text(ip_location.get("location")) or _text(ip_location.get("ip"))
    else:
        ip_text = _text(ip_location)
    return Comment(
        id=_int(item.get("commentId") or item.get("id")),
        user=_text(user.get("nickname")),
        content=_text(item.get("content")),
        user_id=_int(user.get("userId")),
        avatar=_text(user.get("avatarUrl")),
        liked=_int(item.get("likedCount")),
        time=_int(item.get("time")),
        reply=reply,
        ip=ip_text or _text(item.get("ip")),
        liked_by_me=bool(item.get("liked")),
        reply_count=_int(item.get("replyCount")),
        thread_id=_text(item.get("threadId") or thread.get("id")),
        parent_id=_int(item.get("parentCommentId")),
        reply_user=reply_user,
    )


def podcast_from_api(item: dict | None) -> Podcast | None:
    """统一 ``djRadios``、播客首页 creative、我的播客 voicelist 三种形状。"""
    item = _dict(item)
    ui = _dict(item.get("uiElement"))
    main = _dict(ui.get("mainTitle"))
    image = _dict(ui.get("image"))
    cover = _text(
        item.get("picUrl") or item.get("coverUrl")
        or image.get("imageUrl")
        or _dict(item.get("cover")).get("url")
    )
    # 首页 creative 的 action 形如 ``orpheus://djradio/1217585602``
    action = _text(item.get("action"))
    radio_id = _int(item.get("voiceListId") or item.get("id") or item.get("creativeId"))
    if not radio_id and action.rstrip("/").isdigit():
        radio_id = _int(action.rstrip("/"))
    name = _text(
        item.get("name") or item.get("voiceListName")
        or main.get("title") or item.get("radioName")
    )
    if not radio_id or not name:
        return None
    return Podcast(
        id=radio_id,
        name=name,
        cover_url=cover,
        creator=_text(item.get("userName") or _dict(item.get("dj")).get("nickname")),
        description=_text(item.get("desc") or item.get("description")
                         or _dict(ui.get("subTitle")).get("title")),
        program_count=_int(item.get("programCount") or item.get("voiceCount")),
        sub_count=_int(item.get("subCount")),
        play_count=_int(item.get("playCount")),
        subscribed=bool(item.get("subed") or item.get("subscribed")),
    )


def program_from_api(item: dict | None) -> Program | None:
    item = _dict(item)
    main = _dict(item.get("mainSong"))
    if not main:
        return None
    song = song_from_api(main)
    if song is None:
        return None
    radio = _dict(item.get("radio"))
    return Program(
        song=song,
        radio_id=_int(radio.get("id") or item.get("radioId")),
        radio_name=_text(radio.get("name") or item.get("radioName")),
        cover_url=_text(item.get("coverUrl") or item.get("blurCoverUrl")
                        or _dict(item.get("cover")).get("url")),
        description=_text(item.get("description")),
        create_time=_int(item.get("createTime")),
        serial=_int(item.get("serialNum") or item.get("serial")),
        duration=_int(main.get("duration") or item.get("duration")),
    )


def song_from_api(item: dict | None) -> Song | None:
    """把一首歌的各种响应形状统一成 :class:`~ncmgui.resolver.Song`。"""
    item = _dict(item)
    if not item:
        return None
    if isinstance(item.get("simpleSong"), dict):
        base = dict(item["simpleSong"])
        base.setdefault("id", item.get("songId"))
        item = base

    album = _dict(item.get("al")) or _dict(item.get("album"))
    artists_raw = _list(item.get("ar")) or _list(item.get("artists"))
    artists = tuple(
        _text(artist.get("name"))
        for artist in artists_raw
        if isinstance(artist, dict) and artist.get("name")
    )
    if not artists:
        single = item.get("artist") or item.get("artistName")
        if single:
            artists = (_text(single),)

    song_id = _int(item.get("id") or item.get("songId"))
    name = _text(item.get("name") or item.get("songName") or item.get("title"))
    if not song_id or not name:
        return None

    duration = _int(item.get("dt") or item.get("duration"))
    if not duration and item.get("interval"):
        duration = _int(item.get("interval")) * 1000

    return Song(
        id=song_id,
        name=name,
        artists=artists,
        fee=_int(item.get("fee")),
        album=_text(album.get("name") or item.get("albumName")),
        duration=duration,
        pic_url=_text(
            album.get("picUrl")
            or item.get("picUrl")
            or _dict(item.get("album")).get("picUrl")
        ),
        alg=_text(item.get("alg")),
    )


def songs_from_api(items: Iterable[Any]) -> list[Song]:
    result: list[Song] = []
    for item in items:
        song = song_from_api(item if isinstance(item, dict) else None)
        if song is not None:
            result.append(song)
    return result


def chart_from_api(item: dict | None) -> Chart | None:
    """排行榜条目。``targetType`` 不是 ``PLAYLIST`` 的是 H5 榜单，跳不过去。"""
    item = _dict(item)
    if _text(item.get("targetType")).strip() != "PLAYLIST":
        return None
    chart_id = _int(item.get("id"))
    name = _text(item.get("name")).strip()
    if not chart_id or not name:
        return None
    preview: list[tuple[str, str]] = []
    for row in _list(item.get("tracks"))[:3]:
        row = _dict(row)
        first = _text(row.get("first")).strip()
        if first:
            preview.append((first, _text(row.get("second")).strip()))
    return Chart(
        id=chart_id,
        name=name,
        cover_url=_text(item.get("coverUrl")),
        update_frequency=_text(item.get("updateFrequency")).strip(),
        preview=tuple(preview),
    )


def chart_groups_from_api(payload: dict | None) -> list[ChartGroup]:
    """把 ``toplist/detail/v2`` 的 ``data`` 归一化成分组列表。"""
    groups: list[ChartGroup] = []
    for group in _list(_dict(payload).get("data")):
        group = _dict(group)
        charts = tuple(
            chart for chart in (
                chart_from_api(item if isinstance(item, dict) else None)
                for item in _list(group.get("list"))
            ) if chart is not None
        )
        if charts:
            groups.append(ChartGroup(
                name=_text(group.get("name")).strip() or "榜单",
                charts=charts,
            ))
    return groups


def mv_from_api(item: dict | None) -> Mv | None:
    item = _dict(item)
    mv_id = _int(item.get("id") or item.get("mvId"))
    name = _text(item.get("name")).strip()
    if not mv_id or not name:
        return None
    artists = [
        _text(artist.get("name"))
        for artist in _list(item.get("artists"))
        if isinstance(artist, dict)
    ]
    return Mv(
        id=mv_id,
        name=name,
        artist=_text(item.get("artistName")) or " / ".join(filter(None, artists)),
        cover_url=_text(item.get("cover") or item.get("coverUrl")
                        or item.get("picUrl")),
        play_count=_int(item.get("playCount")),
        duration=_int(item.get("duration")),
        description=_text(item.get("briefDesc") or item.get("description")),
        publish_time=_text(item.get("publishTime")),
    )


def user_from_api(payload: dict | None) -> UserProfile | None:
    """``/weapi/w/v1/user/detail/<uid>`` 的响应：资料在 ``profile``，
    等级和累计听歌数在外层。"""
    data = _dict(payload)
    profile = _dict(data.get("profile")) or data
    uid = _int(profile.get("userId"))
    name = _text(profile.get("nickname")).strip()
    if not uid or not name:
        return None
    return UserProfile(
        id=uid,
        name=name,
        avatar=_text(profile.get("avatarUrl")),
        signature=_text(profile.get("signature")),
        follows=_int(profile.get("follows")),
        followeds=_int(profile.get("followeds")),
        level=_int(data.get("level")),
        listen_songs=_int(data.get("listenSongs")),
        vip=_int(profile.get("vipType")) > 0,
        followed=bool(profile.get("followed")),
        record_visible=bool(data.get("peopleCanSeeMyPlayRecord", True)),
    )


def user_notes_from_api(payload: dict | None) -> list[UserNote]:
    """``/weapi/event/get/<uid>`` 的事件列表，只保留能显示出来的条目。"""
    notes: list[UserNote] = []
    for event in _list(_dict(payload).get("events")):
        event = _dict(event)
        raw = event.get("json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        raw = _dict(raw)
        text = _text(raw.get("msg")).strip()
        images = []
        for pic in _list(event.get("pics")):
            pic = _dict(pic)
            url = _text(pic.get("originUrl") or pic.get("url")
                        or pic.get("squareUrl"))
            if url:
                images.append(url)
        song = song_from_api(raw.get("song"))
        if not text and song is None and not images:
            continue
        info = _dict(event.get("info"))
        notes.append(UserNote(
            id=_int(event.get("id")),
            text=text,
            time=_int(event.get("eventTime")),
            song=song,
            images=tuple(images),
            liked=_int(info.get("likedCount")),
            comments=_int(info.get("commentCount")),
        ))
    return notes


def cloud_song_from_api(item: dict | None) -> CloudSong | None:
    item = _dict(item)
    song = song_from_api(item)
    if song is None:
        return None
    return CloudSong(
        song=song,
        file_name=_text(item.get("fileName")),
        size=_int(item.get("fileSize") or item.get("size")),
        upload_time=_int(item.get("addTime")),
    )


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    search_song = {
        "id": 1901371647,
        "name": "孤勇者",
        "fee": 1,
        "dt": 256000,
        "al": {"id": 1, "name": "孤勇者", "picUrl": "https://p1/x.jpg"},
        "ar": [{"id": 2, "name": "陈奕迅"}],
    }
    song = song_from_api(search_song)
    check("搜索结果歌曲 ID", song.id, 1901371647)
    check("搜索结果歌手", song.artists, ("陈奕迅",))
    check("搜索结果专辑", song.album, "孤勇者")
    check("搜索结果封面", song.pic_url, "https://p1/x.jpg")
    check("搜索结果时长", song.duration, 256000)
    check("漫游来源标记", song.alg, "")

    fm_song = song_from_api({"id": 9, "name": "漫游歌", "alg": "alg_fm_x-default"})
    check("漫游来源标记保留", fm_song.alg, "alg_fm_x-default")

    fixed = dislike_reason_from_api({"reason": "推荐重复", "type": "fixed"})
    check("固定理由", fixed.reason, "推荐重复")
    check("固定理由不屏蔽歌手", fixed.is_artist, False)
    artist_reason = dislike_reason_from_api(
        {"reason": "屏蔽歌手：某某", "id": "34940501", "type": "artist"})
    check("屏蔽歌手 ID", artist_reason.artist_id, 34940501)
    check("屏蔽歌手判定", artist_reason.is_artist, True)
    check("缺少理由返回 None", dislike_reason_from_api({"type": "fixed"}), None)

    old_song = {
        "id": 7,
        "name": "旧接口",
        "duration": 1000,
        "album": {"picUrl": "https://p2/y.jpg", "name": "专辑"},
        "artists": [{"name": "甲"}, {"name": "乙"}],
    }
    song = song_from_api(old_song)
    check("旧接口歌手", song.artists, ("甲", "乙"))
    check("旧接口封面", song.pic_url, "https://p2/y.jpg")

    check("缺 ID 返回 None", song_from_api({"name": "x"}), None)
    check("缺歌名返回 None", song_from_api({"id": 1}), None)
    check("非字典返回 None", song_from_api(None), None)

    artist = artist_from_api({"id": 3, "name": "陈奕迅", "alias": ["Eason"],
                              "albumSize": 40, "picUrl": "p"})
    check("歌手别名", artist.alias, ("Eason",))
    check("歌手副标题", artist.subtitle, "Eason")

    album = album_from_api({"id": 4, "name": "U87", "picUrl": "c",
                            "artists": [{"id": 3, "name": "陈奕迅"}],
                            "publishTime": 1, "size": 10})
    check("专辑歌手", album.artist_text, "陈奕迅")
    check("专辑曲目数", album.size, 10)

    playlist = playlist_from_api({
        "id": 5, "name": "歌单", "coverImgUrl": "c", "trackCount": 12,
        "playCount": 100, "specialType": 5,
        "creator": {"nickname": "我", "userId": 9},
    })
    check("歌单创建者", playlist.creator, "我")
    check("我喜欢的音乐不可编辑", playlist.own, True)
    check("歌单副标题", playlist.subtitle, "我 · 12 首")

    comment = comment_from_api({
        "commentId": 6, "content": "好听", "likedCount": 2, "time": 1700000000000,
        "user": {"nickname": "甲", "userId": 1, "avatarUrl": "a"},
        "beReplied": [{"content": "同意"}], "ipLocation": "上海",
    })
    check("评论内容", comment.content, "好听")
    check("评论回复", comment.reply, "同意")
    check("评论 IP", comment.ip, "上海")

    cloud = cloud_song_from_api({
        "songId": 8, "songName": "云盘歌", "artist": "甲", "album": "专辑",
        "fileName": "a.flac", "fileSize": 123, "addTime": 9,
    })
    check("云盘歌曲", cloud.song.name, "云盘歌")
    check("云盘文件名", cloud.file_name, "a.flac")

    page = SearchPage("song", [1, 2], total=31, page=1, limit=30)
    check("还有下一页", page.has_more, True)
    page = SearchPage("song", [], total=30, page=1, limit=30)
    check("最后一页", page.has_more, False)

    chart = chart_from_api({
        "id": 19723756, "name": "飙升榜", "targetType": "PLAYLIST",
        "coverUrl": "c", "updateFrequency": "刚刚更新",
        "tracks": [{"first": "甲", "second": "乙"},
                   {"first": "丙", "second": "丁"}],
    })
    check("榜单 ID", chart.id, 19723756)
    check("榜单预览", chart.preview, (("甲", "乙"), ("丙", "丁")))
    check("H5 榜单过滤", chart_from_api({
        "id": 0, "name": "实体专辑榜", "targetType": "H5"}), None)
    groups = chart_groups_from_api({"data": [
        {"name": "官方榜", "list": [
            {"id": 1, "name": "飙升榜", "targetType": "PLAYLIST"},
            {"id": 0, "name": "H5", "targetType": "H5"},
        ]},
        {"name": "空组", "list": []},
    ]})
    check("榜单分组数", len(groups), 1)
    check("分组内榜单数", len(groups[0].charts), 1)

    mv = mv_from_api({"id": 9, "name": "晴天", "artistName": "高伟",
                      "cover": "c", "playCount": 3, "duration": 192000})
    check("MV 名称", mv.name, "晴天")
    check("MV 时长", mv.duration_text, "3:12")
    check("MV 缺 ID", mv_from_api({"name": "x"}), None)

    profile = user_from_api({
        "level": 7, "listenSongs": 1274, "peopleCanSeeMyPlayRecord": True,
        "profile": {"userId": 12, "nickname": "甲", "follows": 11,
                    "followeds": 2, "vipType": 11, "followed": True},
    })
    check("用户昵称", profile.name, "甲")
    check("用户等级", profile.level, 7)
    check("用户 VIP", profile.vip, True)
    check("用户已关注", profile.followed, True)
    check("用户副标题", profile.subtitle, "LV.7 · VIP · 关注 11 · 粉丝 2 · 累计听歌 1274 首")
    check("用户缺 ID", user_from_api({"profile": {"nickname": "甲"}}), None)

    notes = user_notes_from_api({"events": [
        {"id": 1, "eventTime": 1700000000000,
         "json": '{"msg": "今天听什么", "song": {"id": 2, "name": "晴天"}}',
         "pics": [], "info": {"likedCount": 3, "commentCount": 1}},
        {"id": 2, "json": '{"msg": "纯文字"}', "pics": []},
        {"id": 3, "json": "{}", "pics": []},
    ]})
    check("笔记条数", len(notes), 2)
    check("笔记正文", notes[0].text, "今天听什么")
    check("笔记关联歌曲", notes[0].song.name, "晴天")
    check("笔记点赞数", notes[0].liked, 3)

    if failures:
        print(f"模型自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("模型自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
