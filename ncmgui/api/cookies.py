"""只读解析 ncmctl 的 ``cookie.json``。

ncmctl 扫码/手机/Cookie 登录后会把浏览器风格的 Cookie 写在
``<home>/.ncmctl/cookie.json``。本模块只读这个文件，**绝不写回**：登录态由
ncmctl 自己维护，界面直接改它会让两边的会话状态对不上。

文件结构大致是::

    {"163.com": {"music.163.com;/;MUSIC_U": {"Name": "MUSIC_U",
                                             "Value": "...",
                                             "Domain": "music.163.com",
                                             "Path": "/",
                                             "Expires": "2027-..."}}}

服务端的 ``Set-Cookie`` 可能轮换 ``__csrf``/``NMTID``。轮换后的值只存在内存
覆盖层里，下次进程启动重新从文件读；这样不会污染 ncmctl 的状态。

自检::

    .venv/bin/python -m ncmgui.api.cookies
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

DOMAIN = "music.163.com"
#: 这些 Cookie 没有也不影响只读接口；有则带上
IMPORTANT = ("MUSIC_U", "__csrf", "NMTID", "deviceId", "WNMCID", "_ntes_nnid", "_ntes_nuid")

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_expiry(value: object) -> float | None:
    """把 Go 写入的各种过期时间格式变成 Unix 秒。

    可能是 ``None``、数字、RFC3339 字符串（带纳秒），也可能根本不存在。
    解析不了就当成会话 Cookie —— 宁可多带一个，也不要因为一个字段丢掉登录态。
    """
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # 毫秒时间戳（Go 有时会用）
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number / 1000.0 if number > 1e12 else number
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", text.replace("Z", "+00:00")))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class Cookie:
    name: str
    value: str
    domain: str
    path: str
    expires: float | None = None

    def matches(self, domain: str, path: str) -> bool:
        domain = domain.lstrip(".")
        own = self.domain.lstrip(".")
        if own and domain != own and not domain.endswith("." + own):
            return False
        return path.startswith(self.path or "/")


def parse_cookie_file(path: str | Path) -> list[Cookie]:
    """读取并解析 Cookie 文件；文件不存在或损坏时返回空列表。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []

    found: dict[tuple[str, str, str], Cookie] = {}
    for root in raw.values() if isinstance(raw, dict) else ():
        if not isinstance(root, dict):
            continue
        for entry in root.values():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("Name") or "").strip()
            value = entry.get("Value")
            if not name or value is None:
                continue
            cookie = Cookie(
                name=name,
                value=str(value),
                domain=str(entry.get("Domain") or DOMAIN).lstrip("."),
                path=str(entry.get("Path") or "/"),
                expires=_parse_expiry(entry.get("Expires")),
            )
            found[(cookie.name, cookie.domain, cookie.path)] = cookie
    return list(found.values())


