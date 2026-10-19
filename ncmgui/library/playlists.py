"""本地歌单：只存在于本机曲库，不写网易云。

位置从 0 开始，插入时接在末尾。重复加入同一首会被忽略（主键约束），
返回实际新增数量。删除曲库里的歌曲会通过外键级联清理歌单条目。

自检::

    .venv/bin/python -m ncmgui.library.playlists
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..resolver import Song
from .db import LibraryDB
from .queries import song_from_row


@dataclass(frozen=True)
class LocalPlaylist:
    id: int
    name: str
    track_count: int = 0
    created_at: int = 0
    updated_at: int = 0


def list_playlists(db: LibraryDB) -> list[LocalPlaylist]:
    rows = db.query(
        "SELECT p.*, COUNT(t.track_id) AS n FROM playlists p "
        "LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
        "GROUP BY p.id ORDER BY p.created_at, p.id"
    )
    return [
        LocalPlaylist(
            id=int(row["id"]),
            name=str(row["name"]),
            track_count=int(row["n"] or 0),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
        )
        for row in rows
    ]


def get_playlist(db: LibraryDB, playlist_id: int) -> LocalPlaylist | None:
    for item in list_playlists(db):
        if item.id == int(playlist_id):
            return item
    return None


def create(db: LibraryDB, name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("歌单名不能为空")
    now = int(time.time())
    with db.batch() as conn:
        cursor = conn.execute(
            "INSERT INTO playlists(name, created_at, updated_at) VALUES(?, ?, ?)",
            (name, now, now),
        )
        return int(cursor.lastrowid)


def rename(db: LibraryDB, playlist_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("歌单名不能为空")
    db.execute(
        "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?",
        (name, int(time.time()), int(playlist_id)),
    )


def delete(db: LibraryDB, playlist_id: int) -> None:
    db.execute("DELETE FROM playlists WHERE id = ?", (int(playlist_id),))


def add_tracks(db: LibraryDB, playlist_id: int, track_ids: Sequence[int]) -> int:
    ids = [int(item) for item in track_ids if int(item)]
    if not ids:
        return 0
    now = int(time.time())
    added = 0
    with db.batch() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS p FROM playlist_tracks"
            " WHERE playlist_id = ?",
            (int(playlist_id),),
        ).fetchone()
        position = int(row["p"] if row is not None else -1) + 1
        for track_id in ids:
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO playlist_tracks"
                    "(playlist_id, track_id, position, added_at)"
                    " VALUES(?, ?, ?, ?)",
                    (int(playlist_id), track_id, position, now),
                )
            except sqlite3.IntegrityError:
                continue
            if cursor.rowcount:
                added += 1
                position += 1
        conn.execute(
            "UPDATE playlists SET updated_at = ? WHERE id = ?",
            (now, int(playlist_id)),
        )
    return added


def remove_tracks(db: LibraryDB, playlist_id: int, track_ids: Sequence[int]) -> int:
    ids = [int(item) for item in track_ids if int(item)]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    cursor = db.execute(
        f"DELETE FROM playlist_tracks WHERE playlist_id = ?"
        f" AND track_id IN ({marks})",
        [int(playlist_id), *ids],
    )
    db.execute(
        "UPDATE playlists SET updated_at = ? WHERE id = ?",
        (int(time.time()), int(playlist_id)),
    )
    return int(cursor.rowcount or 0)


def set_order(db: LibraryDB, playlist_id: int, track_ids: Sequence[int]) -> None:
    """把歌单曲目重排为给定顺序；不在列表里的曲目会被移除。"""
    now = int(time.time())
    with db.batch() as conn:
        conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?",
            (int(playlist_id),),
        )
        for position, track_id in enumerate(track_ids):
            conn.execute(
                "INSERT OR IGNORE INTO playlist_tracks"
                "(playlist_id, track_id, position, added_at) VALUES(?, ?, ?, ?)",
                (int(playlist_id), int(track_id), position, now),
            )
        conn.execute(
            "UPDATE playlists SET updated_at = ? WHERE id = ?",
            (now, int(playlist_id)),
        )


def tracks(db: LibraryDB, playlist_id: int) -> list[Song]:
    rows = db.query(
        "SELECT t.* FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id"
        " WHERE pt.playlist_id = ? ORDER BY pt.position, pt.added_at",
        (int(playlist_id),),
    )
    return [song_from_row(row) for row in rows]


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
        with db.batch() as conn:
            ids = []
            for index, name in enumerate(("一", "二", "三")):
                track_id = db.next_track_id()
                ids.append(track_id)
                conn.execute(
                    "INSERT INTO tracks(id, path, name, artists, album, duration_ms,"
                    " quality, size, mtime, added_at, updated_at)"
                    " VALUES(?, ?, ?, '甲', '专辑', 1000, '', 0, 0, 0, 0)",
                    (track_id, f"/music/{name}.mp3", name),
                )

        playlist_id = create(db, "通勤")
        check("新建歌单", get_playlist(db, playlist_id).name, "通勤")
        check("加入两首", add_tracks(db, playlist_id, ids[:2]), 2)
        check("重复加入被忽略", add_tracks(db, playlist_id, [ids[0]]), 0)
        check("曲目数", get_playlist(db, playlist_id).track_count, 2)
        check("曲目顺序", [s.name for s in tracks(db, playlist_id)], ["一", "二"])

        check("移除一首", remove_tracks(db, playlist_id, [ids[0]]), 1)
        check("剩余一首", [s.name for s in tracks(db, playlist_id)], ["二"])

        add_tracks(db, playlist_id, [ids[2]])
        set_order(db, playlist_id, [ids[2], ids[1]])
        check("重排顺序", [s.name for s in tracks(db, playlist_id)], ["三", "二"])

        rename(db, playlist_id, "夜间")
        check("改名", get_playlist(db, playlist_id).name, "夜间")
        other = create(db, "另一个")
        check("两个歌单", len(list_playlists(db)), 2)
        delete(db, other)
        check("删除歌单", len(list_playlists(db)), 1)

        db.execute("DELETE FROM tracks WHERE id = ?", (ids[2],))
        check("删歌级联歌单",
              [s.name for s in tracks(db, playlist_id)], ["二"])
        db.close()

    if failures:
        print(f"本地歌单自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("本地歌单自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
