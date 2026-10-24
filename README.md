# 欢太健康桥 · OPPO Health Bridge

面向 OPPO Watch 4 Pro / 欢太健康的个人健康数据项目。

目标：让支持 MCP 的 AI 助手按需查询你自己的睡眠、心率、步数等记录，同时能够导出数据、长期保存。

**当前状态：第一版功能完成并真机验证——连接检测、增量同步、本地 SQLite 存储、标准化查询、只读 MCP 服务、CSV/JSON 导出、备份/恢复、图形界面（双击启动）全部可用。**

真机验证（2026-10-24，仅本人设备）：手机为小米 14（Android 16，已 Root，KernelSU），欢太健康 `com.heytap.health` 6.9.37。主数据库 `/data/data/com.heytap.health/databases/database.db`（约 400 MB）为 SQLCipher-4 加密、WAL 模式；口令由 App 的 `DBKey` 经 Android Keystore（AES-GCM）解包，无自定义 cipher PRAGMA。已通过 Frida 挂接运行中的 App、用其自身 SQLCipher 句柄读取，枚举出 103 张表并成功读取心率、睡眠、血氧、步数等指标（覆盖多年历史）。读取为只读，未修改源数据。详见[设计草案](docs/superpowers/specs/2026-10-24-oppo-health-bridge-design.md)与[调研资料](docs/research/2026-10-24-sources.md)。

## 第一版使用方式

- Windows 电脑本地运行，中文界面，数据保存在电脑上的 SQLite 数据库。
- 首次提供“同步全部可读取历史”和“仅同步最近数据”，可中断续传。
- 后续点击“立即同步”手动更新，从上次成功进度继续，补齐晚到记录与修订。
- 先通过 USB 连接本人 Root 手机检查兼容性，尽量自动识别型号、Android 与欢太健康版本。
- 数据采集优先验证手机本地读取；不兼容时再评估本人欢太账号云端记录的读取，并明确说明需要联网和账号授权。云端数据来源不等于云端托管数据库。
- AI 通过本机只读 MCP 查询已入库数据，手机断开后仍可查询；电脑关闭时此本地接口不可用。
- 优先验证睡眠、心率、步数、血氧；其他指标按实际可获得的数据逐项加入。
- 提供 CSV / JSON 导出。跨品牌软件同步后续按目标软件单独适配。

当前版本不包含云托管、手机后台常驻或每天 23:30 自动同步，自动化以后再考虑。没有开通 Supabase、其他托管服务或定时任务。使用云端 AI 时，MCP 查询出的数据仍会发送给该 AI 服务；本地存储不等于模型也在本地。

## 程序（第一版核心，开发中）

`bridge/` 为电脑本地读取与同步的核心包（采集后端=活句柄读取，见下）。已实现并真机验证：
设备检测、Frida 只读后端、按 `(modified, rowid)` keyset 的**增量/可续传**同步、按源主键去重合并修订、
忠实镜像源表结构入本地 SQLite。心率/睡眠/步数/血氧的样本与欢太健康 App 对照一致。

**图形界面（新手双击版）**：双击仓库根目录的 `启动健康桥.bat`（或 `run-gui.bat`），一个窗口里完成
连接检测 → 范围选择 → 立即同步（可停止/续传）→ 状态 → 导出 → 备份/恢复 → 打开数据文件夹。
若双击无反应，在终端运行 `python -m bridge gui` 查看报错。

命令行（等价功能）：

```bash
# 需先满足前置条件：USB+已授权、Root、frida-server(自动推送)、App 存活、手表已同步到 App
python -m bridge detect             # 检测设备与前置条件
python -m bridge sync --days 7      # 仅最近 7 天回填（首次）
python -m bridge sync --full        # 全部可读历史
python -m bridge status             # 本地库与各指标游标/行数
python -m bridge gui                # 打开图形界面
```

本地库默认 `%LOCALAPPDATA%\OPPOHealthBridge\health.db`。采集后端做成接口，离线解密后端可后续接入而不改上层。

### 只读 MCP 服务（供 AI 查询已入库数据）

```bash
python -m bridge serve-mcp          # stdio，换行分隔 JSON-RPC，零第三方依赖
```

工具：`health_status`（状态/新鲜度）、`health_coverage`（各指标实际覆盖范围）、
`health_daily_summary`（按日汇总步数/睡眠/心率/血氧）、`health_sleep`（每夜分期）、
`health_timeseries`（心率/血氧逐点，带分页）。只读、不开放任意 SQL、不连手机、不返回凭证或原始加密数据；手机断开后仍可查本地已入库数据。

支持 MCP 的客户端（如 Claude Desktop / Codex）配置示例：

```json
{
  "mcpServers": {
    "oppo-health": {
      "command": "C:\\path\\to\\oppo\\.venv\\Scripts\\python.exe",
      "args": ["-m", "bridge", "serve-mcp"],
      "cwd": "C:\\path\\to\\oppo",
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

### 导出与备份

```bash
python -m bridge export --format both            # CSV（按指标拆分）+ JSON（嵌套汇总）+ manifest
python -m bridge export --start 2026-10-01 --end 2026-10-24
python -m bridge backup                          # 一致快照（sqlite backup API，非文件复制）
python -m bridge backups                         # 列出备份
python -m bridge restore <备份文件> --yes        # 覆盖恢复（恢复前自动备份当前库）
```

导出只读本地库、不改源数据；CSV 用标准转义并带单位，JSON 保留嵌套结构；逐点序列在 CSV。备份/恢复用 SQLite 在线备份保证一致，恢复可逆（自动留 `pre-restore-*` 备份）。默认输出在数据目录 `%LOCALAPPDATA%\OPPOHealthBridge\{exports,backups}`。

运行依赖见仓库 `.tools`（已忽略：adb、frida-server）与 `bridge/agent`（`npm install` 装 frida-java-bridge）。后续可打磨：`sync --full` 完整历史回填、`sport_mode` 代码表与运动轨迹/心电、离线解密后端、打包为免 Python 环境的安装包。

## 文档

- [项目设计草案](docs/superpowers/specs/2026-10-24-oppo-health-bridge-design.md)
- [核心指标字段映射（心率/睡眠/步数/血氧）](docs/research/2026-10-24-field-mapping.md)
- [上游项目与官方资料](docs/research/2026-10-24-sources.md)

用户最新决定：电脑本地保存、手动同步、保留 MCP 查询，自动同步后置。这一决定替代此前的手机定时与托管云方案。手机兼容性尚待连接检测。

## 参考项目

- [foxlesbiao/oppo-health-cloud-mcp](https://github.com/foxlesbiao/oppo-health-cloud-mcp)：云端读取与 MCP。
- [foxlesbiao/oppo-health-export](https://github.com/foxlesbiao/oppo-health-export)：手机本地数据库导出。

本仓库为本项目自行实现的代码与设计文档，未复制或执行以上项目代码。后续若复用实现，保留对应许可证、版权声明和来源记录。

## 许可证

本项目以 MIT 许可证开源，见 [LICENSE](LICENSE)。仅供个人学习与研究，使用时请遵守相关设备与 App 的条款；读取的是你自己设备上的个人数据，请勿用于他人数据或商业用途。
