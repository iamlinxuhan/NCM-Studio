#!/usr/bin/env python3
"""生成提交到 winget-pkgs 的三份清单。

winget 的包仓库（``microsoft/winget-pkgs``）要求每个版本三份清单，其中商店
信息按语言各一份，一共四个文件：

    manifests/i/iamlinxuhan/NCMStudio/<版本>/
      iamlinxuhan.NCMStudio.yaml                    版本与默认语言
      iamlinxuhan.NCMStudio.installer.yaml          安装包地址、校验和与静默参数
      iamlinxuhan.NCMStudio.locale.zh-CN.yaml       中文商店信息
      iamlinxuhan.NCMStudio.locale.en-US.yaml       英文商店信息

安装包用的是 Inno Setup 那份 ``ncm-studio-windows-x86_64-setup.exe``，不是便携版：
winget 需要能静默安装、能在「应用和功能」里卸载的安装程序。校验和必须与
Release 里的文件一致，所以 CI 是**下载已发布的资产**再算，不接受本地随便一个 exe。

用法::

    # 本地生成（默认读取 ncmgui.VERSION，安装包需已存在）
    python scripts/winget_manifests.py \
        --installer dist/ncm-studio-windows-x86_64-setup.exe

    # 指定输出目录与版本（CI 用）
    python scripts/winget_manifests.py \
        --version 3.0.12 --installer dist/setup.exe --out dist/winget/manifests

自检不联网、不读真实安装包::

    python scripts/winget_manifests.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ncmgui import VERSION  # noqa: E402

PACKAGE_ID = "iamlinxuhan.NCMStudio"
PUBLISHER = "iamlinxuhan"
PACKAGE_NAME = "NCM Studio"
PROJECT_URL = "https://github.com/iamlinxuhan/NCM-Studio"
INSTALLER_NAME = "ncm-studio-windows-x86_64-setup.exe"
MANIFEST_VERSION = "1.10.0"
DEFAULT_LOCALE = "zh-CN"
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")

SHORT_ZH = "基于 ncmctl 的第三方网易云音乐播放器，支持搜索、推荐、歌单、本地曲库、歌词与下载。"
SHORT_EN = "A third-party NetEase Cloud Music player built on ncmctl, with search, playlists, local library, lyrics, and downloads."

DESCRIPTION_ZH = """\
NCM Studio 是 ncmctl 的图形前端，提供搜索、推荐、歌单、每日推荐、本地曲库、歌词、
下载与流媒体服务器播放。界面用 PyQt6 编写，实际播放交给随包的 mpv，不需要另外安装
Python、ncmctl 或 mpv。

Windows 版还接入了系统媒体控件（SMTC）：键盘媒体键、任务栏「正在播放」浮层与锁屏
播放器都能显示当前曲目，并控制播放、切歌与进度。"""

DESCRIPTION_EN = """\
NCM Studio is a graphical frontend for ncmctl. It provides search, recommendations,
playlists, a local music library, lyrics, downloads, and streaming-server playback.
The interface is built with PyQt6, while playback is delegated to the bundled mpv;
no separate Python, ncmctl, or mpv installation is required.

