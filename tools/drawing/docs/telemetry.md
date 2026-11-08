# 关联遥测问题（`--telemetry`，默认开）

画本地包时顺手在**本地遥测库**里反查这个包对应哪个线上问题（库怎么建、怎么清理见
[`tools/sentry` 的「`telemetry`：本地遥测库与反查」](../../sentry/docs/evidence.md#telemetry本地遥测库与反查)）。
查到后：

- 图上注记**最前面**加两行：`遥测问题 PYTHON-2X：12 次 · 最后 11-03 08:52 · v1.0.0-beta.61`、
  `遥测失败：Maa task failed: Escort (BaseTaskStopError/action)`；
- 产物文件名前缀用问题号（`PYTHON-2X_TaskEscort_task200001359_px33560.png`），比目录名可靠；
- 拿它的 `release` 当「疑似过时」基线（省掉"日志里没有资源版本"那次跳过）。

| 开关 | 作用 |
| --- | --- |
| 默认 | 联网刷新本地库（只补新增/`lastSeen` 变了的 issue），再在本地查 |
| `--telemetry-offline` | 只读本地库，不联网 |
| `--no-telemetry` | 完全不做这层关联 |
| `--telemetry-issue PYTHON-2X` | 指定问题号，不自动反查 |
| `--telemetry-only` | 只打印候选问题，不画图 |
| `--telemetry-days N` / `--telemetry-gap N` | 反查的保留天数 / 时间窗天数 |

**反查给候选、不给唯一答案**：一次运行常有多个失败事件，同一个包可能对应好几个 issue。
只有"恰好一个 `task_id` + `node_id` 全等"时才自动采用，否则取时间最近的并打提示。
刷新失败（没装 `sentry` CLI / 没登录）只提示一次，照常画图。
