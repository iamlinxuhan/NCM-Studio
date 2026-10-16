#!/usr/bin/env python3
"""扫出产物真正能跑的**最低 glibc 版本**，超线就失败。

    python3 packaging/check_glibc_floor.py <AppDir 或任何目录> [--max 2.35]

── 为什么需要这个 ──────────────────────────────────────────────────────────

AppImage 打包有个特别容易踩的坑：**构建机上越新，产物在别人机器上越跑不动。**

PyInstaller 会把构建机上的系统库（libglib、libgtk、libstdc++、libxcb……）一起收
进产物。这些库是在构建机的 glibc 上编译的，于是它们要求的最高 ``GLIBC_x.y`` 就
成了产物的实际门槛 —— 与 PyQt6 自己的 manylinux 标签毫无关系。

后果是：在 Ubuntu 24.04（glibc 2.39）上打出来的包，在 Debian 12（2.36）上启动
即 ``version `GLIBC_2.39' not found``；构建、上传、发布全都没报错，等到用户下载
才发现。这正是「构建成功 ≠ 能用」，和 pywinpty 少一个 conpty.dll 是同一类问题。

所以这里把它检测出来，并且**在 CI 里当成硬性失败**。要在老系统上也能跑，就得在
老系统上构建（CI 里固定 ``ubuntu-22.04``），或者少收几个构建机的库。

── 关于 objdump ────────────────────────────────────────────────────────────

``objdump -T`` 会列出动态符号表，其中带版本的需求形如 ``GLIBC_2.34``。对静态链接
的二进制（本项目的 ncmctl 就是）这一列是空的，属正常。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: 本项目声称支持的 glibc 下限。由 PyQt6 的 manylinux_2_34 wheel 决定 —— 这是
#: 换打包格式、换构建机都降不下去的硬底。低于它没有意义，高于它就得解释。
DEFAULT_MAX = "2.35"

_VERSION_RE = re.compile(r"\bGLIBC_(\d+)\.(\d+)\b")

def _looks_like_elf(path: Path) -> bool:
    """只看文件名。靠魔数判断更准，但对 300 个文件逐个开文件也更慢 —— 这里
    宁可多扫几个非 ELF（objdump 自己会返回空），也别漏掉真正要紧的库。

    ``.so`` 的判断必须宽松：库名是 ``libfoo.so.0``、``libfoo.so.1.2.3`` 这种，
    枚举版本号必然漏 —— 早先这里写死了 ``.so.1`` 到 ``.so.6``，于是
    ``libsystemd.so.0``（全产物里要求最高、2.39 的那个）被静默跳过，
    检查器报出 2.38 这个偏乐观的结论。一个会漏报的守卫比没有守卫更糟。
    """
    name = path.name
    if ".so" in name:
        return True
    # Python 扩展、以及没有扩展名的可执行文件（PyInstaller 的启动器）
    return ".cpython-" in name or path.suffix == ""


def _max_glibc(path: Path) -> tuple[int, int] | None:
    """这个文件要求的最高 glibc 版本。取不到返回 None。"""
    try:
        out = subprocess.run(
            ["objdump", "-T", str(path)],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    best: tuple[int, int] | None = None
    for major, minor in _VERSION_RE.findall(out):
        version = (int(major), int(minor))
        if best is None or version > best:
            best = version
    return best


def _parse(value: str) -> tuple[int, int]:
    parts = value.strip().split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise argparse.ArgumentTypeError(f"版本号要形如 2.35，收到 {value!r}")
    return int(parts[0]), int(parts[1])


def _fmt(version: tuple[int, int] | None) -> str:
    return "无" if version is None else f"{version[0]}.{version[1]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查产物的 glibc 下限")
    parser.add_argument("path", help="AppDir 或任何要扫的目录")
    parser.add_argument(
        "--max", type=_parse, default=_parse(DEFAULT_MAX),
        help=f"允许的最高 glibc，超过就退出码 1（默认 {DEFAULT_MAX}）",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"找不到目录：{root}", file=sys.stderr)
        return 2

    if shutil.which("objdump") is None:
        print("错误：没有 objdump（binutils），无法检查 glibc 下限", file=sys.stderr)
        return 2

    # follow_symlinks：PyInstaller 的 onedir 里有些库是符号链接，
    # 不跟着走就会漏掉它们（.so 计数凭空少一半，结论也就跟着错）
    offenders: list[tuple[tuple[int, int], str]] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _looks_like_elf(path):
            continue
        scanned += 1
        version = _max_glibc(path)
        if version is not None:
            offenders.append((version, str(path.relative_to(root))))

    if not offenders:
        print(f"已扫 {scanned} 个文件，没有任何 glibc 版本需求（可能是纯静态产物）")
        return 0

    offenders.sort(reverse=True)
    floor = offenders[0][0]

    print(f"已扫 {scanned} 个文件，glibc 下限 = {_fmt(floor)}（允许 {_fmt(args.max)}）")
    print("拖后腿的：")
    shown = 0
    for version, name in offenders:
        if version < args.max or shown >= 10:
            break
        print(f"  GLIBC_{_fmt(version)}  {name}")
        shown += 1
    if shown == 0:
        print(f"  （无 —— 最高的 {_fmt(floor)} 也没超过 {_fmt(args.max)}）")

    if floor > args.max:
        print(
            f"\n产物要求的 glibc（{_fmt(floor)}）高于允许值（{_fmt(args.max)}）。\n"
            "在老系统上会直接 'version `GLIBC_...' not found' 起不来。\n"
            "根因通常是构建机的系统库被 PyInstaller 收进了产物 —— 换一台更老的\n"
            "构建机（CI 里固定 ubuntu-22.04）即可。只想本机试玩、不打算分发的话，\n"
            "用 NCMGUI_MAX_GLIBC=<更高版本> 放行。",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
