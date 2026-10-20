"""网易云接口的领域封装。

页面只跟这里打交道：搜索、每日推荐、歌单/专辑/歌手、评论、云盘、喜欢和歌单
编辑。底层 HTTP 的 Cookie、限流、错误翻译都在 :mod:`.transport`。

所有方法都是同步的，调用方必须放在 ``BackgroundTask`` 里跑。

    .venv/bin/python -m ncmgui.api.client
"""

from __future__ import annotations

import json
from typing import Any

from .. import ncmctl
from .cookies import CookieJar
from .errors import AntiCheatRequired, ApiError, AuthRequired
from .models import (
    Album,
    Artist,
    CloudSong,
    Comment,
    Playlist,
    SearchPage,
    album_from_api,
    artist_from_api,
    cloud_song_from_api,
    comment_from_api,
    playlist_from_api,
    song_from_api,
    songs_from_api,
)
from .transport import Transport

#: 网页端播放日志的出口。不能发到 music.163.com —— 那边没有这个端点。
WEB_LOG_DOMAIN = "https://clientlogusf.music.163.com"

#: 云搜索的类型编号
SEARCH_TYPES = {"song": 1, "album": 10, "artist": 100, "playlist": 1000}
#: 各类型的总数键
_COUNT_KEYS = {
    "song": "songCount",
    "album": "albumCount",
    "artist": "artistCount",
    "playlist": "playlistCount",
}
_RESULT_KEYS = {
    "song": "songs",
    "album": "albums",
    "artist": "artists",
    "playlist": "playlists",
}


