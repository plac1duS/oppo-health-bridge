"""欢太健康桥 · 图形界面（Tkinter，标准库，无第三方依赖，本机窗口不开网络）。

功能：连接检测、范围选择（按天增量/仅最近/全部）、立即同步（可停止/续传）、状态查看、
导出（可自定义导出位置）、备份/恢复、打开数据文件夹。耗时操作在后台线程执行，经队列回传日志，主线程刷新界面。
"""
from __future__ import annotations
import os
import queue
import threading
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

from . import device, query
from .config import db_path, data_dir, export_root, save_setting, METRICS
from .sync import sync_all
from .export import export_all
from .backup import backup as do_backup, restore as do_restore


class BridgeGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.serial: str | None = None
        self.ready = False  # 最近一次检测是否满足全部前置条件

        root.title("欢太健康桥 · 本地同步")
        root.geometry("760x620")
        root.minsize(680, 560)
        pad = {"padx": 8, "pady": 4}

        # —— 连接 ——
        fr_conn = ttk.LabelFrame(root, text="1. 连接")
        fr_conn.pack(fill="x", **pad)
        self.lbl_conn = ttk.Label(fr_conn, text="未检测。请用 USB 连接手机并允许调试，然后点“检测设备”。",
                                  justify="left", wraplength=720)
        self.lbl_conn.pack(side="left", fill="x", expand=True, padx=8, pady=6)
        self.btn_detect = ttk.Button(fr_conn, text="检测设备", command=self.on_detect)
        self.btn_detect.pack(side="right", padx=8, pady=6)

        # —— 同步 ——
        fr_sync = ttk.LabelFrame(root, text="2. 同步")
        fr_sync.pack(fill="x", **pad)
        row = ttk.Frame(fr_sync); row.pack(fill="x", padx=8, pady=4)
        # 本地已有数据时默认“按天增量”，首次默认“仅最近 N 天”
        self.range_mode = tk.StringVar(value="incremental" if db_path().exists() else "recent")
        ttk.Radiobutton(row, text="增量（接着上次同步到的那天）", variable=self.range_mode,
                        value="incremental").pack(side="left", padx=(0, 16))
        ttk.Radiobutton(row, text="仅最近", variable=self.range_mode, value="recent").pack(side="left")
        self.days_var = tk.IntVar(value=7)
        ttk.Spinbox(row, from_=1, to=3650, width=6, textvariable=self.days_var).pack(side="left", padx=(2, 2))
        ttk.Label(row, text="天").pack(side="left")
        ttk.Radiobutton(row, text="全部可读历史", variable=self.range_mode, value="full").pack(side="left", padx=(16, 0))
        self.btn_sync = ttk.Button(row, text="立即同步", command=self.on_sync, state="disabled")
        self.btn_sync.pack(side="right")
        self.btn_stop = ttk.Button(row, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="right", padx=(0, 6))
        self.log = scrolledtext.ScrolledText(fr_sync, height=9, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # —— 状态与数据 ——
        fr_data = ttk.LabelFrame(root, text="3. 本地数据")
        fr_data.pack(fill="both", expand=True, **pad)
        cols = ("metric", "rows", "last_day", "last_sync")
        self.tree = ttk.Treeview(fr_data, columns=cols, show="headings", height=8)
        for c, t, w in [("metric", "指标", 180), ("rows", "本地行数", 90),
                        ("last_day", "数据最新到", 170), ("last_sync", "最后同步", 170)]:
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        btns = ttk.Frame(fr_data); btns.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_refresh = ttk.Button(btns, text="刷新状态", command=self.on_refresh)
        self.btn_refresh.pack(side="left")
        self.btn_export = ttk.Button(btns, text="导出 CSV/JSON", command=self.on_export)
        self.btn_export.pack(side="left", padx=6)
        self.btn_backup = ttk.Button(btns, text="备份", command=self.on_backup)
        self.btn_backup.pack(side="left")
        self.btn_restore = ttk.Button(btns, text="恢复…", command=self.on_restore)
        self.btn_restore.pack(side="left", padx=6)
        self.btn_folder = ttk.Button(btns, text="打开数据文件夹", command=self.on_open_folder)
        self.btn_folder.pack(side="right")

        self.lbl_db = ttk.Label(root, text=f"本地数据库：{db_path()}", foreground="#555")
        self.lbl_db.pack(fill="x", padx=12, pady=(0, 2))
        self.lbl_export = ttk.Label(root, foreground="#555")
        self.lbl_export.pack(fill="x", padx=12, pady=(0, 8))
        self._show_export_dir()

        self._action_buttons = [self.btn_detect, self.btn_sync, self.btn_refresh,
                                 self.btn_export, self.btn_backup, self.btn_restore]
        self.root.after(120, self._poll)
        self.root.after(200, self.on_refresh)

    # ---------------- 后台任务基础设施 ----------------
    def _busy(self, on: bool, allow_stop: bool = False) -> None:
        for b in self._action_buttons:
            b.config(state="disabled" if on else "normal")
        if not on and not self.ready:
            self.btn_sync.config(state="disabled")
        self.btn_stop.config(state="normal" if (on and allow_stop) else "disabled")

    def _log_put(self, msg: str) -> None:
        self.q.put(("log", msg))

    def _append_log(self, msg: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _run(self, fn, on_done, allow_stop: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请稍候", "已有任务在执行。")
            return
        self.stop_event.clear()
        self._busy(True, allow_stop=allow_stop)

        def wrap():
            try:
                result = fn(self._log_put)
                self.q.put(("done", (on_done, result)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"{e}\n{traceback.format_exc()}"))

        self.worker = threading.Thread(target=wrap, daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    on_done, result = payload
                    self._busy(False)
                    try:
                        on_done(result)
                    except Exception as e:  # noqa: BLE001
                        messagebox.showerror("错误", str(e))
                elif kind == "error":
                    self._busy(False)
                    self._append_log("[错误] " + payload.splitlines()[0])
                    messagebox.showerror("出错了", payload.splitlines()[0])
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    # ---------------- 动作 ----------------
    def on_detect(self) -> None:
        def task(log):
            self.ready = False  # 检测出错（如没连手机）时保持“立即同步”不可点
            return device.preconditions()
        def done(result):
            info, problems = result
            self.serial = info.serial
            self.ready = not problems
            lines = [f"{info.manufacturer} {info.model}  Android {info.android_release}",
                     f"欢太健康 {info.app_version or '未检测到'}   Root {'是' if info.rooted else '否'}   "
                     f"数据库 {'存在' if info.db_exists else '未找到'}"]
            if problems:
                lines.append("前置条件未满足：" + "；".join(problems))
                self.btn_sync.config(state="disabled")
            else:
                lines.append("就绪，可同步。")
                self.btn_sync.config(state="normal")
            self.lbl_conn.config(text="\n".join(lines))
        self._run(task, done)

    def on_sync(self) -> None:
        mode = self.range_mode.get()
        full = mode == "full"
        incremental = mode == "incremental"
        days = None
        if not full:  # “增量”也会用到天数：本地还没数据的指标按最近 N 天回填
            try:
                days = int(self.days_var.get())
                if days < 1:
                    raise ValueError
            except (tk.TclError, ValueError):
                messagebox.showwarning("天数不对", "“仅最近”的天数请填写 1 以上的整数，例如 7。")
                return
        if full and not messagebox.askyesno("确认", "同步全部可读历史会从头重新遍历，可能耗时较长"
                                            "（可随时“停止”，进度会保存可续传）。继续？"):
            return
        desc = "按天增量" if incremental else ("全部历史" if full else f"最近 {days} 天")
        self._append_log(f"—— 开始同步（{desc}）——")
        def task(log):
            serial = device.pick_device(self.serial)  # 同步刚才检测的那台手机
            log("确保 frida-server 运行…")
            device.ensure_frida_server(serial)
            # “增量”从本地最后一天起只取新数据（无本地数据的指标按“仅最近 N 天”）；
            # “全部可读历史”清空游标从头回填；“仅最近”按游标增量续传
            return sync_all(serial, days=days, reset=full, since_last=incremental, log=log,
                            should_stop=self.stop_event.is_set)
        def done(summary):
            ok = sum(1 for v in summary.values() if v >= 0)
            if self.stop_event.is_set():
                self._append_log(f"—— 已停止：{ok} 项已处理，进度已保存，下次同步会接着来 ——")
            else:
                self._append_log(f"—— 同步结束：{ok}/{len(summary)} 项完成 ——")
            self.on_refresh()
        self._run(task, done, allow_stop=True)

    def on_stop(self) -> None:
        self.stop_event.set()
        self._append_log("正在停止（当前页完成后停止，进度已保存）…")

    def on_refresh(self) -> None:
        def task(log):
            return query.health_status()
        def done(st):
            for i in self.tree.get_children():
                self.tree.delete(i)
            if not st.get("ok"):
                self.tree.insert("", "end", values=("（本地库尚不存在，请先同步）", "", "", ""))
                return
            for m in st["metrics"]:
                self.tree.insert("", "end", values=(
                    f"{m['label']}", m["rows_local"], m["last_data_day"] or "—", m["last_sync"] or "—"))
        self._run(task, done)

    def _show_export_dir(self) -> None:
        self.lbl_export.config(text=f"导出位置：{export_root()}（点“导出”时可更改）")

    def on_export(self) -> None:
        root_dir = filedialog.askdirectory(
            title="选择导出位置（将在其中新建 export-时间戳 文件夹）",
            initialdir=str(export_root()) if export_root().exists() else str(data_dir()),
            mustexist=False)
        if not root_dir:
            return
        save_setting("export_dir", root_dir)  # 记住，下次默认用这里
        self._show_export_dir()
        def task(log):
            log("导出中…")
            return export_all(fmt="both")  # 默认 = 刚保存的导出位置/export-时间戳
        def done(res):
            self._append_log(f"已导出到：{res['outdir']}")
            if messagebox.askyesno("导出完成", f"已导出到：\n{res['outdir']}\n\n打开该文件夹？"):
                try: os.startfile(res["outdir"])
                except Exception: pass
        self._run(task, done)

    def on_backup(self) -> None:
        def task(log):
            log("备份中…")
            return do_backup()
        def done(res):
            self._append_log(f"已备份：{res['backup']}（{res['size']} 字节）")
            messagebox.showinfo("备份完成", f"已备份：\n{res['backup']}")
        self._run(task, done)

    def on_restore(self) -> None:
        path = filedialog.askopenfilename(
            title="选择备份文件", initialdir=str(data_dir() / "backups"),
            filetypes=[("SQLite 备份", "*.db"), ("所有文件", "*.*")])
        if not path:
            return
        if not messagebox.askyesno("确认恢复", f"将用以下备份覆盖当前本地库（恢复前会自动备份当前库）：\n{path}\n\n继续？"):
            return
        def task(log):
            log("恢复中…")
            return do_restore(path)
        def done(res):
            self._append_log(f"已恢复：{res['restored_from']}")
            messagebox.showinfo("恢复完成", f"已从备份恢复。\n恢复前自动备份：\n{res['safety_backup']}")
            self.on_refresh()
        self._run(task, done)

    def on_open_folder(self) -> None:
        try:
            os.startfile(data_dir())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法打开", str(e))


def run() -> int:
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001
        print("无法启动图形界面：", e)
        return 1
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    BridgeGUI(root)
    root.mainloop()
    return 0
