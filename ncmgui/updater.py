"""从 GitHub Releases 检查并安装本程序的新版本。

流程刻意分成四步，任何一步失败都不会动到正在运行的程序：

1. 读 ``releases/latest``（GitHub 会自动跳过草稿与预发布）；
2. 下载对应平台的资产到**目标同目录**的 ``.new`` 文件；
3. 用 Release 里的 ``SHA256SUMS`` 校验 —— **拿不到校验文件就拒绝安装**；
   校验和本身还要用 ``SHA256SUMS.sig`` 验证 —— 两个文件来自同一个 Release，
   只看哈希等于让发布者自己给自己作证；签名用的私钥离线保管、不进 CI，
   公钥固定在本模块里，才是真正的信任根；
4. 替换并重启（AppImage / macOS 直接原子替换；Windows 交给一个等待进程
   退出后再移动文件的小脚本）。

本模块不依赖 Qt，纯函数部分可以直接自检（不联网、不改文件）::

    .venv/bin/python -m ncmgui.updater
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import VERSION

#: 发布仓库。资产名与 CI 里生成的名字必须一致，改名要两边一起改。
REPO = "iamlinxuhan/NCM-Studio"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
SUMS_ASSET = "SHA256SUMS"
#: ``SHA256SUMS`` 的 Ed25519 分离签名（原始 64 字节）。
SIG_ASSET = "SHA256SUMS.sig"

#: 更新链的信任根：32 字节 Ed25519 公钥的 base64。
#:
#: 私钥只保存在发布者本机的 ``~/.config/ncm-studio-release/signing-key.pem``，
#: 既不进仓库也不进 CI。轮换公钥只能随一次新版本一起发出 —— 旧版本仍认旧公钥，
#: 所以第一次换钥的版本必须用旧私钥签名。见 ``scripts/release_sign.py``。
PUBLIC_KEY_B64 = "97/Xf71U9lq6pW1nO7tGrK/+EqysI9WSNTPpTQRzb7o="

#: 三个平台在 Release 里的资产名（与 .github/workflows/build.yaml 对应）
ASSETS = {
    "linux": "ncm-studio-linux-x86_64.AppImage",
    "windows": "ncm-studio-windows-x86_64.exe",
    "macos": "ncm-studio-macos-arm64",
}

#: 支持的下载块大小。1 MB 在进度条刷新和内存占用之间比较平衡。
CHUNK = 1 << 20


class UpdateError(RuntimeError):
    """更新过程中的失败。消息面向用户，不夹带堆栈。"""


# --------------------------------------------------------------------------- #
# 版本与资产名
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...]:
    """``"v1.3.0"`` → ``(1, 3, 0)``。

    预发布后缀（``-beta.1``）与构建元数据（``+20261017``）在比较时忽略：
    ``latest`` 本来就不会返回预发布，本地版本若是 ``1.3.0-rc1``，拿它跟
    ``1.3.0`` 比也不该被判成"已经比新版还新"。
    """
    value = (text or "").strip().lstrip("vV")
    for separator in ("+", "-"):
        value = value.split(separator, 1)[0]
    parts: list[int] = []
    for chunk in value.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(remote: str | tuple[int, ...], local: str | tuple[int, ...]) -> bool:
    """远端版本是否比本地新。缺位按 0 补齐（``1.3`` == ``1.3.0``）。"""
    try:
        left = remote if isinstance(remote, tuple) else parse_version(remote)
        right = local if isinstance(local, tuple) else parse_version(local)
    except (TypeError, ValueError):
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    """当前平台对应的资产名；不支持的组合返回空串。"""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine() or "").lower()
    if system.startswith("linux"):
        return ASSETS["linux"] if machine in ("x86_64", "amd64") else ""
    if system.startswith(("win32", "cygwin", "msys")):
        return ASSETS["windows"] if machine in ("x86_64", "amd64", "x86") else ""
    if system == "darwin":
        return ASSETS["macos"] if machine in ("arm64", "aarch64") else ""
    return ""


def install_kind(
    *,
    system: str | None = None,
    frozen: bool | None = None,
    appimage: str | None = None,
) -> str:
    """本程序是怎么跑起来的：``appimage`` / ``windows`` / ``macos`` / ``source`` / ``unsupported``。

    源码运行时永远返回 ``source`` —— 那种情况下"替换可执行文件"没有意义，
    能做的只有 ``git pull``。
    """
    system = (system or sys.platform).lower()
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return "source"

    if system.startswith("linux"):
        path = appimage if appimage is not None else os.environ.get("APPIMAGE", "")
        return "appimage" if path and Path(path).is_file() else "unsupported"
    if system.startswith(("win32", "cygwin", "msys")):
        return "windows"
    if system == "darwin":
        return "macos"
    return "unsupported"


def current_executable(kind: str | None = None) -> Path | None:
    """正在运行的这一个可执行文件。源码运行时返回 ``None``。"""
    kind = kind or install_kind()
    if kind == "source":
        return None
    if kind == "appimage":
        value = os.environ.get("APPIMAGE", "").strip()
        return Path(value) if value else None
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


#: 文件名里的版本号：``3.0.0`` / ``v3.0.0`` / ``1.2``。前后不能再贴着数字，
#: 免得把 ``x86_64`` 或 ``2026.10.20.1`` 这类东西切一半。
_NAME_VERSION_RE = re.compile(r"(?<![\d.])(v?)(\d+\.\d+(?:\.\d+)*)(?![\d.])")


def versioned_appimage_name(target: Path | str, version: str) -> Path | None:
    """把文件名里的版本号换成 ``version``；文件名没有版本号时返回 ``None``。

    只在**文件名已经带版本号**时改名：``NCM-Studio-3.0.0-x86_64.AppImage``
    更新到 3.0.3 后应当是 ``NCM-Studio-3.0.3-x86_64.AppImage``。像
    ``ncm-studio-linux-x86_64.AppImage`` 这样的固定名字保持不动 —— 版本号不
    进路径，桌面启动器就永远不会失效。
    """
    target = Path(target)
    version = (version or "").strip().lstrip("vV")
    if not version or target.suffix.lower() != ".appimage":
        return None
    stem = target.name[: -len(target.suffix)]
    match = _NAME_VERSION_RE.search(stem)
    if match is None:
        return None
    prefix = "v" if match.group(1) else ""
    new_stem = stem[: match.start()] + prefix + version + stem[match.end():]
    if new_stem == stem:
        return None
    return target.with_name(new_stem + target.suffix)


def _desktop_roots(old: Path, roots: Iterable[Path] | None = None) -> list[Path]:
    """可能放着启动器条目的目录；去重后返回。"""
    if roots is not None:
        candidates = [Path(item) for item in roots]
    else:
        home = Path.home()
        data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        candidates = [
            data_home / "applications",
            config_home / "autostart",
            home / "Desktop",
            home / "桌面",
            Path(old).parent,
        ]
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def update_desktop_entries(
    old: Path | str,
    new: Path | str,
    *,
    roots: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """把指向 ``old`` 的启动器条目改成 ``new``，返回实际改过的文件。

    AppImage 改名后，如果 .desktop 还指着旧路径，菜单项就会变成“无法找到
    程序”。这里只动 ``Exec=`` / ``TryExec=`` 两行，并且只在路径作为完整
    字符串出现时替换；写不进去（只读、权限不足）就跳过，绝不让更新失败。
    """
    old_text, new_text = str(Path(old)), str(Path(new))
    if old_text == new_text:
        return ()
    updated: list[Path] = []
    for root in _desktop_roots(Path(old), roots):
        try:
            entries = list(root.glob("*.desktop"))
        except OSError:
            continue
        for path in entries:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lines = text.splitlines(keepends=True)
            changed = False
            for index, line in enumerate(lines):
                if not line.lstrip().lower().startswith(("exec=", "tryexec=")):
                    continue
                if old_text not in line:
                    continue
                replaced = line.replace(old_text, new_text)
                if replaced != line:
                    lines[index] = replaced
                    changed = True
            if not changed:
                continue
            temp = path.with_name(path.name + ".ncmgui-tmp")
            try:
                temp.write_text("".join(lines), encoding="utf-8")
                os.chmod(temp, path.stat().st_mode & 0o777)
                os.replace(temp, path)
            except OSError:
                try:
                    temp.unlink()
                except OSError:
                    pass
                continue
            updated.append(path)
    return tuple(updated)


def sync_appimage_name(
    version: str | None = None,
    *,
    target: Path | str | None = None,
    roots: Iterable[Path] | None = None,
) -> Path | None:
    """让正在运行的 AppImage 文件名跟当前版本对齐。

    自动更新是**原地替换内容**，文件名不会自己变；如果用户文件名里带着
    版本号，更新后就会出现「名字写着 3.0.0、里面其实是 3.0.3」。启动时做
    一次改名（只在文件名本来就含版本号时），并同步改掉 .desktop 里的路径。

    失败一律静默：改名只是整理文件名，不能影响程序启动。返回新路径，
    没改则返回 ``None``。
    """
    if target is None:
        if not getattr(sys, "frozen", False):
            return None
        value = os.environ.get("APPIMAGE", "").strip()
        if not value:
            return None
        target = value
    path = Path(target)
    if not path.is_file():
        return None
    final = versioned_appimage_name(path, version or VERSION)
    if final is None or final == path:
        return None
    # 目标名已被别的文件占用（比如同时留着一份旧版本）时不要覆盖：
    # 改名只是整理文件名，没理由拿当前这个文件去盖掉另一个
    if final.exists():
        return None
    try:
        os.replace(path, final)
    except OSError:
        return None
    if os.environ.get("APPIMAGE", "") == str(path):
        os.environ["APPIMAGE"] = str(final)
    update_desktop_entries(path, final, roots=roots)
    return final


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int = 0

    @property
    def size_mb(self) -> str:
        return f"{self.size / (1024 * 1024):.1f} MB" if self.size else "大小未知"


@dataclass(frozen=True)
class Release:
    tag: str
    name: str = ""
    published_at: str = ""
    notes: str = ""
    page_url: str = ""
    assets: tuple[Asset, ...] = ()

    @property
    def version(self) -> tuple[int, ...]:
        return parse_version(self.tag)

    def asset(self, name: str) -> Asset | None:
        for item in self.assets:
            if item.name == name:
                return item
        return None


def _opener(proxy: str = ""):
    """空串走系统代理，``direct`` 强制直连，其它值当作代理地址。

    代理串先过 :func:`ncmgui.netutil.normalize_proxy`：裸 ``host:port`` 补
    ``http://``，``socks5`` 之类明确拒绝，而不是等请求时报一个含糊的错误。
    """
    from .netutil import normalize_proxy

    proxy = normalize_proxy(proxy)
    if proxy == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"ncm-studio/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _read(url: str, proxy: str = "", timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(url, headers=_headers())
    try:
        opener = _opener(proxy)
    except ValueError as exc:
        raise UpdateError(f"代理设置无效：{exc}") from exc
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError(
                "GitHub 拒绝了这次请求（可能是访问太频繁被限流）。稍后再试，"
                "或在「设置 → 更新本程序」里填一个 GitHub 代理。"
            ) from exc
        if exc.code == 404:
            raise UpdateError("仓库或 Release 不存在（404）。") from exc
        raise UpdateError(f"GitHub 返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"连不上 GitHub：{reason}") from exc


def fetch_release(proxy: str = "", timeout: float = 10.0) -> Release:
    """读最新的正式 Release。"""
    payload = _read(API_LATEST, proxy, timeout)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("GitHub 返回的内容不是合法 JSON。") from exc
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("GitHub 返回的 Release 信息不完整。")

    assets = tuple(
        Asset(
            name=str(item.get("name") or ""),
            url=str(item.get("browser_download_url") or ""),
            size=int(item.get("size") or 0),
        )
        for item in data.get("assets") or ()
        if isinstance(item, dict) and item.get("name")
    )
    return Release(
        tag=str(data.get("tag_name") or ""),
        name=str(data.get("name") or ""),
        published_at=str(data.get("published_at") or ""),
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or RELEASES_PAGE),
        assets=assets,
    )


def parse_checksums(text: str) -> dict[str, str]:
    """``sha256sum`` 的输出 → ``{文件名: 小写哈希}``。

    认 ``<hash>  <name>`` 与 ``<hash> *<name>`` 两种写法（``*`` 表示二进制模式），
    空行和 ``#`` 注释跳过。
    """
    found: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
        if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            found[name] = digest
    return found


def verify_signature(
    data: bytes,
    signature: bytes,
    public_key_b64: str = PUBLIC_KEY_B64,
) -> None:
    """验证 ``data`` 的 Ed25519 分离签名；任何问题都抛 :class:`UpdateError`。

    默认用本模块内置的公钥。验签失败必须拒绝安装：``SHA256SUMS`` 与安装包
    来自同一个 Release，谁都能一起替换，只有签名把两者绑到发布者的离线私钥上。
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateError(
            "缺少 cryptography 依赖，无法验证更新签名，已拒绝自动安装。"
        ) from exc

    key_text = (public_key_b64 or "").strip()
    if not key_text:
        raise UpdateError(
            "程序里没有内置更新公钥，无法验证下载来源，已拒绝自动安装。"
            "请到 Release 页面手动下载。"
        )
    try:
        raw_key = base64.b64decode(key_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError("内置更新公钥格式不对，已拒绝自动安装。") from exc
    if len(raw_key) != 32:
        raise UpdateError("内置更新公钥长度不对，已拒绝自动安装。")
    if len(signature) != 64:
        raise UpdateError("更新签名的长度不对，已拒绝自动安装。")

    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, data)
    except InvalidSignature as exc:
        raise UpdateError(
            "更新签名校验失败，下载内容可能被篡改，已拒绝自动安装。"
            "请到 Release 页面手动下载。"
        ) from exc


