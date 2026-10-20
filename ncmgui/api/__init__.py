"""网易云直连接口。

ncmctl 0.8.1 的 ``curl`` 只导出部分方法，搜索、每日推荐、歌单编辑、云盘列表
等都不在其中。本包按 SPlayer-Next 的做法直接请求 ``music.163.com/api/*``，
复用 ncmctl 已保存的登录 Cookie（只读），并在明文接口不可用时回退到 weapi。

页面里的网络请求一律放在 :class:`~ncmgui.workers.BackgroundTask` 里执行。

导出采用惰性加载：``python -m ncmgui.api.cookies`` 这类自检不应该因为父包
预先导入其它子模块而触发 ``runpy`` 警告。
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "ApiClient": ".client",
    "ApiError": ".errors",
    "AuthRequired": ".errors",
    "RateLimited": ".errors",
    "RequestCancelled": ".errors",
    "AntiCheatRequired": ".errors",
    "UnsupportedEndpoint": ".errors",
    "ApiChanged": ".errors",
    "NetworkError": ".errors",
    "Album": ".models",
    "Artist": ".models",
    "Comment": ".models",
    "Playlist": ".models",
    "SearchPage": ".models",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