class ApiClient:
    """一个账号会话。线程安全由 :class:`Transport` 与 :class:`CookieJar` 保证。"""

    def __init__(
        self,
        home: str = "",
        *,
        proxy: str = "",
        timeout: float = 15.0,
        jar: CookieJar | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.jar = jar or CookieJar(ncmctl.cookie_path(home))
        self.transport = transport or Transport(self.jar, proxy=proxy, timeout=timeout)
        self.home = home
        self._profile: dict = {}
        self._uid = 0
        self._liked_playlist: Playlist | None = None

    # ------------------------------------------------------------------ 账号

    @property
    def logged_in(self) -> bool:
        """只看 Cookie 是否存在；真正的在线校验用 :meth:`refresh_account`。"""
        return self.jar.has_login

    @property
    def uid(self) -> int:
        return self._uid

    @property
    def profile(self) -> dict:
        return dict(self._profile)

    def refresh_account(self) -> dict:
        """在线校验登录态，返回 ``profile``（未登录时为空字典）。"""
        self.jar.reload()
        payload = self.transport.get("/api/nuser/account/get")
        profile = payload.get("profile")
        if not isinstance(profile, dict) or not profile.get("userId"):
            self._profile = {}
            self._uid = 0
            return {}
        self._profile = profile
        self._uid = int(profile.get("userId") or 0)
        return dict(profile)

    def account(self) -> dict:
        """优先用缓存；没有缓存时联网取一次。"""
        if self._profile:
            return dict(self._profile)
        return self.refresh_account()

    def need_login(self) -> None:
        if not self.jar.has_login:
            raise AuthRequired("请先在「账号」页登录")
        if not self._uid:
            self.refresh_account()
        if not self._uid:
            raise AuthRequired("登录已失效，请重新登录")

    # ------------------------------------------------------------------ 搜索

    def search(self, keyword: str, kind: str = "song", page: int = 1,
               limit: int = 30) -> SearchPage:
        keyword = (keyword or "").strip()
        if not keyword:
            return SearchPage(kind=kind, total=0, page=page, limit=limit)
        kind = kind if kind in SEARCH_TYPES else "song"
        limit = max(1, min(int(limit), 100))
        page = max(1, int(page))
        payload = self.transport.get(
            "/api/cloudsearch/pc",
            {
                "s": keyword,
                "type": SEARCH_TYPES[kind],
                "limit": limit,
                "offset": (page - 1) * limit,
            },
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            return SearchPage(kind=kind, total=0, page=page, limit=limit)
        raw_items = result.get(_RESULT_KEYS[kind])
        items: list[Any] = []
        if kind == "song":
            items = songs_from_api(raw_items or [])
        elif kind == "album":
            items = [album_from_api(item) for item in (raw_items or [])
                     if isinstance(item, dict)]
        elif kind == "artist":
            items = [artist_from_api(item) for item in (raw_items or [])
                     if isinstance(item, dict)]
        else:
            items = [playlist_from_api(item) for item in (raw_items or [])
                     if isinstance(item, dict)]
        total = int(result.get(_COUNT_KEYS[kind]) or 0)
        return SearchPage(kind=kind, items=items, total=total, page=page, limit=limit)

    def hot_searches(self) -> list[str]:
        """热搜词。明文接口不可用时回退旧路径；再失败返回空列表。"""
        items = []
        for path in ("/api/hotsearchlist/get", "/api/search/hot/detail"):
            try:
                payload = self.transport.get(path)
            except ApiError:
                continue
            items = payload.get("data") or []
            if items:
                break
        words: list[str] = []
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict):
                word = str(item.get("searchWord") or item.get("first") or "").strip()
                if word and word not in words:
                    words.append(word)
        return words

    def suggest(self, keyword: str) -> list[str]:
        """搜索联想词。按歌曲→歌手→专辑→歌单的顺序合并去重。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        try:
            payload = self.transport.get("/api/search/suggest/web",
                                         {"s": keyword, "limit": 8})
        except ApiError:
            return []
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        words: list[str] = []

        def add(word: str) -> None:
            word = (word or "").strip()
            if word and word not in words:
                words.append(word)

        for item in result.get("songs") or []:
            if isinstance(item, dict):
                add(str(item.get("name") or ""))
        for key in ("artists", "albums", "playlists"):
            for item in result.get(key) or []:
                if isinstance(item, dict):
                    add(str(item.get("name") or ""))
        for item in result.get("allMatch") or []:
            if isinstance(item, dict):
                add(str(item.get("keyword") or ""))
        return words

    # ------------------------------------------------------------ 推荐与发现

    def daily_songs(self) -> list:
        """每日推荐。需要登录；接口返回 301 时抛 :class:`AuthRequired`。"""
        self.need_login()
        payload = self.transport.get("/api/v1/discovery/recommend/songs")
        data = payload.get("data") or {}
        return songs_from_api(data.get("dailySongs") or [])

    def personalized_playlists(self, limit: int = 12) -> list[Playlist]:
        payload = self.transport.get("/api/personalized/playlist",
                                     {"limit": max(1, int(limit))})
        return [playlist_from_api(item) for item in (payload.get("result") or [])
                if isinstance(item, dict)]

    def new_albums(self, limit: int = 12) -> list[Album]:
        payload = self.transport.get("/api/album/new",
                                     {"area": "ALL", "limit": limit, "offset": 0})
        return [album_from_api(item) for item in (payload.get("albums") or [])
                if isinstance(item, dict)]

    def top_artists(self, limit: int = 12) -> list[Artist]:
        payload = self.transport.get("/api/artist/top",
                                     {"limit": limit, "offset": 0})
        return [artist_from_api(item) for item in (payload.get("artists") or [])
                if isinstance(item, dict)]

    def personal_fm(self) -> list:
        payload = self.transport.get("/api/v1/radio/get")
        return songs_from_api(payload.get("data") or [])

    # ------------------------------------------------------------ 歌单/专辑/歌手

    def playlist(self, playlist_id: int,
                 track_limit: int = 500) -> tuple[Playlist, list, int]:
        """歌单详情和第一批歌曲。``track_limit<=0`` 时不取歌曲。"""
        payload = self.transport.get(
            "/api/v6/playlist/detail",
            {"id": int(playlist_id), "n": max(0, int(track_limit)), "s": 8},
        )
        raw = payload.get("playlist")
        if not isinstance(raw, dict):
            raise ApiError("歌单不存在或已失效")
        playlist = playlist_from_api(raw)
        tracks = songs_from_api(raw.get("tracks") or []) if track_limit > 0 else []
        return playlist, tracks, playlist.track_count

    def playlist_tracks(self, playlist_id: int, limit: int = 500,
                        offset: int = 0) -> list:
        """分页取歌单歌曲。旧接口不可用时回退详情接口的 ``n/s``。"""
        try:
            payload = self.transport.get(
                "/api/playlist/track/all",
                {"id": int(playlist_id), "limit": int(limit), "offset": int(offset)},
            )
            return songs_from_api(payload.get("songs") or [])
        except ApiError:
            payload = self.transport.get(
                "/api/v6/playlist/detail",
                {"id": int(playlist_id), "n": int(limit), "s": int(offset)},
            )
            raw = payload.get("playlist") or {}
            return songs_from_api(raw.get("tracks") or [])

    def playlist_tracks_all(self, playlist_id: int, *, page_size: int = 500,
                            max_songs: int = 5000) -> list:
        """按页取完整歌单，最多 ``max_songs`` 首，避免异常接口导致死循环。"""
        songs: list = []
        offset = 0
        page_size = max(1, min(int(page_size), 1000))
        while len(songs) < max_songs:
            batch = self.playlist_tracks(playlist_id, page_size, offset)
            if not batch:
                break
            songs.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
        return songs[:max_songs]

    def album(self, album_id: int) -> tuple[Album, list]:
        payload = self.transport.get(f"/api/v1/album/{int(album_id)}")
        raw = payload.get("album")
        if not isinstance(raw, dict):
            raise ApiError("专辑不存在或已下架")
        return album_from_api(raw), songs_from_api(payload.get("songs") or [])

    def artist(self, artist_id: int) -> Artist:
        payload = self.transport.get(f"/api/artist/{int(artist_id)}")
        raw = payload.get("artist")
        if not isinstance(raw, dict):
            raise ApiError("歌手不存在")
        return artist_from_api(raw)

    def artist_songs(self, artist_id: int, page: int = 1,
                     limit: int = 50) -> tuple[list, bool]:
        limit = max(1, min(int(limit), 100))
        payload = self.transport.get(
            "/api/v1/artist/songs",
            {
                "id": int(artist_id),
                "offset": (max(1, int(page)) - 1) * limit,
                "limit": limit,
                "order": "hot",
            },
        )
        songs = songs_from_api(payload.get("songs") or [])
        return songs, bool(payload.get("more"))

    def artist_albums(self, artist_id: int, page: int = 1,
                      limit: int = 30) -> tuple[list[Album], bool]:
        limit = max(1, min(int(limit), 100))
        payload = self.transport.get(
            f"/api/artist/albums/{int(artist_id)}",
            {
                "offset": (max(1, int(page)) - 1) * limit,
                "limit": limit,
                "total": "true",
            },
        )
        albums = [
            album_from_api(item)
            for item in (payload.get("hotAlbums") or payload.get("albums") or [])
            if isinstance(item, dict)
        ]
        return albums, bool(payload.get("more"))

    def song_detail(self, song_ids: list[int]) -> list:
        ids = [int(song_id) for song_id in song_ids if song_id]
        if not ids:
            return []
        payload = self.transport.get(
            "/api/song/detail", {"ids": json.dumps([{"id": i} for i in ids])}
        )
        return songs_from_api(payload.get("songs") or [])

    # -------------------------------------------------------------- 评论

    def comments(self, song_id: int, page: int = 1, limit: int = 20,
                 hot: bool = False) -> tuple[list[Comment], int, bool]:
        limit = max(1, min(int(limit), 100))
        offset = (max(1, int(page)) - 1) * limit
        path = (
            f"/api/v1/resource/hotcomments/R_SO_4_{int(song_id)}"
            if hot else f"/api/v1/resource/comments/R_SO_4_{int(song_id)}"
        )
        payload = self.transport.get(path, {"limit": limit, "offset": offset})
        raw = payload.get("hotComments") if hot else payload.get("comments")
        comments = [comment_from_api(item) for item in (raw or [])
                    if isinstance(item, dict)]
        return comments, int(payload.get("total") or 0), bool(payload.get("more"))

    # -------------------------------------------------------------- 用户

    def user_playlists(self, uid: int | None = None) -> list[Playlist]:
        self.need_login()
        uid = int(uid or self._uid)
        payload = self.transport.get(
            "/api/user/playlist", {"uid": uid, "limit": 1000, "offset": 0}
        )
        return [playlist_from_api(item) for item in (payload.get("playlist") or [])
                if isinstance(item, dict)]

    def liked_playlist(self) -> Playlist:
        if self._liked_playlist is not None:
            return self._liked_playlist
        playlists = self.user_playlists()
        for playlist in playlists:
            if playlist.special_type == 5:
                self._liked_playlist = playlist
                return playlist
        raise ApiError("账号里没有「我喜欢的音乐」歌单")

    def liked_ids(self, uid: int | None = None) -> set[int]:
        self.need_login()
        uid = int(uid or self._uid)
        payload = self.transport.get("/api/song/like/get", {"uid": uid})
        return {int(item) for item in (payload.get("ids") or []) if item}

    def album_sublist(self, limit: int = 100, offset: int = 0) -> list[Album]:
        self.need_login()
        payload = self.transport.get("/api/album/sublist",
                                     {"limit": limit, "offset": offset})
        return [album_from_api(item) for item in (payload.get("data") or [])
                if isinstance(item, dict)]

    def artist_sublist(self, limit: int = 100, offset: int = 0) -> list[Artist]:
        self.need_login()
        payload = self.transport.get("/api/artist/sublist",
                                     {"limit": limit, "offset": offset})
        return [artist_from_api(item) for item in (payload.get("data") or [])
                if isinstance(item, dict)]

    # -------------------------------------------------------------- 云盘

    def cloud(self, limit: int = 100,
              offset: int = 0) -> tuple[list[CloudSong], dict]:
        self.need_login()
        payload = self.transport.get("/api/v1/cloud/get",
                                     {"limit": limit, "offset": offset})
        songs = [cloud_song_from_api(item) for item in (payload.get("data") or [])
                 if isinstance(item, dict)]
        meta = {
            "count": int(payload.get("count") or 0),
            "size": int(payload.get("size") or 0),
            "max_size": int(payload.get("maxSize") or 0),
            "has_more": bool(payload.get("hasMore")),
        }
        return [song for song in songs if song is not None], meta

    # -------------------------------------------------------------- 写操作

    def like(self, song_id: int, on: bool = True) -> None:
        """喜欢/取消喜欢。风控拦截时抛 :class:`AntiCheatRequired`。"""
        self.need_login()
        data = {
            "trackId": int(song_id),
            "like": "true" if on else "false",
        }
        try:
            self.transport.post("/api/song/like", data, csrf=True,
                                write=True, anticheat=True)
            return
        except ApiError as first:
            # 旧版网页接口；部分账号仍可用
            try:
                self.transport.post("/api/radio/like", data, csrf=True,
                                    write=True, anticheat=True)
                return
            except ApiError:
                raise AntiCheatRequired(
                    "网易云拦截了这次操作，请稍后在网页端重试", first.code
                ) from first

    def playlist_create(self, name: str, private: bool = False) -> int:
        self.need_login()
        payload = self.transport.post(
            "/api/playlist/create",
            {"name": name, "privacy": "10" if private else "0"},
            csrf=True, write=True,
        )
        playlist = payload.get("playlist") or {}
        playlist_id = int(playlist.get("id") or payload.get("id") or 0)
        if not playlist_id:
            raise ApiError("歌单创建成功但接口没有返回 ID")
        self._liked_playlist = None
        return playlist_id

    def playlist_delete(self, playlist_id: int) -> None:
        self.need_login()
        # 网页端当前走 weapi，参数是 ``ids`` 的 JSON 数组；明文旧参数
        # ``id`` 会返回 400。先试 weapi，再退回明文，兼容不同账号。
        data = {"ids": json.dumps([int(playlist_id)])}
        try:
            self.transport.weapi("/weapi/playlist/remove", data,
                                 write=True, anticheat=True)
        except ApiError:
            self.transport.post(
                "/api/playlist/remove",
                {"id": int(playlist_id), "ids": json.dumps([int(playlist_id)])},
                csrf=True, write=True,
            )
        self._liked_playlist = None

    def playlist_rename(self, playlist_id: int, name: str) -> None:
        self.need_login()
        self.transport.post("/api/playlist/update/name",
                            {"id": int(playlist_id), "name": name},
                            csrf=True, write=True)

    def playlist_desc(self, playlist_id: int, desc: str) -> None:
        self.need_login()
        self.transport.post("/api/playlist/desc/update",
                            {"id": int(playlist_id), "desc": desc},
                            csrf=True, write=True)

    def playlist_add_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        self._manipulate(playlist_id, song_ids, "add")

    def playlist_remove_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        self._manipulate(playlist_id, song_ids, "del")

    def _manipulate(self, playlist_id: int, song_ids: list[int], op: str) -> None:
        self.need_login()
        if not song_ids:
            return
        self.transport.post(
            "/api/playlist/manipulate/tracks",
            {
                "op": op,
                "pid": int(playlist_id),
                "trackIds": json.dumps([int(i) for i in song_ids]),
                "imme": "true",
            },
            csrf=True, write=True, anticheat=True,
        )

    def subscribe_playlist(self, playlist_id: int, on: bool = True) -> None:
        self.need_login()
        # 取消收藏是独立端点，网页端不再接受 ``t=0`` 的旧写法
        path = "/api/playlist/subscribe" if on else "/api/playlist/unsubscribe"
        self.transport.post(path, {"id": int(playlist_id)},
                            csrf=True, write=True)

    def subscribe_album(self, album_id: int, on: bool = True) -> None:
        self.need_login()
        path = "/api/album/sub" if on else "/api/album/unsub"
        self.transport.post(path, {"id": int(album_id)},
                            csrf=True, write=True)

    def subscribe_artist(self, artist_id: int, on: bool = True) -> None:
        self.need_login()
        path = "/api/artist/sub" if on else "/api/artist/unsub"
        self.transport.post(
            path,
            {
                "artistId": int(artist_id),
                "artistIds": json.dumps([int(artist_id)]),
            },
            csrf=True, write=True,
        )

    def cloud_delete(self, song_ids: list[int]) -> None:
        self.need_login()
        self.transport.post(
            "/api/cloud/del",
            {"songIds": json.dumps([int(i) for i in song_ids])},
            csrf=True, write=True,
        )

    # ---------------------------------------------------------- 播放日志

    def scrobble(self, song_id: int, action: str, seconds: int = 0) -> None:
        """把一次播放动作写进网易云的听歌记录。

        走网页端同一个出口（``clientlogusf`` 的 ``/weapi/feedback/weblog``）：

        * ``startPlay``：真正开始出声时发一次，不带时长；
        * ``play``：这一轮结束（切歌/播完/停止）时发，``seconds`` 是实际
          收听时长，必须大于 0。

        报文形状按 2026-10 的网页端 Firefox 抓包对齐：动作名是
        ``startPlay``（大写 P），``play`` 里没有 ``end`` 字段；网页额外带
        ``x-music-web-os`` / ``Nm-GCore-Status`` 两个请求头，这里一并补上。

        接口本身只回 ``{"code":200,"data":"success"}``，没有更多语义。登录
        失效时由 :class:`Transport` 抛 :class:`AuthRequired`；调用方应当把它
        当成“这次不上报”而不是播放错误。
        """
        song_id = int(song_id)
        if song_id <= 0:
            raise ValueError("播放日志需要正的歌曲 ID")
        if action not in ("startPlay", "play"):
            raise ValueError(f"不认识的播放日志动作：{action}")
        seconds = int(seconds)
        if action == "play" and seconds <= 0:
            return
        if not self.jar.csrf:
            raise AuthRequired("登录信息缺少 CSRF 令牌，请重新登录")

        entry: dict[str, Any] = {
            "mainSite": "2",
            "mainSiteWeb": "2",
            "id": str(song_id),
            "type": "song",
            "content": "",
        }
        if action == "play":
            entry.update({"time": seconds, "wifi": 0, "download": 0})
        logs = json.dumps(
            [{"action": action, "json": entry}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.transport.weapi(
            "/weapi/feedback/weblog",
            {"logs": logs},
            write=True,
            domain=WEB_LOG_DOMAIN,
            headers={
                "x-music-web-os": "web3",
                "Nm-GCore-Status": "1",
            },
        )


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

class _FakeTransport:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, path: str, params: dict | None = None, **_kwargs) -> dict:
        self.calls.append(("GET", path, dict(params or {})))
        return self.responses.get(path, {"code": 200})

    def post(self, path: str, data: dict | None = None, **_kwargs) -> dict:
        self.calls.append(("POST", path, dict(data or {})))
        return self.responses.get(path, {"code": 200})

    def weapi(self, path: str, data: dict | None = None, **kwargs) -> dict:
        self.calls.append(("WEAPI", path, dict(data or {})))
        self.last_weapi_kwargs = kwargs
        return self.responses.get(path, {"code": 200, "data": []})


class _FakeJar:
    has_login = True
    csrf = "csrf"

    def header(self, **_kwargs) -> str:
        return ""

    def reload(self) -> bool:
        return False


def _selftest() -> int:
    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    responses = {
        "/api/cloudsearch/pc": {
            "code": 200,
            "result": {
                "songs": [{"id": 1, "name": "歌", "dt": 1000,
                           "al": {"name": "专辑", "picUrl": "p"},
                           "ar": [{"name": "甲"}]}],
                "songCount": 31,
            },
        },
        "/api/hotsearchlist/get": {"code": 200, "data": [{"searchWord": "热"}]},
        "/api/search/suggest/web": {
            "code": 200,
            "result": {
                "songs": [{"name": "孤勇者"}],
                "artists": [{"name": "陈奕迅"}],
                "albums": [], "playlists": [],
            },
        },
        "/api/v1/discovery/recommend/songs": {
            "code": 200, "data": {"dailySongs": [{"id": 2, "name": "日推",
                                                  "ar": [{"name": "乙"}]}]},
        },
        "/api/nuser/account/get": {
            "code": 200, "profile": {"userId": 9, "nickname": "我"},
        },
        "/api/playlist/track/all": {
            "code": 200, "songs": [{"id": 3, "name": "曲", "ar": [{"name": "丙"}]}],
        },
        "/api/v6/playlist/detail": {
            "code": 200,
            "playlist": {"id": 7, "name": "歌单", "trackCount": 2,
                         "tracks": [{"id": 3, "name": "曲", "ar": [{"name": "丙"}]}]},
        },
    }
    fake = _FakeTransport(responses)
    client = ApiClient(jar=_FakeJar(), transport=fake)  # type: ignore[arg-type]

    page = client.search("孤勇者", page=1, limit=30)
    check("搜索类型", page.kind, "song")
    check("搜索结果数", len(page.items), 1)
    check("搜索总数", page.total, 31)
    check("搜索偏移", fake.calls[-1][2]["offset"], 0)
    check("第二页偏移", client.search("x", page=2, limit=30) and fake.calls[-1][2]["offset"], 30)
    check("热搜", client.hot_searches(), ["热"])
    check("联想", client.suggest("孤"), ["孤勇者", "陈奕迅"])

    check("账号 UID", client.refresh_account()["userId"], 9)
    check("每日推荐", client.daily_songs()[0].name, "日推")
    check("歌单歌曲", client.playlist_tracks(7)[0].id, 3)
    playlist, tracks, total = client.playlist(7)
    check("歌单名", playlist.name, "歌单")
    check("歌单曲目", tracks[0].id, 3)
    check("歌单总数", total, 2)

    # 播放日志：开始动作不带时长，结束动作带实际收听秒数
    client.scrobble(3, "startPlay")
    path = fake.calls[-1][1]
    check("播放日志路径", path, "/weapi/feedback/weblog")
    check("播放日志域名", fake.last_weapi_kwargs.get("domain"), WEB_LOG_DOMAIN)
    check("播放日志请求头", fake.last_weapi_kwargs.get("headers"), {
        "x-music-web-os": "web3",
        "Nm-GCore-Status": "1",
    })
    logs = json.loads(fake.calls[-1][2]["logs"])
    check("开始日志动作", logs[0]["action"], "startPlay")
    check("开始日志不带时长", "time" in logs[0]["json"], False)
    check("开始日志不带结束方式", "end" in logs[0]["json"], False)
    check("开始日志歌曲 ID", logs[0]["json"]["id"], "3")

    client.scrobble(3, "play", 42)
    logs = json.loads(fake.calls[-1][2]["logs"])
    check("结束日志动作", logs[0]["action"], "play")
    check("结束日志时长", logs[0]["json"]["time"], 42)
    check("结束日志无结束方式", "end" in logs[0]["json"], False)

    before = len(fake.calls)
    client.scrobble(3, "play", 0)  # 0 秒不产生请求
    check("零时长不上报", len(fake.calls), before)

    # 未登录时写操作给出明确错误
    client.jar = type("J", (), {"has_login": False, "csrf": "", "reload": lambda self: None})()
    try:
        client.like(1)
    except AuthRequired:
        pass
    else:
        failures.append("未登录时 like 应当抛 AuthRequired")

    if failures:
        print(f"接口客户端自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("接口客户端自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
