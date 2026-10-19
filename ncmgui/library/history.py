"""播放历史：每次实际听满阈值的播放记一条快照。

历史存歌名/歌手/专辑/路径的副本，而不是只存 ``track_id``：在线歌曲不在曲库里，
本地歌曲也可能被删掉。这样即使源文件消失，统计和「最近播放」仍然完整。

自检::

    .venv/bin/python -m ncmgui.library.history
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from ..resolver import Song
from .db import LibraryDB
from .queries import split_artists


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    #: 播放时的歌曲 ID：本地歌曲为负数，网易云歌曲为正数；缺失时为 None
    track_id: int | None
    title: str
    artists: tuple[str, ...]
    album: str
    path: str
    source: str
    quality: str
    started_at: int
    listened_ms: int
    duration_ms: int
    completed: bool
    #: 流媒体歌曲的 ``服务器ID:曲目ID``；网易云/本地为空
    stream_id: str = ""

    @property
    def display(self) -> str:
        joined = ", ".join(self.artists)
        return f"{joined} - {self.title}" if joined else self.title

    @property
    def time_text(self) -> str:
        return datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M")

    @property
    def short_time_text(self) -> str:
        """列表里用短格式；完整时间放在 tooltip 里。"""
        return datetime.fromtimestamp(self.started_at).strftime("%m-%d %H:%M")

    def to_song(self, fallback_id: int | None = None) -> Song | None:
        """转成可播放的 Song；在线歌曲没有直链，需要按 ID 重新解析。

        本地歌曲的文件可能已经被删掉，所以路径不存在时返回 ``None``；
        在线歌曲只要有 ID 就能重新取链。
        """
        song_id = self.track_id if self.track_id else fallback_id
        if self.path and Path(self.path).is_file():
            if not song_id:
                song_id = -abs(int(fallback_id or self.id or 1))
            return Song(
                id=int(song_id),
                name=self.title,
                artists=self.artists,
                album=self.album,
                duration=self.duration_ms,
                path=self.path,
                quality=self.quality,
                source="local",
            )
        if self.source == "local" or not song_id:
            return None
        return Song(
            id=int(song_id),
            name=self.title,
            artists=self.artists,
            album=self.album,
            duration=self.duration_ms,
            path=self.path,
            quality=self.quality,
            source=self.source,
            stream_id=self.stream_id,
        )


def record_play(
    db: LibraryDB,
    song: Song,
    listened_ms: int,
    *,
    completed: bool = False,
    started_at: int | None = None,
) -> int:
    """写一条播放记录，返回事件行 ID（供之后补写时长用）。

    ``listened_ms`` 小于 0 时按 0 处理。
    """
    entry = HistoryEntry(
        id=0,
        track_id=int(song.id) if song.id else None,
        title=song.name,
        artists=tuple(song.artists),
        album=song.album,
        path=song.path,
        source=song.source,
        quality=song.quality,
        started_at=int(started_at or time.time()),
        listened_ms=max(0, int(listened_ms)),
        duration_ms=max(0, int(song.duration)),
        completed=bool(completed),
        stream_id=song.stream_id,
    )
    # 本地歌曲的负 ID 在曲库里有意义；在线歌曲 ID 也可以用来做「同一首」关联
    track_id = int(song.id) if song.id else None
    cursor = db.execute(
        "INSERT INTO play_events(track_id, title, artists, album, path, source,"
        " quality, started_at, listened_ms, duration_ms, completed, stream_id)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            track_id, entry.title, ", ".join(entry.artists), entry.album,
            entry.path, entry.source, entry.quality, entry.started_at,
            entry.listened_ms, entry.duration_ms, 1 if entry.completed else 0,
            entry.stream_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def update_play(
    db: LibraryDB,
    event_id: int,
    listened_ms: int,
    *,
    completed: bool = False,
) -> None:
    """补写一次播放的最终时长与完成标记（切歌/播完时调用）。"""
    db.execute(
        "UPDATE play_events SET listened_ms = ?, completed = ? WHERE id = ?",
        (max(0, int(listened_ms)), 1 if completed else 0, int(event_id)),
    )


def _from_row(row: sqlite3.Row) -> HistoryEntry:
    raw_track_id = row["track_id"]
    return HistoryEntry(
        id=int(row["id"]),
        track_id=int(raw_track_id) if raw_track_id is not None else None,
        title=str(row["title"]),
        artists=split_artists(str(row["artists"] or "")),
        album=str(row["album"] or ""),
        path=str(row["path"] or ""),
        source=str(row["source"] or ""),
        quality=str(row["quality"] or ""),
        started_at=int(row["started_at"] or 0),
        listened_ms=int(row["listened_ms"] or 0),
        duration_ms=int(row["duration_ms"] or 0),
        completed=bool(row["completed"]),
        stream_id=str(row["stream_id"] or ""),
    )


def recent(
    db: LibraryDB,
    limit: int = 500,
    *,
    query: str = "",
) -> list[HistoryEntry]:
    """最近播放，最新在前。``query`` 同时匹配歌名、歌手、专辑。"""
    clauses: list[str] = []
    params: list[object] = []
    for word in (query or "").split():
        clauses.append("(title LIKE ? OR artists LIKE ? OR album LIKE ?)")
        like = f"%{word}%"
        params.extend((like, like, like))
    sql = "SELECT * FROM play_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_from_row(row) for row in db.query(sql, params)]


def count(db: LibraryDB) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM play_events")
    return int(row["n"]) if row is not None else 0


def clear_history(db: LibraryDB) -> None:
    db.execute("DELETE FROM play_events")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from .. import ensure_utf8_stdout
    from .db import LibraryDB

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as root:
        db = LibraryDB(Path(root) / "library.db")
        song = Song(123, "晴天", ("周杰伦",), album="叶惠美",
                    duration=269000, path="/music/a.flac", quality="无损",
                    source="local")
        record_play(db, song, 12000, started_at=1000)
        record_play(db, song, 269000, completed=True, started_at=2000)
        records = recent(db)
        check("两条记录", len(records), 2)
        check("最新在前", records[0].started_at, 2000)
        check("完成标记", records[0].completed, True)
        check("歌名快照", records[0].title, "晴天")
        check("歌手快照", records[0].artists, ("周杰伦",))
        expected = datetime.fromtimestamp(2000).strftime("%Y-%m-%d %H:%M")
        check("时间文本", records[0].time_text, expected)
        check("过滤歌名", len(recent(db, query="晴天")), 2)
        check("过滤专辑", len(recent(db, query="叶惠美")), 2)
        check("过滤不命中", recent(db, query="不存在"), [])
        check("计数", count(db), 2)
        check("本地文件失效不可播", records[0].to_song(), None)

        online = Song(456, "在线歌", ("在线歌手",), album="在线专辑",
                      duration=180000, source="online")
        record_play(db, online, 3000, started_at=3000)
        latest = recent(db, 1)[0]
        check("在线记录 ID", latest.track_id, 456)
        rebuilt = latest.to_song()
        check("在线记录可重播", (rebuilt.id, rebuilt.source), (456, "online"))
        check("在线记录计数", count(db), 3)
        clear_history(db)
        check("清空", count(db), 0)
        db.close()

    if failures:
        print(f"播放历史自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("播放历史自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
