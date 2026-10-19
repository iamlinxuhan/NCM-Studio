"""ncmctl 命令行封装。

本模块只做两件事：把界面上的选择翻译成 **参数数组**，以及从磁盘/输出里读出状态。
刻意不依赖 Qt，因此可以直接运行自检：

    .venv/bin/python -m ncmgui.ncmctl

所有命令一律以参数数组形式返回，交给 ``subprocess`` 直接 exec，不经过 shell。
这样路径里的空格、中文、引号都无需转义，也不存在命令注入问题。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 当前是不是 Windows。全模块唯一的平台判断。
_IS_WINDOWS = sys.platform == "win32"

#: 可执行文件名。Windows 上必须带 ``.exe``，否则 ``shutil.which`` 找不到。
BINARY_NAME = "ncmctl.exe" if _IS_WINDOWS else "ncmctl"

#: PATH 里找不到时，依次翻这些目录。每个目录下 ``ncmctl`` / ``ncmctl.exe``
#: 两种写法都试一遍，这样从别的平台整包拷过来的目录也认得出来。
_COMMON_DIRS: tuple[str, ...] = (
    "~/go/bin",           # go install 的默认落点
    "~/.local/bin",       # 手工放置
    "/usr/local/bin",     # macOS（Intel）与多数 Linux 发行版
    "/usr/bin",
    "/opt/homebrew/bin",  # macOS（Apple Silicon）
)

if _IS_WINDOWS:  # pragma: no cover - 只在 Windows 上生效
    _COMMON_DIRS += (
        os.path.expandvars(r"%USERPROFILE%\go\bin"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
        os.path.expandvars(r"%USERPROFILE%\scoop\shims"),
        r"C:\ProgramData\chocolatey\bin",
    )


def _fallback_paths() -> tuple[Path, ...]:
    """把常见目录与两种可执行文件名组合成候选清单。"""
    names = tuple(dict.fromkeys((BINARY_NAME, "ncmctl")))
    return tuple(
        Path(directory).expanduser() / name
        for directory in _COMMON_DIRS
        for name in names
    )


def _is_executable(path: Path) -> bool:
    """判断这是个能跑的程序。

    Windows 没有执行位，``os.access(..., os.X_OK)`` 在那边只能去翻 ``PATHEXT``
    环境变量来猜，从资源管理器或计划任务启动时未必拿得到，所以不拿它当准。
    """
    if _IS_WINDOWS:
        return path.is_file()
    return path.is_file() and os.access(path, os.X_OK)


class NcmctlNotFound(RuntimeError):
    """找不到 ncmctl 可执行文件。"""


# --------------------------------------------------------------------------- #
# 文本处理
# --------------------------------------------------------------------------- #

ANSI_RE = re.compile(
    r"""
      \x1b\[[0-9;?]*[ -/]*[@-~]          # CSI：颜色、光标移动、清屏
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)  # OSC：终端标题等
    | \x1b[@-Z\\-_]                      # 其余两字节转义
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    """去掉 ANSI 转义序列，让日志在 QPlainTextEdit 里保持干净。"""
    return ANSI_RE.sub("", text)


def describe(argv: list[str]) -> str:
    """把参数数组渲染成可直接粘贴到终端的一行，用于日志回显。

    引号规则跟着平台走，这样「复制命令」按钮拿到的字符串在目标平台上能直接跑。
    **不会脱敏** —— 需要回显或复制时先过一遍 :func:`redact_argv`。
    """
    if _IS_WINDOWS:
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


#: 后面跟敏感值的参数。这些值仍会原样交给 ncmctl（接口只支持从 argv 读），
#: 但不能进入界面回显、复制命令或导出日志。
SENSITIVE_FLAGS = frozenset({
    "--password", "-p",
    "--headers",
    "--xeapi-session-key",
    "--dynamic-key",
})

REDACTED = "******"


def redact_argv(argv: list[str]) -> list[str]:
    """把敏感参数的值替换成 ``******``，参数结构保持不变。

    同时处理 ``--flag value`` 与 ``--flag=value`` 两种写法。真实 argv 不受影响：
    这只用于显示，执行时仍用原始列表。
    """
    out: list[str] = []
    hide_next = False
    for token in argv:
        if hide_next:
            out.append(REDACTED)
            hide_next = False
            continue
        name, separator, _value = token.partition("=")
        if name in SENSITIVE_FLAGS:
            if separator:
                out.append(f"{name}={REDACTED}")
            else:
                out.append(token)
                hide_next = True
            continue
        out.append(token)
    return out


