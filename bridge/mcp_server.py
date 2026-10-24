"""只读 MCP 服务（stdio，换行分隔 JSON-RPC 2.0，零第三方依赖）。

暴露 5 个只读工具，查询本地已入库数据：
  health_status / health_coverage / health_daily_summary / health_sleep / health_timeseries
不开放任意 SQL，不连接手机，凭证与原始加密数据永不经此返回。
stdout 仅用于协议消息；日志走 stderr。
"""
from __future__ import annotations
import sys
import json

from . import query
from . import __version__

PROTOCOL_DEFAULT = "2025-06-18"

TOOLS = [
    {
        "name": "health_status",
        "description": "本地数据库状态：各指标行数、同步游标时间、回填下限、最后同步与错误。先用它了解有哪些数据可查、新鲜度如何。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "health_coverage",
        "description": "各指标在本地的实际数据覆盖：最早/最晚时间、有数据的天数、行数、回填下限。区间内无记录=无数据（非 0）。",
        "inputSchema": {
            "type": "object",
            "properties": {"metric": {"type": "string", "description": "可选，限定指标 key，如 heart_rate/spo2/sleep_day/sport_stat"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "health_daily_summary",
        "description": "指定本地日期区间的每日汇总：步数、距离(km)、卡路里(kcal)、睡眠(深/浅/REM 分钟)、心率(rest/avg/min/max bpm)、血氧(avg/min %)。仅返回有数据的天。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "起始本地日期 YYYY-MM-DD"},
                "end": {"type": "string", "description": "结束本地日期 YYYY-MM-DD（含）"},
            },
            "required": ["start", "end"], "additionalProperties": False,
        },
    },
    {
        "name": "health_sleep",
        "description": "每夜睡眠：入睡/醒来时间、总时长与深/浅/REM/清醒分钟、醒来次数。默认最近若干夜，可按日期区间过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "可选，起始本地日期 YYYY-MM-DD"},
                "end": {"type": "string", "description": "可选，结束本地日期 YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "最多返回夜数，默认 31"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "health_timeseries",
        "description": "逐点时间序列（心率 heart_rate 或血氧 spo2），按本地日期区间返回，带分页(limit/offset)与截断提示。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["heart_rate", "spo2"]},
                "start": {"type": "string", "description": "起始本地日期 YYYY-MM-DD"},
                "end": {"type": "string", "description": "结束本地日期 YYYY-MM-DD（含）"},
                "limit": {"type": "integer", "description": "单页最大点数，默认 2000，上限 10000"},
                "offset": {"type": "integer", "description": "分页偏移，默认 0"},
            },
            "required": ["metric", "start", "end"], "additionalProperties": False,
        },
    },
]

DISPATCH = {
    "health_status": lambda a: query.health_status(),
    "health_coverage": lambda a: query.coverage(a.get("metric")),
    "health_daily_summary": lambda a: query.daily_summary(a["start"], a["end"]),
    "health_sleep": lambda a: query.sleep(a.get("start"), a.get("end"), a.get("limit", 31)),
    "health_timeseries": lambda a: query.timeseries(
        a["metric"], a["start"], a["end"], a.get("limit", 2000), a.get("offset", 0)),
}


def _log(msg: str) -> None:
    print(f"[oppo-health-bridge mcp] {msg}", file=sys.stderr, flush=True)


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(id_, result) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code, message) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    is_request = "id" in msg and mid is not None

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_DEFAULT
        _result(mid, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "oppo-health-bridge", "version": __version__},
            "instructions": "只读查询本地已同步的欢太健康数据。先调用 health_status 了解可用指标与新鲜度。",
        })
        return
    if method in ("notifications/initialized", "initialized"):
        return  # 通知，无需回应
    if method == "ping":
        if is_request:
            _result(mid, {})
        return
    if method == "tools/list":
        _result(mid, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            _error(mid, -32602, f"未知工具：{name}")
            return
        try:
            data = fn(args)
            _result(mid, {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}],
                "isError": False,
            })
        except query.QueryError as e:
            _result(mid, {"content": [{"type": "text", "text": f"查询不可用：{e}"}], "isError": True})
        except Exception as e:  # noqa: BLE001
            _log(f"tool {name} failed: {e}")
            _result(mid, {"content": [{"type": "text", "text": f"工具执行失败：{e}"}], "isError": True})
        return

    if is_request:
        _error(mid, -32601, f"未实现的方法：{method}")


def run() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _log(f"启动，等待 stdio JSON-RPC。版本 {__version__}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log("收到非法 JSON，忽略")
            continue
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001
            _log(f"处理消息异常：{e}")
    _log("stdin 关闭，退出。")
    return 0
