#!/usr/bin/env python3
"""把 mpv 取到 ``vendor/mpv/``，供打包时随程序分发。

    python3 scripts/fetch_mpv.py

三个平台的来源不一样，原因也写在这里：

* **Linux**：mpv 官方**不发**静态构建，也只有一个随发行版走的动态包。所以在
  构建机上 ``apt install mpv``，再把 ``/usr/bin/mpv`` 连同 ``ldd`` 报出来的共享库
  一起收进 ``vendor/mpv/``（库放 ``vendor/mpv/lib/``）。产物与 AppImage 一样以
  构建机的 glibc 为下限（CI 是 Ubuntu 22.04，glibc 2.35）。
* **Windows**：用 zhongfly/mpv-winbuild 的**日期 tag**。它的 tag 一旦发布就不再
  变，所以可以像 ncmctl 那样把版本和 SHA-256 钉死在仓库里。产物是 ``.7z``，
  解压优先用系统的 ``7z``（上游用了 BCJ2 过滤器，``py7zr`` 解不了），没有时
  退回 ``py7zr``（仅构建期依赖）。
* **macOS**：官方 ``git-release`` 是滚动 tag，旧产物会被覆盖；第三方构建又有嵌套
  签名的问题。首版**不随包**，退回探测系统 mpv（``brew install mpv``）。

**校验和同样硬编码，不从网上下回来比对** —— 理由见 ``scripts/fetch_ncmctl.py``。

升级 Windows 版本时：改 ``_WINDOWS_TAG`` / ``_WINDOWS_ASSET``，先跑一次让它报出
实际校验和，确认无误后填进 ``_WINDOWS_SHA256``。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ncmgui import ensure_utf8_stdout  # noqa: E402

#: 仓库根目录。
ROOT = Path(__file__).resolve().parent.parent

#: 播放器放这里，目标是打包后的 ``_MEIPASS/mpv/``。已在 .gitignore 里。
VENDOR = ROOT / "vendor" / "mpv"

#: 可执行文件与共享库分别落在哪里。``lib/`` 会被 ``player.child_env()``
#: 前置进 ``LD_LIBRARY_PATH``。
LINUX_LIB_DIR = VENDOR / "lib"

_WINDOWS_TAG = "2026-10-16-dde0671072"
_WINDOWS_ASSET = "mpv-x86_64-20261016-git-dde0671072.7z"
_WINDOWS_SHA256 = "4bac0ef03e79d24c10128a7d0a4cd8c51b7f0a55774f8080d6fe0889c82a5063"
_WINDOWS_URL = (
    "https://github.com/zhongfly/mpv-winbuild/releases/download/"
    f"{_WINDOWS_TAG}/{_WINDOWS_ASSET}"
)

#: 不带进产物的库。理由与 ``packaging/bundle_libs.py`` 相同：
#: glibc 家族与宿主 ``ld-linux`` 强绑定；``libstdc++`` ABI 敏感；
#: 图形驱动必须用用户机器上那份，否则会顶掉硬件加速甚至起不来。
_SKIP_PREFIXES = (
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
    "libgcc_s",
    "libstdc++",
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
    # 内核/系统接口与桌面基础库，随发行版走。
    # 注意 libasound / libpulse **不在此列**：它们被 mpv 直接 NEEDED，
    # 缺了连进程都起不来，值得自带。
    "libudev",
    "libsystemd",
    "libdbus-1",
    "libX11",
    "libxcb",
    "libwayland",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES)


def _download(url: str, dest: Path) -> None:
    print(f"  下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "ncm-studio-build"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            dest.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"下载失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #

def _ldd_closure(binary: Path) -> list[Path]:
    """``ldd`` 输出的共享库（已含间接依赖）。

    ``ldd`` 是脚本，只能解析文本 —— 每行形如 ``libfoo.so.1 => /path/libfoo.so.1``；
    ``linux-vdso`` 与 ``not found`` 行跳过。
    """
    try:
        proc = subprocess.run(
            ["ldd", str(binary)], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"跑不了 ldd：{exc}") from exc

    libs: list[Path] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "=>" in line:
            target = line.split("=>", 1)[1].strip()
        elif line.startswith("/"):
            target = line
        else:
            continue
        target = target.split(" (", 1)[0].strip()
        if not target or target == "not found":
            print(f"  ⚠ 缺库：{line}")
            continue
        path = Path(target)
        if path.is_file():
            libs.append(path)
    return libs


def _fetch_linux() -> int:
    binary = shutil.which("mpv")
    if not binary:
        raise SystemExit(
            "没找到 mpv。构建机上先装一个：\n"
            "  sudo apt-get install -y --no-install-recommends mpv"
        )

    real = Path(binary).resolve()
    print(f"  mpv：{real}")

    VENDOR.mkdir(parents=True, exist_ok=True)
    LINUX_LIB_DIR.mkdir(parents=True, exist_ok=True)

    target = VENDOR / "mpv"
    shutil.copyfile(real, target)
    target.chmod(0o755)

    kept = skipped = 0
    for lib in _ldd_closure(real):
        if _skip(lib.name):
            skipped += 1
            continue
        destination = LINUX_LIB_DIR / lib.name
        if destination.exists():
            continue
        shutil.copyfile(lib, destination)
        destination.chmod(0o755)
        kept += 1

    print(f"  共享库：收了 {kept} 个，跳过 {skipped} 个（glibc / 驱动 / 系统接口）")

    # 版权声明：分发 GPL 二进制必须带上许可。不同发行版路径不同，都试一下
    for candidate in (
        Path("/usr/share/doc/mpv/copyright"),
        Path("/usr/share/licenses/mpv/LICENSE"),
    ):
        if candidate.is_file():
            shutil.copyfile(candidate, VENDOR / "COPYRIGHT")
            break
    _write_notice(
        source="发行版的 mpv 软件包",
        version=_version_of(target),
    )
    return 0


def _version_of(binary: Path) -> str:
    try:
        proc = subprocess.run(
            # --terminal=no 会连 --version 的输出一起关掉，只能不加
            [str(binary), "--no-config", "--version"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return "未知"
    first = (proc.stdout or "").strip().splitlines()
    return first[0] if first else "未知"


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

def _find_seven_zip() -> str | None:
    """找 7z。除了 PATH，也看 Windows 的默认安装目录 —— runner 上常常是装了
    但没进 PATH。"""
    found = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if found:
        return found
    if sys.platform == "win32":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(key)
            if not root:
                continue
            candidate = Path(root) / "7-Zip" / "7z.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _fetch_windows() -> int:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError:
        py7zr = None

    seven_zip = _find_seven_zip()

    if py7zr is None and seven_zip is None:
        raise SystemExit(
            "解压 .7z 需要 py7zr 或 7z：\n"
            "  pip install py7zr\n"
            "（这是构建期依赖，不用写进 requirements.txt）"
        )

    VENDOR.mkdir(parents=True, exist_ok=True)

    # Windows 上 Defender 会在解压后继续扫这个 .7z，删临时目录时撞上
    # WinError 32 是常事。东西已经复制进 vendor/ 了，清理失败不该让整个
    # 构建失败 —— 系统迟早会回收 %TEMP%。
    with tempfile.TemporaryDirectory(
        prefix="mpv-fetch-", ignore_cleanup_errors=True
    ) as tmpdir:
        archive = Path(tmpdir) / _WINDOWS_ASSET
        _download(_WINDOWS_URL, archive)

        actual = _sha256(archive)
        if actual != _WINDOWS_SHA256:
            raise SystemExit(
                "校验和不匹配，已中止。\n"
                f"  期望 {_WINDOWS_SHA256}\n"
                f"  实际 {actual}\n"
                f"  文件 {_WINDOWS_ASSET}\n\n"
                "上游同名产物被换掉或被篡改的可能性都有。\n"
                "确认 releases 页面之后再更新 scripts/fetch_mpv.py 里的常量。"
            )
        print("  校验和一致")

        unpacked = Path(tmpdir) / "unpacked"
        unpacked.mkdir()
        # 优先用系统的 7z：上游开始用 BCJ2 过滤器压 mpv.exe，py7zr 不支持这种
        # 编码（会直接抛 UnsupportedCompressionMethodError）。没有 7z 的机器上
        # 再退回 py7zr —— 能解开旧包，新包则拿到一条说得清的报错。
        errors: list[str] = []
        extracted = False
        if seven_zip is not None:
            proc = subprocess.run(
                [seven_zip, "x", "-y", f"-o{unpacked}", str(archive)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                extracted = True
            else:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                errors.append(f"7z：{detail[0] if detail else proc.returncode}")
        if not extracted and py7zr is not None:
            try:
                with py7zr.SevenZipFile(archive, mode="r") as handler:
                    handler.extractall(path=unpacked)
                extracted = True
            except Exception as exc:  # noqa: BLE001 - 解压器的异常类型随版本变
                errors.append(f"py7zr：{exc}")
        if not extracted:
            raise SystemExit(
                "解不开 mpv 的 .7z：\n  " + "\n  ".join(errors) + "\n"
                "装一个 7-Zip（或 7z 命令）后重试；py7zr 解不了 BCJ2 压缩。"
            )

        # 发行包可能把所有东西平铺在根上，也可能放进子目录，统一按文件名收
        wanted = [
            path
            for path in unpacked.rglob("*")
            if path.is_file() and (
                path.name.lower() == "mpv.exe"
                or path.suffix.lower() == ".dll"
            )
        ]
        if not any(path.name.lower() == "mpv.exe" for path in wanted):
            raise SystemExit(
                f"压缩包里没有 mpv.exe，只有：{[p.name for p in unpacked.rglob('*')][:20]}\n"
                "上游的打包布局可能变了，需要调整 _fetch_windows。"
            )

        for path in wanted:
            destination = VENDOR / path.name
            shutil.copyfile(path, destination)

        licenses = [
            path for path in unpacked.rglob("*")
            if path.is_file() and ("license" in path.name.lower() or "copyright" in path.name.lower())
        ]
        for path in licenses[:2]:
            shutil.copyfile(path, VENDOR / f"COPYRIGHT-{path.name}")

    _write_notice(
        source=f"zhongfly/mpv-winbuild（{_WINDOWS_TAG}）",
        version=_WINDOWS_ASSET,
    )
    return 0


# --------------------------------------------------------------------------- #
# 公共
# --------------------------------------------------------------------------- #

def _write_notice(source: str, version: str) -> None:
    text = f"""本目录中的 mpv 由构建脚本 scripts/fetch_mpv.py 收集，随本程序分发。

