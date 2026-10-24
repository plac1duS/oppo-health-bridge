# 核心指标字段映射（心率 / 睡眠 / 步数 / 血氧）

验证日期：2026-10-24（Asia/Shanghai）。来源：本人设备 `database.db`（欢太健康 6.9.37，SQLCipher-4）经 App 自身句柄只读取样，少量样本与行数核对。个人数值不入仓库，样本导出见本机 `exports/`（已被 Git 忽略）。

## 通用约定

- **时间戳**：点数据的 `*_timestamp` / `start_time` / `end_time` 一律为 **Unix 毫秒、UTC**。校准方法：取最新样本时间戳 /1000 换算为本地时间，与设备时钟对齐，可据此区分“秒/毫秒”。
- **时区**：查询默认按设备时区 **Asia/Shanghai (+08:00)** 显示；日汇总表另带 `timezone` 文本（如 `+0800`）= 记录发生时的本地偏移，`date` 为 **本地日历日 yyyyMMdd 整数**。跨午夜记录以完整记录的 start/end 为准。
- **记录身份 / 去重**：各表 `PRIMARY KEY` 即来源记录身份（见下）。`modified_timestamp`（ms）标识修订，`sync_status` / `updated` 为同步状态；增量同步应按 `modified_timestamp > 检查点` 取新增与修订，不能仅凭日期或整行哈希。
- **历史覆盖**：不同指标最早可读日期不同（分钟级明细保留较短，日汇总/逐点更长），以实际 `coverage` 查询为准。

## 心率

- **`DBHeartRate`**（逐点）：`heart_rate_value` **bpm**（整数）；`data_created_timestamp` ms UTC；`heart_rate_type` 0=连续/自动（绝大多数），另有 1/2/4/5 少量（运动/手动等，**含义待确认**）；`reliability` 可靠度整数。PK=(`ssoid`,`device_unique_id`,`data_created_timestamp`,`heart_rate_type`)。
- **`DBHeartRateDataStatTable`**（日汇总）：`date` yyyyMMdd；`timezone` 文本；`max_hr`/`min_hr`/`average_hr`/`rest_hr`/`walk_avg_hr`/`sleep_base_hr` 均 **bpm**。

## 睡眠

- **`DBSleepTable`**（分段）：`start_time`/`end_time` ms UTC（多为 60s 段）；`sleep_state` **已确认**：`2=深睡`、`3=REM`、`4=浅睡`、`5=清醒`（按分段分状态求和与当日汇总的深/浅/REM 分钟逐一吻合，已核对）；`sleep_type` 目前仅 0；`alg_origin_state` 算法原始态。PK=(`ssoid`,`device_unique_id`,`start_time`,`sleep_type`)。
- **`DBSleepDayStat`**（每夜汇总）：`date` yyyyMMdd（醒来日）；`sleep_in_timestamp`/`sleep_out_timestamp` ms UTC；`total_sleep_time`/`total_deep_sleep_time`/`total_lightly_sleep_time`/`total_rem_time`/`total_wake_up_time` 均 **分钟**（deep+light+rem=total 已核对）；`wake_count` 次数；`sleep_score` 睡眠评分（样本为 0，**真实评分来源待确认**，可能存 `DBSleepMainStat`/`sleep_main_data`）；`standard_time` 参考值（样本恒 420，疑 7h 基准，**待确认**）。PK=(`ssoid`,`date`)。

## 步数 / 运动

- **`DBSportDataStat`**（日/分段汇总，按 `date`×`sport_mode`）：`date` yyyyMMdd；`total_steps` 步；`total_distance` **米**（距离/步数 ≈ 0.7–0.8 m/步）；`total_calories` **卡 cal（÷1000=kcal）**；`total_duration` 疑 **毫秒（待确认）**；`timezone` 文本。`sport_mode`<0（-2/-3/-4）为**全天聚合**（同日这三者 total_steps 相同），0..9 为活动类型（**具体映射待确认**）。**当日总步数**取聚合行。PK=(`_id` 自增)。
- **`DBSportDataDetail`**（分钟明细）：60s 桶的 `steps`/`distance`(米)/`calories`(cal)；印证卡路里单位——静息分钟约 `1000 cal = 1 kcal/min`。PK=(`ssoid`,`device_unique_id`,`start_time`)。

## 血氧

- **`DBBloodOxygenSaturation`**：`blood_oxygen_saturation_value` **百分比 %**；`data_created_timestamp` ms UTC；`blood_oxygen_saturation_type` 1=连续监测、10=自动、3=手动点测（**1 与 10 的具体区别待确认**）。PK=(`ssoid`,`device_unique_id`,`data_created_timestamp`,`blood_oxygen_saturation_type`)。

## 待确认清单

HR type 1/2/4/5 与 SpO2 type 1/10 的精确含义；`sleep_score` 真实来源；`standard_time` 语义；`total_duration` 单位；`sport_mode` 代码→活动名；`metadata`/`sleep_main_data`/`extension` 等 JSON 字段内容；各表 `modified_timestamp` 作增量游标的完整性边界。以上不影响已验证的读取与基本解码，留待实现同步时逐项核对。
