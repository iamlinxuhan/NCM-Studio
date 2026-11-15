"""MV 观看历史：和歌曲历史同库不同表。

存的是 MV 名称/歌手/封面的快照，而不是只存 ``mv_id`` —— MV 会下架、改名，
快照让历史列表始终完整；重播时再按 ``mv_id`` 重新取播放地址。

自检::

    .venv/bin/python -m ncmgui.library.mvhistory
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

from .db import LibraryDB, like_pattern


@dataclass(frozen=True)
class MvHistoryEntry:
    id: int
    mv_id: int
    name: str
    artist: str
    cover_url: str
    started_at: int
    watched_ms: int
    duration_ms: int
    completed: bool

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.name}" if self.artist else self.name

    @property
    def time_text(self) -> str:
        return datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M")

    @property
    def short_time_text(self) -> str:
        """列表里用短格式；完整时间放在 tooltip 里。"""
        return datetime.fromtimestamp(self.started_at).strftime("%m-%d %H:%M")


def record_mv_play(
    db: LibraryDB,
    *,
    mv_id: int,
    name: str,
    artist: str = "",
    cover_url: str = "",
    duration_ms: int = 0,
    watched_ms: int = 0,
    started_at: int | None = None,
) -> int:
    """写一条观看记录，返回事件行 ID（供之后补写观看时长用）。"""
    cursor = db.execute(
        "INSERT INTO mv_events(mv_id, name, artist, cover_url, started_at,"
        " watched_ms, duration_ms, completed) VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
        (
            int(mv_id), str(name), str(artist or ""), str(cover_url or ""),
            int(started_at or time.time()), max(0, int(watched_ms)),
            max(0, int(duration_ms)),
        ),
    )
    return int(cursor.lastrowid or 0)


def update_mv_play(
    db: LibraryDB,
    event_id: int,
    watched_ms: int,
    *,
    completed: bool = False,
) -> None:
    """补写一次观看的最终时长与完成标记（切走/播完时调用）。"""
    db.execute(
        "UPDATE mv_events SET watched_ms = ?, completed = ? WHERE id = ?",
        (max(0, int(watched_ms)), 1 if completed else 0, int(event_id)),
    )


def _from_row(row: sqlite3.Row) -> MvHistoryEntry:
    return MvHistoryEntry(
        id=int(row["id"]),
        mv_id=int(row["mv_id"]),
        name=str(row["name"] or ""),
        artist=str(row["artist"] or ""),
        cover_url=str(row["cover_url"] or ""),
        started_at=int(row["started_at"] or 0),
        watched_ms=int(row["watched_ms"] or 0),
        duration_ms=int(row["duration_ms"] or 0),
        completed=bool(row["completed"]),
    )


def recent_mv(
    db: LibraryDB,
    limit: int = 500,
    *,
    query: str = "",
) -> list[MvHistoryEntry]:
    """最近看过的 MV，最新在前。``query`` 同时匹配名称与歌手。"""
    clauses: list[str] = []
    params: list[object] = []
    for word in (query or "").split():
        clauses.append(
            "(name LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\')")
        like = like_pattern(word)
        params.extend((like, like))
    sql = "SELECT * FROM mv_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_from_row(row) for row in db.query(sql, params)]


def mv_count(db: LibraryDB) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM mv_events")
    return int(row["n"]) if row is not None else 0


def clear_mv_history(db: LibraryDB) -> None:
    db.execute("DELETE FROM mv_events")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    from pathlib import Path

    from .. import ensure_utf8_stdout
    from .db import LibraryDB, like_pattern

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as root:
        db = LibraryDB(Path(root) / "library.db")
        event = record_mv_play(
            db, mv_id=42, name="晴天 MV", artist="周杰伦",
            cover_url="https://example.invalid/a.jpg", duration_ms=269000,
            watched_ms=5000, started_at=1000,
        )
        check("写回行 ID", event > 0, True)
        update_mv_play(db, event, 269000, completed=True)
        record_mv_play(db, mv_id=43, name="另一支", artist="别人",
                       started_at=2000)
        records = recent_mv(db)
        check("两条记录", len(records), 2)
        check("最新在前", records[0].mv_id, 43)
        check("快照字段", (records[1].name, records[1].artist),
              ("晴天 MV", "周杰伦"))
        check("完成标记", records[1].completed, True)
        check("观看时长", records[1].watched_ms, 269000)
        expected = datetime.fromtimestamp(1000).strftime("%Y-%m-%d %H:%M")
        check("时间文本", records[1].time_text, expected)
        check("过滤名称", len(recent_mv(db, query="晴天")), 1)
        check("过滤歌手", len(recent_mv(db, query="别人")), 1)
        check("过滤不命中", recent_mv(db, query="不存在"), [])
        check("通配符不当成全部", len(recent_mv(db, query="%")), 0)
        check("计数", mv_count(db), 2)
        clear_mv_history(db)
        check("清空", mv_count(db), 0)
        db.close()

    if failures:
        print(f"MV 历史自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("MV 历史自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
