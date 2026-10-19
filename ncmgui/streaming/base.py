"""流媒体适配层的公共模型与错误类型。

播放层只依赖 :class:`StreamSource` 的三个属性：``url``、``level``、
``headers``。适配器把认证塞进 URL 查询参数（Subsonic 的 token / Jellyfin 的
api_key），这样 mpv 不需要额外请求头；确实需要头时再通过 ``headers`` 返回，
播放控制器会在装载前写 ``http-header-fields``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class StreamError(RuntimeError):
    """服务器不可达、认证失败或返回结构变化。消息可直接展示给用户。"""


@dataclass(frozen=True)
class StreamTrack:
    id: str
    name: str
    artists: tuple[str, ...] = ()
    album: str = ""
    album_id: str = ""
    duration: int = 0  # 秒
    cover_id: str = ""
    artist_id: str = ""


@dataclass(frozen=True)
class StreamAlbum:
    id: str
    name: str
    artists: tuple[str, ...] = ()
    year: int = 0
    cover_id: str = ""


@dataclass(frozen=True)
class StreamArtist:
    id: str
    name: str


@dataclass(frozen=True)
class StreamPlaylist:
    id: str
    name: str
    owner: str = ""
    song_count: int = 0


@dataclass(frozen=True)
class StreamSource:
    url: str
    level: str = ""
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class HttpMixin:
    """两套适配器共用的 JSON 请求。故意不走系统代理：服务器通常在内网。"""

    base_url: str = ""
    timeout: float = 15.0
    headers: dict[str, str] = {}

    def _json(
        self,
        path: str,
        params: dict | None = None,
        *,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        url = self.base_url.rstrip("/") + path
        if params:
            query = urllib.parse.urlencode(
                {key: value for key, value in params.items() if value is not None}
            )
            url += ("&" if "?" in url else "?") + query
        payload = None
        merged = dict(self.headers)
        merged.update(headers or {})
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url, data=payload, headers=merged, method="POST" if payload else "GET"
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except OSError:
                pass
            raise StreamError(f"服务器返回 HTTP {exc.code}{('：' + detail) if detail else ''}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise StreamError(f"无法连接服务器：{reason}") from exc
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise StreamError("服务器返回的不是 JSON，请检查地址是否正确") from exc


def join_names(value) -> tuple[str, ...]:
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",") if part.strip()]
        return tuple(names)
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                result.append(name)
        return tuple(result)
    return ()
