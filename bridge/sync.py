"""增量同步引擎：对每个指标按 (modified, rowid) 做 keyset 增量翻页，
经采集后端只读取变化行，按源主键 UPSERT 入本地库。可中断续传——游标持久化，
下次从上次成功位置继续；修订（modified 变大）会被重新取到并覆盖。

另有“按天增量”模式（since_last）：识别本地每个指标已同步到的最后一天，只从
那一天（含，补齐当天未完的数据）起按事件时间读取，之前的天不再遍历。
"""
from __future__ import annotations
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .config import Metric, METRICS
from .store import Store
from .reader import Reader


TZ = timezone(timedelta(hours=8))  # 与 query 一致：按本地日历日（Asia/Shanghai）


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _last_day_floor(metric: Metric, max_time: int) -> tuple[int, str]:
    """本地最大事件时间 → (该日起点的下限值, 'YYYY-MM-DD')。超过今天的异常时间按今天算。"""
    today = datetime.now(TZ).date()
    if metric.time_kind == "ms":
        d = min(datetime.fromtimestamp(max_time / 1000, tz=TZ).date(), today)
        floor = int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() * 1000)
        return floor, d.isoformat()
    n = min(max_time, int(today.strftime("%Y%m%d")))
    return n, _time_label(metric, n)


