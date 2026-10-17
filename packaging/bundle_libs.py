#!/usr/bin/env python3
"""把程序依赖的**系统库**收进 AppDir，让产物不依赖宿主机装了什么。

    python3 packaging/bundle_libs.py <AppDir>

PyInstaller 只带 Qt 自己的 ``.so`` 和 Python 运行时，像 ``libxcb``、
``libxkbcommon``、``fontconfig`` 这些是它在宿主机上找到什么就用什么。装得全的
桌面上没问题，最小化安装的系统上就是一片 ``error while loading shared libraries``。

两类东西要分开对待：

1. **链接期依赖** —— ``ldd`` 能列出来，正常收集。
2. **``dlopen`` 的依赖** —— Qt 的 xcb 平台插件在运行时才去开 ``libxcb-cursor``、
   ``libxkbcommon-x11`` 这些，``ldd`` 里**根本看不到**。漏了它们，程序会在
   完全没有图形界面的情况下报一句含糊的插件加载失败。所以额外按名字去系统里找。

**有些库必须用宿主机的，绝不能带。** glibc 家族与宿主的动态链接器强绑定，
带进去几乎必然崩；``libGL``/``libEGL``/``libdrm`` 与显卡驱动绑定，带一份软件渲染
的进去会把用户的硬件加速顶掉。这些在 ``_NEVER_BUNDLE`` 里排除。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: 这些一律用宿主机的。带上它们的下场见模块文档。
_NEVER_BUNDLE_PREFIXES = (
    # glibc 家族：与宿主 ld-linux 强绑定
    "ld-linux",
    "libc.so",
    "libdl.so",
    "libm.so",
    "libpthread",
    "librt.so",
    "libutil.so",
    "libnsl",
    "libresolv",
    "libanl",
    # C++ 运行时：ABI 敏感，混用比缺失更难查
    "libgcc_s",
    "libstdc++",
    # 图形驱动：必须用用户机器上那份
    "libGL",
    "libEGL",
    "libGLX",
    "libGLdispatch",
    "libOpenGL",
    "libglapi",
    "libdrm",
    "libgbm",
    "libnvidia",
    "libcuda",
    "libvdpau",
    "libva",
)

#: Qt 在运行时会 ``dlopen`` 但不出现在 ``ldd`` 里的库。按前缀从系统里捞。
#: 这些正是最小化系统上最常见的「插件加载失败」元凶。
_DLOPEN_PATTERNS = (
    "libxcb-",
    "libxcb.so",
    "libxkbcommon",
    "libX11-xcb",
)


def _ldd(path: Path) -> dict[str, Path]:
    """返回 ``{库名: 实际路径}``。取不到（非 ELF、无权限）时返回空。"""
    try:
        out = subprocess.run(
            ["ldd", str(path)], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    found: dict[str, Path] = {}
    for line in out.splitlines():
        # 形如 "  libfoo.so.1 => /usr/lib/x86_64-linux-gnu/libfoo.so.1 (0x...)"
        if "=>" not in line:
            continue
        name, _, rest = line.partition("=>")
        target = rest.strip().split(" ")[0]
        if not target or not target.startswith("/"):
            continue
        candidate = Path(target)
        if candidate.is_file():
            found[name.strip()] = candidate.resolve()
    return found


def _system_libs(match) -> dict[str, Path]:
    """用 ``ldconfig -p`` 按名字找系统库。"""
    try:
        out = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    found: dict[str, Path] = {}
    for line in out.splitlines():
        if "=>" not in line:
            continue
        name, _, target = line.partition("=>")
        name = name.strip()
        target = target.strip()
        if not name or "(" in name:
            continue
        if not match(name):
            continue
        candidate = Path(target)
        if candidate.is_file():
            found[name] = candidate.resolve()
    return found


def _is_denied(name: str) -> bool:
    return name.startswith(_NEVER_BUNDLE_PREFIXES)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2

    appdir = Path(argv[1]).resolve()
    if not appdir.is_dir():
        print(f"找不到 AppDir：{appdir}", file=sys.stderr)
        return 2

    target_dir = appdir / "usr" / "lib"
    target_dir.mkdir(parents=True, exist_ok=True)

    # 只扫程序本体和共享库。对整个 _internal/ 逐个文件跑 ldd 要几分钟，而且
    # 目录里的 .py/.png 之类根本不是 ELF，白跑
    elf_files = [p for p in appdir.rglob("*.so*") if p.is_file()]
    main_exe = appdir / "usr" / "bin" / "ncm-studio"
    if main_exe.is_file():
        elf_files.append(main_exe)

    wanted: dict[str, Path] = {}
    for elf in elf_files:
        wanted.update(_ldd(elf))

    # dlopen 的那些 ldd 看不到，单独从系统里找
    dlopened = _system_libs(lambda n: n.startswith(_DLOPEN_PATTERNS))
    wanted.update(dlopened)

    # AppDir 里已经有的（PyInstaller 打进来的 Qt 库）不用再收
    existing = {p.name for p in appdir.rglob("*.so*")}

    copied: list[str] = []
    skipped_driver: list[str] = []
    skipped_have: list[str] = []

    for name, source in sorted(wanted.items()):
        if name in existing:
            skipped_have.append(name)
            continue
        if _is_denied(name):
            skipped_driver.append(name)
            continue
        destination = target_dir / name
        if destination.exists():
            continue
        shutil.copy2(source, destination)
        copied.append(name)

    print(f"  收进 {len(copied)} 个系统库 → usr/lib")
    for name in copied:
        print(f"    + {name}")
    if skipped_driver:
        print(f"  按策略留给宿主机的（{len(skipped_driver)} 个）：")
        for name in skipped_driver:
            print(f"    - {name}")
    print(f"  已在包里、无需重收：{len(skipped_have)} 个")

    # 一条硬性检查：绝对不能把 glibc 收进来。收错了产物在别的机器上必崩，
    # 而且崩法千奇百怪，很难联想到打包问题上
    leaked = [n for n in copied if n.startswith(("ld-linux", "libc.so", "libpthread", "libdl.so"))]
    if leaked:
        print(f"错误：glibc 家族被收进来了 {leaked}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