来源：{source}
版本：{version}

许可：mpv 本体以 GPLv2 或更高版本发布（详见 COPYRIGHT*）。mpv 静态/动态链接了
FFmpeg 及其它库，各自的许可见所附的版权文件；分发时请一并保留。

本程序（NCM Studio）仅以子进程方式调用 mpv，不修改其源码。
"""
    (VENDOR / "NOTICE.txt").write_text(text, encoding="utf-8")


def main() -> int:
    ensure_utf8_stdout()

    if sys.platform.startswith("linux"):
        print("收集 mpv → vendor/mpv/")
        code = _fetch_linux()
    elif sys.platform == "win32":
        print("下载 mpv → vendor/mpv/")
        code = _fetch_windows()
    elif sys.platform == "darwin":
        print(
            "macOS 首版不随包提供 mpv。\n"
            "  请用户自行安装：brew install mpv\n"
            "  本程序会自动探测 /opt/homebrew/bin/mpv。"
        )
        return 0
    else:
        raise SystemExit(f"不认识的平台：{sys.platform}")

    binary = VENDOR / ("mpv.exe" if sys.platform == "win32" else "mpv")
    if code == 0 and binary.is_file():
        size_mb = sum(
            path.stat().st_size for path in VENDOR.rglob("*") if path.is_file()
        ) / (1024 * 1024)
        print(f"  完成 → {VENDOR.relative_to(ROOT)}（共 {size_mb:.1f} MB）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
