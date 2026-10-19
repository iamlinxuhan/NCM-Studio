"""配置持久化。

配置落在各平台约定的位置：

* **Windows**：``%APPDATA%\\ncm-studio\\config.json``
* **macOS**：``~/Library/Application Support/ncm-studio/config.json``
* **其他**：``$XDG_CONFIG_HOME/ncm-studio/config.json``，没设就是 ``~/.config/...``

1.3.x 及更早版本的配置在 ``ncmctl-gui`` 目录下，首次读取时自动迁移。

本模块刻意不依赖 Qt，这样在无显示环境下也能单独导入和测试。
注意 ``~/.ncmctl/`` 存放的是登录凭据，属于敏感目录：本程序只读取其中的
状态用于显示，绝不改写。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

APP_DIR_NAME = "ncm-studio"

#: 1.3.x 及更早版本的配置目录名。改名后首次启动会自动把配置搬过来
LEGACY_APP_DIR_NAME = "ncmctl-gui"


def _config_root() -> Path:
    """各平台存放应用配置的根目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip()
        return Path(base) if base else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(base) if base else Path.home() / ".config"


def config_dir() -> Path:
    """返回本程序的配置目录，按平台惯例走。

    POSIX 上建目录时用 ``0700``：配置里存着下载路径、代理、账号标识等，
    同机其他用户没有理由能读。
    """
    return _private_dir(_config_root() / APP_DIR_NAME)


def _private_dir(path: Path) -> Path:
    """建立（或收紧）一个只有本人可读写的目录；失败不抛异常。"""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def private_tmp_dir() -> Path:
    """本程序专用的临时目录（``config_dir()/tmp``，POSIX 下 ``0700``）。

    二维码目录、Cookie 临时文件这类东西含登录凭据，不能落在全局 ``/tmp``：
    那儿的目录名可被同机其他用户枚举，默认权限也未必只给本人。
    """
    return _private_dir(config_dir() / "tmp")


def cleanup_private_tmp(max_age: float = 3600.0) -> int:
    """删掉 ``private_tmp_dir()`` 里超龄的二维码/Cookie 残留，返回删除数。

    正常退出时页面会自己清理；这里兜住崩溃、断电或强制结束留下的东西。
    只处理本程序自己的前缀，不碰目录里的其它文件。
    """
    root = private_tmp_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0

    deadline = time.time() - max(0.0, float(max_age))
    removed = 0
    for entry in entries:
        if not entry.name.startswith(("ncmctl-qr-", "ncmctl-cookie-")):
            continue
        try:
            if entry.lstat().st_mtime > deadline:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def config_file() -> Path:
    """返回配置文件路径；旧版本的配置在这里自动迁移一次。"""
    target = config_dir() / "config.json"
    _migrate_legacy_config(target)
    return target


def _user_dirs() -> dict[str, str]:
    """解析 ``~/.config/user-dirs.dirs``，以兼容中文桌面环境下的音乐目录名。"""
    out: dict[str, str] = {}
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    path = Path(base) / "user-dirs.dirs" if base else Path.home() / ".config" / "user-dirs.dirs"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    for raw in content.splitlines():
        raw = raw.strip()
        if not raw.startswith("XDG_") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        out[key.strip()] = value.strip().strip('"').replace("$HOME", str(Path.home()))
    return out


def _music_candidates() -> list[Path]:
    """各平台可能的音乐目录，按优先级排列。

    ``~/Music`` 同时覆盖 macOS 和 Windows（``%USERPROFILE%\\Music`` 就是
    系统的默认音乐库）；``~/音乐`` 是中文桌面环境下的常见命名。
    """
    home = Path.home()
    xdg = _user_dirs().get("XDG_MUSIC_DIR", "")
    candidates = [Path(xdg)] if xdg else []
    candidates += [home / "Music", home / "音乐"]
    return candidates


def default_music_dir() -> Path:
    """推断一个合适的音乐输出目录。

    在系统音乐目录下建一个 ``NCM-Studio`` 子目录，避免和用户自己的曲库混在一起。
    """
    for candidate in _music_candidates():
        if candidate.is_dir():
            return candidate / "NCM-Studio"
    return Path.home() / "NCM-Studio-download"


def _legacy_music_dir() -> Path:
    """改名前的默认下载目录。迁移配置时用来识别「用户没改过」的情况。"""
    for candidate in _music_candidates():
        if candidate.is_dir():
            return candidate / "ncmctl"
    return Path.home() / "ncmctl-download"


def _migrate_legacy_config(target: Path) -> None:
    """把旧版配置搬到新目录，只在目标配置不存在时做一次。

    下载目录如果还是旧的默认值，顺手换成新默认值；用户自己改过的路径原样保留。
    迁移失败不抛异常 —— 大不了用默认配置启动，不能因为搬配置把程序挡在门外。
    """
    if target.exists():
        return
    legacy = _config_root() / LEGACY_APP_DIR_NAME / "config.json"
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    download = data.get("download")
    if isinstance(download, dict) and download.get("output") == str(_legacy_music_dir()):
        download["output"] = str(default_music_dir())
    tmp = target.with_name(target.name + ".tmp")
    try:
        _private_dir(target.parent)
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def default_ncm_dir() -> Path:
    """NCM 解码的默认输出目录，与下载目录同级。"""
    return default_music_dir().with_name("ncm-decoded")


