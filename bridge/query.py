"""标准化只读查询层：在本地已入库数据上提供 status / coverage / daily_summary /
sleep / timeseries。所有结果带单位、时区(Asia/Shanghai)、来源表与新鲜度，并区分
“未同步 / 无记录 / 有数据”——缺数据返回 null，不冒充 0。只读，不触碰手机。
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone, timedelta, date as _date

from .config import METRICS, METRIC_BY_KEY, db_path

TZ = timezone(timedelta(hours=8))
TZ_NAME = "Asia/Shanghai"

# 睡眠分期代码（已与 App 对照确认）
SLEEP_STAGE = {2: "deep", 3: "rem", 4: "light", 5: "awake"}


class QueryError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    p = db_path()
    if not p.exists():
        raise QueryError("本地数据库尚不存在，请先执行 sync。")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _fmt(ms) -> str | None:
    if ms in (None, 0):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")


def _day_bounds_ms(d: str) -> tuple[int, int]:
    """本地日期 'YYYY-MM-DD' → [当日 00:00, 次日 00:00) 的 UTC 毫秒。"""
    y, m, dd = map(int, d.split("-"))
    start = datetime(y, m, dd, tzinfo=TZ)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _yyyymmdd(d: str) -> int:
    return int(d.replace("-", ""))


def _ymd_to_dash(n) -> str:
    s = str(int(n))
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


# ---------------------------------------------------------------- status
def health_status() -> dict:
    p = db_path()
    if not p.exists():
        return {"ok": False, "db_path": str(p), "note": "本地数据库尚不存在，请先执行 sync。",
                "timezone": TZ_NAME, "metrics": []}
    conn = _connect()
    try:
        def meta(k):
            r = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone() if _has_table(conn, "meta") else None
            return r[0] if r else None
        metrics = []
        for m in METRICS:
            st = conn.execute(
                "SELECT last_modified,last_rowid,backfill_floor,rows_stored,last_sync_ms,last_error "
                "FROM sync_state WHERE metric=?", (m.key,)
            ).fetchone() if _has_table(conn, "sync_state") else None
            cnt = conn.execute(f'SELECT COUNT(*) FROM "{m.table}"').fetchone()[0] if _has_table(conn, m.table) else 0
            metrics.append({
                "key": m.key, "label": m.label, "table": m.table,
                "rows_local": cnt,
                "synced": bool(st and st["last_modified"] >= 0),
                "cursor_time": _fmt(st["last_modified"]) if st else None,
                "backfill_floor": st["backfill_floor"] if st else None,
                "last_sync": _fmt(st["last_sync_ms"]) if st else None,
                "last_error": st["last_error"] if st else None,
            })
        return {
            "ok": True, "db_path": str(p), "timezone": TZ_NAME,
            "last_sync_started": _fmt(int(meta("last_sync_started_ms"))) if meta("last_sync_started_ms") else None,
            "last_sync_done": _fmt(int(meta("last_sync_done_ms"))) if meta("last_sync_done_ms") else None,
            "metrics": metrics,
            "note": "仅本地已同步数据；不含未同步或源端已清理的记录。本地保存不等于模型在本地。",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- coverage
def coverage(metric: str | None = None) -> dict:
    conn = _connect()
    try:
        keys = [metric] if metric else [m.key for m in METRICS]
        out = []
        for k in keys:
            m = METRIC_BY_KEY.get(k)
            if not m:
                out.append({"metric": k, "error": "未知指标"}); continue
            if not _has_table(conn, m.table):
                out.append({"metric": k, "label": m.label, "rows": 0, "synced": False,
                            "note": "未同步"}); continue
            rows = conn.execute(f'SELECT COUNT(*) FROM "{m.table}"').fetchone()[0]
            info = {"metric": k, "label": m.label, "rows": rows, "source_table": m.table}
            if m.time_kind == "ms" and m.time_col:
                r = conn.execute(f'SELECT MIN("{m.time_col}"),MAX("{m.time_col}") FROM "{m.table}"').fetchone()
                info["first_event"] = _fmt(r[0]); info["last_event"] = _fmt(r[1])
                days = conn.execute(
                    f"SELECT COUNT(DISTINCT strftime('%Y-%m-%d',\"{m.time_col}\"/1000,'unixepoch','+8 hours')) FROM \"{m.table}\""
                ).fetchone()[0]
                info["days_with_data"] = days
            elif m.time_kind == "yyyymmdd" and m.time_col:
                r = conn.execute(f'SELECT MIN("{m.time_col}"),MAX("{m.time_col}") FROM "{m.table}"').fetchone()
                info["first_day"] = _ymd_to_dash(r[0]) if r[0] else None
                info["last_day"] = _ymd_to_dash(r[1]) if r[1] else None
                info["days_with_data"] = conn.execute(f'SELECT COUNT(DISTINCT "{m.time_col}") FROM "{m.table}"').fetchone()[0]
            info["backfill_floor"] = _coverage_floor(conn, m)
            out.append(info)
        return {"timezone": TZ_NAME, "metrics": out,
                "note": "范围为本地已入库的实际数据；日期区间内无记录的天属“无数据”，非 0。"}
    finally:
        conn.close()


def _coverage_floor(conn, m):
    if not _has_table(conn, "sync_state"):
        return None
    r = conn.execute("SELECT backfill_floor FROM sync_state WHERE metric=?", (m.key,)).fetchone()
    if not r or r[0] is None:
        return "全部可读历史"
    if m.time_kind == "yyyymmdd":
        return _ymd_to_dash(r[0])
    return _fmt(r[0])


# ---------------------------------------------------------------- sleep
def sleep(start: str | None = None, end: str | None = None, limit: int = 31) -> dict:
    conn = _connect()
    try:
        if not _has_table(conn, "DBSleepDayStat"):
            return {"nights": [], "note": "睡眠每夜汇总未同步。"}
        where, params = "", []
        if start:
            where += " AND date>=?"; params.append(_yyyymmdd(start))
        if end:
            where += " AND date<=?"; params.append(_yyyymmdd(end))
        rows = conn.execute(
            f"SELECT date,sleep_in_timestamp,sleep_out_timestamp,total_sleep_time,"
            f"total_deep_sleep_time,total_lightly_sleep_time,total_rem_time,"
            f"total_wake_up_time,wake_count,sleep_score FROM DBSleepDayStat "
            f"WHERE 1=1{where} ORDER BY date DESC LIMIT ?", (*params, limit)
        ).fetchall()
        nights = [{
            "date": _ymd_to_dash(r["date"]),
            "sleep_in": _fmt(r["sleep_in_timestamp"]), "sleep_out": _fmt(r["sleep_out_timestamp"]),
            "total_min": r["total_sleep_time"], "deep_min": r["total_deep_sleep_time"],
            "light_min": r["total_lightly_sleep_time"], "rem_min": r["total_rem_time"],
            "awake_min": r["total_wake_up_time"], "wake_count": r["wake_count"],
            "sleep_score": r["sleep_score"] or None,
        } for r in rows]
        return {"nights": nights, "unit": "minutes", "timezone": TZ_NAME,
                "stage_codes": SLEEP_STAGE,
                "note": "总=深+浅+REM；无记录的夜不出现在结果中（非 0）。sleep_score 为 0 可能表示未计算。"}
    finally:
        conn.close()


# ---------------------------------------------------------------- daily summary
def daily_summary(start: str, end: str) -> dict:
    conn = _connect()
    try:
        s_i, e_i = _yyyymmdd(start), _yyyymmdd(end)
        days: dict[str, dict] = {}

        def ensure(d):
            days.setdefault(d, {"date": d, "steps": None, "distance_km": None, "calories_kcal": None,
                                "sleep": None, "heart_rate": None, "spo2": None})

        if _has_table(conn, "DBSportDataStat"):
            for r in conn.execute(
                "SELECT date, MAX(total_steps), MAX(total_distance), MAX(total_calories) "
                "FROM DBSportDataStat WHERE date BETWEEN ? AND ? GROUP BY date", (s_i, e_i)):
                d = _ymd_to_dash(r[0]); ensure(d)
                days[d].update(steps=r[1], distance_km=round((r[2] or 0) / 1000, 2),
                               calories_kcal=round((r[3] or 0) / 1000, 1))
        if _has_table(conn, "DBSleepDayStat"):
            for r in conn.execute(
                "SELECT date,total_sleep_time,total_deep_sleep_time,total_lightly_sleep_time,total_rem_time "
                "FROM DBSleepDayStat WHERE date BETWEEN ? AND ?", (s_i, e_i)):
                d = _ymd_to_dash(r[0]); ensure(d)
                days[d]["sleep"] = {"total_min": r[1], "deep_min": r[2], "light_min": r[3], "rem_min": r[4]}
        if _has_table(conn, "DBHeartRateDataStatTable"):
            for r in conn.execute(
                "SELECT date,max_hr,min_hr,average_hr,rest_hr FROM DBHeartRateDataStatTable "
                "WHERE date BETWEEN ? AND ?", (s_i, e_i)):
                d = _ymd_to_dash(r[0]); ensure(d)
                days[d]["heart_rate"] = {"max": r[1], "min": r[2], "avg": r[3], "rest": r[4], "unit": "bpm"}
        if _has_table(conn, "DBBloodOxygenSaturation"):
            s_ms, _ = _day_bounds_ms(start); _, e_ms = _day_bounds_ms(end)
            for r in conn.execute(
                "SELECT strftime('%Y-%m-%d',data_created_timestamp/1000,'unixepoch','+8 hours') d,"
                "AVG(blood_oxygen_saturation_value),MIN(blood_oxygen_saturation_value),COUNT(*) "
                "FROM DBBloodOxygenSaturation WHERE data_created_timestamp>=? AND data_created_timestamp<? GROUP BY d",
                (s_ms, e_ms)):
                d = r[0]; ensure(d)
                days[d]["spo2"] = {"avg": round(r[1], 1), "min": r[2], "samples": r[3], "unit": "%"}

        ordered = [days[k] for k in sorted(days)]
        return {"range": {"start": start, "end": end}, "timezone": TZ_NAME, "days": ordered,
                "units": {"distance": "km", "calories": "kcal", "sleep": "minutes", "heart_rate": "bpm", "spo2": "%"},
                "note": "仅返回本地有数据的天；区间内未出现的天为“无数据/未同步”，非 0。"}
    finally:
        conn.close()


# ---------------------------------------------------------------- timeseries
_TS = {
    "heart_rate": ("DBHeartRate", "data_created_timestamp", "heart_rate_value", "bpm",
                   "heart_rate_type=0"),
    "spo2": ("DBBloodOxygenSaturation", "data_created_timestamp", "blood_oxygen_saturation_value", "%", None),
}


def timeseries(metric: str, start: str, end: str, limit: int = 2000, offset: int = 0) -> dict:
    if metric not in _TS:
        raise QueryError(f"timeseries 暂支持 {list(_TS)}，收到 {metric}")
    table, tcol, vcol, unit, filt = _TS[metric]
    conn = _connect()
    try:
        if not _has_table(conn, table):
            return {"metric": metric, "points": [], "note": "未同步"}
        s_ms, _ = _day_bounds_ms(start); _, e_ms = _day_bounds_ms(end)
        where = f"{tcol}>=? AND {tcol}<?"
        if filt:
            where += f" AND {filt}"
        limit = max(1, min(int(limit), 10000))
        rows = conn.execute(
            f"SELECT {tcol},{vcol} FROM {table} WHERE {where} ORDER BY {tcol} ASC LIMIT ? OFFSET ?",
            (s_ms, e_ms, limit + 1, offset)
        ).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        pts = [{"time": _fmt(r[0]), "value": r[1]} for r in rows]
        return {"metric": metric, "unit": unit, "timezone": TZ_NAME,
                "range": {"start": start, "end": end},
                "count": len(pts), "offset": offset, "limit": limit,
                "truncated": truncated,
                "next_offset": offset + limit if truncated else None,
                "points": pts,
                "note": "时间为本地时区；truncated=true 表示还有更多，用 next_offset 续取。空结果可能是无记录或未同步。"}
    finally:
        conn.close()
