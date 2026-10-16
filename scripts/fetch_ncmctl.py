#!/usr/bin/env python3
"""把指定版本的 ncmctl 取到 ``vendor/``，供打包时随程序分发。

    python3 scripts/fetch_ncmctl.py

本程序是 ncmctl 的图形前端，两者版本必须匹配，所以这里钉死版本号与校验和，
不接受「随便下个最新的」。版本号写在 :mod:`ncmgui` 的 ``NCMCTL_VERSION``。

**校验和是硬编码的，不是从网上下回来比对的。** 从同一个 release 下载校验和文件
再比对，只能防传输损坏，防不住 release 本身被换掉 —— 那样两边一起变，比对照样通过。
写死在仓库里，上游动了任何字节这里就会失败。

升级到新版本时：改 ``NCMCTL_VERSION``，跑一次脚本让它报出新的实际校验和，填进
``_CHECKSUMS``。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ncmgui import NCMCTL_VERSION, ensure_utf8_stdout  # noqa: E402

#: 仓库根目录。
ROOT = Path(__file__).resolve().parent.parent

#: 下载物放这里。已在 .gitignore 里 —— 20 MB 的二进制不进版本库。
VENDOR = ROOT / "vendor"

_RELEASE_URL = "https://github.com/chaunsin/netease-cloud-music/releases/download"

#: 各平台对应的产物名与 SHA-256。
#:
#: 键是 ``(sys.platform 前缀, 目标架构)``。目前只发 x86_64 的 Linux/Windows 和
#: arm64 的 macOS —— 与 CI 的三平台矩阵一致。要加平台就在这里加一行。
_CHECKSUMS: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "ncmctl_Linux_x86_64.tar.gz",
        "66c52ed88e22fd4af316515c775e7a93aa851ca0a192f494753c5396c81a6873",
    ),
    ("win32", "x86_64"): (
        "ncmctl_Windows_x86_64.zip",
        "bae8a94d87e6999b1f908630f636af1bce8098d2f153b049f42e82c22244fd2a",
    ),
    ("darwin", "arm64"): (
        "ncmctl_Darwin_arm64.tar.gz",
        "e8f646cf79f214e2ac9311b24e3af7ee678e9ad4c5e918e018885d4a48f688e4",
    ),
}


def _host_key() -> tuple[str, str]:
    """当前机器对应哪个产物。

    刻意用 :data:`sys.platform` 而不是 ``platform.system()``：前者在交叉编译和
    打包场景下的行为更可预期，且与项目里其它平台判断（``ncmgui/ncmctl.py`` 的
    ``_IS_WINDOWS``）保持一致。
    """
    import platform

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        arch = machine

    if sys.platform.startswith("linux"):
        platform_key = "linux"
    elif sys.platform == "win32":
        platform_key = "win32"
    elif sys.platform == "darwin":
        platform_key = "darwin"
    else:
        platform_key = sys.platform

    return platform_key, arch

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    print(f"  下载 {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            with dest.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"下载失败：HTTP {exc.code} {exc.reason}\n"
            f"URL: {url}\n"
            "该版本可能还没有对应平台的产物，检查 ncmgui/__init__.py 里的 "
            "NCMCTL_VERSION 与 scripts/fetch_ncmctl.py 里的 _CHECKSUMS。"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"下载失败：{exc.reason}\nURL: {url}") from exc


def _extract(archive: Path, into: Path) -> list[str]:
    """解出压缩包里的所有文件，返回文件名列表。

    只按文件名取，不看目录结构 —— 上游把文件平铺在包根下，但万一以后加了层
    目录也不会因此取不到。
    """
    names: list[str] = []
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = Path(member.filename).name
                if not name:
                    continue
                with zf.open(member) as src, (into / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                names.append(name)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if not name:
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, (into / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                names.append(name)
    return names


def main() -> int:
    # 下面的输出里有「→」和中文。Windows 控制台默认 cp1252，编码不了这些字符，
    # 一 print 就 UnicodeEncodeError —— 而这是脚本的第一句话，等于一行活没干就崩。
    ensure_utf8_stdout()

    key = _host_key()
    entry = _CHECKSUMS.get(key)
    if entry is None:
        supported = "、".join(f"{p}/{a}" for p, a in _CHECKSUMS)
        raise SystemExit(
            f"没有为 {key[0]}/{key[1]} 准备产物。\n目前支持：{supported}\n"
            "需要新平台就往 scripts/fetch_ncmctl.py 的 _CHECKSUMS 里加一条。"
        )

    asset, expected = entry
    url = f"{_RELEASE_URL}/v{NCMCTL_VERSION}/{asset}"

    print(f"ncmctl v{NCMCTL_VERSION} → {key[0]}/{key[1]}")
    print(f"  产物 {asset}")

    VENDOR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ncmctl-fetch-") as tmpdir:
        archive = Path(tmpdir) / asset
        _download(url, archive)

        actual = _sha256(archive)
        if actual != expected:
            raise SystemExit(
                "校验和不匹配，已中止。\n"
                f"  期望 {expected}\n"
                f"  实际 {actual}\n"
                f"  文件 {asset}\n\n"
                "这可能是上游重新发布了同名产物，也可能是下载被篡改。\n"
                "不要直接放行 —— 先去 releases 页面确认，再决定是否更新 "
                "scripts/fetch_ncmctl.py 里的 _CHECKSUMS。"
            )
        print("  校验和一致")

        names = _extract(archive, Path(tmpdir))
        exe_name = "ncmctl.exe" if key[0] == "win32" else "ncmctl"
        if exe_name not in names:
            raise SystemExit(
                f"压缩包里没有 {exe_name}，只有：{names}\n"
                "上游的打包布局可能变了，需要调整 _extract。"
            )

        # 只留这三样：可执行文件、许可证、上游 README。
        # 许可证是必须的 —— 分发第三方二进制得带上它的许可声明。
        for name in (exe_name, "LICENSE", "README.md"):
            if name in names:
                target = VENDOR / name
                shutil.copyfile(Path(tmpdir) / name, target)
                if name == exe_name and key[0] != "win32":
                    target.chmod(0o755)

    exe = VENDOR / ("ncmctl.exe" if key[0] == "win32" else "ncmctl")
    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"  完成 → {exe.relative_to(ROOT)}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
