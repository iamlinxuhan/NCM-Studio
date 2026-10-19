"""本地曲库的 SQLite 连接与 schema。

数据库用 WAL：扫描在后台写、界面同时在读，不会互相把对方锁住。所有写操作
通过一把进程内可重入锁串行化 —— 曲库规模是几千首，瓶颈在标签读取，不在锁。

歌曲 ID 用负数，和网易云的正数 ID 隔开。ID 由 ``meta.next_track_id`` 单调
分配，重新扫描不会让同一首歌换 ID，播放队列/本地歌单的引用因此稳定。

自检::

    .venv/bin/python -m ncmgui.library.db
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..config import config_dir

SCHEMA_VERSION = 3


def default_db_path() -> Path:
    return config_dir() / "library.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    artists     TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    quality     TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL NOT NULL DEFAULT 0,
    added_at    INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_name ON tracks(name);
CREATE INDEX IF NOT EXISTS idx_tracks_artists ON tracks(artists);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    added_at    INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_order
    ON playlist_tracks(playlist_id, position);

CREATE TABLE IF NOT EXISTS play_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER,
    title       TEXT NOT NULL,
    artists     TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    quality     TEXT NOT NULL DEFAULT '',
    started_at  INTEGER NOT NULL,
    listened_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    completed   INTEGER NOT NULL DEFAULT 0,
    stream_id   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_play_events_started
    ON play_events(started_at);
CREATE INDEX IF NOT EXISTS idx_play_events_track
    ON play_events(track_id);
"""


class LibraryDB:
    """一个线程安全的曲库句柄。

    ``sqlite3`` 连接用 ``check_same_thread=False`` 建立，再由 :attr:`_lock`
    保护。扫描线程只会在提交批次时占用锁，读查询可以穿插进行。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=15.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            # 旧库的 play_events 没有 stream_id；CREATE TABLE IF NOT EXISTS 不会
            # 补列，只能在启动时探测一次再 ALTER。流媒体歌曲靠它才能在历史里重播。
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(play_events)")
            }
            if "stream_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE play_events ADD COLUMN stream_id TEXT NOT NULL DEFAULT ''"
                )
            version_row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            previous = int(version_row["value"]) if version_row is not None else 0
            if previous < 3:
                # 3.0.0 早期的扫描器把缺失标签写成了字符串 "None"。
                # mtime 归零后，下一次增量扫描会重新读取这些文件并覆盖坏数据；
                # 保留歌曲 ID，本地歌单的引用不受影响。
                self._conn.execute(
                    "UPDATE tracks SET mtime = -1"
                    " WHERE name = 'None' OR artists = 'None' OR album = 'None'"
                )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 基础访问

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    @contextmanager
    def batch(self) -> Iterator[sqlite3.Connection]:
        """一批写操作共用一个事务；扫描时每 N 个文件调用一次。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ 元数据

    def meta(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def next_track_id(self) -> int:
        """分配下一个负数 ID；调用方应在同一个事务里使用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'next_track_id'"
            ).fetchone()
            value = int(row["value"]) if row is not None else -1
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('next_track_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(value - 1),),
            )
            return value

    # ------------------------------------------------------------ 维护

    def compact(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> int:
    import tempfile

    from .. import ensure_utf8_stdout

    ensure_utf8_stdout()
    failures: list[str] = []

    def check(name: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "library.db"
        db = LibraryDB(path)
        check("schema 版本", db.meta("schema_version"), str(SCHEMA_VERSION))
        check("默认没有歌曲", db.query_one("SELECT COUNT(*) AS n FROM tracks")["n"], 0)

        now = int(time.time())
        with db.batch() as conn:
            track_id = db.next_track_id()
            conn.execute(
                "INSERT INTO tracks(id, path, name, artists, album, duration_ms,"
                " quality, size, mtime, added_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (track_id, str(Path(root) / "a.flac"), "甲", "歌手", "专辑",
                 200000, "无损", 123, 1.0, now, now),
            )
        check("负数 ID", track_id, -1)
        check("下一首 ID", db.next_track_id(), -2)
        check("歌曲数量", db.query_one("SELECT COUNT(*) AS n FROM tracks")["n"], 1)

        with db.batch() as conn:
            conn.execute(
                "INSERT INTO playlists(name, created_at, updated_at) VALUES(?, ?, ?)",
                ("本地歌单", now, now),
            )
            playlist_id = conn.execute("SELECT id FROM playlists").fetchone()["id"]
            conn.execute(
                "INSERT INTO playlist_tracks(playlist_id, track_id, position, added_at)"
                " VALUES(?, ?, ?, ?)",
                (playlist_id, track_id, 0, now),
            )
        check("歌单曲目", db.query_one(
            "SELECT COUNT(*) AS n FROM playlist_tracks")["n"], 1)

        with db.batch() as conn:
            conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        check("删除歌曲级联歌单",
              db.query_one("SELECT COUNT(*) AS n FROM playlist_tracks")["n"], 0)

        db.close()
        reopened = LibraryDB(path)
        check("重开保留 schema", reopened.meta("schema_version"), str(SCHEMA_VERSION))
        check("重开保留下一首 ID", reopened.next_track_id(), -3)
        reopened.close()

        # 2 -> 3：旧扫描器把缺失标签写成 "None"，mtime 归零以触发重读
        legacy = LibraryDB(path)
        with legacy.batch() as conn:
            conn.execute(
                "INSERT INTO tracks(id, path, name, artists, album, duration_ms,"
                " quality, size, mtime, added_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (-99, str(Path(root) / "bad.flac"), "None", "None", "None",
                 1000, "", 1, 123.0, now, now),
            )
        legacy.set_meta("schema_version", "2")
        legacy.close()
        repaired = LibraryDB(path)
        row = repaired.query_one("SELECT mtime FROM tracks WHERE id = -99")
        check("旧库 None 元数据触发重扫", row["mtime"], -1.0)
        repaired.close()

    if failures:
        print(f"曲库数据库自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("曲库数据库自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
