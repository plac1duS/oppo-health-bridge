"""欢太健康桥 · 配置与指标规格。

本地数据库默认位于 %LOCALAPPDATA%\\OPPOHealthBridge\\health.db（见设计草案）。
指标规格描述每张源表的主键、增量游标列（modified）和事件时间列，供同步引擎按
(modified, rowid) 做 keyset 增量，并按主键去重/合并修订。
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "com.heytap.health"
FRIDA_VERSION = "17.23.1"  # 必须与设备端 frida-server 一致


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "OPPOHealthBridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "health.db"


@dataclass(frozen=True)
class Metric:
    key: str            # 本地标识
    table: str          # 源表名
    pk: tuple[str, ...]  # 源表主键（记录身份，用于去重/合并修订）
    modified: str       # 增量游标列（修订时间，ms）
    time_col: str | None = None  # 事件时间列（用于“仅最近 N 天”回填过滤）
    time_kind: str | None = None  # 'ms' | 'yyyymmdd' | None
    label: str = ""     # 中文名

    @property
    def local_table(self) -> str:
        return self.table  # 本地保留源表名，忠实镜像


# 第一版优先四类核心指标 + 其日汇总与必要的用户/设备信息。
METRICS: list[Metric] = [
    Metric("heart_rate", "DBHeartRate",
           ("ssoid", "device_unique_id", "data_created_timestamp", "heart_rate_type"),
           "modified_timestamp", "data_created_timestamp", "ms", "心率逐点"),
    Metric("heart_rate_daily", "DBHeartRateDataStatTable",
           ("_id",), "modified_timestamp", "date", "yyyymmdd", "心率日汇总"),
    Metric("spo2", "DBBloodOxygenSaturation",
           ("ssoid", "device_unique_id", "data_created_timestamp", "blood_oxygen_saturation_type"),
           "modified_timestamp", "data_created_timestamp", "ms", "血氧"),
    Metric("sleep_segment", "DBSleepTable",
           ("ssoid", "device_unique_id", "start_time", "sleep_type"),
           "modified_timestamp", "start_time", "ms", "睡眠分段"),
    Metric("sleep_day", "DBSleepDayStat",
           ("ssoid", "date"), "modified_timestamp", None, None, "睡眠每夜汇总"),
    Metric("sport_stat", "DBSportDataStat",
           ("_id",), "modified_time", "date", "yyyymmdd", "运动/步数汇总"),
    Metric("sport_detail", "DBSportDataDetail",
           ("ssoid", "device_unique_id", "start_time"),
           "modified_time", "start_time", "ms", "运动/步数分钟明细"),
]

METRIC_BY_KEY = {m.key: m for m in METRICS}
