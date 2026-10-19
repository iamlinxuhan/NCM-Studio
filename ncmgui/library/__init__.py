"""本地曲库：SQLite 索引、增量扫描、本地歌单、播放历史与统计。

这个包不依赖 Qt，页面与播放器通过普通函数调用；扫描和查询可以放到任何
后台线程里。数据库默认落在 ``config_dir()/library.db``。

对外名称按需导入。若在这里直接把 ``.db``、``.scan`` 全部拉进来，用
``python -m ncmgui.library.db`` 跑自检时 runpy 会先导入本包，再执行同名的
子模块，于是打印「found in sys.modules」警告。延迟导入既消掉警告，也让
只用到统计的调用方不必连带加载扫描器。
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "LibraryDB": "db",
    "default_db_path": "db",
    "HistoryEntry": "history",
    "clear_history": "history",
    "history_count": "history",
    "recent": "history",
    "record_play": "history",
    "update_play": "history",
    "playlists": "playlists",
    "LocalPlaylist": "playlists",
    "album_list": "queries",
    "artist_list": "queries",
    "counts": "queries",
    "folder_list": "queries",
    "song_from_row": "queries",
    "tracks": "queries",
    "ScanResult": "scan",
    "forget_directory": "scan",
    "scan": "scan",
    "DayEntry": "stats",
    "RankEntry": "stats",
    "SliceEntry": "stats",
    "StatsData": "stats",
    "collect": "stats",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    if name == "history_count":
        value = module.count
    elif name == "playlists":
        value = module
    else:
        value = getattr(module, name)
    globals()[name] = value
    return value