def fetch_checksums(release: Release, proxy: str = "", timeout: float = 10.0) -> dict[str, str]:
    """下载、验签并解析这个 Release 的 ``SHA256SUMS``。

    缺校验和、缺签名、签名不符都抛 :class:`UpdateError` —— 不验签不安装。
    """
    sums_asset = release.asset(SUMS_ASSET)
    sig_asset = release.asset(SIG_ASSET)
    missing = [
        name
        for name, item in ((SUMS_ASSET, sums_asset), (SIG_ASSET, sig_asset))
        if item is None
    ]
    if missing:
        raise UpdateError(
            f"这个 Release 没有 {' / '.join(missing)}，无法验证下载来源，"
            "已拒绝自动安装。请到 Release 页面手动下载。"
        )

    payload = _read(sums_asset.url, proxy, timeout)
    signature = _read(sig_asset.url, proxy, timeout)
    verify_signature(payload, signature)

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{SUMS_ASSET} 不是 UTF-8 文本。") from exc
    checksums = parse_checksums(text)
    if not checksums:
        raise UpdateError(f"{SUMS_ASSET} 里没有可用的校验和。")
    return checksums


# --------------------------------------------------------------------------- #
# 下载与校验
# --------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    url: str,
    dest: Path,
    on_progress: Callable[[int, int], None] | None = None,
    proxy: str = "",
    timeout: float = 30.0,
) -> None:
    """把 ``url`` 流式写到 ``dest``。

    ``on_progress(已下载字节, 总字节)``；服务端没给 ``Content-Length`` 时总字节是
    ``-1``。写的是调用方给的路径（通常是 ``.part``），完成后由调用方改名。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=_headers())
    try:
        opener = _opener(proxy)
    except ValueError as exc:
        raise UpdateError(f"代理设置无效：{exc}") from exc
    try:
        with opener.open(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or -1)
            done = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
            if total >= 0 and done != total:
                raise UpdateError(f"下载不完整：应为 {total} 字节，实际 {done} 字节。")
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"下载失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"下载失败：{reason}") from exc
    finally:
        if on_progress is not None:
            try:
                on_progress(done if "done" in locals() else 0, total if "total" in locals() else -1)
            except Exception:  # noqa: BLE001 - 回调不该影响下载结果
                pass


def verify(path: Path, expected: str) -> None:
    """校验 SHA-256，不一致就抛错。"""
    actual = sha256_file(path)
    if actual.lower() != (expected or "").lower():
        raise UpdateError(
            "下载的文件校验和不匹配，已中止安装。\n"
            f"期望 {expected}\n实际 {actual}"
        )


# --------------------------------------------------------------------------- #
# 安装与重启
# --------------------------------------------------------------------------- #

def install(
    new: Path,
    target: Path,
    kind: str,
    *,
    version: str = "",
    desktop_roots: Iterable[Path] | None = None,
) -> Path:
    """把已经下载并校验过的 ``new`` 换成 ``target``，返回新版本所在的路径。

    * ``appimage`` / ``macos``：``os.replace`` 是原子的，运行中的文件也能换；
    * ``windows``：运行中的 exe 不能覆盖。这里起一个等待当前进程退出的小脚本，
      由它重试移动文件并启动新版本 —— 调用方随后应立刻退出程序；
    * ``source`` / ``unsupported``：直接拒绝。

    AppImage 的文件名里如果带着版本号，替换后再把它改成新版本号，并同步
    更新 .desktop 里的 ``Exec`` —— 否则用户会看到一个写着旧版本的名字，
    或者启动器指向一个已经不存在的路径。文件名不带版本号时保持原样。
    """
    new, target = Path(new), Path(target)
    if not new.is_file():
        raise UpdateError(f"找不到已下载的文件：{new}")

    if kind == "windows":
        _schedule_windows_replace(new, target)
        return target
    if kind not in ("appimage", "macos"):
        raise UpdateError("当前运行方式不支持自动替换，请手动下载新版本。")

    final = versioned_appimage_name(target, version) if kind == "appimage" else None
    if final is None:
        final = target
    try:
        os.chmod(new, 0o755)
        # 先原地替换：即使后面的改名失败，旧路径上也已经是新版本，程序还能用
        os.replace(new, target)
        if final != target:
            try:
                os.replace(target, final)
            except OSError:
                final = target
    except OSError as exc:
        raise UpdateError(f"替换 {target.name} 失败：{exc}") from exc

    if final != target:
        update_desktop_entries(target, final, roots=desktop_roots)
    return final


def clean_appimage_env(env: Mapping[str, str]) -> dict[str, str]:
    """去掉 AppImage runtime 注入的变量。

    带着 ``APPDIR`` / ``APPIMAGE`` 去启动新的 AppImage，新进程会以为自己仍挂在
    旧挂载点上，可能直接起不来。``OWD`` 是旧工作目录，也一并清掉。
    """
    cleaned = dict(env)
    for key in ("APPDIR", "APPIMAGE", "ARGV0", "OWD"):
        cleaned.pop(key, None)
    return cleaned


def restart(kind: str, target: Path) -> None:
    """启动新版本，调用方随后应退出当前进程。

    Windows 的替换脚本自己会 ``Start-Process``，这里什么都不用做。
    """
    target = Path(target)
    if kind == "windows":
        return
    if kind in ("source", "unsupported"):
        raise UpdateError("当前运行方式不支持自动重启。")

    env = clean_appimage_env(os.environ) if kind == "appimage" else dict(os.environ)
    # 稍等一会儿再 exec：给当前进程留出退出、释放单实例锁和窗口的时间
    subprocess.Popen(
        ["sh", "-c", 'sleep 0.8; exec "$@"', "sh", str(target)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _powershell_quote(value: str) -> str:
    """PowerShell 单引号字符串转义：把 ``'`` 写成 ``''``。"""
    return "'" + str(value).replace("'", "''") + "'"


