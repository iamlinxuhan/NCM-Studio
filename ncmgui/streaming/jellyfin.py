"""Jellyfin / Emby 适配器。

认证优先用配置里的 API Key；没有 Key 时用用户名密码调用
``/Users/AuthenticateByName`` 换 ``AccessToken``（缓存在配置里）。播放与封面地址
都带 ``api_key`` 查询参数，因此 mpv 不需要额外请求头。

Emby 的接口与 Jellyfin 基本一致；若地址是 ``http://host/emby`` 形式，直接把
该前缀当作 base_url 即可。
"""

from __future__ import annotations

import uuid

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
from .. import VERSION

CLIENT_NAME = "NCM Studio"
DEVICE_NAME = "Linux"
#: 报给服务器的客户端版本，跟程序版本保持一致，避免两处各改各的
DEVICE_VERSION = VERSION


def _text(value) -> str:
    return str(value or "").strip()


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


class JellyfinAdapter(HttpMixin):
    kind = "jellyfin"
    kind_label = "Jellyfin / Emby"

    def __init__(self, server: dict) -> None:
        self.server = dict(server)
        self.base_url = _text(server.get("url"))
        self.username = _text(server.get("username"))
        self.password = str(server.get("password") or "")
        self.token = _text(server.get("token"))
        self.user_id = _text(server.get("user_id"))
        self.device_id = _text(server.get("device_id")) or uuid.uuid4().hex
        if not self.base_url:
            raise StreamError("服务器地址为空")

    # ------------------------------------------------------------ 认证

    def _client_header(self, token: str = "") -> str:
        parts = [
            f'Client="{CLIENT_NAME}"',
            f'Device="{DEVICE_NAME}"',
            f'DeviceId="{self.device_id}"',
            f'Version="{DEVICE_VERSION}"',
        ]
        if token:
            parts.append(f'Token="{token}"')
        return "MediaBrowser " + ", ".join(parts)

    def _headers(self) -> dict[str, str]:
        token = self.token
        return {
            "X-Emby-Authorization": self._client_header(token),
            "Accept": "application/json",
        }

    def _login(self) -> None:
        if not self.username:
            raise StreamError("未填写用户名，也没有可用的 API Key")
        headers = {
            "X-Emby-Authorization": self._client_header(),
            "Accept": "application/json",
        }
        payload = self._json(
            "/Users/AuthenticateByName",
            data={"Username": self.username, "Pw": self.password},
            headers=headers,
        )
        token = _text(payload.get("AccessToken"))
        user = payload.get("User") or {}
        user_id = _text(user.get("Id")) if isinstance(user, dict) else ""
        if not token or not user_id:
            raise StreamError("登录响应缺少 AccessToken/UserId")
        self.token = token
        self.user_id = user_id
        # 让 registry 能把换到的 token 写回配置，避免每次启动都登录
        self.server["token"] = token
        self.server["user_id"] = user_id
        self.server["device_id"] = self.device_id

    def _ensure_auth(self) -> None:
        if not self.token:
            self._login()
        if self.user_id:
            return
        # 只填了 API Key 时还不知道用户 ID；``/Users/Me`` 通常可用，
        # 老版本/受限 Key 再退回 ``/Users`` 取第一个。
        try:
            payload = self._json("/Users/Me", headers=self._headers())
            self.user_id = _text(payload.get("Id")) if isinstance(payload, dict) else ""
        except StreamError:
            payload = self._json("/Users", headers=self._headers())
            items = payload.get("Items") if isinstance(payload, dict) else None
            if not items and isinstance(payload, list):
                items = payload
            if isinstance(items, list) and items and isinstance(items[0], dict):
                self.user_id = _text(items[0].get("Id"))
        if not self.user_id:
            raise StreamError("API Key 可用，但没有找到对应的用户 ID")
        self.server["user_id"] = self.user_id
        self.server["token"] = self.token
        self.server["device_id"] = self.device_id

    def _get(self, path: str, params: dict | None = None):
        self._ensure_auth()
        return self._json(path, params, headers=self._headers())

    # ------------------------------------------------------------ 探测

    def ping(self) -> str:
        self._ensure_auth()
        info = self._json("/System/Info", headers=self._headers())
        name = _text(info.get("ServerName")) or "服务器"
        version = _text(info.get("Version"))
        product = _text(info.get("ProductName")) or "Jellyfin"
        return f"{product} {version}（{name}）".replace("  ", " ")

    # ------------------------------------------------------------ 浏览

    def _track(self, item: dict) -> StreamTrack | None:
        track_id = _text(item.get("Id"))
        if not track_id:
            return None
        artists = join_names(item.get("Artists"))
        if not artists:
            artists = join_names(item.get("AlbumArtist"))
        album_id = _text(item.get("AlbumId"))
        cover_id = album_id or track_id
        return StreamTrack(
            id=track_id,
            name=_text(item.get("Name")) or "未知曲目",
            artists=artists,
            album=_text(item.get("Album")),
            album_id=album_id,
            duration=_int(item.get("RunTimeTicks")) // 10_000_000,
            cover_id=cover_id,
            artist_id=(
                _text(item.get("AlbumArtistId"))
                or _text(item.get("ArtistId"))
                or (join_names(item.get("ArtistIds")) or ("",))[0]
            ),
        )

    def songs(self, limit: int = 200, offset: int = 0) -> list[StreamTrack]:
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": max(1, int(limit)),
                "StartIndex": max(0, int(offset)),
                "Fields": "Artists,AlbumId,AlbumArtist,AlbumArtistId,ArtistIds,RunTimeTicks,ImageTags,ParentId",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if isinstance(item, dict):
                track = self._track(item)
                if track is not None:
                    result.append(track)
        return result

    def albums(self, limit: int = 200, offset: int = 0) -> list[StreamAlbum]:
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "Recursive": "true",
                "IncludeItemTypes": "MusicAlbum",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": max(1, int(limit)),
                "StartIndex": max(0, int(offset)),
                "Fields": "AlbumArtist,Artists,ProductionYear,ImageTags",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if not isinstance(item, dict):
                continue
            album_id = _text(item.get("Id"))
            if not album_id:
                continue
            artists = join_names(item.get("AlbumArtist")) or join_names(item.get("Artists"))
            result.append(
                StreamAlbum(
                    id=album_id,
                    name=_text(item.get("Name")) or "未知专辑",
                    artists=artists,
                    year=_int(item.get("ProductionYear")),
                    cover_id=album_id,
                )
            )
        return result

    def artists(self, limit: int = 500, offset: int = 0) -> list[StreamArtist]:
        payload = self._get(
            "/Artists",
            {
                "UserId": self.user_id,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": max(1, int(limit)),
                "StartIndex": max(0, int(offset)),
                "Fields": "ImageTags",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if not isinstance(item, dict):
                continue
            artist_id = _text(item.get("Id"))
            if artist_id:
                result.append(
                    StreamArtist(artist_id, _text(item.get("Name")) or "未知歌手")
                )
        return result

    def album_songs(self, album_id: str) -> list[StreamTrack]:
        if not album_id:
            return []
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "ParentId": album_id,
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
                "Fields": "Artists,AlbumId,AlbumArtist,AlbumArtistId,ArtistIds,RunTimeTicks,ImageTags,ParentId",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if isinstance(item, dict):
                track = self._track(item)
                if track is not None:
                    result.append(track)
        return result

    def artist_albums(self, artist_id: str) -> list[StreamAlbum]:
        if not artist_id:
            return []
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "AlbumArtistIds": artist_id,
                "Recursive": "true",
                "IncludeItemTypes": "MusicAlbum",
                "SortBy": "ProductionYear,SortName",
                "SortOrder": "Descending",
                "Fields": "AlbumArtist,Artists,ProductionYear,ImageTags",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if not isinstance(item, dict):
                continue
            album_id = _text(item.get("Id"))
            if not album_id:
                continue
            result.append(
                StreamAlbum(
                    id=album_id,
                    name=_text(item.get("Name")) or "未知专辑",
                    artists=join_names(item.get("AlbumArtist"))
                    or join_names(item.get("Artists")),
                    year=_int(item.get("ProductionYear")),
                    cover_id=album_id,
                )
            )
        return result

    def playlists(self, limit: int = 200, offset: int = 0) -> list[StreamPlaylist]:
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "Recursive": "true",
                "IncludeItemTypes": "Playlist",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": max(1, int(limit)),
                "StartIndex": max(0, int(offset)),
                "Fields": "ChildCount,ImageTags",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if not isinstance(item, dict):
                continue
            playlist_id = _text(item.get("Id"))
            if not playlist_id:
                continue
            result.append(
                StreamPlaylist(
                    id=playlist_id,
                    name=_text(item.get("Name")) or "未命名歌单",
                    song_count=_int(item.get("ChildCount")),
                )
            )
        return result

    def playlist_songs(self, playlist_id: str) -> list[StreamTrack]:
        if not playlist_id:
            return []
        payload = self._get(
            f"/Playlists/{playlist_id}/Items",
            {
                "UserId": self.user_id,
                "Fields": "Artists,AlbumId,AlbumArtist,AlbumArtistId,ArtistIds,RunTimeTicks,ImageTags,ParentId",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if isinstance(item, dict):
                track = self._track(item)
                if track is not None:
                    result.append(track)
        return result

    def search(self, query: str, limit: int = 50) -> list[StreamTrack]:
        query = (query or "").strip()
        if not query:
            return []
        payload = self._get(
            "/Items",
            {
                "UserId": self.user_id,
                "SearchTerm": query,
                "Recursive": "true",
                "IncludeItemTypes": "Audio",
                "Limit": max(1, min(200, int(limit))),
                "Fields": "Artists,AlbumId,AlbumArtist,AlbumArtistId,ArtistIds,RunTimeTicks,ImageTags,ParentId",
                "EnableImages": "false",
            },
        )
        result = []
        for item in payload.get("Items") or []:
            if isinstance(item, dict):
                track = self._track(item)
                if track is not None:
                    result.append(track)
        return result

    # ------------------------------------------------------------ 播放

    def stream_url(self, track: StreamTrack) -> StreamSource:
        self._ensure_auth()
        # static=true 让服务器直接发原文件，避免不必要的转码；客户端解码交给 mpv
        url = (
            f"{self.base_url.rstrip('/')}/Audio/{track.id}/stream"
            f"?static=true&api_key={self.token}"
        )
        return StreamSource(url=url, level="流媒体（原始文件）")

    def cover_url(self, cover_id: str, size: int = 300) -> str:
        if not cover_id:
            return ""
        self._ensure_auth()
        height = max(64, min(1024, size))
        return (
            f"{self.base_url.rstrip('/')}/Items/{cover_id}/Images/Primary"
            f"?maxHeight={height}&quality=90&api_key={self.token}"
        )


def _selftest() -> int:
    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    adapter = JellyfinAdapter(
        {
            "url": "http://demo.example:8096",
            "username": "u",
            "password": "p",
            "token": "tok",
            "user_id": "uid",
        }
    )
    header = adapter._headers()["X-Emby-Authorization"]
    check("认证头含客户端", header.startswith("MediaBrowser"), True)
    check("认证头含 token", 'Token="tok"' in header, True)

    item = {
        "Id": "t1",
        "Name": "歌曲",
        "Artists": ["歌手"],
        "Album": "专辑",
        "AlbumId": "a1",
        "RunTimeTicks": 215_000_0000,
        "AlbumArtistId": "ar1",
    }
    track = adapter._track(item)
    check("曲目 ID", track.id, "t1")
    check("歌手", track.artists, ("歌手",))
    check("时长（秒）", track.duration, 215)
    check("封面取专辑", track.cover_id, "a1")

    payload = {"Items": [item]}
    adapter._get = lambda path, params=None: payload  # type: ignore[method-assign]
    songs = adapter.songs()
    check("歌曲数量", len(songs), 1)
    check("歌曲名", songs[0].name, "歌曲")
    # 专辑接口的封面始终是专辑自己
    adapter._get = lambda path, params=None: {  # type: ignore[method-assign]
        "Items": [{"Id": "a1", "Name": "专辑", "AlbumArtist": "歌手",
                   "ProductionYear": 2021}]
    }
    albums = adapter.albums()
    check("专辑年份", albums[0].year, 2021)
    check("专辑歌手", albums[0].artists, ("歌手",))

    source = adapter.stream_url(track)
    check("播放地址含 static", "static=true" in source.url, True)
    check("播放地址含 api_key", "api_key=tok" in source.url, True)
    cover = adapter.cover_url("a1", 400)
    check("封面地址含 maxHeight", "maxHeight=400" in cover, True)

    if failures:
        print(f"Jellyfin 自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Jellyfin 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