def parse_targets(text: str) -> list[str]:
    """把用户粘贴的歌曲 ID / URL 拆成列表。

    - 以换行、空格或逗号分隔
    - 整行以 ``#`` 开头视为注释；行内的 ``#`` 保留（URL 片段可能用到）
    - 去重并保持原有顺序
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in re.split(r"[\s,]+", line):
            token = token.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
    return out


def clamp(value: int, low: int, high: int) -> int:
    """把并发数等数值限制在 ncmctl 接受的区间内。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# 可执行文件与登录状态探测
# --------------------------------------------------------------------------- #

def bundled_binary() -> str | None:
    """随程序分发的 ncmctl，没有则返回 None。

    两处会放这个东西：

    * 打包后 —— 跟本程序在同一个 bundle 里，解包目录由 ``sys._MEIPASS`` 给出
      （onefile 是临时目录，onedir 是 ``_internal``，这个属性在两种模式下都可用）。
    * 源码运行 —— 仓库里的 ``vendor/``，跑过 ``scripts/fetch_ncmctl.py`` 就有。

    源码运行也认 ``vendor/``，是为了让开发和打包后的行为一致：我们分发的就是
    那个版本，开发时也用它，免得出现「本地好好的、打包后行为不一样」。
    想用自己的 ncmctl，在设置页填路径即可 —— 那一项优先级更高。
    """
    candidates: list[Path] = []

    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / BINARY_NAME)

    candidates.append(Path(__file__).resolve().parent.parent / "vendor" / BINARY_NAME)

    for candidate in candidates:
        if _is_executable(candidate):
            return str(candidate)
    return None


