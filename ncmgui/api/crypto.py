"""网易云 weapi 请求加密。

大部分读接口用明文 ``/api/*`` 就能取到数据；少数接口（热搜、评论区写入、
部分歌单操作）只认 ``/weapi/*``。这里实现 weapi 的两段 AES + 一段 RSA，
仅在明文接口返回 404/400 时作为兜底。

算法是公开且固定的：

1. 用 ``0CoJUm6Qyw8W8jud`` 对 JSON 做一次 AES-128-CBC；
2. 用随机 16 字节密钥再加密一次；
3. 随机密钥倒序后做 RSA（``pow(int(hex), 65537, modulus)``），补足 256 个十六进制字符。

服务端不校验随机密钥，只要求能解开。自检只验证结构和往返，不依赖网络。

    .venv/bin/python -m ncmgui.api.crypto
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import random
import string

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

NONCE = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
PUBKEY = 65537
MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
    "3ece0462db0a22b8e7",
    16,
)

_ALPHABET = string.ascii_letters + string.digits


def _pkcs7(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def aes_encrypt(text: str, key: str | bytes) -> str:
    """AES-128-CBC + PKCS#7 + base64，返回字符串。"""
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    if len(key_bytes) != 16:
        raise ValueError("weapi 的密钥必须是 16 字节")
    encryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(IV)).encryptor()
    encrypted = encryptor.update(_pkcs7(text.encode("utf-8"))) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def rsa_encrypt(text: str, modulus: int = MODULUS, pubkey: int = PUBKEY) -> str:
    """把随机密钥倒序后做教科书式 RSA，返回 256 位十六进制。"""
    number = int(binascii.hexlify(text.encode("utf-8")[::-1]), 16)
    return format(pow(number, pubkey, modulus), "x").zfill(256)


def create_secret_key(length: int = 16) -> str:
    """随机密钥。用 ``secrets`` 更好，但这里只需要不可预测，不涉及机密。"""
    return "".join(random.choice(_ALPHABET) for _ in range(length))


def weapi_params(params: dict, csrf_token: str = "") -> dict[str, str]:
    """把业务参数变成 ``params`` / ``encSecKey`` 两个表单字段。"""
    payload = dict(params)
    payload["csrf_token"] = csrf_token
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    secret = create_secret_key()
    return {
        "params": aes_encrypt(aes_encrypt(text, NONCE), secret),
        "encSecKey": rsa_encrypt(secret),
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    # AES 固定向量：用同一个 key 加密两遍结果相同
    first = aes_encrypt('{"a":1}', NONCE)
    second = aes_encrypt('{"a":1}', NONCE)
    check("AES 输出稳定", first, second)
    check("AES 输出是 base64", len(first) % 4, 0)
    try:
        aes_encrypt("x", "short")
    except ValueError:
        pass
    else:
        failures.append("非 16 字节密钥应当报错")

    # RSA：256 个十六进制字符，且每次随机密钥不同
    key_one = create_secret_key()
    key_two = create_secret_key()
    check("随机密钥长度", len(key_one), 16)
    check("随机密钥不重复", key_one == key_two, False)
    check("RSA 输出 256 位", len(rsa_encrypt(key_one)), 256)
    check("RSA 只含十六进制", all(ch in "0123456789abcdef" for ch in rsa_encrypt(key_one)), True)

    result = weapi_params({"s": "孤勇者"}, "csrf")
    check("weapi 有两个字段", sorted(result), ["encSecKey", "params"])
    check("encSecKey 长度", len(result["encSecKey"]), 256)

    # 能解回原文：AES 的密钥和 IV 都是公开的
    def decrypt(text: str, key: bytes) -> str:
        raw = base64.b64decode(text)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
        data = decryptor.update(raw) + decryptor.finalize()
        return data[:-data[-1]].decode("utf-8")

    # 重新走一遍，保留中间值，验证两段 AES 的确能还原
    text = json.dumps({"s": "孤勇者", "csrf_token": "csrf"}, ensure_ascii=False,
                      separators=(",", ":"))
    secret = create_secret_key()
    inner = aes_encrypt(text, NONCE)
    outer = aes_encrypt(inner, secret)
    check("AES 往返", decrypt(decrypt(outer, secret.encode()).encode(), NONCE), text)

    if failures:
        print(f"weapi 自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("weapi 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
