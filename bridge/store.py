"""本地 SQLite 存储：忠实镜像源表（同列名 + 源主键），按主键 UPSERT 合并修订，
并持久化每指标的增量游标 (last_modified, last_rowid) 与回填下限。
"""
from __future__ import annotations
import sqlite3
import time
from pathlib import Path

from .config import Metric, db_path


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_meta()

    def close(self) -> None:
        self.conn.close()

    def _init_meta(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sync_state (
                 metric TEXT PRIMARY KEY,
                 last_modified INTEGER NOT NULL DEFAULT -1,
                 last_rowid INTEGER NOT NULL DEFAULT -1,
                 backfill_floor INTEGER,
                 rows_stored INTEGER NOT NULL DEFAULT 0,
                 last_sync_ms INTEGER,
                 last_error TEXT )"""
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.commit()

    # ---- schema ----
    def ensure_table_ddl(self, source_ddl: str) -> None:
        """用源表的 CREATE SQL 原样建本地表，保留列类型与主键（忠实镜像）。"""
        ddl = source_ddl.strip()
        low = ddl.lower()
        if low.startswith("create table if not exists"):
            pass
        elif low.startswith("create table"):
            ddl = "CREATE TABLE IF NOT EXISTS" + ddl[len("create table"):]
        self.conn.execute(ddl)
        self.conn.commit()

    def ensure_table(self, metric: Metric, columns: list[str]) -> None:
        cols_sql = ", ".join(_q(c) for c in columns)
        pk_sql = ", ".join(_q(c) for c in metric.pk)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_q(metric.local_table)} ({cols_sql}, PRIMARY KEY ({pk_sql}))"
        )
        self.conn.commit()

    def upsert(self, metric: Metric, columns: list[str], rows: list[list]) -> int:
        if not rows:
            return 0
        ph = ", ".join(["?"] * len(columns))
        cols_sql = ", ".join(_q(c) for c in columns)
        sql = f"INSERT OR REPLACE INTO {_q(metric.local_table)} ({cols_sql}) VALUES ({ph})"
        self.conn.executemany(sql, rows)
        self.conn.commit()
        return len(rows)

    def table_count(self, metric: Metric) -> int:
        try:
            cur = self.conn.execute(f"SELECT COUNT(*) FROM {_q(metric.local_table)}")
            return cur.fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    # ---- sync cursor ----
    def get_state(self, metric: Metric) -> tuple[int, int, int | None, int]:
        row = self.conn.execute(
            "SELECT last_modified, last_rowid, backfill_floor, rows_stored FROM sync_state WHERE metric=?",
            (metric.key,),
        ).fetchone()
        if row is None:
            return (-1, -1, None, 0)
        return (row[0], row[1], row[2], row[3])

    def set_backfill_floor(self, metric: Metric, floor: int | None) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(metric, backfill_floor) VALUES(?,?) "
            "ON CONFLICT(metric) DO UPDATE SET backfill_floor=excluded.backfill_floor",
            (metric.key, floor),
        )
        self.conn.commit()

    def advance(self, metric: Metric, last_modified: int, last_rowid: int, added: int) -> None:
        self.conn.execute(
            """INSERT INTO sync_state(metric, last_modified, last_rowid, rows_stored, last_sync_ms, last_error)
                 VALUES(?,?,?,?,?,NULL)
               ON CONFLICT(metric) DO UPDATE SET
                 last_modified=excluded.last_modified,
                 last_rowid=excluded.last_rowid,
                 rows_stored=sync_state.rows_stored+excluded.rows_stored,
                 last_sync_ms=excluded.last_sync_ms,
                 last_error=NULL""",
            (metric.key, last_modified, last_rowid, added, int(time.time() * 1000)),
        )
        self.conn.commit()

    def set_error(self, metric: Metric, err: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(metric, last_error) VALUES(?,?) "
            "ON CONFLICT(metric) DO UPDATE SET last_error=excluded.last_error",
            (metric.key, err[:500]),
        )
        self.conn.commit()

    def reset_state(self, keys: list[str] | None = None) -> None:
        """清空同步游标（保留已入库表数据）。下次同步将从头重新遍历并 UPSERT。"""
        if keys:
            self.conn.executemany("DELETE FROM sync_state WHERE metric=?", [(k,) for k in keys])
        else:
            self.conn.execute("DELETE FROM sync_state")
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