class CookieJar:
    """按域名/路径取 Cookie，并在内存里吸收服务端的轮换。"""

    def __init__(
        self,
        path: str | Path | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.RLock()
        self._cookies: list[Cookie] = []
        self._overlay: dict[tuple[str, str, str], Cookie] = {}
        self._stamp: tuple[int, int] | None = None
        self.reload(force=True)

    # ------------------------------------------------------------------ 加载

    def reload(self, force: bool = False) -> bool:
        """文件变化时重新读取；返回是否真的读了。"""
        if self.path is None:
            return False
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            with self._lock:
                self._cookies = []
                self._stamp = None
            return False
        with self._lock:
            if not force and stamp == self._stamp:
                return False
            self._cookies = parse_cookie_file(self.path)
            self._stamp = stamp
            # 文件已经变化，旧的轮换值不再可靠
            self._overlay.clear()
        return True

    # ------------------------------------------------------------------ 查询

    def _all(self) -> list[Cookie]:
        with self._lock:
            merged = {(c.name, c.domain, c.path): c for c in self._cookies}
            merged.update(self._overlay)
        return list(merged.values())

    def header(self, domain: str = DOMAIN, path: str = "/") -> str:
        """生成 ``Cookie`` 请求头。过期的、路径不匹配的都不带。"""
        now = self._clock()
        chosen: dict[str, tuple[int, Cookie]] = {}
        for cookie in self._all():
            if cookie.expires is not None and cookie.expires <= now:
                continue
            if not cookie.matches(domain, path):
                continue
            # 同一名字有多条路径规则时，选路径最长的那条（RFC 6265）
            depth = len(cookie.path or "/")
            previous = chosen.get(cookie.name)
            if previous is None or depth >= previous[0]:
                chosen[cookie.name] = (depth, cookie)
        return "; ".join(
            f"{name}={cookie.value}"
            for name, (_depth, cookie) in sorted(chosen.items())
        )

    def value(self, name: str, domain: str = DOMAIN, path: str = "/") -> str:
        for cookie in self._all():
            if cookie.name == name and cookie.matches(domain, path):
                if cookie.expires is not None and cookie.expires <= self._clock():
                    continue
                return cookie.value
        return ""

    @property
    def csrf(self) -> str:
        return self.value("__csrf")

    @property
    def device_id(self) -> str:
        return self.value("deviceId")

    @property
    def has_login(self) -> bool:
        """有 ``MUSIC_U`` 才算登录；其余 Cookie 匿名也会下发。"""
        return bool(self.value("MUSIC_U"))

    # -------------------------------------------------------------- 轮换吸收

    def apply_set_cookie(self, headers: Iterable[str]) -> None:
        """把响应里的 ``Set-Cookie`` 放进内存覆盖层，不落盘。"""
        now = self._clock()
        with self._lock:
            for header in headers:
                try:
                    first, *attributes = str(header).split(";")
                    name, _, value = first.partition("=")
                    name = name.strip()
                    if not name:
                        continue
                    domain, path = DOMAIN, "/"
                    expires: float | None = None
                    for attribute in attributes:
                        key, _, val = attribute.partition("=")
                        key = key.strip().lower()
                        val = val.strip()
                        if key == "domain" and val:
                            domain = val.lstrip(".")
                        elif key == "path" and val:
                            path = val
                        elif key == "max-age":
                            try:
                                expires = now + float(val)
                            except ValueError:
                                pass
                        elif key == "expires":
                            expires = _parse_expiry(val)
                    self._overlay[(name, domain, path)] = Cookie(
                        name=name,
                        value=value.strip(),
                        domain=domain,
                        path=path,
                        expires=expires,
                    )
                except (ValueError, TypeError):
                    continue


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    raw = {
        "163.com": {
            "music.163.com;/;MUSIC_U": {
                "Name": "MUSIC_U",
                "Value": "token",
                "Domain": "music.163.com",
                "Path": "/",
                "Expires": "2099-01-01T00:00:00.123456789+08:00",
            },
            "music.163.com;/;__csrf": {
                "Name": "__csrf",
                "Value": "csrf",
                "Domain": "music.163.com",
                "Path": "/",
                "Expires": "2099-01-01T00:00:00Z",
            },
            "music.163.com;/api;MUSIC_A_T": {
                "Name": "MUSIC_A_T",
                "Value": "old",
                "Domain": "music.163.com",
                "Path": "/api",
                "Expires": "2000-01-01T00:00:00Z",
            },
            "other.example;/;MUSIC_U": {
                "Name": "MUSIC_U",
                "Value": "wrong",
                "Domain": "other.example",
                "Path": "/",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cookie.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        jar = CookieJar(path)
        check("识别登录态", jar.has_login, True)
        check("取到 CSRF", jar.csrf, "csrf")
        check("Cookie 头包含 MUSIC_U", "MUSIC_U=token" in jar.header(), True)
        check("不混入别的域名", "wrong" in jar.header(), False)
        check("过期 Cookie 不发送", "MUSIC_A_T" in jar.header("/api/test"), False)
        check("文件不存在时为空", CookieJar(Path(tmp) / "none.json").has_login, False)

        jar.apply_set_cookie(["__csrf=new; Path=/; Max-Age=3600"])
        check("内存覆盖生效", jar.csrf, "new")
        check("不写回文件", "__csrf=new" in path.read_text(encoding="utf-8"), False)

    # 过期时间解析
    check("空过期时间是会话", _parse_expiry(""), None)
    check("数字过期时间按秒", _parse_expiry(123), 123.0)
    check("毫秒过期时间换算", _parse_expiry(1230000000000), 1230000000.0)
    check("坏格式当会话", _parse_expiry("not-a-date"), None)

    if failures:
        print(f"Cookie 自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("Cookie 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
