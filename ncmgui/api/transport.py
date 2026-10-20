"""HTTP 传输：Cookie、代理、限流、重试与错误翻译。

用标准库 ``urllib``，与封面下载、更新器保持一致，不引入 requests/httpx。
所有请求都在后台线程执行；本模块不做任何 Qt 依赖。

**限流**：网易云对高频请求会返回 HTTP 200 + ``code=460``，或者干脆空数据。
读请求并发上限 3、请求间隔至少 120ms；写请求全局串行且不自动重试，避免
重复提交（比如重复喜欢/取消喜欢）。

    .venv/bin/python -m ncmgui.api.transport
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from ..config import config_dir
from ..netutil import ProxyError, normalize_proxy
from .cookies import CookieJar
from .crypto import weapi_params
from .errors import (
    ApiChanged,
    ApiError,
    AuthRequired,
    NetworkError,
    RateLimited,
)

BASE_URL = "https://music.163.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
#: 易盾反作弊 token 的公开获取端点（SPlayer-Next 用的同一入口）
ANTICHEAT_URL = "https://dun.163.com/v3/b?pn=YD00000558929251"
#: 反作弊 token 只缓存 4 分钟，避免长时间复用被判定异常
ANTICHEAT_TTL = 240.0

#: 读请求的最小间隔（秒）
MIN_INTERVAL = 0.12
#: 同时进行的读请求上限
READ_CONCURRENCY = 3

#: 触发 429/460 后全局暂停请求的时长。读请求会等它结束，写请求直接失败，
#: 避免在同一个限流窗口里反复撞接口。
RATE_LIMIT_COOLDOWN = 30.0

_TOKEN_RE = re.compile(r'\[200,0,"([^"]+)"\]')


def _today() -> str:
    """本地日期：发现页的按天缓存到第二天自然失效。"""
    return time.strftime("%Y-%m-%d")


def _cache_path(path: str, query: dict, account: str) -> Path:
    """按接口、参数和账号生成缓存文件名。

    账号参与哈希是为了换号后不把上一个账号的每日推荐端给新账号；文件名
    只存摘要，不落 ``MUSIC_U`` 原文。
    """
    raw = json.dumps(
        [path, sorted((str(key), str(value)) for key, value in query.items()),
         account],
        ensure_ascii=False, sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return config_dir() / "http-cache" / f"{digest}.json"


def _cache_load(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("date") != _today():
        return None
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else None


def _cache_store(path: Path, payload: dict) -> None:
    """原子写缓存；失败只当没缓存，不影响请求结果。"""
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"date": _today(), "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _prune_cache(path.parent)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _prune_cache(directory: Path, keep_days: int = 7) -> None:
    """删掉一周前的缓存文件。发现页每天 4 个文件，不清会一直堆着。"""
    cutoff = time.time() - keep_days * 86400
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def mask_sensitive(text: str) -> str:
    """把日志里可能出现的 Cookie/令牌遮掉。

    只用于异常消息和调试输出；真实请求不受影响。
    """
    text = str(text)
    text = re.sub(r"(MUSIC_U=)[^;,\s]+", r"\1***", text)
    text = re.sub(r"(__csrf=)[^;,\s]+", r"\1***", text)
    text = re.sub(r"(encSecKey=)[^&,\s]+", r"\1***", text)
    return text


def _error_for(payload: dict) -> ApiError | None:
    """把业务 ``code`` 翻译成异常；成功返回 ``None``。"""
    raw = payload.get("code")
    if raw is None:
        return None
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return None
    if code in (200, 201, 250):
        return None
    message = str(
        payload.get("message") or payload.get("msg") or f"接口返回 code={code}"
    )
    if code in (301, 302):
        return AuthRequired("登录已失效，请重新登录", code)
    if code in (429, 460, 462, -103, -462):
        return RateLimited("请求过于频繁，请稍后再试", code)
    if code in (400, 403):
        return ApiError(message, code)
    return ApiError(message, code)


class Transport:
    """一次性的 HTTP 客户端。线程安全，可被多个页面共用。"""

    def __init__(
        self,
        jar: CookieJar,
        *,
        proxy: str = "",
        timeout: float = 15.0,
        base_url: str = BASE_URL,
        clock=time.monotonic,
        rate_limit_listener: Callable[[bool], None] | None = None,
    ) -> None:
        self.jar = jar
        self.proxy = proxy
        self.timeout = float(timeout)
        self.base_url = base_url.rstrip("/")
        self._clock = clock
        #: 限流状态变化回调；界面用它显示一条不会自动消失的提示
        self._rate_limit_listener = rate_limit_listener
        self._rate_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._cooldown_notified = False
        self._read_slots = threading.BoundedSemaphore(READ_CONCURRENCY)
        self._write_lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last_request = 0.0
        self._anticheat_lock = threading.Lock()
        #: 取 token 的网络请求单独串行，避免并发写操作同时打易盾
        self._anticheat_fetch_lock = threading.Lock()
        self._anticheat_token = ""
        self._anticheat_at = 0.0

    # ------------------------------------------------------------------ 公共

    def get(self, path: str, params: dict | None = None, *,
            csrf: bool = False, cache_day: bool = False) -> dict:
        """GET 一个明文接口。

        ``cache_day=True`` 时按「接口 + 参数 + 账号 + 本地日期」落盘缓存，
        当天再次请求直接读文件；发现页的推荐位用它避免每次切页都重拉。
        带 CSRF 的请求不缓存。
        """
        query = dict(params or {})
        if csrf and self.jar.csrf:
            query.setdefault("csrf_token", self.jar.csrf)
        cache: Path | None = None
        if cache_day and not csrf:
            cache = _cache_path(path, query, self.jar.value("MUSIC_U"))
            cached = _cache_load(cache)
            if cached is not None:
                return cached
        payload = self.request("GET", path, query)
        if cache is not None:
            _cache_store(cache, payload)
        return payload

    def post(self, path: str, data: dict | None = None, *,
             csrf: bool = False, write: bool = False,
             anticheat: bool = False) -> dict:
        """POST 一个明文接口。``write=True`` 时串行且不重试。"""
        payload = dict(data or {})
        if csrf:
            token = self.jar.csrf
            if not token:
                raise AuthRequired("登录信息缺少 CSRF 令牌，请重新登录")
            payload.setdefault("csrf_token", token)
        headers = {}
        if anticheat:
            token = self.anticheat_token()
            if token:
                headers["X-antiCheatToken"] = token
        return self.request("POST", path, payload, write=write, headers=headers)

    def weapi(self, path: str, data: dict | None = None, *,
              write: bool = False, anticheat: bool = False,
              domain: str = "", headers: dict[str, str] | None = None) -> dict:
        """POST 一个 weapi 接口。``path`` 形如 ``/weapi/cloudsearch/pc``。

        ``domain`` 非空时把请求发到另一台主机（例如播放日志的
        ``clientlogusf.music.163.com``），Cookie 仍按该主机的域名筛选。
        ``headers`` 用于补齐某些接口要求、与网页端一致的请求头。
        """
        if not self.jar.csrf:
            raise AuthRequired("登录信息缺少 CSRF 令牌，请重新登录")
        body = weapi_params(dict(data or {}), self.jar.csrf)
        extra = dict(headers or {})
        if anticheat:
            token = self.anticheat_token()
            if token:
                extra.setdefault("X-antiCheatToken", token)
        return self.request("POST", path, body, write=write, headers=extra,
                            retry=not write, domain=domain)

    def request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        *,
        write: bool = False,
        headers: dict | None = None,
        retry: bool | None = None,
        domain: str = "",
    ) -> dict:
        """发请求并返回 JSON。``domain`` 可覆盖接口主机。"""
        if retry is None:
            retry = not write
        attempts = 3 if retry else 1
        last: ApiError | None = None
        for attempt in range(attempts):
            if attempt:
                # 指数退避 + 抖动；写请求不会走到这里
                time.sleep(min(4.0, 0.35 * (2 ** (attempt - 1))) * (0.7 + random.random() * 0.6))
            # 上一次限流还没退避完：读请求等它结束，写请求直接失败，
            # 避免在同一个限流窗口里继续撞接口
            self._wait_cooldown(write)
            try:
                return self._once(method, path, data or {}, write=write,
                                  headers=headers or {}, domain=domain)
            except RateLimited as exc:
                last = exc
                self._trip_cooldown()
                if not retry:
                    raise
            except NetworkError as exc:
                last = exc
                if not retry:
                    raise
            except ApiError:
                raise
        raise last or NetworkError("请求失败")

    # -------------------------------------------------------------- 单次请求

    def _once(self, method: str, path: str, data: dict, *,
              write: bool, headers: dict, domain: str = "") -> dict:
        if not path.startswith("/"):
            path = "/" + path
        base_url = (domain or self.base_url).rstrip("/")
        url = base_url + path
        body: bytes | None = None
        if method == "GET":
            if data:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                    data, doseq=True
                )
        else:
            encoded = urllib.parse.urlencode(data, doseq=True)
            body = encoded.encode("utf-8")
            headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded; charset=utf-8"
            )

        request_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        # Cookie 按实际请求的主机筛选：播放日志发到 clientlogusf 子域，
        # 但登录凭据的 Domain 是 music.163.com，仍应带上。
        host = urllib.parse.urlsplit(base_url).hostname or "music.163.com"
        cookie = self.jar.header(domain=host, path=path)
        if cookie:
            request_headers["Cookie"] = cookie
        request_headers.update(headers)

        opener = self._opener()
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )

        slot = self._write_lock if write else self._read_slots
        with slot:
            self._pace()
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    set_cookies = response.headers.get_all("Set-Cookie") or []
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                set_cookies = exc.headers.get_all("Set-Cookie") or []
                status = exc.code
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                raise NetworkError(mask_sensitive(f"网络请求失败：{reason}")) from exc
            else:
                status = 200
        self.jar.apply_set_cookie(set_cookies)

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            if status >= 500:
                raise NetworkError(f"服务器返回 HTTP {status}") from exc
            raise ApiChanged("接口返回的不是 JSON，可能已经改版") from exc
        if not isinstance(payload, dict):
            raise ApiChanged("接口返回的 JSON 不是对象，可能已经改版")

        if status >= 400 and "code" not in payload:
            if status in (429, 503):
                raise RateLimited(f"服务器限流（HTTP {status}）", status)
            raise NetworkError(f"服务器返回 HTTP {status}")
        error = _error_for(payload)
        if error is not None:
            raise error
        self._clear_cooldown()
        return payload

    def _opener(self):
        try:
            proxy = normalize_proxy(self.proxy)
        except ProxyError as exc:
            raise NetworkError(f"代理设置无效：{exc}") from exc
        if proxy == "direct":
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        return urllib.request.build_opener()

    def _pace(self) -> None:
        with self._pace_lock:
            now = self._clock()
            wait = MIN_INTERVAL - (now - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = self._clock()

    # ------------------------------------------------------------ 限流退避

    def _notify_rate_limit(self, active: bool) -> None:
        listener = self._rate_limit_listener
        if listener is None:
            return
        try:
            listener(active)
        except Exception:  # noqa: BLE001 - 界面回调失败不能影响请求
            pass

    def _trip_cooldown(self) -> None:
        with self._rate_lock:
            self._cooldown_until = max(
                self._cooldown_until, self._clock() + RATE_LIMIT_COOLDOWN
            )
            notify = not self._cooldown_notified
            self._cooldown_notified = True
        if notify:
            self._notify_rate_limit(True)

    def _clear_cooldown(self) -> None:
        with self._rate_lock:
            if not self._cooldown_notified:
                return
            if self._clock() < self._cooldown_until:
                return
            self._cooldown_until = 0.0
            self._cooldown_notified = False
        self._notify_rate_limit(False)

    def _wait_cooldown(self, write: bool) -> None:
        with self._rate_lock:
            remaining = self._cooldown_until - self._clock()
        if remaining <= 0:
            return
        if write:
            raise RateLimited("接口限流，正在自动退避，请稍后再试", 460)
        # 后台读请求等退避结束；上限就是 RATE_LIMIT_COOLDOWN，不会无限拖住
        time.sleep(min(remaining, RATE_LIMIT_COOLDOWN))

    # ------------------------------------------------------------ 反作弊 token

    def anticheat_token(self) -> str:
        """取易盾 token；失败返回空串（调用方决定是否降级）。"""
        now = self._clock()
        with self._anticheat_lock:
            if self._anticheat_token and now - self._anticheat_at < ANTICHEAT_TTL:
                return self._anticheat_token
        # 未命中时只放一个线程去取，其余等它填好缓存再看一次；否则几个
        # 并发写请求会同时打易盾，既浪费也更容易被风控。这里不占用
        # _anticheat_lock 做网络 I/O，读缓存的一方不会被慢请求堵住。
        with self._anticheat_fetch_lock:
            with self._anticheat_lock:
                if (self._anticheat_token
                        and self._clock() - self._anticheat_at < ANTICHEAT_TTL):
                    return self._anticheat_token
            try:
                request = urllib.request.Request(
                    ANTICHEAT_URL,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": "https://music.163.com/",
                    },
                )
                with self._opener().open(request, timeout=min(self.timeout, 8.0)) as response:
                    text = response.read().decode("utf-8", errors="replace")
                match = _TOKEN_RE.search(text)
                token = match.group(1) if match else ""
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException):
                token = ""
            fetched_at = self._clock()
            with self._anticheat_lock:
                self._anticheat_token = token
                # 记响应到达时刻：TTL 从拿到 token 开始算，慢请求不会
                # 让缓存提前过期。
                self._anticheat_at = fetched_at
            return token


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

    check("成功码不报错", _error_for({"code": 200}), None)
    check("没有 code 不报错", _error_for({}), None)
    check("301 要登录", isinstance(_error_for({"code": 301}), AuthRequired), True)
    check("302 要登录", isinstance(_error_for({"code": 302}), AuthRequired), True)
    check("460 限流", isinstance(_error_for({"code": 460}), RateLimited), True)
    check("-103 可重试", _error_for({"code": -103}).retryable, True)
    check("普通错误保留消息", str(_error_for({"code": 404, "message": "没了"})), "没了")
    check("脱敏 MUSIC_U", "MUSIC_U=***" in mask_sensitive("Cookie: MUSIC_U=abc; x=1"), True)
    check("脱敏 csrf", "__csrf=***" in mask_sensitive("__csrf=abc&a=1"), True)
    check("不误伤普通文本", mask_sensitive("播放失败"), "播放失败")

    class _Jar:
        csrf = "token"

    class _Recorder(Transport):
        def __init__(self) -> None:
            super().__init__(jar=_Jar())  # type: ignore[arg-type]
            self.seen: dict = {}

        def request(self, method, path, data=None, **kwargs) -> dict:  # type: ignore[override]
            self.seen = kwargs
            return {"code": 200}

    recorder = _Recorder()
    recorder.weapi("/weapi/feedback/weblog", {"logs": "[]"},
                   headers={"x-music-web-os": "web3", "Nm-GCore-Status": "1"})
    check("weapi 透传自定义请求头", recorder.seen.get("headers"),
          {"x-music-web-os": "web3", "Nm-GCore-Status": "1"})

    if failures:
        print(f"传输层自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("传输层自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