On Windows it integrates with System Media Transport Controls (SMTC), so media keys,
the taskbar now-playing flyout, and the lock screen can display and control playback."""

TAGS = (
    "music",
    "music-player",
    "netease",
    "netease-cloud-music",
    "ncmctl",
    "mpv",
    "pyqt6",
    "streaming",
)


def _quoted(value: str) -> str:
    """YAML 双引号标量。用 JSON 转义，中文保持原样。"""
    return json.dumps(str(value), ensure_ascii=False)


def _block(text: str, indent: int = 2) -> str:
    """把多行文本写成 YAML 块标量（``|-``）的内容。"""
    pad = " " * indent
    return "\n".join(pad + line if line else "" for line in text.splitlines())


def default_url(version: str) -> str:
    return (
        f"https://github.com/iamlinxuhan/NCM-Studio/releases/download/"
        f"v{version}/{INSTALLER_NAME}"
    )


def _tag(version: str) -> str:
    return f"v{version}"


def manifest_files(
    version: str,
    sha256: str,
    *,
    url: str | None = None,
    release_date: str | None = None,
) -> dict[str, str]:
    """生成四个清单文件的内容，返回 ``文件名 → 文本``。

    ``sha256`` 是安装包十六进制摘要（大小写都行，输出统一大写）。
    """
    if not VERSION_RE.match(version):
        raise ValueError(f"版本号格式不对：{version!r}（应形如 3.0.12）")
    digest = sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("SHA256 必须是 64 位十六进制")
    digest = digest.upper()
    url = url or default_url(version)
    if not url.startswith("https://"):
        raise ValueError("安装包地址必须是 https")
    release_date = release_date or date.today().isoformat()

    prefix = PACKAGE_ID
    common = (
        f"PackageIdentifier: {prefix}\n"
        f"PackageVersion: {version}\n"
    )
    header = (
        f"# yaml-language-server: $schema="
        f"https://aka.ms/winget-manifest.{{kind}}.1.10.0.schema.json\n\n"
    )

    version_yaml = (
        header.format(kind="version")
        + common
        + f"DefaultLocale: {DEFAULT_LOCALE}\n"
        + "ManifestType: version\n"
        + f"ManifestVersion: {MANIFEST_VERSION}\n"
    )

    installer_yaml = (
        header.format(kind="installer")
        + common
        + "Platform:\n"
        + "- Windows.Desktop\n"
        + "MinimumOSVersion: 10.0.0.0\n"
        + "InstallerType: inno\n"
        + "Scope: user\n"
        + "InstallModes:\n"
        + "- interactive\n"
        + "- silent\n"
        + "- silentWithProgress\n"
        + "InstallerSwitches:\n"
        + "  Silent: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-\n"
        + "  SilentWithProgress: /SILENT /SUPPRESSMSGBOXES /NORESTART /SP-\n"
        + "UpgradeBehavior: install\n"
        + f"ReleaseDate: {release_date}\n"
        + "Installers:\n"
        + "- Architecture: x64\n"
        + "  Scope: user\n"
        + f"  InstallerUrl: {url}\n"
        + f"  InstallerSha256: {digest}\n"
        + "ManifestType: installer\n"
        + f"ManifestVersion: {MANIFEST_VERSION}\n"
    )

    def locale_yaml(locale: str, short: str, description: str) -> str:
        return (
            header.format(kind="defaultLocale")
            + common
            + f"PackageLocale: {locale}\n"
            + f"Publisher: {PUBLISHER}\n"
            + f"PublisherUrl: {PROJECT_URL}\n"
            + f"PublisherSupportUrl: {PROJECT_URL}/issues\n"
            + f"Author: {PUBLISHER}\n"
            + f"PackageName: {PACKAGE_NAME}\n"
            + f"PackageUrl: {PROJECT_URL}\n"
            + "License: AGPL-3.0\n"
            + f"LicenseUrl: {PROJECT_URL}/blob/main/LICENSE\n"
            + "Copyright: Copyright © iamlinxuhan\n"
            + f"ShortDescription: {_quoted(short)}\n"
            + "Description: |-\n"
            + _block(description)
            + "\n"
            + "Tags:\n"
            + "".join(f"- {tag}\n" for tag in TAGS)
            + f"ReleaseNotesUrl: {PROJECT_URL}/releases/tag/{_tag(version)}\n"
            + "ManifestType: defaultLocale\n"
            + f"ManifestVersion: {MANIFEST_VERSION}\n"
        )

    return {
        f"{prefix}.yaml": version_yaml,
        f"{prefix}.installer.yaml": installer_yaml,
        f"{prefix}.locale.zh-CN.yaml": locale_yaml("zh-CN", SHORT_ZH, DESCRIPTION_ZH),
        f"{prefix}.locale.en-US.yaml": locale_yaml("en-US", SHORT_EN, DESCRIPTION_EN),
    }


def manifest_dir(version: str, root: Path) -> Path:
    """清单在 winget-pkgs 里的相对路径；首字母按仓库规则小写。"""
    return root / "i" / PUBLISHER / "NCMStudio" / version


def write_manifests(
    files: dict[str, str],
    version: str,
    root: Path,
) -> list[Path]:
    target = manifest_dir(version, root)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = target / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _selftest() -> int:
    problems: list[str] = []

    def check(label: str, condition: bool) -> None:
        if not condition:
            problems.append(label)

    digest = "ab" * 32
    files = manifest_files("3.0.12", digest, release_date="2026-10-24")
    check("四个清单", len(files) == 4)
    version_text = files[f"{PACKAGE_ID}.yaml"]
    installer_text = files[f"{PACKAGE_ID}.installer.yaml"]
    zh_text = files[f"{PACKAGE_ID}.locale.zh-CN.yaml"]
    en_text = files[f"{PACKAGE_ID}.locale.en-US.yaml"]

    check("版本", "PackageVersion: 3.0.12" in version_text)
    check("默认语言", "DefaultLocale: zh-CN" in version_text)
    check("Inno 类型", "InstallerType: inno" in installer_text)
    check("用户级安装", "Scope: user" in installer_text)
    check("静默参数", "/VERYSILENT /SUPPRESSMSGBOXES" in installer_text)
    check("校验和大写", digest.upper() in installer_text)
    check("下载地址", f"releases/download/v3.0.12/{INSTALLER_NAME}" in installer_text)
    check("发布日期", "ReleaseDate: 2026-10-24" in installer_text)
    check("中文商店名", "PackageName: NCM Studio" in zh_text)
    check("英文 locale", "PackageLocale: en-US" in en_text)
    check("许可证", "License: AGPL-3.0" in zh_text)
    check("标签", "- netease-cloud-music" in zh_text)
    check("清单版本", "ManifestVersion: 1.10.0" in installer_text)

    for bad in ("3", "3.0.12.1.2", "v3.0.12", ""):
        try:
            manifest_files(bad, digest)
        except ValueError:
            pass
        else:
            problems.append(f"坏版本号未被拒绝：{bad!r}")
    try:
        manifest_files("3.0.12", "xyz")
    except ValueError:
        pass
    else:
        problems.append("坏校验和未被拒绝")
    try:
        manifest_files("3.0.12", digest, url="http://example.com/a.exe")
    except ValueError:
        pass
    else:
        problems.append("明文 URL 未被拒绝")

    with tempfile.TemporaryDirectory(prefix="ncm-winget-") as tmp:
        root = Path(tmp)
        written = write_manifests(files, "3.0.12", root)
        check("落盘数量", len(written) == 4 and all(p.is_file() for p in written))
        check(
            "仓库路径",
            (root / "i" / "iamlinxuhan" / "NCMStudio" / "3.0.12").is_dir(),
        )

    if problems:
        print("winget 清单自检失败：")
        for item in problems:
            print(" -", item)
        return 1
    print("winget 清单自检通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 winget-pkgs 清单")
    parser.add_argument("--version", default=VERSION, help=f"默认为 {VERSION}")
    parser.add_argument("--installer", type=Path, help="安装包路径，用来计算 SHA256")
    parser.add_argument("--sha256", help="直接给校验和（与 --installer 二选一）")
    parser.add_argument("--url", help="安装包下载地址；默认按版本拼 GitHub Release")
    parser.add_argument("--date", help="发布日期，默认今天")
    parser.add_argument(
        "--out",
        type=Path,
        help="清单根目录；默认 dist/winget/manifests",
    )
    parser.add_argument("--self-test", action="store_true", help="只跑离线自检")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _selftest()

    if bool(args.installer) == bool(args.sha256):
        print("错误：--installer 与 --sha256 必须且只能给一个", file=sys.stderr)
        return 2
    if args.installer:
        if not args.installer.is_file():
            print(f"错误：找不到安装包 {args.installer}", file=sys.stderr)
            return 2
        sha = sha256_file(args.installer)
    else:
        sha = args.sha256

    try:
        files = manifest_files(
            args.version,
            sha,
            url=args.url,
            release_date=args.date,
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    root = args.out or (ROOT / "dist" / "winget" / "manifests")
    written = write_manifests(files, args.version, root)
    print(f"已生成 {len(written)} 个清单（版本 {args.version}，SHA256 {sha.upper()}）：")
    for path in written:
        print(" ", path)
    return 0


if __name__ == "__main__":
    from ncmgui import ensure_utf8_stdout

    ensure_utf8_stdout()
    raise SystemExit(main())