def windows_replace_script(new: Path, target: Path, pid: int | None = None) -> str:
    """生成等待当前进程退出、替换文件、再启动新版本的 PowerShell 脚本。

    单独成函数是为了能在自检里检查内容（Windows 上真正执行只在 CI / 用户机器）。
    """
    pid = int(pid if pid is not None else os.getpid())
    target_q = _powershell_quote(str(target))
    new_q = _powershell_quote(str(new))
    return f"""$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id {pid} -Timeout 60
for ($i = 0; $i -lt 60; $i++) {{
    try {{
        Move-Item -LiteralPath {new_q} -Destination {target_q} -Force -ErrorAction Stop
        break
    }} catch {{
        Start-Sleep -Milliseconds 250
    }}
}}
if (Test-Path -LiteralPath {target_q}) {{
    Start-Process -FilePath {target_q}
}}
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force
"""


def _schedule_windows_replace(new: Path, target: Path) -> None:
    """把替换脚本写到临时目录并脱离当前进程启动。"""
    script = Path(tempfile.gettempdir()) / f"ncmgui-update-{os.getpid()}.ps1"
    try:
        script.write_text(
            windows_replace_script(new, target, os.getpid()), encoding="utf-8-sig"
        )
    except OSError as exc:
        raise UpdateError(f"写更新脚本失败：{exc}") from exc

    flags = 0
    if sys.platform == "win32":  # pragma: no cover - 只在 Windows 生效
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        # 脚本没跑起来就不会自删，留着会在临时目录里越积越多
        try:
            script.unlink()
        except OSError:
            pass
        raise UpdateError(f"启动更新脚本失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    # ---- 版本
    check("v 前缀", parse_version("v1.3.0"), (1, 3, 0))
    check("不带 v", parse_version("1.3.0"), (1, 3, 0))
    check("缺位", parse_version("1.3"), (1, 3))
    check("预发布后缀忽略", parse_version("1.3.0-beta.2"), (1, 3, 0))
    check("构建元数据忽略", parse_version("1.3.0+20261017"), (1, 3, 0))
    check("空版本", parse_version(""), ())
    check("非法版本", parse_version("nightly"), ())

    check("新版本更大", is_newer("v1.3.0", "1.2.0"), True)
    check("同版本不新", is_newer("1.3.0", "v1.3.0"), False)
    check("旧版本不新", is_newer("1.2.9", "1.3.0"), False)
    check("缺位按 0 补齐", is_newer("1.3", "1.3.0"), False)
    check("位数少但数值大", is_newer("1.4", "1.3.9"), True)
    check("预发布与正式版等价", is_newer("1.3.0-rc1", "1.3.0"), False)

    # ---- 资产名
    check("Linux x86_64", asset_name("linux", "x86_64"), ASSETS["linux"])
    check("Linux amd64", asset_name("linux", "AMD64"), ASSETS["linux"])
    check("Linux arm64 不支持", asset_name("linux", "aarch64"), "")
    check("Windows", asset_name("win32", "AMD64"), ASSETS["windows"])
    check("macOS arm64", asset_name("darwin", "arm64"), ASSETS["macos"])
    check("macOS x86 不支持", asset_name("darwin", "x86_64"), "")
    check("不认识的系统", asset_name("plan9", "mips"), "")

    # ---- AppImage 文件名里的版本
    check(
        "名称版本替换",
        versioned_appimage_name(Path("/x/NCM-Studio-3.0.0-x86_64.AppImage"), "v3.0.3"),
        Path("/x/NCM-Studio-3.0.3-x86_64.AppImage"),
    )
    check(
        "保留版本号后的后缀",
        versioned_appimage_name(Path("/x/NCM-Studio-3.0.0-x86_64-3.AppImage"), "3.0.3"),
        Path("/x/NCM-Studio-3.0.3-x86_64-3.AppImage"),
    )
    check(
        "保留 v 前缀",
        versioned_appimage_name(Path("/x/App-v1.2.AppImage"), "3.0.3"),
        Path("/x/App-v3.0.3.AppImage"),
    )
    check("固定文件名不动",
          versioned_appimage_name(Path("/x/ncm-studio-linux-x86_64.AppImage"), "3.0.3"),
          None)
    check("非 AppImage 不动",
          versioned_appimage_name(Path("/x/NCM-Studio-3.0.0.exe"), "3.0.3"), None)
    check("版本没变不动",
          versioned_appimage_name(Path("/x/NCM-Studio-3.0.3.AppImage"), "3.0.3"), None)

    # ---- 启动时的改名
    with tempfile.TemporaryDirectory() as tmp:
        old = Path(tmp) / "NCM-Studio-3.0.0-x86_64.AppImage"
        old.write_bytes(b"new")
        target = Path(tmp) / "NCM-Studio-3.0.8-x86_64.AppImage"
        check("改名成功",
              sync_appimage_name("3.0.8", target=old, roots=[]), target)
        check("旧名不再存在", old.exists(), False)
        check("新名内容不变", target.read_bytes(), b"new")

        # 目标名已经被另一个文件占用：不覆盖，也不动当前文件
        occupied = Path(tmp) / "NCM-Studio-3.0.1-x86_64.AppImage"
        occupied.write_bytes(b"old")
        check("目标已存在时不改名",
              sync_appimage_name("3.0.8", target=occupied, roots=[]), None)
        check("没有覆盖已有文件", target.read_bytes(), b"new")
        check("原文件保持不动", occupied.read_bytes(), b"old")

    # ---- 安装形态
    check("源码运行", install_kind(system="linux", frozen=False, appimage=""), "source")
    check("Linux 冻结但没有 APPIMAGE",
          install_kind(system="linux", frozen=True, appimage=""), "unsupported")
    check("Linux 冻结且 APPIMAGE 不存在",
          install_kind(system="linux", frozen=True, appimage="/nonexistent/a.AppImage"),
          "unsupported")
    with tempfile.TemporaryDirectory(prefix="ncmgui-updater-") as tmp:
        appimage = Path(tmp) / "ncm-studio.AppImage"
        appimage.write_bytes(b"x")
        check("AppImage", install_kind(system="linux", frozen=True, appimage=str(appimage)),
              "appimage")
    check("源码运行没有可替换的可执行文件", current_executable("source"), None)
    check("Windows 冻结", install_kind(system="win32", frozen=True), "windows")
    check("macOS 冻结", install_kind(system="darwin", frozen=True), "macos")

    # ---- 校验和解析
    text = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  a.AppImage\n"
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789 *b.exe\n"
        "# 注释\n\n坏的 行\n"
    )
    checksums = parse_checksums(text)
    check("解析两个校验和", len(checksums), 2)
    check("普通格式",
          checksums.get("a.AppImage"),
          "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    check("二进制模式星号被去掉", "b.exe" in checksums, True)

    # ---- Ed25519 签名（现场生成测试密钥，不联网、不碰真实私钥）
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        failures.append("缺少 cryptography，更新验签不可用")
    else:
        def public_b64(private: object) -> str:
            raw = private.public_key().public_bytes(  # type: ignore[attr-defined]
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            return base64.b64encode(raw).decode("ascii")

        private = Ed25519PrivateKey.generate()
        key = public_b64(private)
        payload = b"0123  file\n"
        signature = private.sign(payload)

        def rejects(name: str, call) -> None:
            try:
                call()
            except UpdateError:
                check(name, True, True)
            else:
                check(name, False, True)

        try:
            verify_signature(payload, signature, key)
        except UpdateError as exc:
            failures.append(f"正确签名应通过：{exc}")

        rejects("篡改内容必须失败",
                lambda: verify_signature(payload + b"x", signature, key))
        flipped = bytes([signature[0] ^ 0xFF]) + signature[1:]
        rejects("篡改签名必须失败",
                lambda: verify_signature(payload, flipped, key))
        rejects("换公钥必须失败",
                lambda: verify_signature(payload, signature,
                                         public_b64(Ed25519PrivateKey.generate())))
        rejects("签名长度不对必须失败",
                lambda: verify_signature(payload, signature[:32], key))
        rejects("空公钥必须失败", lambda: verify_signature(payload, signature, ""))
        rejects("非法 base64 公钥必须失败",
                lambda: verify_signature(payload, signature, "不是base64"))

        # 缺 .sig 的 Release 必须在联网之前就被拒绝
        no_sig = Release(tag="v1.0.0", assets=(
            Asset(SUMS_ASSET, "https://example.invalid/SHA256SUMS"),
        ))
        rejects("缺少签名文件必须失败", lambda: fetch_checksums(no_sig))

    # ---- 下载与替换（本地文件 URL，不联网）
    with tempfile.TemporaryDirectory(prefix="ncmgui-updater-") as tmp:
        root = Path(tmp)
        source_file = root / "release.bin"
        source_file.write_bytes(b"new version" * 1000)
        expected = sha256_file(source_file)
        part = root / "download.part"
        steps: list[tuple[int, int]] = []
        download(source_file.as_uri(), part, on_progress=lambda a, b: steps.append((a, b)))
        check("下载内容一致", part.read_bytes(), source_file.read_bytes())
        check("进度回调收到总长", bool(steps) and steps[-1][1] == source_file.stat().st_size, True)
        verify(part, expected)
        check("校验通过不抛错", True, True)

        try:
            verify(part, "0" * 64)
        except UpdateError:
            check("校验不符会拒绝", True, True)
        else:
            check("校验不符会拒绝", False, True)

        target = root / "ncm-studio"
        target.write_bytes(b"old")
        new = root / "ncm-studio.new"
        new.write_bytes(b"new")
        install(new, target, "macos")
        check("替换后内容", target.read_bytes(), b"new")
        check("replace 后新文件不在了", new.exists(), False)

        # AppImage：文件名带版本号时改名，并把 .desktop 一起改过来
        apps = root / "applications"
        apps.mkdir()
        old_name = root / "NCM-Studio-3.0.0-x86_64-3.AppImage"
        old_name.write_bytes(b"old")
        desktop = apps / "NCM-Studio.desktop"
        desktop.write_text(
            "[Desktop Entry]\n"
            f"Exec={old_name} %U\n"
            f"TryExec={old_name}\n"
            "Name=NCM-Studio\n",
            encoding="utf-8",
        )
        new_name = root / "download.new"
        new_name.write_bytes(b"updated")
        final = install(new_name, old_name, "appimage", version="v3.0.3",
                        desktop_roots=[apps])
        check("AppImage 改名到新版本", final,
              root / "NCM-Studio-3.0.3-x86_64-3.AppImage")
        check("旧文件名不再存在", old_name.exists(), False)
        check("改名后内容正确", final.read_bytes(), b"updated")
        entries = desktop.read_text(encoding="utf-8")
        check("desktop Exec 已同步", f"Exec={final} %U" in entries, True)
        check("desktop TryExec 已同步", f"TryExec={final}" in entries, True)
        check("desktop 其它内容不动", "Name=NCM-Studio" in entries, True)

        # 固定文件名（版本号不在名字里）保持原样
        stable = root / "ncm-studio-linux-x86_64.AppImage"
        stable.write_bytes(b"old")
        stable_new = root / "stable.new"
        stable_new.write_bytes(b"updated")
        check("固定文件名不改名",
              install(stable_new, stable, "appimage", version="v3.0.3"), stable)
        check("固定文件名内容更新", stable.read_bytes(), b"updated")

        # 启动时同步：上次更新留下的旧版本号自动对齐
        sync_apps = root / "sync-applications"
        sync_apps.mkdir()
        sync_old = root / "NCM-Studio-3.0.0-x86_64.AppImage"
        sync_old.write_bytes(b"current")
        sync_desktop = sync_apps / "NCM-Studio.desktop"
        sync_desktop.write_text(f"[Desktop Entry]\nExec={sync_old}\n",
                                encoding="utf-8")
        renamed = sync_appimage_name("3.0.3", target=sync_old, roots=[sync_apps])
        check("启动时对齐文件名", renamed,
              root / "NCM-Studio-3.0.3-x86_64.AppImage")
        check("启动时旧文件已不在", sync_old.exists(), False)
        check("启动时 desktop 已同步",
              f"Exec={renamed}" in sync_desktop.read_text(encoding="utf-8"), True)

        try:
            install(new, target, "source")
        except UpdateError:
            check("源码运行拒绝安装", True, True)
        else:
            check("源码运行拒绝安装", False, True)

        # Windows 脚本只是生成文本，不执行。路径片段要拿 str(Path) 现算 ——
        # 在 Windows 上它会变成反斜杠，写死 "C:/x/a.new" 只在 Linux 上成立
        new_path, target_path = Path("C:/x/a.new"), Path("C:/x/a.exe")
        script = windows_replace_script(new_path, target_path, 4321)
        for fragment in ("Wait-Process -Id 4321", "Move-Item", "Start-Process",
                         f"'{new_path}'", f"'{target_path}'"):
            check(f"Windows 脚本含 {fragment}", fragment in script, True)
        check("单引号被转义",
              "''" in windows_replace_script(Path("C:/it's/a.new"), Path("C:/it's/a.exe"), 1),
              True)

    # ---- AppImage 环境清理
    cleaned = clean_appimage_env({"APPDIR": "/tmp/x", "APPIMAGE": "/a", "ARGV0": "a",
                                  "OWD": "/old", "PATH": "/bin"})
    check("AppImage 变量被清掉",
          [key for key in ("APPDIR", "APPIMAGE", "ARGV0", "OWD") if key in cleaned], [])
    check("其它变量保留", cleaned.get("PATH"), "/bin")

    # ---- Release 结构
    release = Release(
        tag="v1.3.0",
        assets=(Asset("SHA256SUMS", "https://x/sums"), Asset("a", "https://x/a", 1048576)),
    )
    check("Release 版本", release.version, (1, 3, 0))
    check("按名字找资产", release.asset("a") is not None, True)
    check("不存在的资产", release.asset("b"), None)
    check("资产大小", release.asset("a").size_mb if release.asset("a") else "", "1.0 MB")

    if failures:
        print(f"更新自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("更新自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
