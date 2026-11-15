"""本地 MV 收藏。

官方明文接口只能收藏、不能取消（取消走客户端 eapi），所以默认把收藏存在
本地库里：加、删都由本程序说了算。设置里可以切回「官方账号收藏」，那种
模式下仍只能加、不能删。

自检::

    .venv/bin/python -m ncmgui.library.mvfavorites
"""

from __future__ import annotations

import sqlite3
import time

from ..api.models import Mv
from .db import LibraryDB, like_pattern


def add_favorite(db: LibraryDB, mv) -> None:
    """收藏一支 MV；重复收藏时刷新快照。"""
    db.execute(
        "INSERT INTO mv_favorites(mv_id, name, artist, cover_url, duration_ms,"
        " added_at) VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(mv_id) DO UPDATE SET name = excluded.name,"
        " artist = excluded.artist, cover_url = excluded.cover_url,"
        " duration_ms = excluded.duration_ms",
        (
            int(mv.id), str(mv.name),
            str(getattr(mv, "artist", "") or ""),
            str(getattr(mv, "cover_url", "") or ""),
            int(getattr(mv, "duration", 0) or 0),
            int(time.time()),
        ),
    )


def remove_favorite(db: LibraryDB, mv_id: int) -> bool:
    cursor = db.execute(
        "DELETE FROM mv_favorites WHERE mv_id = ?", (int(mv_id),))
    return bool(cursor.rowcount)


def is_favorite(db: LibraryDB, mv_id: int) -> bool:
    row = db.query_one(
        "SELECT 1 AS hit FROM mv_favorites WHERE mv_id = ?", (int(mv_id),))
    return row is not None


def favorite_ids(db: LibraryDB) -> set[int]:
    return {int(row["mv_id"]) for row in db.query("SELECT mv_id FROM mv_favorites")}


def _from_row(row: sqlite3.Row) -> Mv:
    return Mv(
        id=int(row["mv_id"]),
        name=str(row["name"] or ""),
        artist=str(row["artist"] or ""),
        cover_url=str(row["cover_url"] or ""),
        duration=int(row["duration_ms"] or 0),
    )


def favorites(
    db: LibraryDB,
    limit: int = 500,
    *,
    query: str = "",
) -> list[Mv]:
    """收藏的 MV，最新收藏在前。``query`` 同时匹配名称与歌手。"""
    clauses: list[str] = []
    params: list[object] = []
    for word in (query or "").split():
        clauses.append(
            "(name LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\')")
        like = like_pattern(word)
        params.extend((like, like))
    sql = "SELECT * FROM mv_favorites"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY added_at DESC, mv_id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_from_row(row) for row in db.query(sql, params)]


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
        mv = Mv(id=42, name="晴天 MV", artist="周杰伦",
                cover_url="https://example.invalid/x.jpg", duration=269000)
        check("初始未收藏", is_favorite(db, 42), False)
        add_favorite(db, mv)
        check("收藏后", is_favorite(db, 42), True)
        check("收藏列表", [item.name for item in favorites(db)], ["晴天 MV"])
        check("快照字段", (favorites(db)[0].artist, favorites(db)[0].duration),
              ("周杰伦", 269000))
        add_favorite(db, mv)
        check("重复收藏不重复", len(favorites(db)), 1)
        check("搜索命中", len(favorites(db, query="晴天")), 1)
        check("搜索不命中", favorites(db, query="不存在"), [])
        check("id 集合", favorite_ids(db), {42})
        check("取消收藏", remove_favorite(db, 42), True)
        check("取消后", is_favorite(db, 42), False)
        check("重复取消", remove_favorite(db, 42), False)
        db.close()

    if failures:
        print(f"MV 收藏自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("MV 收藏自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
