"""导出本地已入库数据：CSV（按指标拆分、本地时间与单位、标准转义）+ JSON（保留
嵌套的每日/每夜汇总）+ manifest。只读本地库，不触碰手机，不修改欢太健康源数据。
"""
from __future__ import annotations
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import query
from .config import db_path, data_dir

TZ = query.TZ


def _ro_conn() -> sqlite3.Connection:
    p = db_path()
    if not p.exists():
        raise query.QueryError("本地数据库尚不存在，请先执行 sync。")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _coverage_range() -> tuple[str | None, str | None]:
    cov = query.coverage()
    days: list[str] = []
    for m in cov["metrics"]:
        for k in ("first_event", "last_event"):
            if m.get(k):
                days.append(m[k][:10])
        for k in ("first_day", "last_day"):
            if m.get(k):
                days.append(m[k])
    return (min(days), max(days)) if days else (None, None)


def _write_csv(path: Path, header: list[str], rows) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def _export_points(conn, table, tcol, vcol, s_ms, e_ms, path, filt=None) -> int:
    where = f"{tcol}>=? AND {tcol}<?"
    if filt:
        where += f" AND {filt}"
    cur = conn.execute(f"SELECT {tcol},{vcol} FROM {table} WHERE {where} ORDER BY {tcol} ASC", (s_ms, e_ms))

    def rows():
        for ts, v in cur:
            yield [datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S"), v]
    return _write_csv(path, ["local_time", vcol], rows())


def export_all(outdir: str | Path | None = None, fmt: str = "both",
               start: str | None = None, end: str | None = None) -> dict:
    if start is None or end is None:
        cs, ce = _coverage_range()
        start = start or cs
        end = end or ce
    if not start or not end:
        raise query.QueryError("本地无数据可导出。")
    ts = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    outdir = Path(outdir) if outdir else (data_dir() / "exports" / f"export-{ts}")
    outdir.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    want_csv = fmt in ("csv", "both")
    want_json = fmt in ("json", "both")

    daily = query.daily_summary(start, end)
    sleep = query.sleep(start, end, limit=100000)

    if want_csv:
        # 每日汇总（跨指标，扁平）
        hdr = ["date", "steps", "distance_km", "calories_kcal",
               "sleep_total_min", "sleep_deep_min", "sleep_light_min", "sleep_rem_min",
               "hr_rest", "hr_avg", "hr_min", "hr_max", "spo2_avg", "spo2_min"]
        def daily_rows():
            for d in daily["days"]:
                sl = d.get("sleep") or {}
                hr = d.get("heart_rate") or {}
                sp = d.get("spo2") or {}
                yield [d["date"], d.get("steps"), d.get("distance_km"), d.get("calories_kcal"),
                       sl.get("total_min"), sl.get("deep_min"), sl.get("light_min"), sl.get("rem_min"),
                       hr.get("rest"), hr.get("avg"), hr.get("min"), hr.get("max"),
                       sp.get("avg"), sp.get("min")]
        n = _write_csv(outdir / "daily_summary.csv", hdr, daily_rows())
        files.append({"name": "daily_summary.csv", "rows": n, "desc": "每日跨指标汇总",
                      "units": {"distance": "km", "calories": "kcal", "sleep": "min", "hr": "bpm", "spo2": "%"}})

        # 每夜睡眠
        shdr = ["date", "sleep_in", "sleep_out", "total_min", "deep_min", "light_min", "rem_min", "awake_min", "wake_count"]
        def sleep_rows():
            for nt in sleep["nights"]:
                yield [nt["date"], nt["sleep_in"], nt["sleep_out"], nt["total_min"], nt["deep_min"],
                       nt["light_min"], nt["rem_min"], nt["awake_min"], nt["wake_count"]]
        n = _write_csv(outdir / "sleep_nights.csv", shdr, sleep_rows())
        files.append({"name": "sleep_nights.csv", "rows": n, "desc": "每夜睡眠与分期", "units": {"sleep": "min"}})

        # 逐点：心率、血氧
        conn = _ro_conn()
        try:
            s_ms, _ = query._day_bounds_ms(start)
            _, e_ms = query._day_bounds_ms(end)
            for metric, (table, tcol, vcol, unit, filt) in query._TS.items():
                if not query._has_table(conn, table):
                    continue
                n = _export_points(conn, table, tcol, vcol, s_ms, e_ms, outdir / f"{metric}_points.csv", filt)
                files.append({"name": f"{metric}_points.csv", "rows": n, "desc": f"{metric} 逐点", "units": {"value": unit}})
        finally:
            conn.close()

    if want_json:
        bundle = {
            "meta": {"generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                     "timezone": query.TZ_NAME, "range": {"start": start, "end": end},
                     "note": "嵌套结构保留在 JSON；逐点序列见 CSV。缺数据为 null，非 0。"},
            "status": query.health_status(),
            "coverage": query.coverage(),
            "daily_summary": daily,
            "sleep": sleep,
        }
        (outdir / "export.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
        files.append({"name": "export.json", "desc": "嵌套汇总（状态/覆盖/每日/每夜）"})

    manifest = {"generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": query.TZ_NAME, "range": {"start": start, "end": end},
                "format": fmt, "files": files,
                "note": "导出自本地已同步数据；不含未同步或源端已清理的记录。读取与导出不修改欢太健康源数据。"}
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"outdir": str(outdir), "files": files, "range": {"start": start, "end": end}}
