"""Subsonic / Navidrome / Airsonic / Gonic / LMS 适配器。

认证用 ``t = md5(password + salt)``，密码本身不出现在 URL 里。所有请求都带
``f=json&c=NCMStudio&v=1.16.1``；返回结构按 Subsonic 1.16.1 的
``subsonic-response`` 解析。
"""

from __future__ import annotations

import hashlib
import secrets
import urllib.parse

from .base import (
    HttpMixin,
    StreamAlbum,
    StreamArtist,
    StreamError,
    StreamPlaylist,
    StreamSource,
    StreamTrack,
    join_names,
)

API_VERSION = "1.16.1"
CLIENT_NAME = "NCMStudio"


def _text(value) -> str:
    return str(value or "").strip()


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class SubsonicAdapter(HttpMixin):
    kind = "subsonic"
    kind_label = "Subsonic / Navidrome"

    def __init__(self, server: dict) -> None:
        self.server = dict(server)
        self.base_url = _text(server.get("url"))
        self.username = _text(server.get("username"))
        self.password = str(server.get("password") or "")
        self._salt = secrets.token_hex(6)
        if not self.base_url:
            raise StreamError("服务器地址为空")
        if not self.username:
            raise StreamError("用户名为空")

    def _auth(self) -> dict:
        token = hashlib.md5((self.password + self._salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": self._salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    def _call(self, view: str, **params):
        query = self._auth()
        query.update(params)
        payload = self._json(f"/rest/{view}.view", query)
        root = payload.get("subsonic-response") if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            raise StreamError("返回里没有 subsonic-response，地址可能不是 Subsonic 服务")
        if str(root.get("status") or "").lower() == "failed":
            error = root.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else ""
            raise StreamError(f"服务器拒绝：{message or '未知错误'}")
        return root

    # ------------------------------------------------------------ 探测

    def ping(self) -> str:
        root = self._call("ping")
        version = _text(root.get("version")) or API_VERSION
        return f"Subsonic {version}"

    # ------------------------------------------------------------ 浏览

    def _track(
        self,
        item: dict,
        *,
        album: str = "",
        album_id: str = "",
        cover: str = "",
        artist: str = "",
    ) -> StreamTrack | None:
        track_id = _text(item.get("id"))
        if not track_id:
            return None
        artists = join_names(item.get("artist")) or ((artist,) if artist else ())
        return StreamTrack(
            id=track_id,
            name=_text(item.get("title")) or "未知曲目",
            artists=artists,
            album=_text(item.get("album")) or album,
            album_id=_text(item.get("albumId")) or album_id,
            duration=_int(item.get("duration")),
            cover_id=_text(item.get("coverArt")) or cover or album_id,
            artist_id=_text(item.get("artistId")),
        )

    def songs(self, limit: int = 200, offset: int = 0) -> list[StreamTrack]:
        """随机歌曲。

        Subsonic 没有「列出全部歌曲」的视图（``getAlbumList2`` 只给专辑），
        逐张展开专辑会发几十个请求、首屏明显卡顿。这里改用一次
        ``getRandomSongs``，把「歌曲」Tab 当作随机浏览；专辑页仍按专辑展开。
        """
        root = self._call("getRandomSongs", size=max(1, min(500, int(limit))))
        items = ((root.get("randomSongs") or {}).get("song")) or []
        result: list[StreamTrack] = []
        for item in items:
            if isinstance(item, dict):
                track = self._track(item)
                if track is not None:
                    result.append(track)
        return result[offset:offset + limit]

    def albums(self, limit: int = 200, offset: int = 0) -> list[StreamAlbum]:
        root = self._call(
            "getAlbumList2",
            type="alphabeticalByName",
            size=max(1, min(500, int(limit))),
            offset=max(0, int(offset)),
        )
        items = ((root.get("albumList2") or {}).get("album")) or []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            album_id = _text(item.get("id"))
            if not album_id:
                continue
            result.append(
                StreamAlbum(
                    id=album_id,
                    name=_text(item.get("name")) or "未知专辑",
                    artists=join_names(item.get("artist")),
                    year=_int(item.get("year")),
                    cover_id=_text(item.get("coverArt")) or album_id,
                )
            )
        return result

    def artists(self, limit: int = 500, offset: int = 0) -> list[StreamArtist]:
        root = self._call("getArtists")
        indexes = ((root.get("artists") or {}).get("index")) or []
        result = []
        for index in indexes:
            if not isinstance(index, dict):
                continue
            for item in index.get("artist") or []:
                if not isinstance(item, dict):
                    continue
                artist_id = _text(item.get("id"))
                if artist_id:
                    result.append(
                        StreamArtist(artist_id, _text(item.get("name")) or "未知歌手")
                    )
        return result[offset:offset + limit]

    def album_songs(self, album_id: str) -> list[StreamTrack]:
        if not album_id:
            return []
        root = self._call("getAlbum", id=album_id)
        album = root.get("album") or {}
        album_name = _text(album.get("name"))
        album_artist = _text(album.get("artist"))
        cover = _text(album.get("coverArt")) or album_id
        result = []
        for item in album.get("song") or []:
            if not isinstance(item, dict):
                continue
            track_id = _text(item.get("id"))
            if not track_id:
                continue
            artists = join_names(item.get("artist")) or (
                (album_artist,) if album_artist else ()
            )
            result.append(
                StreamTrack(
                    id=track_id,
                    name=_text(item.get("title")) or "未知曲目",
                    artists=artists,
                    album=_text(item.get("album")) or album_name,
                    album_id=album_id,
                    duration=_int(item.get("duration")),
                    cover_id=_text(item.get("coverArt")) or cover,
                    artist_id=_text(item.get("artistId")),
                )
            )
        return result

    def artist_albums(self, artist_id: str) -> list[StreamAlbum]:
        if not artist_id:
            return []
        root = self._call("getArtist", id=artist_id)
        artist = root.get("artist") or {}
        name = _text(artist.get("name"))
        result = []
        for item in artist.get("album") or []:
            if not isinstance(item, dict):
                continue
            album_id = _text(item.get("id"))
            if not album_id:
                continue
            result.append(
                StreamAlbum(
                    id=album_id,
                    name=_text(item.get("name")) or "未知专辑",
                    artists=(name,) if name else (),
                    year=_int(item.get("year")),
                    cover_id=_text(item.get("coverArt")) or album_id,
                )
            )
        return result

    def playlists(self, limit: int = 200, offset: int = 0) -> list[StreamPlaylist]:
        root = self._call("getPlaylists")
        items = ((root.get("playlists") or {}).get("playlist")) or []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            playlist_id = _text(item.get("id"))
            if not playlist_id:
                continue
            result.append(
                StreamPlaylist(
                    id=playlist_id,
                    name=_text(item.get("name")) or "未命名歌单",
                    owner=_text(item.get("owner")),
                    song_count=_int(item.get("songCount")),
                )
            )
        return result[offset:offset + limit]

    def playlist_songs(self, playlist_id: str) -> list[StreamTrack]:
        if not playlist_id:
            return []
        root = self._call("getPlaylist", id=playlist_id)
        playlist = root.get("playlist") or {}
        result = []
        for item in playlist.get("entry") or []:
            if not isinstance(item, dict):
                continue
            track = self._track(item)
            if track is not None:
                result.append(track)
        return result

    def search(self, query: str, limit: int = 50) -> list[StreamTrack]:
        query = (query or "").strip()
        if not query:
            return []
        root = self._call(
            "search3", query=query, songCount=max(1, min(200, limit)),
            albumCount=0, artistCount=0,
        )
        items = (((root.get("searchResult3") or {}).get("song")) or [])
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            track_id = _text(item.get("id"))
            if not track_id:
                continue
            result.append(
                StreamTrack(
                    id=track_id,
                    name=_text(item.get("title")) or "未知曲目",
                    artists=join_names(item.get("artist")),
                    album=_text(item.get("album")),
                    album_id=_text(item.get("albumId")),
                    duration=_int(item.get("duration")),
                    cover_id=_text(item.get("coverArt")) or _text(item.get("albumId")),
                    artist_id=_text(item.get("artistId")),
                )
            )
        return result

    # ------------------------------------------------------------ 播放

    def stream_url(self, track: StreamTrack) -> StreamSource:
        query = self._auth()
        query.update({"id": track.id, "format": "raw"})
        url = self.base_url.rstrip("/") + "/rest/stream.view?" + urllib.parse.urlencode(query)
        return StreamSource(url=url, level="流媒体（原始格式）")

    def cover_url(self, cover_id: str, size: int = 300) -> str:
        if not cover_id:
            return ""
        query = self._auth()
        query.update({"id": cover_id, "size": str(max(64, min(1024, size)))})
        return self.base_url.rstrip("/") + "/rest/getCoverArt.view?" + urllib.parse.urlencode(query)


def _selftest() -> int:
    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    adapter = SubsonicAdapter(
        {"url": "https://demo.example", "username": "u", "password": "p"}
    )
    token = adapter._auth()["t"]
    check(
        "认证 token 是 md5",
        token,
        hashlib.md5(("p" + adapter._salt).encode()).hexdigest(),
    )
    check("token 不含明文密码", "p" not in token, True)

    root = {
        "albumList2": {
            "album": [
                {"id": "a1", "name": "专辑", "artist": "歌手", "year": 2020,
                 "coverArt": "c1"}
            ]
        }
    }
    original = adapter._call
    adapter._call = lambda view, **params: root  # type: ignore[method-assign]
    albums = adapter.albums()
    check("专辑数量", len(albums), 1)
    check("专辑名", albums[0].name, "专辑")
    check("专辑年份", albums[0].year, 2020)
    adapter._call = original  # type: ignore[method-assign]

    def fake_call(view: str, **params):
        if view == "getPlaylists":
            return {"playlists": {"playlist": [
                {"id": "p1", "name": "夜间歌单", "owner": "me", "songCount": 2}
            ]}}
        return {"playlist": {"entry": [
            {"id": "s1", "title": "歌一", "artist": "甲", "duration": 120}
        ]}}

    adapter._call = fake_call  # type: ignore[method-assign]
    playlists = adapter.playlists()
    check("歌单数量", len(playlists), 1)
    check("歌单名", playlists[0].name, "夜间歌单")
    check("歌单曲目", len(adapter.playlist_songs("p1")), 1)
    adapter._call = original  # type: ignore[method-assign]

    source = adapter.stream_url(StreamTrack("9", "歌"))
    check("播放地址含 stream", "/rest/stream.view" in source.url, True)
    check("播放地址不含密码", "password" not in source.url, True)
    cover = adapter.cover_url("c1")
    check("封面地址含 getCoverArt", "/rest/getCoverArt.view" in cover, True)

    if failures:
        print(f"Subsonic 自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Subsonic 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
