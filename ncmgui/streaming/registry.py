"""流媒体服务器配置与适配器缓存。

服务器列表存在 ``config.json`` 的 ``streaming.servers`` 里（文件本身 0600）。
密码/API Key 只用于本机连接，不写日志、不进控制台输出。

歌曲用负整数 ID（与本地曲库同一符号约定），实际索引靠 ``Song.stream_id``
里的 ``服务器ID:曲目ID``。这样同一首歌来自不同服务器也不会互相顶掉。
"""

from __future__ import annotations

import uuid
import zlib

from PyQt6.QtCore import QObject, pyqtSignal

from ..resolver import Song
from .base import StreamError, StreamPlaylist, StreamSource, StreamTrack
from .jellyfin import JellyfinAdapter
from .subsonic import SubsonicAdapter

KINDS = {
    "subsonic": SubsonicAdapter,
    "jellyfin": JellyfinAdapter,
}

#: 界面下拉用的 ``(显示名, 值)``
KIND_CHOICES = (
    ("Subsonic / Navidrome", "subsonic"),
    ("Jellyfin / Emby", "jellyfin"),
)

def _text(value) -> str:
    return str(value or "").strip()


def split_stream_id(stream_id: str) -> tuple[str, str]:
    """``服务器ID:曲目ID`` -> 两段。曲目 ID 里允许再出现冒号。"""
    text = _text(stream_id)
    if ":" not in text:
        raise StreamError("流媒体歌曲标识不完整，请重新从服务器列表里选择")
    server_id, track_id = text.split(":", 1)
    if not server_id or not track_id:
        raise StreamError("流媒体歌曲标识不完整，请重新从服务器列表里选择")
    return server_id, track_id


def song_id_for(server_id: str, track_id: str) -> int:
    """稳定的负整数 ID。CRC32 只用于界面播放标记，不参与取链。"""
    digest = zlib.crc32(f"{server_id}:{track_id}".encode("utf-8")) & 0x7FFFFFFF
    return -(digest + 1)


