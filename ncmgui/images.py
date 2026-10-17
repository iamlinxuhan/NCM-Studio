"""专辑封面：把网易云的图片地址放大、下载，并在内存里缓存。

接口给的 ``al.picUrl`` 默认只有 130×130 左右，塞进黑胶唱片正中间会糊成一团。
网易云的图片服务认 ``param=<宽>y<高>`` 这个查询参数，改一下就能拿到大图，
不必自己缩放（见 :func:`upgrade_url`）。

**只缓存到内存，不落盘。** 封面是一次性的展示数据，为它维护缓存目录、过期和
清理规则不值得；重启后重新下载一张 600×600 的图也就几十毫秒。

本模块的纯函数部分可以直接自检（不需要 QApplication）::

    .venv/bin/python -m ncmgui.images
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication

from . import VERSION, theme
from .workers import BackgroundTask

#: 默认取多大的图。600 足够铺满黑胶中心，也不会让内存缓存变得太重。
DEFAULT_SIZE = 600

#: 内存里最多留多少张。一张 600×600 的 ARGB 图约 1.4 MB，24 张约 34 MB。
DEFAULT_CACHE = 24


def upgrade_url(url: str, size: int = DEFAULT_SIZE) -> str:
    """把缩略图地址改成 ``size×size`` 的大图。

    地址可能已经带查询串（``?param=130y130`` 或别的签名参数），所以不能无脑
    拼 ``?param=``。已经带 ``param`` 的就地替换 —— 重复的参数会让服务端取第一个。
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    param = f"{int(size)}y{int(size)}"
    replaced = False
    for position, (key, _value) in enumerate(query):
        if key == "param":
            query[position] = (key, param)
            replaced = True
            break
    if not replaced:
        query.append(("param", param))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def _opener(proxy: str = ""):
    """按配置构造 opener。空串走系统默认，``direct`` 强制直连。"""
    proxy = (proxy or "").strip()
    if proxy == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def fetch(url: str, proxy: str = "", timeout: float = 10.0) -> bytes:
    """下载一张图片。失败时抛 ``RuntimeError``，消息可以直接给人看。"""
    if not url:
        raise RuntimeError("没有封面地址")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"ncmctl-gui/{VERSION}",
            "Referer": "https://music.163.com/",
        },
    )
    try:
        with _opener(proxy).open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"封面下载失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"封面下载失败：{reason}") from exc


def _read_local_artwork(path: str) -> bytes | None:
    """本地音频的封面。惰性导入，避免 images ↔ covers 的循环依赖。"""
    from .covers import read_artwork

    try:
        return read_artwork(path)
    except Exception:  # noqa: BLE001 - 封面读不出来不该影响播放
        return None


