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

## 按版本筛（`--telemetry-version`）

`--telemetry-days` 管的是「本地库留多久」，管不了「画哪些版本」。要只看某一段版本，用 `cases` 的
`--telemetry-version`（`python -m tools.sentry.cli flow` 是同一个实现，参数照传）：

```bash
# 只要 beta.55 及之后
python tools/drawing/cli.py cases --telemetry-bulk --telemetry-version ">=v1.0.0-beta.55"

# 只要 beta.55 ~ beta.60 这一段：两条条件按「且」叠加
python tools/drawing/cli.py cases --telemetry-bulk \
    --telemetry-version ">=v1.0.0-beta.55" --telemetry-version "<=v1.0.0-beta.60"

# 只要一条版本
python -m tools.sentry.cli flow --telemetry-bulk --telemetry-version "==v1.0.0-beta.52"
```

- **写法**：`[运算符]版本`。运算符 `==`（缺省）/ `>=` / `<=` / `>` / `<` / `!=`；版本写
  `v1.0.0-beta.61`、`1.0.0-beta.61`、`v1.0.0`，也接受整串 release
  （`MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61`，取其中的**资源版本**，不是 `MFA@` 那个客户端版本）。
- **排序口径**：`alpha < beta < rc < 正式版`，同档位比序号，与 `task-trend` / `speed` 的版本排序同一套
  （`tools/sentry/lib/report_common.py` 的 `release_version_key()`）。所以 `>=v1.0.0-beta.61` 也会命中
  `v1.0.0-rc.1` 与 `v1.0.0`；要排除它们再加一条上界。
- **作用范围**：批量出图遍历的 issue 集合、`--telemetry-only` 列出的候选，以及 `--case` 的反查结论。
  `--case` 明确给了的包，若反查到的问题版本不在范围内就整包跳过（终端写明命中的是哪个版本）；
  **只认已经确定身份的命中**（高置信、或显式 `--telemetry-issue`），中/低置信的节点名匹配只进注记，
  不会凭它丢包。对不上任何 issue 的包照画（版本无从判断），只是文件名不带问题号。
- **被排掉的会分开报数**：`库内 4 个 issue → 命中 1 个（0 个没有可解析 release、3 个版本不在范围内）`。
  `release` 里解析不出本项目版本的 issue **不算命中**——版本未知不能算落进范围。
- 条件写错（如 `>=1.0`、`=v1.0.0`）直接报错退出，不会静默当成"没有合条件的 issue"。
- 单日志的 `flow` 子命令没有这个参数：一份日志就是一个版本，没有可筛的集合。
