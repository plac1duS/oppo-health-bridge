"""设备连接检测与 frida-server 保障（经 adb + root）。

第一版复用仓库 .tools 下的 adb 与 frida-server 二进制（体积大，已被 Git 忽略；
后续可打包进安装包）。只做只读检测与推送/拉起 frida-server，不改动手机数据。
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from dataclasses import dataclass

from .config import PACKAGE, FRIDA_VERSION

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".tools"
ADB = TOOLS / "platform-tools" / "adb.exe"
FRIDA_SERVER_LOCAL = TOOLS / "frida-server"
FRIDA_SERVER_REMOTE = f"/data/local/tmp/oppo-health-bridge-frida-{FRIDA_VERSION}"


class DeviceError(RuntimeError):
    pass


@dataclass
class DeviceInfo:
    serial: str
    manufacturer: str
    model: str
    android_release: str
    sdk: str
    security_patch: str
    app_version: str
    rooted: bool
    db_exists: bool


def _adb(*args: str, serial: str | None = None, timeout: int = 30) -> str:
    if not ADB.exists():
        raise DeviceError(f"找不到 adb：{ADB}")
    cmd = [str(ADB)]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise DeviceError(f"adb {' '.join(args)} 失败：{p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def list_devices() -> list[str]:
    out = _adb("devices")
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def pick_device(serial: str | None = None) -> str:
    serials = list_devices()
    if not serials:
        raise DeviceError("没有已授权的设备。请用 USB 连接手机并允许调试。")
    if serial:
        if serial not in serials:
            raise DeviceError(f"未找到设备 {serial}；当前：{serials}")
        return serial
    return serials[0]


def _getprop(serial: str, key: str) -> str:
    return _adb("shell", "getprop", key, serial=serial)


def _su(serial: str, cmd: str, timeout: int = 30) -> str:
    return _adb("shell", f"su -c '{cmd}'", serial=serial, timeout=timeout)


def detect(serial: str | None = None) -> DeviceInfo:
    serial = pick_device(serial)
    rooted = False
    try:
        rooted = _su(serial, "id").find("uid=0") >= 0
    except DeviceError:
        rooted = False
    app_version = ""
    try:
        dump = _adb("shell", f"dumpsys package {PACKAGE}", serial=serial)
        for line in dump.splitlines():
            line = line.strip()
            if line.startswith("versionName="):
                app_version = line.split("=", 1)[1].split()[0]
                break
    except DeviceError:
        pass
    db_exists = False
    if rooted:
        try:
            ls = _su(serial, f"ls /data/data/{PACKAGE}/databases/database.db")
            db_exists = "database.db" in ls and "No such" not in ls
        except DeviceError:
            db_exists = False
    return DeviceInfo(
        serial=serial,
        manufacturer=_getprop(serial, "ro.product.manufacturer"),
        model=_getprop(serial, "ro.product.model"),
        android_release=_getprop(serial, "ro.build.version.release"),
        sdk=_getprop(serial, "ro.build.version.sdk"),
        security_patch=_getprop(serial, "ro.build.version.security_patch"),
        app_version=app_version,
        rooted=rooted,
        db_exists=db_exists,
    )


def app_pid(serial: str) -> int | None:
    try:
        out = _adb("shell", "pidof", PACKAGE, serial=serial)
    except DeviceError:
        return None
    out = out.strip()
    return int(out.split()[0]) if out and out.split()[0].isdigit() else None


def ensure_app_running(serial: str) -> int:
    pid = app_pid(serial)
    if pid:
        return pid
    # 拉起 App（DB 引擎由代理经 DelegateInit 注册，无需前台交互）
    _adb("shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
    import time
    for _ in range(10):
        time.sleep(1)
        pid = app_pid(serial)
        if pid:
            return pid
    raise DeviceError("欢太健康进程未能启动。")


def frida_server_running(serial: str) -> bool:
    try:
        out = _su(serial, f"pidof {Path(FRIDA_SERVER_REMOTE).name}")
        return bool(out.strip())
    except DeviceError:
        return False


def ensure_frida_server(serial: str) -> None:
    if frida_server_running(serial):
        return
    if not FRIDA_SERVER_LOCAL.exists():
        raise DeviceError(f"找不到 frida-server：{FRIDA_SERVER_LOCAL}")
    _adb("push", str(FRIDA_SERVER_LOCAL), FRIDA_SERVER_REMOTE, serial=serial, timeout=120)
    _su(serial, f"chmod 755 {FRIDA_SERVER_REMOTE}")
    # 后台拉起（nohup/setsid，断开 adb 后仍存活）
    _su(serial, f"setsid {FRIDA_SERVER_REMOTE} >/dev/null 2>&1 &")
    import time
    for _ in range(8):
        time.sleep(1)
        if frida_server_running(serial):
            return
    raise DeviceError("frida-server 未能启动。")


def preconditions(serial: str | None = None) -> tuple[DeviceInfo, list[str]]:
    """返回设备信息与未满足的前置条件列表（空=就绪）。"""
    info = detect(serial)
    problems: list[str] = []
    if not info.rooted:
        problems.append("设备未取得 Root（需要 su）。")
    if not info.app_version:
        problems.append("未检测到欢太健康，或无法读取版本。")
    if not info.db_exists:
        problems.append("未找到 database.db（需要 Root 且 App 至少同步过一次）。")
    return info, problems
