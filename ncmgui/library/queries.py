"""曲库查询：歌曲、文件夹、本地歌手、本地专辑。

返回的歌曲仍是 :class:`ncmgui.resolver.Song`，``id`` 为负数、``path`` 指向
本地文件。页面因此可以继续复用通用歌曲表格和播放器。

自检::

    .venv/bin/python -m ncmgui.library.queries
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..resolver import Song
from .db import LibraryDB

SORTS = (
    ("歌名", "name"),
    ("歌手", "artist"),
    ("专辑", "album"),
    ("时长", "duration"),
    ("最近添加", "added"),
)


def split_artists(value: str) -> tuple[str, ...]:
    return tuple(
        part.strip() for part in str(value or "").replace("/", ",").split(",")
        if part.strip()
    )


def song_from_row(row: sqlite3.Row) -> Song:
    return Song(
        id=int(row["id"]),
        name=str(row["name"]),
        artists=split_artists(str(row["artists"] or "")),
        album=str(row["album"] or ""),
        duration=int(row["duration_ms"] or 0),
        path=str(row["path"] or ""),
        quality=str(row["quality"] or ""),
        source="local",
    )


def get_track(db: LibraryDB, track_id: int) -> Song | None:
    row = db.query_one("SELECT * FROM tracks WHERE id = ?", (int(track_id),))
    return song_from_row(row) if row is not None else None


def tracks(
    db: LibraryDB,
    *,
    query: str = "",
    folder: str = "",
    artist: str = "",
    album: str = "",
    sort: str = "name",
    descending: bool = False,
    limit: int | None = None,
) -> list[Song]:
    """按条件取歌曲。

    ``folder`` 是精确的父目录；``artist`` 按完整歌手名匹配（标签里常见
    ``A, B``，这里不做分词，避免把 ``A`` 误匹配到 ``AB``）。
    """
    clauses: list[str] = []
    params: list[object] = []
    text = (query or "").strip()
    if text:
        for word in text.split():
            clauses.append("(name LIKE ? OR artists LIKE ? OR album LIKE ?)")
            like = f"%{word}%"
            params.extend((like, like, like))
    if folder:
        # 只要**直接**放在这个文件夹里的文件。用前缀 + 剩余部分不含分隔符，
        # 避免把子目录里的歌也算进来（文件夹列表本身是按目录分开统计的）。
        prefix = str(Path(folder).expanduser()) + os.sep
        clauses.append("substr(path, 1, ?) = ? AND instr(substr(path, ?), ?) = 0")
        params.extend((len(prefix), prefix, len(prefix) + 1, os.sep))
    if artist:
        clauses.append("artists = ?")
        params.append(artist)
    if album:
        clauses.append("album = ?")
        params.append(album)

    order = {
        "name": "name COLLATE NOCASE",
        "artist": "artists COLLATE NOCASE, name COLLATE NOCASE",
        "album": "album COLLATE NOCASE, name COLLATE NOCASE",
        "duration": "duration_ms",
        "added": "added_at",
        "path": "path",
    }.get(sort, "name COLLATE NOCASE")
    if descending:
        order += " DESC"

    sql = "SELECT * FROM tracks"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [song_from_row(row) for row in db.query(sql, params)]


@dataclass(frozen=True)
class FolderEntry:
    path: str
    count: int
    duration_ms: int


@dataclass(frozen=True)
class ArtistEntry:
    name: str
    count: int
    album_count: int


@dataclass(frozen=True)
class AlbumEntry:
    name: str
    artist: str
    count: int
    duration_ms: int


def folder_list(db: LibraryDB) -> list[FolderEntry]:
    rows = db.query("SELECT path, duration_ms FROM tracks")
    counts: Counter[str] = Counter()
    durations: Counter[str] = Counter()
    for row in rows:
        folder = str(Path(str(row["path"])).parent)
        counts[folder] += 1
        durations[folder] += int(row["duration_ms"] or 0)
    return [
        FolderEntry(path=folder, count=counts[folder], duration_ms=durations[folder])
        for folder in sorted(counts, key=lambda item: item.casefold())
    ]


def artist_list(db: LibraryDB) -> list[ArtistEntry]:
    rows = db.query("SELECT artists, album FROM tracks")
    counts: Counter[str] = Counter()
    albums: dict[str, set[str]] = {}
    for row in rows:
        name = str(row["artists"] or "").strip() or "未知歌手"
        counts[name] += 1
        albums.setdefault(name, set()).add(str(row["album"] or ""))
    return [
        ArtistEntry(name=name, count=counts[name], album_count=len(albums.get(name, ())))
        for name in sorted(counts, key=lambda item: item.casefold())
    ]


def album_list(db: LibraryDB) -> list[AlbumEntry]:
    rows = db.query("SELECT album, artists, duration_ms FROM tracks")
    counts: Counter[tuple[str, str]] = Counter()
    durations: Counter[tuple[str, str]] = Counter()
    for row in rows:
        key = (str(row["album"] or "").strip() or "未知专辑",
               str(row["artists"] or "").strip())
        counts[key] += 1
        durations[key] += int(row["duration_ms"] or 0)
    return [
        AlbumEntry(name=name, artist=artist, count=counts[(name, artist)],
                   duration_ms=durations[(name, artist)])
        for name, artist in sorted(counts, key=lambda item: (item[0].casefold(),
                                                             item[1].casefold()))
    ]


def counts(db: LibraryDB) -> tuple[int, int]:
    """``(歌曲数, 总时长毫秒)``。"""
    row = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(duration_ms), 0) AS ms FROM tracks"
    )
    if row is None:
        return 0, 0
    return int(row["n"]), int(row["ms"])


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile
    import time

    from .. import ensure_utf8_stdout
    from .db import LibraryDB

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as root:
        db = LibraryDB(Path(root) / "library.db")
        now = int(time.time())
        rows = [
            ("/music/A/甲 - 一.mp3", "一", "甲", "专辑一", 1000, "320 kbps"),
            ("/music/A/甲 - 二.mp3", "二", "甲", "专辑一", 2000, "320 kbps"),
            ("/music/B/乙 - 三.flac", "三", "乙", "专辑二", 3000, "无损"),
            ("/music/B/乙 - 四.flac", "四", "乙", "专辑二", 4000, "无损"),
        ]
        with db.batch() as conn:
            for path, name, artist, album, duration, quality in rows:
                conn.execute(
                    "INSERT INTO tracks(id, path, name, artists, album, duration_ms,"
                    " quality, size, mtime, added_at, updated_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (db.next_track_id(), path, name, artist, album, duration,
                     quality, now, now),
                )

        check("全部歌曲", len(tracks(db)), 4)
        # SQLite 没有中文拼音排序，NOCASE 对汉字按码位比较
        check("按歌名排序", [s.name for s in tracks(db)], ["一", "三", "二", "四"])
        check("歌手筛选", [s.name for s in tracks(db, artist="甲")], ["一", "二"])
        check("专辑筛选", len(tracks(db, album="专辑二")), 2)
        check("关键字筛选", [s.name for s in tracks(db, query="乙 四")], ["四"])
        check("文件夹筛选", len(tracks(db, folder="/music/B")), 2)
        check("本地来源", tracks(db)[0].source, "local")
        check("时长排序", [s.duration for s in tracks(db, sort="duration")],
              [1000, 2000, 3000, 4000])

        folders = folder_list(db)
        check("文件夹数量", len(folders), 2)
        check("文件夹计数", folders[0].count, 2)
        artists = artist_list(db)
        check("歌手数量", len(artists), 2)
        check("歌手专辑数", artists[0].album_count, 1)
        albums = album_list(db)
        check("专辑数量", len(albums), 2)
        check("专辑计数", albums[0].count, 2)
        check("总数与总时长", counts(db), (4, 10000))

        # 子目录里的歌不能算进父目录
        db.execute(
            "INSERT INTO tracks(id, path, name, artists, album, duration_ms,"
            " quality, size, mtime, added_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, '', 0, 0, 0, 0)",
            (db.next_track_id(), "/music/A/子/丙 - 五.mp3", "五", "丙",
             "专辑三", 5000),
        )
        check("文件夹不含子目录", len(tracks(db, folder="/music/A")), 2)
        check("子目录单独统计", len(tracks(db, folder="/music/A/子")), 1)
        db.close()

    if failures:
        print(f"曲库查询自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("曲库查询自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
