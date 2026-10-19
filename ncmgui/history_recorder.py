"""把播放控制器的事件翻译成曲库里的播放记录。

规则与常见播放器一致，也按 SPlayer 的口径：

* 真正播放满 5 秒才记一条，反复点开又立刻切走不会污染统计；
* 自然播完的歌即使不足 5 秒也算一次（短音频/片头）；
* 切歌、暂停后退出、程序退出时补写实际听了多久；
* 单曲循环每播一轮算新一轮。

记录只做「先插入、后补全」：满 5 秒插入一行，结束时更新 ``listened_ms``
和 ``completed``。这样即使程序崩溃，也已经留下这次播放。

自检::

    .venv/bin/python -m ncmgui.history_recorder
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from PyQt6.QtCore import QObject, QTimer

from .library.db import LibraryDB
from .library.history import record_play, update_play
from .playback import STATE_PLAYING, PlaybackController
from .resolver import Song

#: 至少听满这么久才记一条
DEFAULT_THRESHOLD_MS = 5000

_TICK_MS = 1000


class HistoryRecorder(QObject):
    """挂在 :class:`PlaybackController` 上的记录器，每个窗口一个。"""

    def __init__(
        self,
        db: LibraryDB,
        playback: PlaybackController,
        *,
        threshold_ms: int = DEFAULT_THRESHOLD_MS,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._playback = playback
        self._threshold_ms = max(0, int(threshold_ms))
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time

        self._song: Song | None = None
        self._started_at = 0
        self._listened_ms = 0
        self._active_since: float | None = None
        self._event_id: int | None = None
        self._completed = False
        self._closed = False

        playback.trackStarted.connect(self._on_started)
        playback.trackEnded.connect(self._on_ended)
        playback.stateChanged.connect(self._on_state)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ------------------------------------------------------------ 事件

    def _on_started(self, song: object) -> None:
        if self._closed or not isinstance(song, Song):
            return
        # 手动切歌：上一首按「未播完」收尾
        self._finalize(completed=False)
        self._song = song
        self._started_at = int(self._wall_clock())
        self._listened_ms = 0
        self._active_since = None
        self._event_id = None
        self._completed = False
        if self._playback.state == STATE_PLAYING and not self._playback.paused:
            self._active_since = self._clock()

    def _on_ended(self, song: object) -> None:
        if self._closed:
            return
        if self._song is not None and isinstance(song, Song) and song.id != self._song.id:
            # 迟到的旧曲目通知，别把新歌的记录提前收掉
            return
        self._finalize(completed=True)

    def _on_state(self, state: str) -> None:
        if self._closed or self._song is None:
            return
        playing = state == STATE_PLAYING and not self._playback.paused
        self._sync_active(playing)
        if state == "idle":
            # 用户按了停止，或顺序播放在末尾自然停下
            self._finalize(completed=False)

    def _on_tick(self) -> None:
        if self._closed or self._song is None:
            return
        self._sync_active(
            self._playback.state == STATE_PLAYING and not self._playback.paused
        )
        self._maybe_insert()

    # ------------------------------------------------------------ 计时

    def _sync_active(self, playing: bool) -> None:
        now = self._clock()
        if playing and self._active_since is None:
            self._active_since = now
        elif not playing and self._active_since is not None:
            self._listened_ms += max(0, int((now - self._active_since) * 1000))
            self._active_since = None

    def _total_listened(self) -> int:
        total = self._listened_ms
        if self._active_since is not None:
            total += max(0, int((self._clock() - self._active_since) * 1000))
        return total

    def _maybe_insert(self) -> None:
        if self._event_id is not None or self._song is None:
            return
        listened = self._total_listened()
        if listened < self._threshold_ms:
            return
        self._event_id = self._insert(listened)
        self._listened_ms = listened
        # 从插入时刻重新计增量，避免结束后把同一段时间重复累加
        if self._active_since is not None:
            self._active_since = self._clock()

    # ------------------------------------------------------------ 落库

    def _insert(self, listened_ms: int) -> int | None:
        assert self._song is not None
        try:
            return record_play(
                self._db,
                self._song,
                listened_ms,
                completed=self._completed,
                started_at=self._started_at,
            )
        except sqlite3.Error:
            # 记录失败只丢统计，不能让播放路径报错
            return None

    def _finalize(self, *, completed: bool) -> None:
        if self._song is None:
            return
        self._sync_active(False)
        total = self._total_listened()
        self._completed = self._completed or completed

        if self._event_id is None:
            # 自然播完的短音频也算；没播完又不足阈值就不记
            if completed or total >= self._threshold_ms:
                self._event_id = self._insert(total)
        if self._event_id is not None:
            try:
                update_play(
                    self._db, self._event_id, total, completed=self._completed
                )
            except sqlite3.Error:
                pass

        self._song = None
        self._started_at = 0
        self._listened_ms = 0
        self._active_since = None
        self._event_id = None
        self._completed = False

    def flush(self) -> None:
        """把当前播放补写成一条完整记录（暂停/退出前调用）。"""
        if self._closed:
            return
        self._finalize(completed=False)

    def shutdown(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._timer.stop()


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    from pathlib import Path

    from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal

    from . import ensure_utf8_stdout
    from .library.db import LibraryDB

    ensure_utf8_stdout()
    app = QCoreApplication.instance() or QCoreApplication([])
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    class FakePlayback(QObject):
        trackStarted = pyqtSignal(object)
        trackEnded = pyqtSignal(object)
        stateChanged = pyqtSignal(str)

        def __init__(self) -> None:
            super().__init__()
            self.state = "idle"
            self.paused = False

        def set_state(self, value: str) -> None:
            self.state = value
            self.stateChanged.emit(value)

        def start(self, song: Song) -> None:
            self.trackStarted.emit(song)
            self.set_state("playing")

        def end(self, song: Song) -> None:
            # 控制器先发 trackEnded，再切到 idle
            self.trackEnded.emit(song)
            self.set_state("idle")

    class FakeClock:
        def __init__(self) -> None:
            self.now = 10_000.0

        def monotonic(self) -> float:
            return self.now

        def time(self) -> float:
            return 1_700_000_000.0 + self.now - 10_000.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    with tempfile.TemporaryDirectory() as root:
        db = LibraryDB(Path(root) / "library.db")
        clock = FakeClock()
        fake = FakePlayback()
        recorder = HistoryRecorder(db, fake, clock=clock.monotonic,
                                   wall_clock=clock.time)

        one = Song(1, "一", ("甲",), duration=200_000)
        two = Song(2, "二", ("乙",), duration=200_000)
        three = Song(3, "三", ("丙",), duration=200_000)

        # 不足 5 秒就切走：不记录
        fake.start(one)
        clock.advance(3)
        recorder._on_tick()
        fake.start(two)
        rows = db.query("SELECT * FROM play_events")
        check("短播放不记录", len(rows), 0)

        # 满 5 秒插入，结束时补写时长
        clock.advance(6)
        recorder._on_tick()
        rows = db.query("SELECT * FROM play_events")
        check("满阈值后记录", len(rows), 1)
        first_id = int(rows[0]["id"])
        check("记录歌名", rows[0]["title"], "二")

        clock.advance(4)
        fake.end(two)
        row = db.query_one("SELECT * FROM play_events WHERE id = ?", (first_id,))
        check("结束补写时长", int(row["listened_ms"]), 10_000)
        check("自然播完标记", int(row["completed"]), 1)

        # 单曲循环：第二轮重新开一条
        fake.start(two)
        clock.advance(6)
        fake.set_state("paused")
        fake.set_state("playing")
        recorder._on_tick()
        check("循环第二轮", db.query_one(
            "SELECT COUNT(*) AS n FROM play_events")["n"], 2)

        # 手动切歌收尾，完整播放标记为 0
        clock.advance(2)
        fake.start(three)
        row = db.query_one(
            "SELECT * FROM play_events ORDER BY id DESC LIMIT 1")
        check("切歌补写", int(row["listened_ms"]), 8_000)
        check("切歌未完成", int(row["completed"]), 0)

        # 未满阈值退出：不新增
        clock.advance(1)
        recorder.shutdown()
        check("退出不新增", db.query_one(
            "SELECT COUNT(*) AS n FROM play_events")["n"], 2)
        db.close()

    del app
    if failures:
        print(f"播放记录器自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("播放记录器自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
