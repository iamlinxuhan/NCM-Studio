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


def _looks_like_nonstandard_ip(name: str) -> bool:
    """``134744072``、``0x08080808``、``0177.0.0.1`` 这类伪 IP 表示。

    网络栈会把它们解析成真实的 IPv4（例如 134744072 → 8.8.8.8），但
    ``ipaddress`` 不认；不显式拒绝的话会被当成「无点号的内网主机名」。
    """
    parts = [part for part in str(name or "").split(".") if part]
    if not parts:
        return False
    return all(part.startswith("0x") or part.isdigit() for part in parts)


def _is_local_or_private(host: str) -> bool:
    """本机、私有网段、链路本地。

    主机名必须解析后再判断：单标签名（``nas``）也可能被 DNS 解析到公网。
    解析失败时按不安全处理 —— 让用户改用 HTTPS 或直接填内网 IP。
    """
    import ipaddress
    import socket

    name = str(host or "").strip().lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        # 不是标准 IP 字面量：伪 IP 表示直接拒绝，其余按主机名解析
        if _looks_like_nonstandard_ip(name):
            return False
    else:
        return bool(addr.is_loopback or addr.is_private or addr.is_link_local)

    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    seen = False
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if not (addr.is_loopback or addr.is_private or addr.is_link_local):
            return False
        seen = True
    return seen


def validate_server_url(url: str) -> str:
    """流媒体服务器地址校验：HTTPS 任意，HTTP 仅限本机/内网。

    Jellyfin/Emby 的 Token 放在请求头里，走 HTTP 时同网段的攻击者可以直接
    嗅探。公网地址一律要求 HTTPS；内网明文由界面单独提示。
    """
    text = str(url or "").strip().rstrip("/")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("服务器地址必须以 http:// 或 https:// 开头")
    host = parts.hostname or ""
    if not host:
        raise ValueError("服务器地址缺少主机名")
    if scheme == "https":
        return text
    if _is_local_or_private(host):
        return text
    raise ValueError(
        "公网地址请使用 HTTPS：HTTP 会明文传输 Token 与密码。")


def media_url(url: str) -> str:
    """确认媒体地址只使用 http/https。

    ``urllib`` 与 mpv 都接受 ``file://`` 等本地协议：如果地址来自被控的
    接口响应或流媒体服务器，``file://`` 能让播放器读到本机文件。网络来源
    的媒体一律只允许 http/https。
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("媒体地址为空")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的媒体地址协议：{scheme or '无'}")
    if parts.username or parts.password:
        # 用户名密码会出现在播放器参数、代理与服务器访问日志里
        raise ValueError("媒体地址不应包含用户名或密码")
    if not parts.hostname:
        raise ValueError("媒体地址缺少主机名")
    return text


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

    # ---- 媒体地址白名单
    check("允许 http", media_url("http://x/a.mp4"), "http://x/a.mp4")
    check("允许 https", media_url("https://x/a"), "https://x/a")
    # ---- 流媒体服务器地址
    check("HTTPS 任意", validate_server_url("https://media.example.com"),
          "https://media.example.com")
    check("HTTP 本机", validate_server_url("http://127.0.0.1:8096"),
          "http://127.0.0.1:8096")
    check("HTTP 私网", validate_server_url("http://192.168.1.2:4533"),
          "http://192.168.1.2:4533")
    # 主机名按 DNS 结果判断：解析到私网才允许
    import socket
    from unittest.mock import patch

    def addresses(ip: str):
        def fake(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        return fake

    with patch("socket.getaddrinfo", addresses("192.168.1.9")):
        check("主机名解析到私网", validate_server_url("http://nas:8096"),
              "http://nas:8096")
    with patch("socket.getaddrinfo", addresses("8.8.8.8")):
        try:
            validate_server_url("http://nas.example.com")
        except ValueError:
            check("主机名解析到公网被拒绝", True, True)
        else:
            check("主机名解析到公网被拒绝", False, True)
    for bad in ("http://media.example.com", "http://8.8.8.8",
                "http://134744072", "http://0x08080808", "http://0177.0.0.1",
                "ftp://x/a", "", "http:///a"):
        try:
            validate_server_url(bad)
        except ValueError:
            check(f"拒绝服务器地址 {bad or '空'}", True, True)
        else:
            check(f"拒绝服务器地址 {bad or '空'}", False, True)

    for bad in ("file:///etc/passwd", "ftp://x/a", "data:text/plain,x", "",
                "http://user:pass@x/a", "http:///a"):
        try:
            media_url(bad)
        except ValueError:
            check(f"拒绝 {bad or '空地址'}", True, True)
        else:
            check(f"拒绝 {bad or '空地址'}", False, True)

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
