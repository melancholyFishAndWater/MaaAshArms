# tools/sentry 程序流程

## 总流程

```mermaid
flowchart TD
    A[启动 tools.sentry.cli] --> B{子命令}
    B -->|fetch| F1[解析参数] --> F2[生成查询]
    F2 --> F3[Explore 并发与分页]
    F3 --> F4[转换中性字段] --> F5[组装 Artifact]
    F5 --> F6[写入 telemetry]
    B -->|task-failure/context| R1{数据来源}
    R1 -->|input| R2[load_artifact]
    R1 -->|在线| R3[选择 release 并查询]
    R2 --> R4[build_report]
    R3 --> R4
    R4 --> R5[任务/marker/运行/环境聚合]
    R5 --> R6[console/markdown/json]
    B -->|task-trend| T1[discover_versions]
    T1 --> T2[任务别名翻译] --> T3[按 name 查询]
    T3 --> T4[趋势聚合] --> T5[Delta pp/Wilson]
    T5 --> T6[输出]
    B -->|speed| S1[读取工件] --> S2[解析 duration]
    S2 --> S3[排序截取] --> S4[A..B 比较]
    S4 --> S5[输出/可选图表]
    B -->|evidence| E1[调查 issue/event]
    E1 --> E2[下载附件] --> E3[安全解包]
    E3 --> E4[拉取 trace logs] --> E5[去重排序]
    E5 --> E6[写 maafw.log/evidence.json]
    B -->|issue-resolve| I1[解析 tag/ref]
    I1 --> I2[提取 issue] --> I3[读取本地状态]
    I3 --> I4{--status?}
    I4 -->|是| I5[查询远端]
    I4 -->|否| I6[分类输出]
    I5 --> I6
    I6 --> I7{--apply?}
    I7 -->|是| I8[PUT 远端]
    I7 -->|否| Z[结束]
    I8 --> Z
    B -->|telemetry| L1[刷新 issue 索引] --> L2[离线匹配] --> Z
    B -->|flow| D1[转发 drawing] --> Z
    B -->|selftest| Q1[28 项纯逻辑检查] --> Z
    F6 --> Z
    R6 --> Z
    T6 --> Z
    S5 --> Z
    E6 --> Z
    Z --> X[参数错误=2；运行错误=1]
```

## 入口与公共派发

`python -m tools.sentry.cli <子命令>` 进入 `cli.py`。
入口先配置标准输出和错误流，再由 `REPORTS` 延迟导入对应的
`lib/<module>.py:main`。报告命令使用
`temp/telemetry/report.lock`。参数错误返回 2，运行错误返回 1。

## 报告流程

### fetch

1. 解析 target、period、preset 或手工 fields。
2. `preset_queries()` 生成查询。
3. `explore_all()` 并发执行查询，并负责游标分页和重复游标保护。
4. `to_neutral_rows()` 将 Sentry 字段转换为工件中性字段：
   `span.description`→`description`、`span.status`→`status`、计数列转换为
   `_count` / `_count_user`。
5. 组装 schema 1 `Artifact`，写入 `temp/telemetry/`。

### task-failure / context

- 离线：`load_artifact()` → 按 release 推断目标版本 → `build_report()`。
- 在线：选择最新 release，查询任务、状态、运行和环境数据。
- `build_task_rows()` 将正常任务与仅失败时上报的 marker 分开。
- `build_release_rows()` 计算运行失败率，`build_context_rows()` 聚合环境。
- 输出层支持 console、markdown、json；JSON 字段由 dataclass 契约决定。

### task-trend

1. `discover_versions()` 按数据量选择版本，再按版本号展示。
2. `task_alias_map()` 从 `assets/interface.json` 建立 `name`、`label`、
   `entry` 到遥测 name 的别名。
3. 将 `--task` 翻译为遥测 name，再拼 `span.description` 查询。
4. `build_trend_rows()` 聚合成功、失败、取消，计算 Δpp 和 Wilson 区间。
5. 输出趋势明细；版本选择失败时明确报错。

### speed

读取 `fetch` 工件中的速度 measurement，解析 duration、release 和任务名；
排除系统 span，排序并截取版本，按 `A..B` 比较两端。
两端样本都达到门槛时才输出方向；`--chart` 延迟写 PNG。

### evidence

调查 issue/event → 枚举事件附件 → 安全下载解包 → 拉取 trace logs → 去重排序
→ 写 `debug/maafw.log` → `reconcile_written()` 对齐清单和磁盘
→ 写 schema 1 `evidence.json`。单附件失败只标记 `attachment_failed`。

### issue-resolve

解析 tag、pre-push ref 或手动 tag 范围 → 从提交正文提取
Fixes/Closes/Resolves → 合并显式 issue → 读取本地状态。
只有 `--status` 联网查询，只有 `--apply` 写远端；hook 不阻断 push。

### telemetry / flow / selftest

`telemetry` 刷新本地 issue 索引并支持离线匹配；`flow` 转发到 drawing；
`selftest` 按固定 CHECKS 顺序运行 28 项纯逻辑检查。

## 不可改变的契约

- CLI 子命令、参数、退出码、报告 JSON 字段和工件 schema 1 不变。
- `report_common`、`telemetry_index` 的跨工具导出保持原导入路径。
- `explore()` 返回中性字段；release 解析取双 `@` 中最后一个版本匹配。
- Sentry/git 不可用时不阻止本地绘图；单附件失败不废整包。