def _time_label(metric: Metric, v: int) -> str:
    if metric.time_kind == "ms":
        return datetime.fromtimestamp(v / 1000, tz=TZ).strftime("%Y-%m-%d")
    s = str(int(v))
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _ensure_local_table(reader: Reader, store: Store, metric: Metric, src_cols: list[str]) -> None:
    """按源表 DDL 建本地表（忠实镜像）；取不到 DDL 时按列名 + 主键建表。"""
    ddl_res = reader.select(
        f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{metric.table}'", 1)
    if ddl_res["rows"] and ddl_res["rows"][0][0]:
        store.ensure_table_ddl(ddl_res["rows"][0][0])
    else:
        store.ensure_table(metric, src_cols)


def _scan_by_time(reader: Reader, store: Store, metric: Metric, lo: int | None, hi: int | None,
                  page: int, log: Callable[[str], None],
                  should_stop: Callable[[], bool] | None) -> tuple[int, bool]:
    """按 (事件时间, rowid) keyset 翻页读取 lo <= 时间 < hi（None 表示不限）的行并 UPSERT。
    返回 (行数, 是否读完)；被停止时“读完”为 False。"""
    tcol, tbl = _q(metric.time_col), _q(metric.local_table)
    bounds = []
    if lo is not None:
        bounds.append(f"{tcol} >= {lo}")
    if hi is not None:
        bounds.append(f"{tcol} < {hi}")
    last_t: int | None = None
    last_rowid = -1
    total = 0
    while True:
        if should_stop is not None and should_stop():
            return total, False
        conds = list(bounds)
        if last_t is not None:
            conds.append(f"(({tcol} > {last_t}) OR ({tcol} = {last_t} AND rowid > {last_rowid}))")
        where = " AND ".join(conds) or "1=1"
        sql = (f"SELECT rowid AS __rowid, * FROM {tbl} WHERE {where} "
               f"ORDER BY {tcol} ASC, rowid ASC LIMIT {page}")
        res = reader.select(sql, page)
        cols, rows = res["columns"], res["rows"]
        if not rows:
            return total, True
        if total == 0:
            _ensure_local_table(reader, store, metric, cols[1:])
        t_idx = cols.index(metric.time_col)
        store.upsert(metric, cols[1:], [r[1:] for r in rows])
        last = rows[-1]
        last_t, last_rowid = int(last[t_idx]), int(last[0])
        total += len(rows)
        log(f"  {metric.label}（{metric.key}）… 已取 {total} 行")
        if len(rows) < page:
            return total, True


def sync_metric_since_last(reader: Reader, store: Store, metric: Metric, page: int = 2000,
                           log: Callable[[str], None] = print,
                           should_stop: Callable[[], bool] | None = None) -> int | None:
    """从本地已同步到的最后一天起，按事件时间读取。
    本地无数据或指标无事件时间列时返回 None，由调用方回退到常规增量。
    中途停止无需额外游标：下次重新取本地最大时间即自然续传。"""
    max_time = store.local_max_time(metric)
    if max_time is None or metric.time_kind not in ("ms", "yyyymmdd"):
        return None
    floor, day = _last_day_floor(metric, max_time)
    log(f"  {metric.label}（{metric.key}）：本地已同步到 {day}，从该日起增量")
    total, finished = _scan_by_time(reader, store, metric, floor, None, page, log, should_stop)
    if not finished:
        log(f"  已停止（{metric.label} 下次从本地最后一天继续）")
    store.mark_synced(metric, total)
    return total


def _floor_value(metric: Metric, days: int | None) -> int | None:
    if days is None or metric.time_kind is None or metric.time_col is None:
        return None
    if metric.time_kind == "ms":
        return int((time.time() - days * 86400) * 1000)
    if metric.time_kind == "yyyymmdd":
        return int((date.today() - timedelta(days=days)).strftime("%Y%m%d"))
    return None


def sync_metric(reader: Reader, store: Store, metric: Metric,
                days: int | None, page: int = 2000,
                log: Callable[[str], None] = print,
                should_stop: Callable[[], bool] | None = None) -> int:
    last_mod, last_rowid, floor, _ = store.get_state(metric)
    first = last_mod == -1
    if first:  # 首次：按模式设定回填下限
        floor = _floor_value(metric, days)
        store.set_backfill_floor(metric, floor)

    mcol, tcol = _q(metric.modified), (_q(metric.time_col) if metric.time_col else None)
    tbl = _q(metric.local_table)
    total = 0
    pages = 0
    while True:
        if should_stop is not None and should_stop():
            log(f"  已停止（{metric.label} 进度已保存，可续传）")
            return total
        where = f"(({mcol} > {last_mod}) OR ({mcol} = {last_mod} AND rowid > {last_rowid}))"
        if floor is not None and tcol is not None:
            where += f" AND {tcol} >= {floor}"
        sql = (f"SELECT rowid AS __rowid, * FROM {tbl} WHERE {where} "
               f"ORDER BY {mcol} ASC, rowid ASC LIMIT {page}")
        res = reader.select(sql, page)
        cols, rows = res["columns"], res["rows"]
        if not rows:
            break
        src_cols = cols[1:]
        if pages == 0:
            _ensure_local_table(reader, store, metric, src_cols)
        mod_idx = cols.index(metric.modified)
        src_rows = [r[1:] for r in rows]
        store.upsert(metric, src_cols, src_rows)
        last = rows[-1]
        last_mod, last_rowid = int(last[mod_idx]), int(last[0])
        store.advance(metric, last_mod, last_rowid, len(rows))
        total += len(rows)
        pages += 1
        log(f"  {metric.label}（{metric.key}）… 已取 {total} 行")
        if len(rows) < page:
            break

    # 范围扩大（如先前只同步最近 7 天，现在选 30 天或全部）：补齐旧下限之前的那段。
    # 上面的游标增量只覆盖旧下限之后，这里按事件时间补 [新下限, 旧下限)，读完才更新下限。
    new_floor = _floor_value(metric, days)
    if not first and floor is not None and tcol is not None and (new_floor is None or new_floor < floor):
        frm = "最早" if new_floor is None else _time_label(metric, new_floor)
        log(f"  {metric.label}（{metric.key}）：同步范围扩大，补齐 {frm} 至 {_time_label(metric, floor)} 之前的数据")
        n, finished = _scan_by_time(reader, store, metric, new_floor, floor, page, log, should_stop)
        total += n
        if finished:
            store.set_backfill_floor(metric, new_floor)
            store.mark_synced(metric, n)
        else:
            log(f"  已停止（{metric.label} 补齐未完成，下次继续）")
    return total


def sync_all(serial: str, days: int | None = None, only: list[str] | None = None,
             store: Store | None = None, log: Callable[[str], None] = print,
             should_stop: Callable[[], bool] | None = None,
             reset: bool = False, page: int = 2000, since_last: bool = False) -> dict:
    """days=None 全量；days=N 仅最近 N 天回填（比之前的范围大时自动补齐）。only 限定指标 key。
    reset=True 先清空游标，从头重新遍历（UPSERT 幂等）。
    since_last=True 按天增量：从本地已同步到的最后一天起只取之后的数据；
    本地尚无数据（或指标无事件时间列）的指标回退到常规增量（首次按 days 回填）。
    should_stop 返回 True 时在页/指标边界安全停止（游标已持久化，可续传）。
    只有全部指标成功且未被停止时才记录“上次同步完成”时间。"""
    own_store = store is None
    store = store or Store()
    metrics = [m for m in METRICS if (only is None or m.key in only)]
    if reset:
        store.reset_state([m.key for m in metrics])
        log("已清空游标，将从头完整回填。")
    mode = "全部可读历史" if days is None else f"最近 {days} 天"
    if since_last:
        mode = f"按天增量（从上次同步到的那天起；无本地数据的指标按{mode}）"
    log(f"同步模式：{mode}；指标 {len(metrics)} 项")
    summary: dict[str, int] = {}
    stopped = False
    try:
        with Reader(serial) as reader:
            store.set_meta("last_sync_started_ms", str(int(time.time() * 1000)))
            for m in metrics:
                if should_stop is not None and should_stop():
                    break
                try:
                    n = None
                    if since_last:
                        n = sync_metric_since_last(reader, store, m, page=page, log=log,
                                                   should_stop=should_stop)
                    if n is None:
                        n = sync_metric(reader, store, m, days, page=page, log=log, should_stop=should_stop)
                    summary[m.key] = n
                except Exception as e:  # 单指标失败不影响其它；记录错误
                    store.set_error(m, str(e))
                    log(f"  [失败] {m.label}：{str(e)[:120]}")
                    summary[m.key] = -1
            stopped = should_stop is not None and should_stop()
            if stopped:
                log("已停止同步（进度已保存，下次继续）。")
            elif all(n >= 0 for n in summary.values()):
                store.set_meta("last_sync_done_ms", str(int(time.time() * 1000)))
    finally:
        if own_store:
            store.close()
    return summary
