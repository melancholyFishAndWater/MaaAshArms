"""sentry 工具的实现模块都在这里，入口只有上层的 `cli.py`。

分层：`config`（配置）→ `report_common` / `evidence`（公共设施与证据包）→
`task_*_report` / `speed_*` / `telemetry_index` / `explore_csv` / `fetch`（子命令实现），
`selftest` 与 `evidence_lib`、`selftests` 是自检与证据包的内部包。
"""
