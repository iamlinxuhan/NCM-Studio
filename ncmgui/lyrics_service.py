"""当前歌曲歌词的共享服务。

播放页和桌面歌词窗都需要同一份歌词。以前两者各自请求、各自解析，切歌时会
重复读文件、重复打接口；这里集中缓存并按歌曲标识复用。

偏移量单位是毫秒。正值表示歌词**晚出现**，即用
``播放位置 - offset/1000`` 去匹配歌词时间轴。

本地歌曲的偏移按文件记在曲库数据库（``library.db`` 的 ``lyric_offsets``）：
同一首歌下次播放自动恢复，换歌单或重新扫描都不丢。在线歌曲没有稳定的本地
文件，仍用配置里的全局偏移。
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import lyrics, ncmctl
from .resolver import Resolver, Song
from .workers import BackgroundTask

#: 缓存多少首歌的解析结果。歌词本身不大，主要省掉网络往返；
#: 一轮听歌来回切歌时，100 首足够覆盖，不用反复请求同一首
CACHE_SIZE = 100


def song_key(song: Song | None) -> tuple:
    if song is None:
        return ()
    return (
        int(getattr(song, "id", 0) or 0),
        str(getattr(song, "path", "") or ""),
        str(getattr(song, "source", "") or ""),
    )


class LyricsService(QObject):
    """歌词加载与缓存。

    ``lyricsChanged(song, parsed, placeholder)``：``parsed`` 为
    :class:`ncmgui.lyrics.ParsedLyrics` 或 ``None``；没有歌词时 ``placeholder``
    是界面该显示的一句话。
    """

    lyricsChanged = pyqtSignal(object, object, str)
    offsetChanged = pyqtSignal(int)

    def __init__(self, window, parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.cfg = window.cfg
        self._cache: OrderedDict[tuple, lyrics.ParsedLyrics] = OrderedDict()
        self._song: Song | None = None
        self._key: tuple = ()
        self._parsed: lyrics.ParsedLyrics | None = None
        self._placeholder = "点一首歌，这里会显示歌词。"
        self._generation = 0
        self._task: BackgroundTask | None = None
        #: 当前歌曲的本地路径；为空表示用全局偏移（在线歌曲 / 还没选歌）
        self._offset_path: str | None = None
        #: 最近一次按文件保存是否成功；写库失败时提示不能谎称「已记住」
        self._offset_saved = True

        try:
            self._offset_ms = int(self.cfg.get("player.lyric_offset", 0) or 0)
        except (TypeError, ValueError):
            self._offset_ms = 0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._persist_offset)

    # ------------------------------------------------------------ 只读状态

    @property
    def song(self) -> Song | None:
        return self._song

    @property
    def lines(self) -> list[lyrics.TimedLine]:
        return list(self._parsed.lines) if self._parsed is not None else []

    @property
    def parsed(self) -> lyrics.ParsedLyrics | None:
        return self._parsed

    @property
    def placeholder(self) -> str:
        return self._placeholder

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    @property
    def offset_seconds(self) -> float:
        return self._offset_ms / 1000.0

    @property
    def offset_is_per_song(self) -> bool:
        """当前偏移是否按本地文件记住（在线歌曲用全局配置）。"""
        return self._offset_path is not None

    @property
    def offset_saved(self) -> bool:
        """最近一次对本地歌曲的偏移调整是否成功写进曲库。"""
        return self._offset_saved

    def display_position(self, position: float | None) -> float:
        """把 mpv 位置换算成歌词时间轴上的位置。"""
        try:
            value = float(position or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value - self.offset_seconds

    # ------------------------------------------------------------ 请求

    def request(self, song: Song | None) -> None:
        """切换到某首歌的歌词。同一首歌重复调用不会重新加载。"""
        key = song_key(song)
        if key == self._key:
            if self._parsed is not None:
                # 后加入的订阅者（比如刚打开的桌面歌词窗）要能立刻拿到结果
                self.lyricsChanged.emit(self._song, self._parsed, "")
            return
        self._generation += 1
        generation = self._generation
        self._song = song
        self._key = key
        self._parsed = None
        self._task = None
        self._apply_offset_scope(song)

        if song is None:
            self._emit(None, "点一首歌，这里会显示歌词。")
            return

        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._emit(cached, "")
            return

        self._emit(None, "正在加载歌词…")

        local = str(getattr(song, "path", "") or "")
        if local:
            # 本地文件在 UI 线程读即可：几十 KB，不值得开线程
            try:
                _format, text = lyrics.read_local_lyrics(Path(local))
            except (OSError, lyrics.LyricsError):
                self._emit(None, "无法读取本地歌词")
                return
            self._accept(text, generation)
            return

        if getattr(self.window, "defer_page_loads", False):
            self._emit(None, "播放时显示歌词")
            return
        binary = getattr(self.window, "binary", "")
        if not binary:
            self._emit(None, "找不到 ncmctl，无法获取歌词。")
            return

        opts = self.window.global_opts()
        song_id = int(song.id)

        def work():
            resolver = Resolver(binary, opts=opts, timeout="15s")
            return resolver.lyric(song_id)

        task = BackgroundTask(work, parent=self)
        self._task = task
        task.done.connect(lambda payload: self._accept(payload, generation))
        task.failed.connect(lambda _message: self._failed(generation))
        task.start()

    def invalidate(self, song: Song | None = None) -> None:
        """丢掉缓存（默认只丢当前歌曲），下次请求会重新取。"""
        if song is None:
            self._cache.pop(self._key, None)
        else:
            self._cache.pop(song_key(song), None)
        if song is None or song_key(song) == self._key:
            key = self._key
            self._key = ()
            self.request(song or self._song)

    # ------------------------------------------------------------ 偏移

    @staticmethod
    def _local_path(song: Song | None) -> str:
        return str(getattr(song, "path", "") or "")

    @staticmethod
    def _clamp(value) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(-30000, min(30000, number))

    def _configured_offset(self) -> int:
        return self._clamp(self.cfg.get("player.lyric_offset", 0) or 0)

    def _apply_offset_scope(self, song: Song | None) -> None:
        """切歌时换成这首歌该用的偏移。

        本地文件按路径记，换歌单、重扫都不丢；在线歌曲没有稳定的本地文件，
        继续用配置里的全局值。
        """
        path = self._local_path(song)
        self._offset_path = path or None
        self._offset_saved = True
        self._set_offset_value(
            self._read_song_offset(path) if path else self._configured_offset()
        )

    def _read_song_offset(self, path: str) -> int:
        library = getattr(self.window, "library", None)
        if library is None:
            return 0
        try:
            return self._clamp(library.lyric_offset(path))
        except (sqlite3.Error, OSError, ValueError):
            return 0

    def _save_song_offset(self, path: str, value: int) -> bool:
        library = getattr(self.window, "library", None)
        if library is None:
            return False
        try:
            library.set_lyric_offset(path, value)
        except (sqlite3.Error, OSError, ValueError):
            return False
        return True

    def _set_offset_value(self, value: int) -> None:
        value = self._clamp(value)
        if value == self._offset_ms:
            return
        self._offset_ms = value
        self.offsetChanged.emit(value)

    def set_offset(self, milliseconds: int) -> None:
        value = self._clamp(milliseconds)
        if value == self._offset_ms:
            return
        self._offset_ms = value
        if self._offset_path:
            # 本地歌曲：立刻落库。一次点击就两三个字节，不值得再排一个定时器。
            self._offset_saved = self._save_song_offset(self._offset_path, value)
        else:
            self.cfg.set("player.lyric_offset", value)
            self._save_timer.start()
        self.offsetChanged.emit(value)

    def nudge_offset(self, delta_ms: int) -> None:
        self.set_offset(self._offset_ms + int(delta_ms))

    def _persist_offset(self) -> None:
        try:
            self.cfg.save()
        except (OSError, ValueError):
            pass

    def shutdown(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist_offset()

    # ------------------------------------------------------------ 内部

    def _accept(self, payload, generation: int) -> None:
        if generation != self._generation:
            return
        self._task = None
        try:
            parsed = lyrics.parse_any(payload)
        except lyrics.LyricsError:
            self._emit(None, "歌词解析失败")
            return
        if not parsed.lines:
            self._emit(None, "暂无歌词")
            return
        self._remember(parsed)
        self._emit(parsed, "")

    def _failed(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._task = None
        self._emit(None, "歌词获取失败")

    def _remember(self, parsed: lyrics.ParsedLyrics) -> None:
        key = self._key
        if not key:
            return
        self._cache[key] = parsed
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _emit(self, parsed: lyrics.ParsedLyrics | None, placeholder: str) -> None:
        self._parsed = parsed
        self._placeholder = placeholder
        self.lyricsChanged.emit(self._song, parsed, placeholder)


def _selftest() -> int:
    """不依赖 Qt 事件循环的解析/偏移冒烟。"""
    from . import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    song = Song(1, "歌", ("手",), path="/tmp/不存在.lrc")
    check("歌曲键包含路径", song_key(song)[1], "/tmp/不存在.lrc")
    check("空歌曲键", song_key(None), ())
    check("本地读取失败", lyrics.read_local_lyrics(Path("/tmp/不存在.lrc")), ("", ""))

    parsed = lyrics.parse_any("[00:01.00]第一句\n[00:03.00]第二句\n")
    check("解析行数", len(parsed.lines), 2)
    check("第一句时间", round(parsed.lines[0].start, 2), 1.0)

    if failures:
        print(f"歌词服务自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("歌词服务自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
