"""听歌统计：90 天热力图、时段分布、排行、音质构成。

只读取 ``play_events``，不碰曲库表，所以历史清空后统计自然归零。
按本地时区切分日期和小时；窗口外的记录不参与统计。

自检::

    .venv/bin/python -m ncmgui.library.stats
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Iterable

from .db import LibraryDB
from .queries import split_artists

DEFAULT_DAYS = 90


@dataclass(frozen=True)
class DayEntry:
    day: date
    plays: int
    listened_ms: int


@dataclass(frozen=True)
class RankEntry:
    title: str
    subtitle: str
    plays: int
    listened_ms: int

    @property
    def duration_text(self) -> str:
        return format_duration(self.listened_ms)


@dataclass(frozen=True)
class SliceEntry:
    label: str
    count: int


@dataclass(frozen=True)
class StatsData:
    days: tuple[DayEntry, ...] = ()
    hours: tuple[int, ...] = (0,) * 24
    songs: tuple[RankEntry, ...] = ()
    albums: tuple[RankEntry, ...] = ()
    artists: tuple[RankEntry, ...] = ()
    qualities: tuple[SliceEntry, ...] = ()
    total_plays: int = 0
    total_listened_ms: int = 0
    active_days: int = 0
    current_streak: int = 0
    longest_streak: int = 0

    @property
    def total_listened_text(self) -> str:
        return format_duration(self.total_listened_ms)

    @property
    def peak_hour(self) -> int:
        """播放次数最多的小时；没有记录时返回 0。"""
        best = max(self.hours) if self.hours else 0
        return self.hours.index(best) if best else 0

    @property
    def max_day_plays(self) -> int:
        return max((item.plays for item in self.days), default=0)


def format_duration(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


class _Counter:
    """``key -> (plays, listened_ms)``，省去到处写 defaultdict。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], list[int]] = {}

    def add(self, title: str, subtitle: str, listened_ms: int) -> None:
        bucket = self._data.setdefault((title, subtitle), [0, 0])
        bucket[0] += 1
        bucket[1] += max(0, int(listened_ms))

    def ranked(self, limit: int = 10) -> tuple[RankEntry, ...]:
        ordered = sorted(
            self._data.items(),
            key=lambda item: (-item[1][0], -item[1][1], item[0][0].casefold()),
        )
        return tuple(
            RankEntry(title=title, subtitle=subtitle,
                      plays=plays, listened_ms=listened_ms)
            for (title, subtitle), (plays, listened_ms) in ordered[:limit]
        )

    def __len__(self) -> int:
        return len(self._data)


def _local_day(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp).date()