def find_binary(explicit: str = "") -> str | None:
    """定位 ncmctl 可执行文件，找不到返回 None。

    顺序：**设置页指定的路径 → 随包分发的 → PATH → 常见安装目录**。

    随包那份排在 PATH 前面，是为了让绝大多数用户拿到的永远是测过的那个版本 ——
    界面里每个按钮都对应 ncmctl 的一个子命令，版本对不上就会出现「有这个按钮
    但命令不支持」。想用自己的（比如更新的）在设置页填路径，那一项优先级最高。
    """
    explicit = (explicit or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        # 裸名字（不含分隔符、不是绝对路径）只允许 ncmctl / ncmctl.exe 走 PATH。
        # 不能把用户随手填的 "evil" 当成命令名去 PATH 里解析 —— 那等于让设置页
        # 变成一个任意命令启动器。
        looks_like_path = (
            candidate.is_absolute()
            or explicit.startswith("~")
            or "/" in explicit
            or "\\" in explicit
        )
        if looks_like_path:
            # 显式指定了却找不到，如实返回 None 让界面报错。不能悄悄退回随包的那份，
            # 否则用户填错的路径永远发现不了
            return str(candidate) if _is_executable(candidate) else None
        if explicit in ("ncmctl", "ncmctl.exe"):
            return shutil.which(explicit)
        return None

    bundled = bundled_binary()
    if bundled:
        return bundled

    found = shutil.which(BINARY_NAME)
    if found:
        return found

    for candidate in _fallback_paths():
        if _is_executable(candidate):
            return str(candidate)
    return None


def state_dir(home: str = "") -> Path:
    """返回 ncmctl 的状态目录 ``<home>/.ncmctl``。"""
    root = Path(home).expanduser() if (home or "").strip() else Path.home()
    return root / ".ncmctl"


def cookie_path(home: str = "") -> Path:
    return state_dir(home) / "cookie.json"


def is_logged_in(home: str = "") -> bool:
    """cookie.json 存在即认为已登录（与 ncmctl 的持久化位置一致）。"""
    try:
        return cookie_path(home).is_file()
    except OSError:
        return False


def login_timestamp(home: str = "") -> float | None:
    """返回 cookie.json 的修改时间，用于显示「上次登录」。"""
    try:
        return cookie_path(home).stat().st_mtime
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 全局参数
# --------------------------------------------------------------------------- #

@dataclass
class GlobalOpts:
    """ncmctl 的根级持久化参数，会附加到每条命令末尾。

    留空表示「不传递」，交给 ncmctl 使用自身默认值。
    """

    home: str = ""
    config: str = ""
    debug: bool = False

    def apply(self, argv: list[str]) -> list[str]:
        if self.home.strip():
            argv += ["--home", self.home.strip()]
        if self.config.strip():
            argv += ["-c", self.config.strip()]
        if self.debug:
            argv.append("--debug")
        return argv


# --------------------------------------------------------------------------- #
# 选项取值表（供界面下拉框使用）
# --------------------------------------------------------------------------- #

#: 下载音质：(界面显示, 传给 --level 的值)
DOWNLOAD_LEVELS: list[tuple[str, str]] = [
    ("标准 128 kbps", "standard"),
    ("较高 192 kbps", "higher"),
    ("极高 320 kbps", "exhigh"),
    ("无损 FLAC", "lossless"),
    ("Hi-Res 高解析", "hires"),
]


def level_label(value: str) -> str:
    """把 ``--level`` 的档位值翻成人话，其他描述原样返回。

    界面、播放历史、统计都会碰到同一个值：本地文件已经是
    「无损 44.1kHz/16bit」这样的描述，在线歌曲则是 ``lossless`` 这样的键。
    统一在这里转换，免得三处各写一份映射、发版后各显示各的。
    """
    for name, level in DOWNLOAD_LEVELS:
        if level == value:
            return name
    return value


#: curl 的 API 包装器包名
CURL_KINDS: list[tuple[str, str]] = [
    ("weapi（网页端）", "weapi"),
    ("eapi（客户端）", "eapi"),
    ("linux（Linux 客户端）", "linux"),
    ("api（原生接口）", "api"),
]

#: crypto 加密支持的格式
ENCRYPT_KINDS: list[tuple[str, str]] = [
    ("weapi", "weapi"),
    ("eapi", "eapi"),
    ("linux", "linux"),
]

#: crypto 解密支持的格式
DECRYPT_KINDS: list[tuple[str, str]] = [
    ("eapi", "eapi"),
    ("xeapi", "xeapi"),
    ("weapi", "weapi"),
    ("linux", "linux"),
]

#: 解密输入编码
DECRYPT_ENCODINGS: list[tuple[str, str]] = [
    ("hex 十六进制", "hex"),
    ("base64", "base64"),
    ("string 原始字符串", "string"),
]

#: HAR 解密目标
DECRYPT_TARGETS: list[tuple[str, str]] = [
    ("自动判断", "auto"),
    ("请求 request", "request"),
    ("响应 response", "response"),
    ("两者都要（仅 HAR）", "both"),
]


# --------------------------------------------------------------------------- #
# 命令构建器
# --------------------------------------------------------------------------- #

def build_download(
    targets: list[str],
    level: str = "lossless",
    output: str = "",
    parallel: int = 5,
    strict: bool = False,
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl download``：解析 ID/URL 后下载。

    :param targets: 歌曲 ID、专辑/歌单/歌手 URL 列表
    :param strict: 精确音质不可得时跳过该曲，而不是降级下载
    """
    if not targets:
        raise ValueError("至少需要一个歌曲 ID 或链接")

    argv = ["download"]
    if level:
        argv += ["--level", level]
    if output.strip():
        argv += ["--output", output.strip()]
    argv += ["--parallel", str(clamp(parallel, 1, 20))]
    if strict:
        argv.append("--strict")
    argv += list(targets)
    return (opts or GlobalOpts()).apply(argv)


def build_ncm(
    inputs: list[str],
    output: str = "",
    parallel: int = 10,
    no_tag: bool = False,
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl ncm``：把 .ncm 解密成 .mp3/.flac。

    :param no_tag: 传入 ``--tag``，对应 help 中的「禁用音频标签写入」
    """
    if not inputs:
        raise ValueError("至少需要一个 .ncm 文件或目录")

    argv = ["ncm"]
    if output.strip():
        argv += ["--output", output.strip()]
    argv += ["--parallel", str(clamp(parallel, 1, 50))]
    if no_tag:
        argv.append("--tag")
    argv += list(inputs)
    return (opts or GlobalOpts()).apply(argv)


def build_cloud(
    source: str,
    minsize: str = "",
    parallel: int = 3,
    regexp: str = "",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl cloud``：上传音乐到账号云盘（单文件或目录，递归三层）。"""
    if not source.strip():
        raise ValueError("请选择要上传的文件或目录")

    argv = ["cloud"]
    if minsize.strip():
        argv += ["--minsize", minsize.strip()]
    if regexp.strip():
        argv += ["--regexp", regexp.strip()]
    argv += ["--parallel", str(clamp(parallel, 1, 10))]
    argv.append(source.strip())
    return (opts or GlobalOpts()).apply(argv)


def build_sign(automatic: bool = False, opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl sign``：云贝 + VIP 每日签到。"""
    argv = ["sign"]
    if automatic:
        argv.append("--automatic")
    return (opts or GlobalOpts()).apply(argv)


def build_scrobble(num: int = 300, opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl scrobble``：提交播放记录以增加听歌数。"""
    argv = ["scrobble", "--num", str(clamp(num, 1, 300))]
    return (opts or GlobalOpts()).apply(argv)


def build_partner(
    star: list[int] | None = None,
    extra: list[int] | None = None,
    num: str = "random",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl partner``：提交音乐合伙人评测。

    :param star: 基础评分候选，取值 1-5
    :param extra: 附加评分候选，取值 1-5
    :param num: ``random`` 或 0-15 的整数
    """
    argv = ["partner"]
    if star:
        argv += ["--star", ",".join(str(s) for s in star)]
    if extra:
        argv += ["--extra", ",".join(str(e) for e in extra)]
    if num:
        argv += ["--num", str(num)]
    return (opts or GlobalOpts()).apply(argv)


@dataclass
class TaskSpec:
    """``ncmctl task`` 的定时任务配置。"""

    sign: bool = False
    scrobble: bool = False
    partner: bool = False
    sign_cron: str = "0 10 * * *"
    scrobble_cron: str = "0 18 * * *"
    partner_cron: str = "0 18 * * *"
    location: str = "Asia/Shanghai"
    sign_automatic: bool = False
    scrobble_num: int = 300
    partner_num: str = "random"


def build_task(spec: TaskSpec, opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl task``：以常驻服务方式按 cron 调度任务。

    未勾选任何任务时退化为 ``--runAll``，与「不带选择器即全部注册」等价。
    """
    argv = ["task"]
    selectors = spec.sign or spec.scrobble or spec.partner

    if not selectors:
        argv.append("--runAll")
    if spec.sign:
        argv.append("--sign")
        if spec.sign_automatic:
            argv.append("--sign.automatic")
        if spec.sign_cron.strip():
            argv += ["--sign.cron", spec.sign_cron.strip()]
    if spec.scrobble:
        argv.append("--scrobble")
        if spec.scrobble_cron.strip():
            argv += ["--scrobble.cron", spec.scrobble_cron.strip()]
        argv += ["--scrobble.num", str(clamp(spec.scrobble_num, 1, 300))]
    if spec.partner:
        argv.append("--partner")
        if spec.partner_cron.strip():
            argv += ["--partner.cron", spec.partner_cron.strip()]
        if spec.partner_num:
            argv += ["--partner.extNum", str(spec.partner_num)]

    if spec.location.strip():
        argv += ["--location", spec.location.strip()]
    return (opts or GlobalOpts()).apply(argv)


@dataclass
class ProxySpec:
    """``ncmctl proxy`` 的监听与解密配置。"""

    listen: str = "127.0.0.1:9000"
    ca_cert: str = ""
    ca_key: str = ""
    max_body: str = "1MB"
    show_sensitive: bool = False
    xeapi_session_id: str = ""
    xeapi_session_key: str = ""
    xeapi_state_file: str = ""


def build_proxy(spec: ProxySpec, opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl proxy``：启动本次会话的 HTTP(S) 监控代理。"""
    if (spec.ca_cert.strip() and not spec.ca_key.strip()) or (
        spec.ca_key.strip() and not spec.ca_cert.strip()
    ):
        raise ValueError("CA 证书与私钥必须同时提供，或同时留空")
    if (spec.xeapi_session_id.strip() and not spec.xeapi_session_key.strip()) or (
        spec.xeapi_session_key.strip() and not spec.xeapi_session_id.strip()
    ):
        raise ValueError("XEAPI Session ID 与密钥必须同时提供，或同时留空")

    argv = ["proxy"]
    if spec.listen.strip():
        argv += ["--listen", spec.listen.strip()]
    if spec.ca_cert.strip():
        argv += ["--ca-cert", spec.ca_cert.strip(), "--ca-key", spec.ca_key.strip()]
    if spec.max_body.strip():
        argv += ["--max-body", spec.max_body.strip()]
    if spec.show_sensitive:
        argv.append("--show-sensitive")
    if spec.xeapi_session_id.strip():
        argv += ["--xeapi-session-id", spec.xeapi_session_id.strip()]
        argv += ["--xeapi-session-key", spec.xeapi_session_key.strip()]
    if spec.xeapi_state_file.strip():
        argv += ["--xeapi-state-file", spec.xeapi_state_file.strip()]
    return (opts or GlobalOpts()).apply(argv)


def build_crypto_encrypt(
    payload: str,
    kind: str = "weapi",
    url: str = "",
    output: str = "",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl crypto encrypt``：本地加密 NetEase API 载荷。

    :param payload: JSON 文本，或一个包含 JSON 的文件路径
    :param url: EAPI 必须提供，请求路由参与摘要计算
    """
    if not payload.strip():
        raise ValueError("请填写要加密的 JSON 内容")

    argv = ["crypto", "encrypt", "--kind", kind]
    if url.strip():
        argv += ["--url", url.strip()]
    if output.strip():
        argv += ["--output", output.strip()]
    argv.append(payload)
    return (opts or GlobalOpts()).apply(argv)


def build_crypto_decrypt(
    ciphertext: str,
    kind: str = "eapi",
    encode: str = "hex",
    dynamic_key: str = "",
    dynamic_key_encode: str = "string",
    target: str = "auto",
    url: str = "*",
    output: str = "",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl crypto decrypt``：解密 EAPI/XEAPI 载荷或 HAR 抓包。"""
    if not ciphertext.strip():
        raise ValueError("请填写要解密的密文或 HAR 文件路径")

    argv = ["crypto", "decrypt", "--kind", kind, "--encode", encode]
    if dynamic_key.strip():
        argv += ["--dynamic-key", dynamic_key.strip()]
        argv += ["--dynamic-key-encode", dynamic_key_encode]
    if target:
        argv += ["--target", target]
    if url.strip():
        argv += ["--url", url.strip()]
    if output.strip():
        argv += ["--output", output.strip()]
    argv.append(ciphertext)
    return (opts or GlobalOpts()).apply(argv)


def build_curl(
    method: str,
    kind: str = "weapi",
    data: str = "{}",
    output: str = "",
    timeout: str = "15s",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl curl``：调用导出的 Go API 方法。

    注意这里的 method 是 Go 方法名（如 ``GetUserInfo``），不是 HTTP 动词。
    """
    if not method.strip():
        raise ValueError("请填写要调用的 API 方法名")

    argv = ["curl", "--kind", kind, "--data", data or "{}"]
    if timeout.strip():
        argv += ["--timeout", timeout.strip()]
    if output.strip():
        argv += ["--output", output.strip()]
    argv.append(method.strip())
    return (opts or GlobalOpts()).apply(argv)


def build_login_qrcode(
    directory: str,
    level: int = 1,
    timeout: str = "5m",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl login qrcode``：生成 ``qrcode.png`` 并等待手机端确认。

    界面会轮询 ``directory/qrcode.png`` 把它显示出来。
    """
    if not directory.strip():
        raise ValueError("二维码输出目录不能为空")

    argv = ["login", "qrcode", "--dir", directory.strip(), "--level", str(clamp(level, 0, 3))]
    if timeout.strip():
        argv += ["--timeout", timeout.strip()]
    return (opts or GlobalOpts()).apply(argv)


def build_login_phone(
    number: str,
    countrycode: int = 86,
    password: str = "",
    timeout: str = "10m",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl login phone``：短信验证码或密码登录。

    :param password: 留空则走短信验证码流程（需要在控制台里输入验证码）
    """
    if not number.strip():
        raise ValueError("请填写手机号")

    argv = ["login", "phone", "--countrycode", str(int(countrycode))]
    if password:
        argv += ["--password", password]
    if timeout.strip():
        argv += ["--timeout", timeout.strip()]
    argv.append(number.strip())
    return (opts or GlobalOpts()).apply(argv)


def build_login_cookie(
    file: str = "",
    content: str = "",
    fmt: str = "",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl login cookie``：从浏览器导出的 Cookie 登录。

    界面通常先把粘贴的 Cookie 写入 0600 权限的临时文件再传 ``--file``，
    避免凭据出现在进程参数里。
    """
    if not file.strip() and not content.strip():
        raise ValueError("请选择 Cookie 文件，或粘贴 Cookie 内容")

    argv = ["login", "cookie"]
    if fmt.strip():
        argv += ["--format", fmt.strip()]
    if file.strip():
        argv += ["--file", file.strip()]
    else:
        argv.append(content.strip())
    return (opts or GlobalOpts()).apply(argv)


def build_login_cookiecloud(
    uuid: str,
    password: str,
    server: str = "",
    timeout: str = "",
    headers: str = "",
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl login cookiecloud``：从 CookieCloud 服务器同步 Cookie 登录。"""
    if not uuid.strip() or not password:
        raise ValueError("CookieCloud 的 UUID 与密码都是必填项")

    argv = ["login", "cookiecloud", "--uuid", uuid.strip(), "--password", password]
    if server.strip():
        argv += ["--server", server.strip()]
    if timeout.strip():
        argv += ["--timeout", timeout.strip()]
    if headers.strip():
        argv += ["--headers", headers.strip()]
    return (opts or GlobalOpts()).apply(argv)


def build_logout(
    clear_anonymous_token: bool = False,
    opts: GlobalOpts | None = None,
) -> list[str]:
    """``ncmctl logout``：登出并删除本地会话状态。"""
    argv = ["logout"]
    if clear_anonymous_token:
        argv.append("--clear-anonymous-token")
    return (opts or GlobalOpts()).apply(argv)


def build_update(proxy: str = "", opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl update``：从 GitHub Release 更新自身。

    :param proxy: 空格分隔的代理前缀；传空字符串表示强制直连
    """
    argv = ["update"]
    if proxy.strip():
        argv += ["--proxy", proxy.strip()]
    return (opts or GlobalOpts()).apply(argv)


def build_version(opts: GlobalOpts | None = None) -> list[str]:
    """``ncmctl --version``：探测版本。"""
    return (opts or GlobalOpts()).apply(["--version"])


#: ``--version`` 输出里的版本行。它先打一个 ASCII 艺术字，再打
#: `` Version: <tab>0.8.1`` —— 所以认冒号后面的字段，而不是抓第一个
#: 长得像版本号的东西（艺术字里就有数字和点）。
_VERSION_RE = re.compile(r"Version:\s*(\S+)")


def probe_version(binary: str, opts: GlobalOpts | None = None) -> str | None:
    """跑一次 ``ncmctl --version`` 取出版本号；取不到返回 None。

    这是个会起子进程的阻塞调用，别在界面线程里直接调 —— 用
    :class:`ncmgui.workers.BackgroundTask` 包一层。

    之所以要真的去问，是因为设置页允许用户指定自己的 ncmctl，那个版本
    只有问过才知道。随包分发的那个版本是编译期就知道的，用不着跑。
    """
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, *build_version(opts)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(strip_ansi(result.stdout) + strip_ansi(result.stderr))
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    """对每个构建器断言参数数组，返回失败数量。"""
    from unittest import mock

    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    g = GlobalOpts()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}\n    期望 {want}\n    实际 {got}")

    module = sys.modules[__name__]

    # find_binary 的回退分支必须能跑完。它只在 PATH 命不中时才去翻常见安装
    # 目录，而开发机的 PATH 里通常就有 ncmctl —— 那条分支平时根本走不到，里
    # 面就是把名字写错了，自检也照样报「全部通过」。所以这里把 PATH 临时清掉
    # 逼它走一遍，而不是听凭运行环境决定测不测得到。
    #
    # 同时得把随包副本也屏蔽掉，否则它会先命中，回退循环照样执行不到 —— 守卫
    # 又一次变成摆设。
    try:
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-ncmgui-probe"}), \
                mock.patch.object(module, "bundled_binary", return_value=None):
            find_binary()
    except Exception as exc:
        failures.append(f"find_binary 回退分支抛异常：{exc!r}")

    # 内置副本优先于 PATH —— 这是「锁定版本」的核心保证，写错顺序就等于没锁
    with tempfile.TemporaryDirectory(prefix="ncmgui-selftest-") as tmpdir:
        fake_dir = Path(tmpdir)
        fake = fake_dir / BINARY_NAME
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)

        bundled = bundled_binary()
        check("vendor/ 里的 ncmctl 能被认出", bool(bundled), True)
        check(
            "随包副本的文件名是 ncmctl",
            Path(bundled).name if bundled else None,
            BINARY_NAME,
        )

        if bundled:
            with mock.patch.dict(os.environ, {"PATH": str(fake_dir)}):
                check(
                    "PATH 上有别的 ncmctl 时仍然优先用随包的",
                    find_binary(),
                    bundled,
                )
            # 设置页填了路径则一切以它为准，这是留给用户的逃生口
            check(
                "设置页指定的路径优先级最高",
                find_binary(str(fake)),
                str(fake),
            )

    fallbacks = _fallback_paths()
    check("回退候选非空", bool(fallbacks), True)
    check(
        "回退候选数与常见目录数对得上",
        len(fallbacks),
        len(_COMMON_DIRS) * len({BINARY_NAME, "ncmctl"}),
    )
    check(
        "回退候选都指向 ncmctl",
        sorted({p.name for p in fallbacks}),
        sorted({BINARY_NAME, "ncmctl"}),
    )

    check(
        "download 常规",
        build_download(["2161154646", "1820944399"], "lossless", "/tmp/m", 4, False),
        ["download", "--level", "lossless", "--output", "/tmp/m", "--parallel", "4",
         "2161154646", "1820944399"],
    )
    check(
        "download 严格 + 并发越界钳制",
        build_download(["1"], "hires", "", 99, True),
        ["download", "--level", "hires", "--parallel", "20", "--strict", "1"],
    )
    check(
        "ncm 默认写标签",
        build_ncm(["/m/a.ncm"], "/tmp/n", 10, False),
        ["ncm", "--output", "/tmp/n", "--parallel", "10", "/m/a.ncm"],
    )
    check(
        "ncm 禁用标签",
        build_ncm(["/m/a.ncm"], "", 10, True),
        ["ncm", "--parallel", "10", "--tag", "/m/a.ncm"],
    )
    check(
        "cloud 全参数",
        build_cloud("/m", "1MB", 5, r".*\.flac$"),
        ["cloud", "--minsize", "1MB", "--regexp", r".*\.flac$", "--parallel", "5", "/m"],
    )
    check("sign", build_sign(True), ["sign", "--automatic"])
    check("scrobble 越界钳制", build_scrobble(999), ["scrobble", "--num", "300"])
    check(
        "partner",
        build_partner([3, 4], [2, 3, 4], "5"),
        ["partner", "--star", "3,4", "--extra", "2,3,4", "--num", "5"],
    )
    check(
        "task 全部默认",
        build_task(TaskSpec()),
        ["task", "--runAll", "--location", "Asia/Shanghai"],
    )
    check(
        "task 仅签到",
        build_task(TaskSpec(sign=True, sign_cron="0 10 * * *")),
        ["task", "--sign", "--sign.cron", "0 10 * * *", "--location", "Asia/Shanghai"],
    )
    check(
        "proxy 默认",
        build_proxy(ProxySpec()),
        ["proxy", "--listen", "127.0.0.1:9000", "--max-body", "1MB"],
    )
    check(
        "proxy CA 配对",
        build_proxy(ProxySpec(ca_cert="/c.crt", ca_key="/c.key")),
        ["proxy", "--listen", "127.0.0.1:9000",
         "--ca-cert", "/c.crt", "--ca-key", "/c.key", "--max-body", "1MB"],
    )
    check(
        "crypto encrypt",
        build_crypto_encrypt('{"key":"value"}', "weapi", "", ""),
        ["crypto", "encrypt", "--kind", "weapi", '{"key":"value"}'],
    )
    check(
        "crypto decrypt xeapi",
        build_crypto_decrypt("DEADBEEF", "xeapi", "hex", "", "string", "auto", "*", ""),
        ["crypto", "decrypt", "--kind", "xeapi", "--encode", "hex",
         "--target", "auto", "--url", "*", "DEADBEEF"],
    )
    check(
        "curl",
        build_curl("GetUserInfo", "weapi", "{}", "", "15s"),
        ["curl", "--kind", "weapi", "--data", "{}", "--timeout", "15s", "GetUserInfo"],
    )
    check(
        "login qrcode",
        build_login_qrcode("/tmp/qr", 1, "5m"),
        ["login", "qrcode", "--dir", "/tmp/qr", "--level", "1", "--timeout", "5m"],
    )
    check(
        "login phone 短信",
        build_login_phone("18800008888"),
        ["login", "phone", "--countrycode", "86", "--timeout", "10m", "18800008888"],
    )
    check(
        "login cookie 文件优先",
        build_login_cookie(file="/tmp/c.txt", content="MUSIC_U=x", fmt="header"),
        ["login", "cookie", "--format", "header", "--file", "/tmp/c.txt"],
    )
    check(
        "login cookiecloud",
        build_login_cookiecloud("u", "p", "", "", ""),
        ["login", "cookiecloud", "--uuid", "u", "--password", "p"],
    )
    check("logout", build_logout(True), ["logout", "--clear-anonymous-token"])
    check("update", build_update(""), ["update"])

    # 全局参数附加
    check(
        "全局参数附加",
        build_sign(False, GlobalOpts(home="/srv/h", config="/c.yaml", debug=True)),
        ["sign", "--home", "/srv/h", "-c", "/c.yaml", "--debug"],
    )

    # 凭据遮罩
    check(
        "密码分离写法被遮罩",
        redact_argv(["login", "phone", "--password", "hunter2", "188"]),
        ["login", "phone", "--password", REDACTED, "188"],
    )
    check(
        "密码等号写法被遮罩",
        redact_argv(["login", "phone", "--password=hunter2"]),
        ["login", "phone", f"--password={REDACTED}"],
    )
    check(
        "多个敏感参数都遮罩",
        redact_argv(["-p", "a", "--headers", '{"Cookie":"x"}',
                     "--xeapi-session-key", "k", "--dynamic-key", "d", "ok"]),
        ["-p", REDACTED, "--headers", REDACTED,
         "--xeapi-session-key", REDACTED, "--dynamic-key", REDACTED, "ok"],
    )
    check(
        "普通参数不受影响",
        redact_argv(["download", "--output", "/music", "123"]),
        ["download", "--output", "/music", "123"],
    )

    # 二进制定位：只接受带路径的显式值，裸名字只认 ncmctl
    with tempfile.TemporaryDirectory(prefix="ncmgui-bin-") as tmp:
        fake = Path(tmp) / "evil"
        fake.write_bytes(b"#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        real = Path(tmp) / "my-ncmctl"
        real.write_bytes(b"#!/bin/sh\nexit 0\n")
        real.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        try:
            check("裸名字不解析任意命令", find_binary("evil"), None)
            check("显式完整路径可用", find_binary(str(real)), str(real))
        finally:
            os.environ["PATH"] = old_path
        check("填错路径不悄悄回退", find_binary(str(Path(tmp) / "missing")), None)

    # 目标解析
    parsed = parse_targets("2161154646\n# 注释\n2161154646  https://music.163.com/song?id=1,\n")
    if parsed != ["2161154646", "https://music.163.com/song?id=1"]:
        failures.append(f"parse_targets 期望去重并跳过注释，实际 {parsed}")

    # ANSI 清洗
    if strip_ansi("\x1b[31m红色\x1b[0m 正常") != "红色 正常":
        failures.append("strip_ansi 未能清除 CSI 序列")

    # 参数校验
    for name, fn in (
        ("download 空目标", lambda: build_download([])),
        ("proxy CA 不配对", lambda: build_proxy(ProxySpec(ca_cert="/a.crt"))),
        ("login phone 空号", lambda: build_login_phone("")),
    ):
        try:
            fn()
        except ValueError:
            pass
        else:
            failures.append(f"{name} 应当抛出 ValueError")

    if failures:
        print(f"自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("ncmctl 命令构建自检全部通过")
    print(f"探测到的可执行文件：{find_binary() or '未找到'}")
    print(f"登录状态：{'已登录' if is_logged_in() else '未登录'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
