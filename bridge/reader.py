"""Frida 读取后端：挂接欢太健康进程，经 read_agent.js 执行只读 SELECT。

这是“采集后端”的第一个实现（活句柄）。后续离线解密后端可实现相同接口
（ensure/select/tables），上层同步引擎无需改动。
"""
from __future__ import annotations
from pathlib import Path

import frida

from . import device
from .config import PACKAGE

AGENT_DIR = Path(__file__).resolve().parent / "agent"
AGENT_JS = AGENT_DIR / "read_agent.js"


class Reader:
    def __init__(self, serial: str):
        self.serial = serial
        self._session = None
        self._script = None

    def __enter__(self) -> "Reader":
        pid = device.ensure_app_running(self.serial)
        source = frida.Compiler().build(str(AGENT_JS), project_root=str(AGENT_DIR))
        dev = frida.get_usb_device(timeout=8)
        self._session = dev.attach(pid)
        self._script = self._session.create_script(source)
        self._script.load()
        self._script.exports_sync.ensure()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._script is not None:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session is not None:
                self._session.detach()
        except Exception:
            pass

    def select(self, sql: str, max_rows: int) -> dict:
        return self._script.exports_sync.select(sql, max_rows)

    def tables(self) -> list[str]:
        return self._script.exports_sync.tables()
