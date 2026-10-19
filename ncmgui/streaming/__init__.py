"""自建流媒体服务器：Subsonic 系（Navidrome/Airsonic/Gonic）与 Jellyfin/Emby。"""

from .base import (
    StreamAlbum,
    StreamArtist,
    StreamError,
    StreamPlaylist,
    StreamSource,
    StreamTrack,
)


def __getattr__(name: str):
    # 延迟导入：让 ``python -m ncmgui.streaming.subsonic`` 这类自检不会在
    # 包初始化时就把 registry 拉进来（否则 runpy 会给出模块已加载的警告）。
    if name == "StreamingRegistry":
        from .registry import StreamingRegistry

        return StreamingRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "StreamAlbum",
    "StreamArtist",
    "StreamError",
    "StreamPlaylist",
    "StreamSource",
    "StreamTrack",
    "StreamingRegistry",
]
