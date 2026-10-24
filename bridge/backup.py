"""SQLite 备份与恢复：用 sqlite3 在线备份 API 做一致快照（含 WAL 已提交内容），
不是简单文件复制。恢复前先自动备份当前库（可逆），校验来源，再原子替换。
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import query
from .config import db_path, data_dir

TZ = query.TZ


def backups_dir() -> Path:
    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup(dest: str | Path | None = None) -> dict:
    src_path = db_path()
    if not src_path.exists():
        raise query.QueryError("本地数据库不存在，无法备份。")
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    dest = Path(dest) if dest else backups_dir() / f"health-{ts}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return {"backup": str(dest), "size": dest.stat().st_size}


def list_backups() -> list[dict]:
    out = []
    for p in sorted(backups_dir().glob("*.db"), reverse=True):
        st = p.stat()
        out.append({"file": str(p), "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")})
    return out


def restore(src: str | Path) -> dict:
    src = Path(src)
    if not src.exists():
        raise query.QueryError(f"备份文件不存在：{src}")
    chk = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        ok = chk.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'").fetchone()
    finally:
        chk.close()
    if not ok:
        raise query.QueryError("该文件不像本桥的备份（缺 sync_state 表），已拒绝恢复。")

    dst_path = db_path()
    safety = None
    if dst_path.exists():
        safety = backup(backups_dir() / f"pre-restore-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.db")["backup"]

    tmp = Path(str(dst_path) + ".restore-tmp")
    if tmp.exists():
        tmp.unlink()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    t = sqlite3.connect(str(tmp))
    try:
        s.backup(t)
    finally:
        t.close()
        s.close()
    for ext in ("", "-wal", "-shm"):
        f = Path(str(dst_path) + ext)
        if f.exists():
            f.unlink()
    os.replace(tmp, dst_path)
    return {"restored_from": str(src), "safety_backup": safety, "db": str(dst_path)}
