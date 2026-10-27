"""命令行入口：detect / sync / status。中文界面。

用法：
  python -m bridge detect
  python -m bridge sync --days 3        仅最近 3 天回填
  python -m bridge sync --full          全部可读历史
  python -m bridge sync --incremental   按天增量：从上次同步到的那天起只取新数据
  python -m bridge sync --only heart_rate,spo2
  python -m bridge status
  python -m bridge export-dir D:\健康导出   设置默认导出位置（不带参数则查看）
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta

from . import device
from .config import METRICS, METRIC_BY_KEY, db_path, export_root, save_setting
from .store import Store
from .sync import sync_all

TZ = timezone(timedelta(hours=8))


def _fmt_ms(ms: int | None) -> str:
    if not ms or int(ms) <= 0:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, tz=TZ).strftime("%Y-%m-%d %H:%M:%S")


def cmd_detect(args) -> int:
    info, problems = device.preconditions(args.serial)
    print("设备检测：")
    print(f"  序列号    {info.serial}")
    print(f"  机型      {info.manufacturer} {info.model}")
    print(f"  系统      Android {info.android_release} (SDK {info.sdk})，补丁 {info.security_patch}")
    print(f"  欢太健康  {info.app_version or '未检测到'}")
    print(f"  Root      {'是' if info.rooted else '否'}")
    print(f"  数据库    {'存在' if info.db_exists else '未找到'}")
    if problems:
        print("前置条件未满足：")
        for p in problems:
            print("  - " + p)
        return 1
    print("就绪：可执行同步。")
    return 0


def cmd_sync(args) -> int:
    serial = device.pick_device(args.serial)
    device.ensure_frida_server(serial)
    days = None if args.full else args.days
    only = args.only.split(",") if args.only else None
    summary = sync_all(serial, days=days, only=only, reset=args.reset, page=args.page,
                       since_last=args.incremental)
    print("\n本次同步结果：")
    for k, n in summary.items():
        label = METRIC_BY_KEY[k].label
        print(f"  {label}（{k}）：{'失败' if n < 0 else f'{n} 行'}")
    print(f"\n本地数据库：{db_path()}")
    return 0


def cmd_export(args) -> int:
    from .export import export_all
    res = export_all(outdir=args.out, fmt=args.format, start=args.start, end=args.end)
    print(f"已导出到：{res['outdir']}")
    print(f"范围：{res['range']['start']} .. {res['range']['end']}")
    for f in res["files"]:
        rows = f"（{f['rows']} 行）" if "rows" in f else ""
        print(f"  {f['name']}{rows} — {f['desc']}")
    return 0


def cmd_export_dir(args) -> int:
    from pathlib import Path
    if args.reset:
        save_setting("export_dir", None)
    elif args.path:
        save_setting("export_dir", str(Path(args.path).expanduser().resolve()))
    print(f"默认导出位置：{export_root()}（每次导出在其中新建 export-时间戳 文件夹）")
    return 0


def cmd_backup(args) -> int:
    from .backup import backup
    res = backup(args.out)
    print(f"已备份：{res['backup']}（{res['size']} 字节）")
    return 0


def cmd_backups(args) -> int:
    from .backup import list_backups, backups_dir
    rows = list_backups()
    print(f"备份目录：{backups_dir()}")
    if not rows:
        print("（暂无备份）")
    for r in rows:
        print(f"  {r['modified']}  {r['size']:>12} 字节  {r['file']}")
    return 0


def cmd_restore(args) -> int:
    from .backup import restore
    if not args.yes:
        print("恢复会覆盖当前本地库。确认请加 --yes（会先自动备份当前库）。")
        return 2
    res = restore(args.file)
    print(f"已从 {res['restored_from']} 恢复到 {res['db']}")
    if res["safety_backup"]:
        print(f"恢复前已自动备份：{res['safety_backup']}")
    return 0


def cmd_status(args) -> int:
    store = Store()
    try:
        print(f"本地数据库：{db_path()}")
        print(f"上次同步完成：{_fmt_ms(store.get_meta('last_sync_done_ms'))}")
        print(f"{'指标':<22}{'本地行数':>10}{'游标时间(本地)':>24}  状态")
        for m in METRICS:
            last_mod, _last_rowid, floor, rows_stored = store.get_state(m)
            cnt = store.table_count(m)
            cursor = _fmt_ms(last_mod if last_mod > 0 else None)
            state = "就绪" if last_mod >= 0 else "未同步"
            print(f"{m.label+'('+m.key+')':<22}{cnt:>10}{cursor:>24}  {state}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:  # Windows 控制台默认非 UTF-8，强制 UTF-8 以正确显示中文
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="bridge", description="欢太健康桥 · 本地读取与同步")
    ap.add_argument("--serial", help="指定设备序列号（多设备时）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="检测设备与前置条件")

    sp = sub.add_parser("sync", help="同步数据到本地库")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="全部可读历史")
    g.add_argument("--days", type=int, default=7, help="仅最近 N 天回填（默认 7）")
    g.add_argument("--incremental", action="store_true",
                   help="按天增量：识别本地已同步到的最后一天，只取该日及之后的数据"
                        "（本地无数据的指标按最近 7 天回填）")
    sp.add_argument("--only", help="仅同步指定指标（逗号分隔 key）")
    sp.add_argument("--reset", action="store_true", help="清空游标后从头回填（完整历史用）")
    sp.add_argument("--page", type=int, default=2000, help="每页行数（默认 2000）")

    sub.add_parser("status", help="查看本地库与同步状态")
    sub.add_parser("serve-mcp", help="启动只读 MCP 服务（stdio）供 AI 查询")
    sub.add_parser("gui", help="打开图形界面（新手双击版）")

    ep = sub.add_parser("export", help="导出 CSV/JSON")
    ep.add_argument("--out", help="输出目录（默认 导出位置/export-时间戳，导出位置见 export-dir）")
    ep.add_argument("--format", choices=["csv", "json", "both"], default="both")
    ep.add_argument("--start", help="起始本地日期 YYYY-MM-DD（默认覆盖最早）")
    ep.add_argument("--end", help="结束本地日期 YYYY-MM-DD（默认覆盖最晚）")

    xp = sub.add_parser("export-dir", help="查看/设置默认导出位置")
    xp.add_argument("path", nargs="?", help="新的默认导出位置（父目录）")
    xp.add_argument("--reset", action="store_true", help="恢复默认（数据目录/exports）")

    bp = sub.add_parser("backup", help="备份本地库（一致快照）")
    bp.add_argument("--out", help="备份文件路径（默认 数据目录/backups/health-时间戳.db）")

    sub.add_parser("backups", help="列出备份")

    rp = sub.add_parser("restore", help="从备份恢复（覆盖当前库）")
    rp.add_argument("file", help="备份文件路径")
    rp.add_argument("--yes", action="store_true", help="确认覆盖（恢复前会自动备份当前库）")

    args = ap.parse_args(argv)
    if args.cmd == "serve-mcp":
        from .mcp_server import run
        return run()
    if args.cmd == "gui":
        from .gui import run
        return run()
    handlers = {"detect": cmd_detect, "sync": cmd_sync, "status": cmd_status,
                "export": cmd_export, "export-dir": cmd_export_dir, "backup": cmd_backup, "backups": cmd_backups,
                "restore": cmd_restore}
    return handlers[args.cmd](args)
