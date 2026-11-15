"""从 CookieCloud 拉取并解密浏览器 Cookie。

``ncmctl login cookiecloud`` 要求把密码放在命令行参数里，ncmctl 自己的帮助
也写着 "visible in process arguments"：同机其他用户、进程监控与 shell 历史
都能看到。这里自己完成获取与解密，只把解密后的 Netscape 文件交给
``ncmctl login cookie --file``，密码全程不进入进程参数。

自检::

    .venv/bin/python -m ncmgui.cookiecloud
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: CookieCloud 的 AES 是 CryptoJS ``AES.decrypt(ciphertext, passphrase)``，
#: 也就是 OpenSSL 的 ``Salted__`` 格式：MD5 派生 32 字节 key + 16 字节 IV。
_KEY_LEN = 32
_IV_LEN = 16
_SALTED = b"Salted__"


class CookieCloudError(Exception):
    """CookieCloud 拉取或解密失败；消息可以直接显示给用户。"""


def _derive_key_iv(password: str, salt: bytes) -> tuple[bytes, bytes]:
    """OpenSSL ``EVP_BytesToKey``（MD5），与 CryptoJS 的默认 KDF 一致。"""
    secret = password.encode("utf-8")
    material = b""
    block = b""
    while len(material) < _KEY_LEN + _IV_LEN:
        block = hashlib.md5(block + secret + salt).digest()
        material += block
    return material[:_KEY_LEN], material[_KEY_LEN:_KEY_LEN + _IV_LEN]


def decrypt(encrypted: str, password: str) -> dict:
    """解密 CookieCloud 返回的 base64 密文，返回 JSON 对象。"""
    try:
        raw = base64.b64decode(str(encrypted or ""), validate=True)
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都按格式错误处理
        raise CookieCloudError("CookieCloud 返回的数据无法解码") from exc
    if len(raw) < 16 or not raw.startswith(_SALTED):
        raise CookieCloudError("CookieCloud 数据格式不认识（缺少 Salted__ 头）")

    salt = raw[8:16]
    key, iv = _derive_key_iv(password, salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()

    pad = padded[-1] if padded else 0
    if pad < 1 or pad > 16 or padded[-pad:] != bytes([pad]) * pad:
        raise CookieCloudError("CookieCloud 解密失败：密码不对或数据损坏")
    text = padded[:-pad].decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CookieCloudError("CookieCloud 解密后的内容不是 JSON") from exc
    if not isinstance(payload, dict):
        raise CookieCloudError("CookieCloud 解密后的内容不是对象")
    return payload


def _request_headers(extra: str) -> dict[str, str]:
    headers = {
        "User-Agent": "ncm-studio",
        "Content-Type": "application/json",
    }
    for part in str(extra or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def fetch_payload(
    server: str,
    uuid: str,
    password: str,
    *,
    headers: str = "",
    timeout: float = 30.0,
) -> dict:
    """请求 CookieCloud 的 ``/get`` 并解密。密码只在本函数的内存里。"""
    base = str(server or "http://127.0.0.1:8088").strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:8088"
    body = json.dumps({"uuid": str(uuid or "").strip()}).encode("utf-8")
    request = urllib.request.Request(
        base + "/get", data=body, headers=_request_headers(headers),
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception as exc:  # noqa: BLE001 - 网络层细节对用户没意义
        reason = getattr(exc, "reason", exc)
        raise CookieCloudError(f"无法连接 CookieCloud：{reason}") from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise CookieCloudError("CookieCloud 返回的不是 JSON") from exc
    encrypted = str((payload or {}).get("encrypted") or "")
    if not encrypted:
        raise CookieCloudError("CookieCloud 没有返回加密数据（UUID 不存在？）")
    return decrypt(encrypted, password)


def netscape_text(payload: dict) -> str:
    """把解密后的 CookieCloud 数据转成 Netscape Cookie 文件内容。

    只保留网易云域名，避免把浏览器里其他站点的 Cookie 写进临时文件。
    """
    data = payload.get("cookie_data")
    if not isinstance(data, dict):
        data = payload
    lines = ["# Netscape HTTP Cookie File", ""]
    count = 0
    for domain, items in data.items():
        if "163.com" not in str(domain).lower():
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            value = str(item.get("value") or "")
            cookie_domain = str(item.get("domain") or domain or "")
            include_sub = "TRUE" if cookie_domain.startswith(".") else "FALSE"
            path = str(item.get("path") or "/")
            secure = "TRUE" if item.get("secure") else "FALSE"
            try:
                expiry = int(item.get("expirationDate") or 0)
            except (TypeError, ValueError):
                expiry = 0
            lines.append("\t".join([
                cookie_domain, include_sub, path, secure, str(expiry),
                name, value,
            ]))
            count += 1
    if not count:
        raise CookieCloudError("CookieCloud 里没有网易云的 Cookie")
    return "\n".join(lines) + "\n"


def write_netscape(
    server: str,
    uuid: str,
    password: str,
    *,
    headers: str = "",
    timeout: float = 30.0,
    directory: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """拉取并写一个 0600 的 Netscape 临时文件，返回路径。

    调用方负责在登录作业结束后删除它。``cancelled`` 在写盘前后各检查一次：
    页面已经关闭时不再留下凭据文件。
    """
    from .config import private_tmp_dir

    text = netscape_text(fetch_payload(
        server, uuid, password, headers=headers, timeout=timeout))
    if cancelled is not None and cancelled():
        raise CookieCloudError("已取消")
    root = Path(directory) if directory else private_tmp_dir()
    root.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="cookiecloud-", suffix=".txt", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if os.name == "posix":
            os.chmod(path, 0o600)
        if cancelled is not None and cancelled():
            raise CookieCloudError("已取消")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _encrypt_for_test(data: bytes, password: str) -> str:
    """只给自检用：按 CryptoJS 的格式加密一段数据。"""
    salt = os.urandom(8)
    key, iv = _derive_key_iv(password, salt)
    pad = 16 - len(data) % 16
    padded = data + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(_SALTED + salt + ciphertext).decode("ascii")


def _selftest() -> int:
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    payload = {"cookie_data": {"music.163.com": [
        {"name": "MUSIC_U", "value": "abc", "domain": "music.163.com",
         "path": "/", "secure": True, "expirationDate": 1893456000},
        {"name": "NMTID", "value": "def", "domain": ".music.163.com",
         "path": "/", "secure": False, "expirationDate": 1775732161},
    ], "example.com": [{"name": "x", "value": "y"}]}}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted = _encrypt_for_test(raw, "secret-pass")
    decoded = decrypt(encrypted, "secret-pass")
    check("解密往返", decoded, payload)

    try:
        decrypt(encrypted, "wrong-pass")
    except CookieCloudError:
        check("错误密码被拒绝", True, True)
    else:
        check("错误密码被拒绝", False, True)

    text = netscape_text(decoded)
    check("只保留网易云域名", "example.com" not in text, True)
    check("含 MUSIC_U", "MUSIC_U\tabc" in text, True)
    check("子域标记", ".music.163.com\tTRUE" in text, True)
    check("表头", text.startswith("# Netscape HTTP Cookie File"), True)

    if failures:
        print(f"CookieCloud 自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("CookieCloud 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