def placeholder(size: int = 160) -> QPixmap:
    """没有封面时用的渐变方块，中间一个音符。

    需要 :class:`QApplication`（``QPixmap`` 依赖图形平台插件）；只在界面线程里调。
    """
    size = max(16, int(size))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QRadialGradient(size * 0.5, size * 0.36, size * 0.8)
        gradient.setColorAt(0.0, QColor(theme.PANEL_HI))
        gradient.setColorAt(1.0, QColor(theme.BG_DEEP))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(theme.BORDER_HI), max(1, size // 90)))
        painter.drawRoundedRect(1, 1, size - 2, size - 2, size * 0.08, size * 0.08)

        painter.setPen(QPen(QColor(theme.TEXT_MUTE)))
        font = QFont()
        font.setPixelSize(max(12, int(size * 0.42)))
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
    finally:
        painter.end()
    return pixmap


def rounded(pixmap: QPixmap, size: int, radius: float | None = None) -> QPixmap:
    """把封面缩放并裁成圆角方块，供底部条、队列等小尺寸位置使用。"""
    size = max(8, int(size))
    radius = size * 0.16 if radius is None else float(radius)
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(size), float(size), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(
            (size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled
        )
    finally:
        painter.end()
    return canvas


class CoverStore(QObject):
    """按歌曲 ID 缓存封面，后台下载，晚到的旧结果直接丢掉。

    ``coverReady(song_id, pixmap)`` 在主线程发出。调用方收到后先比一下
    ``song_id`` 是不是当前这首 —— 切歌很快时，前面那首的封面可能后到。
    """

    coverReady = pyqtSignal(int, QPixmap)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        size: int = DEFAULT_SIZE,
        capacity: int = DEFAULT_CACHE,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        super().__init__(parent)
        self._size = int(size)
        self._capacity = max(1, int(capacity))
        self._fetcher = fetcher or fetch
        self._cache: OrderedDict[int, QPixmap] = OrderedDict()
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._tasks: set[BackgroundTask] = set()

    # ------------------------------------------------------------ 查询

    def get(self, song_id: int | None) -> QPixmap | None:
        """取已缓存的封面；没有返回 ``None``。命中会刷新 LRU 位置。"""
        if song_id is None:
            return None
        pixmap = self._cache.get(int(song_id))
        if pixmap is not None:
            self._cache.move_to_end(int(song_id))
        return pixmap

    def put(self, song_id: int, pixmap: QPixmap) -> None:
        """直接塞一张（截图演示、或调用方自己拿到了图）。"""
        if pixmap.isNull():
            return
        self._cache[int(song_id)] = pixmap
        self._cache.move_to_end(int(song_id))
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        self._emit(song_id, pixmap)

    def clear(self) -> None:
        self._cache.clear()
        with self._lock:
            self._pending.clear()

    # ------------------------------------------------------------ 请求

    def request(self, song) -> QPixmap | None:
        """确保这首歌的封面正在下载；已缓存时立即返回。

        返回缓存的图或 ``None``；下载完成走 :attr:`coverReady`。
        没有 ``pic_url`` 的（本地文件、接口没给）不发请求。
        """
        song_id = int(getattr(song, "id", 0))
        cached = self.get(song_id)
        if cached is not None:
            return cached

        url = upgrade_url(str(getattr(song, "pic_url", "") or ""), self._size)
        local = str(getattr(song, "path", "") or "")
        if not url and not local:
            return None
        with self._lock:
            if song_id in self._pending:
                return None
            self._pending.add(song_id)

        if local:
            # 本地文件不联网：同名图片或内嵌封面，读不到就不显示
            task = BackgroundTask(lambda: _read_local_artwork(local), parent=self)
        else:
            task = BackgroundTask(lambda: self._fetcher(url), parent=self)
        self._tasks.add(task)

        def done(data: object) -> None:
            self._finish(song_id)
            self._tasks.discard(task)
            image = QPixmap()
            if isinstance(data, (bytes, bytearray)) and image.loadFromData(bytes(data)):
                self.put(song_id, image)

        def failed(_message: str) -> None:
            self._finish(song_id)
            self._tasks.discard(task)

        task.done.connect(done)
        task.failed.connect(failed)
        task.start()
        return None

    def _finish(self, song_id: int) -> None:
        with self._lock:
            self._pending.discard(song_id)

    def _emit(self, song_id: int, pixmap: QPixmap) -> None:
        try:
            self.coverReady.emit(int(song_id), pixmap)
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}：期望 {want!r}，实际 {got!r}")

    check(
        "没有查询串时补 ?param",
        upgrade_url("https://p1.music.126.net/a.jpg"),
        "https://p1.music.126.net/a.jpg?param=600y600",
    )
    check(
        "已有查询串时用 &",
        upgrade_url("https://p1.music.126.net/a.jpg?x=1"),
        "https://p1.music.126.net/a.jpg?x=1&param=600y600",
    )
    check(
        "已有的 param 就地替换（不重复）",
        upgrade_url("https://p1.music.126.net/a.jpg?param=130y130"),
        "https://p1.music.126.net/a.jpg?param=600y600",
    )
    check(
        "自定义尺寸",
        upgrade_url("https://a/b.jpg", 200),
        "https://a/b.jpg?param=200y200",
    )
    check("空地址返回空串", upgrade_url(""), "")
    check("空白地址返回空串", upgrade_url("   "), "")

    # 缓存部分需要 QPixmap；无显示环境下用 offscreen，没有就跳过
    app = QApplication.instance()
    created = False
    if app is None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            app = QApplication([])
            created = True
        except Exception:  # noqa: BLE001 - 起不来就只少测这一块
            app = None
    if app is not None:
        store = CoverStore(capacity=2)
        one = placeholder(32)
        check("占位图尺寸", (one.width(), one.height()), (32, 32))
        store.put(1, one)
        check("put 后能取到", store.get(1) is one, True)
        store.put(2, one)
        store.put(3, one)
        check("超出容量时淘汰最久未用", store.get(1), None)
        check("最近放入的还在", store.get(3) is one, True)
        store.get(2)
        store.put(4, one)
        check("命中会刷新 LRU 位置", store.get(3), None)
        check("刚命中的那张还在", store.get(2) is one, True)
        if created:
            app.quit()

    if failures:
        print(f"封面自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("封面自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
