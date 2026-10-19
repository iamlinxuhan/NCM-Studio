"""网易云响应到本程序模型的归一化。

接口字段随客户端版本漂移：同一首歌在云搜索里是 ``al``/``ar``/``dt``，在云盘
里可能是 ``simpleSong``，在旧接口里又是 ``album``/``artists``/``duration``。
页面不应该到处写 ``or`` 链，所以集中在这里做一次，缺字段就退回空值。

只做只读转换，不发请求。自检用几段真实响应的缩样，不联网::

    .venv/bin/python -m ncmgui.api.models
"""

from __future__ import annotations

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
    liked: int = 0
    time: int = 0
    reply: str = ""
    ip: str = ""

    @property
    def time_text(self) -> str:
        if not self.time:
            return ""
        from datetime import datetime

        return datetime.fromtimestamp(self.time / 1000).strftime("%Y-%m-%d %H:%M")


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
class SearchPage:
    kind: str
    items: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    limit: int = 30

    @property
    def has_more(self) -> bool:
        return self.page * self.limit < self.total


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
    if replies and isinstance(replies[0], dict):
        reply = _text(replies[0].get("content"))
    return Comment(
        id=_int(item.get("commentId") or item.get("id")),
        user=_text(user.get("nickname")),
        content=_text(item.get("content")),
        user_id=_int(user.get("userId")),
        avatar=_text(user.get("avatarUrl")),
        liked=_int(item.get("likedCount")),
        time=_int(item.get("time")),
        reply=reply,
        ip=_text(item.get("ipLocation") or item.get("ip")),
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
    )


def songs_from_api(items: Iterable[Any]) -> list[Song]:
    result: list[Song] = []
    for item in items:
        song = song_from_api(item if isinstance(item, dict) else None)
        if song is not None:
            result.append(song)
    return result


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

    if failures:
        print(f"模型自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("模型自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
