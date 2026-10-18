"""代理地址的校验与规范化。

设置页里的两个代理输入框、更新器、封面下载都接受同一个字符串，但 ``urllib``
对它的要求比用户习惯写的更严格：``127.0.0.1:7890`` 不带 scheme 时不会被当成
HTTP 代理，``socks5://`` 则是 ``urllib`` 根本不支持的协议。与其让请求在运行时
报一个含糊的错，不如在各入口统一过一遍这里。

本模块不依赖 Qt，可以单独自检：

    .venv/bin/python -m ncmgui.netutil
"""

from __future__ import annotations

from urllib.parse import urlsplit


class ProxyError(ValueError):
    """代理串不合法。消息直接展示给用户。"""


def normalize_proxy(value: str) -> str:
    """返回规范化后的代理串。

    * 空串：不指定，走系统代理；
    * ``direct``：强制直连（保留这个语义，不能折成空串，否则会变回系统代理）；
    * ``host:port``：补成 ``http://host:port``；
    * 只接受 ``http`` / ``https``；其它协议明确拒绝，而不是等请求时才失败。

    非法值抛 :class:`ProxyError`（``ValueError`` 的子类）。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower() == "direct":
        return "direct"

    candidate = text if "://" in text else "http://" + text
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ProxyError(
            f"只支持 http / https 代理，不支持 {scheme or '这种'} 协议"
        )
    if not parts.hostname:
        raise ProxyError("代理地址缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProxyError(f"代理端口不合法：{exc}") from exc
    if port is not None and not 0 < port < 65536:
        raise ProxyError(f"代理端口超出范围：{port}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyError("代理地址不应包含路径、查询参数或锚点")

    return f"{scheme}://{parts.netloc}"


def _selftest() -> int:
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    def rejects(name: str, value: str) -> None:
        try:
            normalize_proxy(value)
        except ProxyError:
            check(name, True, True)
        else:
            check(name, False, True)

    check("空串不动", normalize_proxy(""), "")
    check("只有空白", normalize_proxy("   "), "")
    check("direct 保留", normalize_proxy("direct"), "direct")
    check("DIRECT 大小写", normalize_proxy("DIRECT"), "direct")
    check("裸 host:port 补 scheme",
          normalize_proxy("127.0.0.1:7890"), "http://127.0.0.1:7890")
    check("IPv6 裸地址",
          normalize_proxy("[::1]:7890"), "http://[::1]:7890")
    check("http 原样",
          normalize_proxy("http://proxy.local:8080"), "http://proxy.local:8080")
    check("https",
          normalize_proxy("https://proxy.local:443"), "https://proxy.local:443")
    check("去掉首尾空白",
          normalize_proxy("  proxy.local:3128  "), "http://proxy.local:3128")

    rejects("socks5 明确拒绝", "socks5://127.0.0.1:1080")
    rejects("socks5h 拒绝", "socks5h://127.0.0.1:1080")
    rejects("ftp 拒绝", "ftp://proxy.local")
    rejects("缺少主机名", "http://")
    rejects("端口非数字", "http://proxy.local:abc")
    rejects("端口越界", "http://proxy.local:70000")
    rejects("带路径拒绝", "http://proxy.local:8080/path")
    rejects("带查询拒绝", "http://proxy.local:8080?x=1")

    if failures:
        print(f"代理自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("代理自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