def collect(
    db: LibraryDB,
    days: int = DEFAULT_DAYS,
    *,
    now: float | None = None,
    rank_limit: int = 10,
) -> StatsData:
    """统计最近 ``days`` 天。``now`` 仅用于自检，正常调用不用传。"""
    days = max(1, int(days))
    now_ts = time.time() if now is None else float(now)
    today = _local_day(now_ts)
    start_day = today - timedelta(days=days - 1)
    start_ts = int(datetime.combine(start_day, dtime.min).timestamp())

    rows = db.query(
        "SELECT title, artists, album, quality, started_at, listened_ms"
        " FROM play_events WHERE started_at >= ? ORDER BY started_at",
        (start_ts,),
    )

    day_plays: dict[date, int] = {}
    day_ms: dict[date, int] = {}
    hours = [0] * 24
    songs = _Counter()
    albums = _Counter()
    artists = _Counter()
    qualities: dict[str, int] = {}
    total_ms = 0
    total_plays = 0

    for row in rows:
        started_at = int(row["started_at"] or 0)
        listened_ms = max(0, int(row["listened_ms"] or 0))
        day = _local_day(started_at)
        if day < start_day or day > today:
            continue
        name = str(row["title"] or "").strip() or "未知歌曲"
        artist_names = split_artists(str(row["artists"] or ""))
        artist_text = ", ".join(artist_names) or "未知歌手"
        album = str(row["album"] or "").strip() or "未知专辑"
        quality = str(row["quality"] or "").strip() or "未知音质"

        total_plays += 1
        total_ms += listened_ms
        day_plays[day] = day_plays.get(day, 0) + 1
        day_ms[day] = day_ms.get(day, 0) + listened_ms
        hours[datetime.fromtimestamp(started_at).hour] += 1
        songs.add(name, artist_text, listened_ms)
        albums.add(album, artist_text, listened_ms)
        for artist in artist_names or ("未知歌手",):
            artists.add(artist, "", listened_ms)
        qualities[quality] = qualities.get(quality, 0) + 1

    day_entries = tuple(
        DayEntry(
            day=start_day + timedelta(days=offset),
            plays=day_plays.get(start_day + timedelta(days=offset), 0),
            listened_ms=day_ms.get(start_day + timedelta(days=offset), 0),
        )
        for offset in range(days)
    )

    active = {item.day for item in day_entries if item.plays}
    longest = 0
    run = 0
    cursor = start_day
    while cursor <= today:
        if cursor in active:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        cursor += timedelta(days=1)

    # 当前连续天数：今天还没听不算断，从昨天继续往前数。
    current = 0
    cursor = today if today in active else today - timedelta(days=1)
    while cursor >= start_day and cursor in active:
        current += 1
        cursor -= timedelta(days=1)

    return StatsData(
        days=day_entries,
        hours=tuple(hours),
        songs=songs.ranked(rank_limit),
        albums=albums.ranked(rank_limit),
        artists=artists.ranked(rank_limit),
        qualities=tuple(
            SliceEntry(label=label, count=count)
            for label, count in sorted(
                qualities.items(), key=lambda item: (-item[1], item[0].casefold())
            )
        ),
        total_plays=total_plays,
        total_listened_ms=total_ms,
        active_days=len(active),
        current_streak=current,
        longest_streak=longest,
    )


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    from pathlib import Path

    from .. import ensure_utf8_stdout
    from .db import LibraryDB

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    def at(day: date, hour: int = 12) -> int:
        return int(datetime.combine(day, dtime(hour, 0)).timestamp())

    def insert(db: LibraryDB, title: str, artists: str, album: str,
               quality: str, started_at: int, listened_ms: int) -> None:
        db.execute(
            "INSERT INTO play_events(track_id, title, artists, album, path,"
            " source, quality, started_at, listened_ms, duration_ms, completed)"
            " VALUES(NULL, ?, ?, ?, '', 'local', ?, ?, ?, ?, 0)",
            (title, artists, album, quality, started_at, listened_ms, listened_ms),
        )

    # 2026-10-19 12:00 作为“现在”，窗口内从 2026-07-22 起
    now = at(date(2026, 10, 19), 12)
    today = date(2026, 10, 19)

    with tempfile.TemporaryDirectory() as root:
        db = LibraryDB(Path(root) / "library.db")
        insert(db, "晴天", "周杰伦", "叶惠美", "无损",
               at(today, 9), 200_000)
        insert(db, "晴天", "周杰伦", "叶惠美", "无损",
               at(today, 21), 180_000)
        insert(db, "夜曲", "周杰伦", "十一月的萧邦", "320 kbps",
               at(today - timedelta(days=1), 9), 300_000)
        insert(db, "孤勇者", "陈奕迅", "孤勇者", "320 kbps",
               at(today - timedelta(days=2), 20), 240_000)
        # 窗口外
        insert(db, "古老", "旧人", "旧专辑", "未知",
               at(today - timedelta(days=200), 10), 100_000)

        data = collect(db, 90, now=now)
        check("窗口天数", len(data.days), 90)
        check("首日", data.days[0].day, today - timedelta(days=89))
        check("末日", data.days[-1].day, today)
        check("总播放", data.total_plays, 4)
        check("总时长", data.total_listened_ms, 920_000)
        check("今日次数", data.days[-1].plays, 2)
        check("活跃天数", data.active_days, 3)
        check("连续天数", data.current_streak, 3)
        check("最长连续", data.longest_streak, 3)
        check("9 点次数", data.hours[9], 2)
        check("21 点次数", data.hours[21], 1)
        check("高峰时段", data.peak_hour, 9)
        check("单日峰值", data.max_day_plays, 2)

        check("歌曲榜首", data.songs[0].title, "晴天")
        check("歌曲次数", data.songs[0].plays, 2)
        check("歌曲时长", data.songs[0].listened_ms, 380_000)
        check("专辑榜首", data.albums[0].title, "叶惠美")
        check("歌手榜首", data.artists[0].title, "周杰伦")
        check("歌手次数", data.artists[0].plays, 3)
        check("音质种类", len(data.qualities), 2)
        check("音质排序", data.qualities[0].label, "320 kbps")
        check("音质计数", data.qualities[0].count, 2)
        check("时长文本", data.songs[0].duration_text, "6 分 20 秒")

        empty = collect(db, 90, now=at(date(2027, 6, 1), 12))
        check("空窗口播放数", empty.total_plays, 0)
        check("空窗口天数", len(empty.days), 90)
        check("空窗口连续", empty.current_streak, 0)
        db.close()

    if failures:
        print(f"听歌统计自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("听歌统计自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
