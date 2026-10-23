# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置，三平台共用。

    python3 scripts/fetch_ncmctl.py          # 先取 ncmctl 到 vendor/
    pyinstaller ncm-studio.spec --noconfirm

两种产物形态，用环境变量 ``NCMGUI_MODE`` 切换：

* ``onefile``（默认）—— 单个可执行文件。Windows / macOS 用这个。
* ``onedir`` —— 一个目录树（可执行文件 + ``_internal/``）。Linux 用这个，
  因为 AppImage 必须建在目录树上：onefile 在运行时会把自己再解包一次，套进
  AppImage 就成了双重解包，启动慢且白占一份临时空间。

名字可以用 ``NCMGUI_BUILD_NAME`` 覆盖，CI 里靠它区分平台。

**ncmctl 本体是打包进去的。** 本程序是它的图形前端，界面里每个按钮都对应它的
一个子命令，两者必须一起测、一起发。版本钉在 ``ncmgui.NCMCTL_VERSION``，
由 ``scripts/fetch_ncmctl.py`` 下载并校验后放进 ``vendor/``。

用户想用自己的 ncmctl，在「设置」页填路径即可 —— 那一项优先级高于内置副本。
"""

import os
import sys
import warnings
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

HERE = Path(SPECPATH)

MODE = os.environ.get("NCMGUI_MODE", "onefile").strip().lower()
ONEDIR = MODE in {"onedir", "dir", "appimage"}
NAME = os.environ.get("NCMGUI_BUILD_NAME", "ncm-studio")

#: CI 里设成 1，表示「这个产物必须有 ncmctl」。缺了就报错而不是警告 ——
#: 一个不含 ncmctl 的包发出去，用户那边是直接不能用
REQUIRE_NCMCTL = os.environ.get("NCMGUI_BUNDLE_NCMCTL") == "1"

#: 同理，但针对 mpv。Linux / Windows 的产物必须带播放器，否则「流媒体播放」
#: 这个主打功能在新机器上是坏的（用户还得自己去装 mpv）；macOS 不设这一项。
REQUIRE_MPV = os.environ.get("NCMGUI_BUNDLE_MPV") == "1"

datas: list = []
binaries: list = []

# ncmgui.library 与 ncmgui.streaming 的包入口按需导入子模块（用
# importlib.import_module 拼接名字），PyInstaller 的静态分析看不到这些目标。
# 不列出来的话，打包版会在打开本地曲库或流媒体页时 ImportError。
hiddenimports: list = [
    # ncmgui.api 的包入口同样按需导入
    "ncmgui.api.client",
    "ncmgui.api.errors",
    "ncmgui.api.models",
    "ncmgui.library.db",
    "ncmgui.library.history",
    "ncmgui.library.playlists",
    "ncmgui.library.queries",
    "ncmgui.library.scan",
    "ncmgui.library.stats",
    "ncmgui.streaming.base",
    "ncmgui.streaming.jellyfin",
    "ncmgui.streaming.registry",
    "ncmgui.streaming.subsonic",
]

# Windows 上 pywinpty 除了 Python 扩展之外还带几个**非 .pyd 的二进制**：
# conpty.dll、OpenConsole.exe、winpty.dll、winpty-agent.exe。PyInstaller 的
# 常规依赖分析只看得到 .pyd，这几个会全部漏掉——而 ConPTY 后端运行时要按
# 相对路径找到它们。collect_all 把整个包连同这些文件一起收进来。
if sys.platform == "win32":
    winpty_datas, winpty_binaries, winpty_hidden = collect_all("winpty")
    datas.extend(winpty_datas)
    binaries.extend(winpty_binaries)
    hiddenimports.extend(winpty_hidden)

    # SMTC（ncmgui/smtc.py）用到的 WinRT 投影。每个 Windows 命名空间是一个
    # .pyd，运行时按类型名动态导入别的命名空间，PyInstaller 的静态分析看不到；
    # collect_all 会把已安装的 winrt/* 以及 msvcp140.dll 一起收进来 ——
    # 那个 dll 是 _winrt.pyd 的 C++ 运行时依赖，必须和它落在同一个目录。
    # 少收任何一个，媒体控件都会在用户机器上静默失效。
    winrt_datas, winrt_binaries, winrt_hidden = collect_all("winrt")
    datas.extend(winrt_datas)
    binaries.extend(winrt_binaries)
    hiddenimports.extend(winrt_hidden)
    hiddenimports.extend(
        [
            "winrt.runtime",
            "winrt.system",
            "winrt.windows.foundation",
            "winrt.windows.foundation.collections",
            "winrt.windows.media",
            "winrt.windows.media.core",
            "winrt.windows.media.playback",
            "winrt.windows.storage",
            "winrt.windows.storage.streams",
        ]
    )

# ── 图标 ────────────────────────────────────────────────────────────────────
# 放进包里的这份供 Qt 窗口图标使用（ncmgui/resources.py 按相对位置找它）。
# AppImage 与 Windows 可执行文件用的是 packaging/ 下的同一份源文件。
icon_png = HERE / "packaging" / "ncm-studio.png"
if icon_png.is_file():
    datas.append((str(icon_png), "ncmgui/assets"))
else:
    warnings.warn(f"缺少图标 {icon_png}，窗口图标将不可用")

# ── ncmctl ──────────────────────────────────────────────────────────────────
# 放 binaries 而不是 datas：binaries 会被**无条件** chmod 0755，datas 则要看
# 源文件本身有没有执行位。万一某次 checkout 或下载丢了权限位，datas 会静默退化
# 成一个不可执行的普通文件，直到用户点下载才报 Permission denied —— 那是最难
# 查的一类打包 bug。
helper_name = "ncmctl.exe" if sys.platform == "win32" else "ncmctl"
helper = HERE / "vendor" / helper_name
if helper.is_file():
    binaries.append((str(helper), "."))
else:
    if REQUIRE_NCMCTL:
        raise SystemExit(
            f"NCMGUI_BUNDLE_NCMCTL=1 但找不到 {helper}\n"
            "先跑 python3 scripts/fetch_ncmctl.py"
        )
    warnings.warn(f"未找到 {helper}，本次产物不含 ncmctl")

# 分发第三方二进制必须带上它的许可。ncmctl 是 MIT
#
# 注意 datas 元组的第二项是**目标目录**而不是文件名 —— 源文件叫什么，落地就还
# 叫什么。所以这里给它们单独一个目录，避免要和本程序自己的 LICENSE 挤在一起。
for license_name in ("LICENSE", "README.md"):
    license_file = HERE / "vendor" / license_name
    if license_file.is_file():
        datas.append((str(license_file), "ncmgui/assets/ncmctl"))

# ── mpv ─────────────────────────────────────────────────────────────────────
# 「我喜欢的音乐」用 mpv 播流媒体。它落地到包里的 ``mpv/`` 目录 —— 与
# ncmgui.player 里的 ``BUNDLED_DIR`` 一致；onefile 与 onedir 都会变成
# ``_MEIPASS/mpv/``，所以 find_mpv() 只需要认这一个位置。
#
# Linux 上 mpv 是动态链接的，它的共享库放在 ``mpv/lib/``，由
# player.child_env() 前置进 LD_LIBRARY_PATH；Windows 则是一堆 .dll 平铺在
# ``mpv/`` 里。
mpv_dir = HERE / "vendor" / "mpv"
mpv_exe = mpv_dir / ("mpv.exe" if sys.platform == "win32" else "mpv")
if mpv_exe.is_file():
    binaries.append((str(mpv_exe), "mpv"))

    lib_dir = mpv_dir / "lib"
    if lib_dir.is_dir():
        for lib in sorted(lib_dir.glob("*")):
            if lib.is_file():
                binaries.append((str(lib), "mpv/lib"))

    for dll in sorted(mpv_dir.glob("*.dll")):
        binaries.append((str(dll), "mpv"))

    # 许可与来源说明。分发 GPL 二进制必须带上
    for extra in sorted(mpv_dir.glob("COPYRIGHT*")) + sorted(mpv_dir.glob("LICENSE*")):
        datas.append((str(extra), "ncmgui/assets/mpv"))
    notice = mpv_dir / "NOTICE.txt"
    if notice.is_file():
        datas.append((str(notice), "ncmgui/assets/mpv"))
else:
    if REQUIRE_MPV:
        raise SystemExit(
            f"NCMGUI_BUNDLE_MPV=1 但找不到 {mpv_exe}\n"
            "先跑 python3 scripts/fetch_mpv.py"
        )
    warnings.warn(f"未找到 {mpv_exe}，本次产物不含播放器")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 这些是 PyQt6 拖得进来、界面里一个都没用到的大家伙
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngine",
        "PyQt6.QtBluetooth",
        "PyQt6.QtMultimedia",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# icon= 只在 Windows / macOS 生效，Linux 的图标完全由 AppImage 那边的 AppDir 决定
exe_icon = HERE / "packaging" / ("ncm-studio.ico" if sys.platform == "win32" else "ncm-studio.png")
exe_kwargs = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # 不要另外弹一个黑框控制台：所有输出都走界面里的作业控制台。
    # ncmctl 子进程有它自己的伪终端，跟这个开关没有关系。
    # （Linux 上这个参数本就无效，启动器总是同一个。）
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(exe_icon) if exe_icon.is_file() else None,
)

if ONEDIR:
    # exclude_binaries=True 是必须的：否则 EXE 会自己打成一个 onefile 包，
    # COLLECT 再收一遍，两头都不对
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=NAME,
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_kwargs)