def _defaults() -> dict[str, Any]:
    return {
        # 空字符串表示「使用 ncmctl 自身默认值」，即不附加对应全局参数
        "ncmctl_path": "",
        "home": "",
        "config_file": "",
        "debug": False,
        "download": {
            "output": str(default_music_dir()),
            "level": "lossless",
            "parallel": 5,
            "strict": False,
            # 下载前先比对输出目录，已有的直接跳过（ncmctl 自身不会跳过）
            "skip_existing": True,
            # 歌词：lrc（独立 .lrc 文件）/ embed（写进音频标签）/ none。
            # 取值见 ncmgui.lyrics 的 MODE_*；写在这里是为了让它出现在配置文件里，
            # 用户翻配置文件时能发现这一项。
            "lyric": "lrc",
            # 封面：file（同名图片）/ embed（写进音频标签）/ none。
            # 取值见 ncmgui.covers 的 MODE_*
            "cover": "file",
        },
        "ncm": {
            "output": str(default_ncm_dir()),
            "parallel": 10,
            "no_tag": False,
        },
        "cloud": {
            "source": "",
            "minsize": "",
            "parallel": 3,
            "regexp": "",
        },
        # 流媒体播放。mpv_path 留空表示「随包 → PATH → 常见目录」依次探测。
        # local_dir 留空表示「本地音乐默认打开下载目录」（download.output）。
        "player": {
            "mpv_path": "",
            "level": "exhigh",
            "volume": 70,
            "mode": "loop",
            # 会员歌曲只返回 30 秒试听片段。默认与 SPlayer 一样拒绝播放，
            # 避免用户以为听到了完整版；允许后仍会在播放页提示「试听」。
            "allow_trial": False,
            "local_dir": "",
            # 播放页里黑胶 : 歌词的宽度比例（[0.367, 0.633]），空表示用默认
            "split": [],
            # 分栏比例的存储版本；旧版本可能存下布局自动压缩的结果
            "split_rev": 0,
            # 上次的播放队列（只存歌曲标识，不存会过期的直链）。启动后恢复
            # 队列和进度，但不自动出声
            "queue": [],
            "queue_index": -1,
            "position": 0.0,
            # 音频工具：倍速、变调（半音）、均衡器、响度归一化、输出设备。
            # 具体能否生效由 mpv 能力探测决定，不支持的控件会隐藏。
            "speed": 1.0,
            "pitch": 0.0,
            "equalizer": {
                "enabled": False,
                "gains": [0.0] * 10,
                "preamp": 0.0,
            },
            "loudness": False,
            "audio_device": "",
            "device_volumes": {},
            # 歌词偏移（毫秒）。正值让歌词晚出现
            "lyric_offset": 0,
            # 桌面歌词窗。locked 时鼠标事件穿透/不可拖动；位置 -1 表示居中
            "desktop_lyrics": {
                "enabled": False,
                "locked": False,
                "x": -1,
                "y": -1,
                "width": 760,
                "height": 130,
                "font_size": 26,
                "translation": True,
                "opaque": False,
            },
        },
        # 自建流媒体服务器（Subsonic 系 / Jellyfin / Emby）。密码与
        # AccessToken 只存在这个 0600 文件里，不写日志。
        "streaming": {
            "servers": [],
            "last_server": "",
        },
        # 本地曲库。dirs 为空时自动落到 player.local_dir / 下载目录；
        # db 留空表示 config_dir()/library.db。sort 取值见 library.queries.SORTS
        "library": {
            "dirs": [],
            "db": "",
            "sort": "name",
            "descending": False,
        },
        # 界面外观。theme 取值 dark / light，由侧边栏的「主题」键切换
        "ui": {"theme": "dark", "close_to_tray": False},
        # 主窗口大小。用户缩放过后记在这里，下次启动照着打开
        "window": {"width": 1180, "height": 780, "maximized": False},
        # 自动更新。git_proxy 为空走系统代理，direct 表示直连
        "update": {
            "git_proxy": "",
            "check_on_start": True,
        },
        "crypto": {"output": ""},
        "curl": {"output": "", "kind": "weapi", "timeout": "15s"},
        "proxy": {"listen": "127.0.0.1:9000", "max_body": "1MB"},
        "task": {"location": "Asia/Shanghai"},
    }


DEFAULTS: dict[str, Any] = _defaults()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """把 overlay 合并进 base（就地修改 base），只递归合并 dict。"""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class Config:
    """扁平的「点号路径」配置访问器。

    读写形如 ``cfg.get("download.output")`` / ``cfg.set("download.parallel", 8)``，
    内部按嵌套字典存储。文件损坏或缺失时静默回退到默认值，不让配置问题阻断启动。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else config_file()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        raw: Any = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None

        merged = json.loads(json.dumps(DEFAULTS))
        if isinstance(raw, dict):
            _deep_merge(merged, raw)
        self._data = merged

    def save(self) -> bool:
        """原子写入配置文件；失败时返回 False 而不是抛异常。

        配置里可能有账号标识、代理与本地路径，POSIX 下建目录用 ``0700``、
        文件用 ``0600``。Windows 依赖用户目录本身的 ACL。
        """
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            _private_dir(self.path.parent)
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return True
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def reset(self) -> None:
        self._data = json.loads(json.dumps(DEFAULTS))
