# FileMover2

高性能文件监听与分片整理工具：监听源目录变更，同步至多个目标；按修改时间将文件分批归档到含时间戳的目录，支持异步、重试、去重、配置向导与一键启动。

## 特性
- 多映射监听：一个 `source` 对应多个 `targets`，实时镜像（可保留源）。
- 异步 + 工作池：`asyncio` 队列、线程池加速 I/O；可配置并发与重试退避。
- 自动整理：目标目录文件数超阈值时自动按批切分（含冷却时间、前缀）。
- 分片整理（多根目录）：`organize.target_dirs` 汇总多个根目录按 mtime 分批；支持保留源与去重策略。
- 批次命名模板：`name_template` + `timestamp_format` 自定义批次目录名（占位符 `{timestamp}` `{prefix}` `{index}`）。
- 去重策略：`skip | rename | overwrite`。
- 配置保护：校验、备份、恢复。
- 交互式向导与一键启动：`filemover2 config-wizard`、`filemover2 start`（记忆上次模式选择）。
- 路径规范化与 Windows 友好：建议使用正斜杠 `L:/...`，并内置规范化。
- 管理员模式（Windows）：`--admin` 可触发 UAC 提权。

## 快速开始
```powershell
pip install -e .
filemover2 config-wizard --out filemover.yaml
# 或设置环境变量后可省略 -c
$env:FILEMOVER2_CONFIG = "L:/Project/Python/FileMover2/filemover.yaml"

# 监听并同步（按配置的 use_async）
filemover2 watch

# 立即整理（按配置的 organize.target_dirs）
filemover2 organize-now
```

## 关键配置
- 监听映射（多目标）：
```yaml
watch:
  mappings:
    - source: "L:/Temp/Source"
      targets:
        - "L:/Temp/Move"
        - "L:/Temp/Move2"
```
- 多根目录整理与命名模板：
```yaml
organize:
  target_dirs:
    - "C:/path/A"
    - "C:/path/B"
  chunk_size: 1000
  prefix: "batch"
  name_template: "{timestamp}_{prefix}_part{index:02d}"
  timestamp_format: "%Y%m%d_%H%M%S"
  use_async: true
  dedup_strategy: rename
  keep_source: false
```

## 一键启动与模式记忆
- `filemover2 start`：
  - 自动解析配置（CLI > 环境变量 > 本地 `filemover.yaml`）。
  - 提供模式选择菜单（watch / organize / organize-now）。
  - 记忆上次选择，存储于 `APPDATA/FileMover2/state.json`。

## 管理员模式（Windows）
- 任一命令可加 `--admin` 触发 UAC 提权。已在管理员下则忽略。

## 目录缺失与容错
- 监听启动前会检查 `watch.mappings[*].targets` 并可一键创建。
- 整理时若 `organize.target_dirs` 中部分目录不存在，将以告警列出并自动跳过；若全部缺失，将温和退出并提示。

## 稳定性与校验
- 重试：默认 3 次，退避起始 200ms，指数翻倍至 5s 上限。
- 完整性校验：`verify_mode: size`（可设 `none`）。
- 去重策略：`skip | rename | overwrite`。

## 向导与保护
- `filemover2 config-wizard`：交互式生成与保存配置，路径规范化为正斜杠。
- `filemover2 config-validate`：验证配置。
- `filemover2 config-backup` / `config-restore`：备份与恢复。

## 新增：感兴趣事件过滤（interested_events）
在 `watch` 配置中可指定触发同步的事件类型：

```yaml
watch:
  interested_events: [created, modified, moved, deleted]
```

- 支持的事件：`created`、`modified`、`moved`、`deleted`
- 仅当事件属于上述列表时才会触发同步逻辑；未包含的事件将被忽略。

## 自动整理优化
- **多目录监控**：自动整理现在会监控所有相关目录，包括 `watch.mappings` 中的目标目录和 `organize.target_dirs` 中的目录。
- **独立冷却时间**：每个目录都有独立的冷却时间，避免一个目录整理后影响其他目录的自动整理触发。
- **分区溢出修复**：修复了异步整理中的填充逻辑，确保分区严格遵循 `chunk_size` 限制。

## 新增：文件日志（events.jsonl 和 failures.jsonl）
程序会在日志目录下追加两类 JSON Lines 文件，便于后续分析与重试：

- `events.jsonl`：记录重要文件事件与关键节点，例如创建/修改/移动/删除。
- `failures.jsonl`：记录反复重试后仍失败的文件操作（已存在）。

每一行是一条 JSON：
```json
{"ts":"2025-09-11T12:00:00.000Z","type":"created","path":"L:/Temp/Source/a.png","is_dir":false}
```

结合 `filemover2 retry-failures` 可对失败项进行后续重试处理。