class StreamingRegistry(QObject):
    changed = pyqtSignal()

    def __init__(self, cfg, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._server_list: list[dict] = []
        self._adapters: dict[str, object] = {}
        self._load()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        raw = self.cfg.get("streaming.servers", [])
        servers: list[dict] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                server_id = _text(item.get("id")) or uuid.uuid4().hex[:8]
                kind = _text(item.get("kind"))
                if kind not in KINDS:
                    continue
                servers.append(
                    {
                        "id": server_id,
                        "name": _text(item.get("name")) or _text(item.get("url")) or "流媒体",
                        "kind": kind,
                        "url": _text(item.get("url")),
                        "username": _text(item.get("username")),
                        "password": str(item.get("password") or ""),
                        "token": _text(item.get("token")),
                        "user_id": _text(item.get("user_id")),
                        "device_id": _text(item.get("device_id")),
                    }
                )
        self._server_list = servers

    def save(self) -> None:
        self.cfg.set("streaming.servers", [dict(server) for server in self._server_list])
        try:
            self.cfg.save()
        except OSError:
            pass
        self.changed.emit()

    # ------------------------------------------------------------ 查询

    def servers(self) -> list[dict]:
        """配置副本；调用方修改副本不会影响已保存的服务器。"""
        return [dict(server) for server in self._server_list]

    def server(self, server_id: str) -> dict | None:
        for server in self._server_list:
            if server["id"] == server_id:
                return dict(server)
        return None

    def name_for(self, server_id: str) -> str:
        server = self.server(server_id)
        return server["name"] if server is not None else server_id

    @property
    def last_server(self) -> str:
        return _text(self.cfg.get("streaming.last_server", ""))

    def set_last_server(self, server_id: str) -> None:
        self.cfg.set("streaming.last_server", _text(server_id))
        try:
            self.cfg.save()
        except OSError:
            pass

    # ------------------------------------------------------------ 编辑

    def add_server(self, data: dict) -> dict:
        server = self._clean(data)
        server["id"] = uuid.uuid4().hex[:8]
        self._server_list.append(server)
        self.save()
        return dict(server)

    def update_server(self, server_id: str, data: dict) -> dict | None:
        for index, old in enumerate(self._server_list):
            if old["id"] != server_id:
                continue
            updated = self._clean(data)
            updated["id"] = server_id
            # 地址或账号变了，上一次换来的 AccessToken 不一定还属于这个账号
            if (
                updated["kind"] != old["kind"]
                or updated["url"] != old["url"]
                or updated["username"] != old["username"]
                or updated["password"] != old["password"]
            ):
                updated["token"] = ""
                updated["user_id"] = ""
                if updated["url"] != old["url"]:
                    updated["device_id"] = ""
            else:
                updated["token"] = updated["token"] or old.get("token", "")
                updated["user_id"] = updated["user_id"] or old.get("user_id", "")
            updated["device_id"] = updated["device_id"] or old.get("device_id", "")
            self._server_list[index] = updated
            self._adapters.pop(server_id, None)
            self.save()
            return dict(updated)
        return None

    def remove_server(self, server_id: str) -> bool:
        before = len(self._server_list)
        self._server_list = [s for s in self._server_list if s["id"] != server_id]
        if len(self._server_list) == before:
            return False
        self._adapters.pop(server_id, None)
        if self.last_server == server_id:
            self.cfg.set("streaming.last_server", "")
        self.save()
        return True

    @staticmethod
    def _clean(data: dict) -> dict:
        kind = _text(data.get("kind"))
        if kind not in KINDS:
            kind = "subsonic"
        url = _text(data.get("url")).rstrip("/")
        return {
            "name": _text(data.get("name")) or url or "流媒体",
            "kind": kind,
            "url": url,
            "username": _text(data.get("username")),
            "password": str(data.get("password") or ""),
            "token": _text(data.get("token")),
            "user_id": _text(data.get("user_id")),
            "device_id": _text(data.get("device_id")),
        }

    # ------------------------------------------------------------ 适配器

    def adapter(self, server_id: str):
        cached = self._adapters.get(server_id)
        if cached is not None:
            return cached
        server = self.server(server_id)
        if server is None:
            raise StreamError("服务器不存在，可能已被删除")
        factory = KINDS.get(server["kind"])
        if factory is None:
            raise StreamError(f"不支持的服务器类型：{server['kind']}")
        adapter = factory(server)
        self._adapters[server_id] = adapter
        return adapter

    def invalidate(self, server_id: str = "") -> None:
        if server_id:
            self._adapters.pop(server_id, None)
        else:
            self._adapters.clear()

    def persist_tokens(self, server_id: str) -> None:
        """Jellyfin 首次登录后会换到 AccessToken，写回配置免得每次登录。

        只应从主线程调用：它最终会 ``cfg.save()`` 并发出 ``changed`` 信号。
        """
        adapter = self._adapters.get(server_id)
        if adapter is None:
            return
        fresh = getattr(adapter, "server", None)
        if not isinstance(fresh, dict):
            return
        for index, server in enumerate(self._server_list):
            if server["id"] != server_id:
                continue
            changed = False
            for key in ("token", "user_id", "device_id"):
                value = _text(fresh.get(key))
                if value and value != server.get(key):
                    server[key] = value
                    changed = True
            if changed:
                self.save()
            return

    def test(self, server_id: str) -> str:
        return self.adapter(server_id).ping()

    # ------------------------------------------------------------ 浏览

    def songs(self, server_id: str, limit: int = 200, offset: int = 0):
        return self.adapter(server_id).songs(limit=limit, offset=offset)

    def albums(self, server_id: str, limit: int = 200, offset: int = 0):
        return self.adapter(server_id).albums(limit=limit, offset=offset)

    def artists(self, server_id: str, limit: int = 500, offset: int = 0):
        return self.adapter(server_id).artists(limit=limit, offset=offset)

    def album_songs(self, server_id: str, album_id: str):
        return self.adapter(server_id).album_songs(album_id)

    def artist_albums(self, server_id: str, artist_id: str):
        return self.adapter(server_id).artist_albums(artist_id)

    def playlists(self, server_id: str, limit: int = 200, offset: int = 0) -> list[StreamPlaylist]:
        return self.adapter(server_id).playlists(limit=limit, offset=offset)

    def playlist_songs(self, server_id: str, playlist_id: str):
        return self.adapter(server_id).playlist_songs(playlist_id)

    def search(self, server_id: str, query: str, limit: int = 50):
        return self.adapter(server_id).search(query, limit=limit)

    def cover_url(self, server_id: str, cover_id: str, size: int = 300) -> str:
        try:
            return self.adapter(server_id).cover_url(cover_id, size=size)
        except StreamError:
            return ""

    # ------------------------------------------------------------ 播放

    def song_for(self, server_id: str, track: StreamTrack) -> Song:
        server = self.server(server_id)
        if server is None:
            raise StreamError("服务器不存在，可能已被删除")
        cover = ""
        try:
            cover = self.adapter(server_id).cover_url(track.cover_id)
        except StreamError:
            cover = ""
        return Song(
            id=song_id_for(server_id, track.id),
            name=track.name,
            artists=tuple(track.artists),
            album=track.album,
            duration=max(0, int(track.duration)) * 1000,
            pic_url=cover,
            source=server["kind"],
            stream_id=f"{server_id}:{track.id}",
        )

    def songs_for(self, server_id: str, tracks) -> list[Song]:
        return [self.song_for(server_id, track) for track in tracks]

    def resolve(self, song: Song) -> StreamSource:
        """播放控制器调用：把队列里的流媒体 Song 换成可播放地址。"""
        server_id, track_id = split_stream_id(song.stream_id)
        server = self.server(server_id)
        if server is None:
            raise StreamError("流媒体服务器已被删除，无法播放这首歌")
        adapter = self.adapter(server_id)
        return adapter.stream_url(StreamTrack(track_id, song.name))


def _selftest() -> int:
    import tempfile
    from pathlib import Path

    from .. import ensure_utf8_stdout
    from ..config import Config

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as folder:
        cfg = Config(Path(folder) / "config.json")
        registry = StreamingRegistry(cfg)
        check("初始为空", registry.servers(), [])

        server = registry.add_server(
            {
                "name": "家里的 Navidrome",
                "kind": "subsonic",
                "url": "http://192.168.1.2:4533/",
                "username": "me",
                "password": "secret",
            }
        )
        check("地址去掉末尾斜杠", server["url"], "http://192.168.1.2:4533")
        check("服务器数量", len(registry.servers()), 1)
        check("落盘", len(StreamingRegistry(cfg).servers()), 1)

        song = registry.song_for(server["id"], StreamTrack("track:1", "歌", duration=200))
        check("流媒体歌曲 ID 为负", song.id < 0, True)
        check("source 是类型", song.source, "subsonic")
        check("stream_id 含服务器", song.stream_id, f"{server['id']}:track:1")
        check("时长转毫秒", song.duration, 200_000)
        check("稳定 ID", registry.song_for(server["id"], StreamTrack("track:1", "歌")).id, song.id)
        check("不同曲目 ID 不同", registry.song_for(server["id"], StreamTrack("track:2", "歌")).id != song.id, True)
        check("拆分标识", split_stream_id(song.stream_id), (server["id"], "track:1"))

        source = registry.resolve(song)
        check("取链是 Subsonic", "/rest/stream.view" in source.url, True)

        updated = registry.update_server(
            server["id"], {**server, "name": "改名"}
        )
        check("改名", updated["name"], "改名")
        check("密码不变保留 token", updated["token"], "")
        registry.remove_server(server["id"])
        check("删除后为空", registry.servers(), [])

    if failures:
        print(f"流媒体注册表自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("流媒体注册表自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
