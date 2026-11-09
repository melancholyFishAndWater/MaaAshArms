# MaaAshArms 遥测分析工具

把 `assets/interface.json` 里配置的 Sentry 项目（`telemetry.sentry`）中**已经上报**的遥测数据
下载到本地、做统计分析、输出结论。只读已上报的数据，只出表格与结论；采集与上报在 MFA 侧。

- 模块分布、编写约束、该工具的功能取舍：[`AGENTS.md`](AGENTS.md)
- 统计口径、release/版本口径、证据包与反查、已知坑：[`docs/`](docs/)
- 改代码前要跑的校验清单：[`tools/validation/README.md`](../validation/README.md)

## 前置条件

1. **Sentry CLI**（[cli.sentry.dev](https://cli.sentry.dev/)，命令名 `sentry`，不是旧版 `sentry-cli`）：

   ```bash
   pnpm add -g sentry    # 或 npm install -g sentry
   ```

2. **登录一次**（OAuth device flow，需要本人浏览器确认）：

   ```bash
   sentry auth login
   sentry auth whoami     # 确认登录（whoami 不读库，只读库的命令在只读环境下会失败）
   ```

3. **受限沙箱下把凭据指向可写目录**：`sentry` 每次 `explore` 都要写 `~/.config/sentry/cli.db`，
   工作区外只读时会报 `Fatal: Error: unable to open database file`。把凭据复制进工作区再指过去即可：

   ```powershell
   copy "$env:USERPROFILE\.config\sentry\cli.db" temp\sentry-cfg\cli.db
   $env:SENTRY_CONFIG_DIR = "$PWD\temp\sentry-cfg"
   ```

Python 侧零第三方依赖——只用标准库和 `sentry` 命令行。只有画图（`flow`）需要 Pillow。

## 命令一览

唯一入口是 `python -m tools.sentry.cli <子命令>`（也支持 `python tools/sentry/cli.py <子命令>`）。

| 子命令 | 作用 | 默认窗口 |
| --- | --- | --- |
| `fetch` | 下载遥测聚合数据到 JSON 工件，可离线复用 | `7d` |
| `task-failure` | 单版本任务失败率深度体检 | `7d` |
| `task-trend` | 跨版本失败率趋势与 Δpp 对比 | `30d` |
| `speed` | 跨版本耗时趋势与重构前后对比（可出 small multiples 图） | 离线，读工件 |
| `context` | 只看运行环境（os / environment）分布 | `7d` |
| `evidence` | 把失败现场图与**那次运行**的日志拉到本地（联网；不画图，只打印命令） | `7d` |
| `explore-csv` | 把浏览器导出的 Explore CSV 还原成日志正文 + 清单（不联网） | — |
| `issue-resolve` | 按提交正文的 `Fixes/Closes/Resolves PYTHON-XX` 关闭 Sentry issue（`--install` 装 `pre-push` 钩子） | — |
| `flow` | 把本地日志证据包渲染成流程图（默认按本地遥测库反查问题） | — |
| `telemetry` | 本地遥测库：增量下载 issue 详情，并按本地日志反查对应的问题 | — |
| `selftest` | 零网络的纯逻辑自检 | — |

## 典型用法

```bash
# 1. 先看一眼最近什么情况：按失败次数排的任务表 + 失败节点标记
python -m tools.sentry.cli task-failure --limit 15

# 2. 找回归：最新版本里 Δpp 涨得最多的任务
python -m tools.sentry.cli task-trend --sort delta --limit 10

# 3. 单任务穿透
python -m tools.sentry.cli task-trend --task 训练所 --versions 5

# 4. 下载一份数据，之后离线分析（不再联网）
python -m tools.sentry.cli fetch --preset task-failure --period 7d
python -m tools.sentry.cli task-failure --input temp/telemetry/python-7d-20261014T090000Z.json

# 5. 任意自定义查询
python -m tools.sentry.cli fetch --name os_users \
    --field os.name --field "count_unique(user)" --sort "-count_unique(user)" --period 30d

# 6. 本地已有日志时，把某次失败画成执行流程图（落到 temp/flow/）
python -m tools.sentry.cli flow --case D:/MaaAshArms/debug --task-id 200000118

# 7. 只有浏览器导出的 Explore CSV 时：先还原成日志正文，再画
python -m tools.sentry.cli explore-csv D:/Downloads/Explore_2026-October-15_190806.csv
python tools/drawing/cli.py flow temp/flow/Explore_2026-October-15_190806.log --task-id 200000039

# 8. 某个失败要现场图：联网把证据拉下来（图 + 那次运行的日志），再照它打印的命令画
python -m tools.sentry.cli evidence --list                  # 先看哪些 issue 真有证据
python -m tools.sentry.cli evidence --issue PYTHON-14
python -m tools.sentry.cli flow --case temp/evidence/PYTHON-14/debug --task-id 200000062

# 9. 按版本批量出图：只画 beta.55 及之后的 issue（条件可重复，多条按「且」叠加）
python -m tools.sentry.cli flow --telemetry-bulk --telemetry-version ">=v1.0.0-beta.55"

# 10. 推送 tag 时自动关闭 issue：装 pre-push 钩子（默认只列清单；写完 Sentry 才用 --apply）
python -m tools.sentry.cli issue-resolve --install
python -m tools.sentry.cli issue-resolve --check
# 手动跑（不经过 push）：默认按最新的本地 v* tag，先出清单；--tag 可指定别的
python -m tools.sentry.cli issue-resolve
python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.71
```

## 常用参数

| 子命令 | 关键参数 |
| --- | --- |
| `fetch` | `--name` 工件名、`--preset <task-failure\|task-trend\|context\|all>`、`--period`、`--query`、`--field`（可重复）、`--sort`、`-o/--output`、`--target`、`--no-fresh` |
| `task-failure` | `--limit`、`--sort <failures\|rate\|total\|name>`、`--release`、`--min-users`、`--min-samples`、`--input`、`--format <console\|markdown\|json>`、`--sections` |
| `task-trend` | `--task`、`--versions N`（看最近几版）、`--sort <runs\|rate\|delta\|name>`、`--stable-only`、`--min-latest-runs`、`--input`、`--format` |
| `context` | 与 `task-failure` 同一套参数，默认 `--sections runtime`、按运行量排 |
| `speed` | `--input`（必需，读 `fetch` 工件）、`--last N`、`--task A,B`、`--compare A..B`、`--min-count`、`--compare-min-count`、`--noise-factor`、`--chart <png>`、`--format` |
| `evidence` | `--list` 只报告不下载、`--issue`、`--event`（历史事件，需配 `--issue`）、`--period`、`--limit`、`--max-events`、`--no-images` / `--no-log`、`--force`、`--outdir`、`--sort <newest\|oldest>`、`--keep-duplicates`、`--log-limit` |
| `explore-csv` | `--log-out`、`--json-out`、`--no-write` |
| `issue-resolve` | `--install`（写 `.git/hooks/pre-push`，默认 list-only）、`--install --apply`（写远端）、`--uninstall`、`--check`、`--tag NAME`（手动指定本地 tag，可重复；不传且没有 stdin 输入时按最新的本地 `v*` tag）、`--ref LINE`（可重复，模拟 pre-push 输入）、`--stdin-file`、`--issue SHORT-ID`（追加，可重复）、`--only`（只用 `--issue`，不扫提交范围）、`--org`、`--base-url`、`--issue-prefix`、`--force` |
| `flow` | `--case`（目录 / 多卷 zip / 日志文件）、`--task-id`、`--pid`、`--from` / `--to`、`--outdir`、`--all-tasks`、`--list-cases`、`--mermaid`、`--force`、`--clean-flow`（整目录清空后全量重画）/ `--no-prune`（增量但不删旧产物）、`--telemetry-offline` / `--telemetry-issue`、`--telemetry-version`（`>=` / `<=` / `==` 筛 issue 的版本，可重复）、`--evidence-dir`、`--telemetry-bulk`、`--fetch-evidence` / `--no-fetch-evidence`、`--log-level` / `--log-file` |
| `telemetry` | `--refresh`、`--case`、`--issue`、`--days`、`--gap`、`--keep-resolved`、`--period`、`--limit` |
| `selftest` | 无参数 |

参数的完整说明与口径见 [`docs/`](docs/)：统计口径与 `speed` 见 [`docs/methodology.md`](docs/methodology.md)，
`evidence` / `telemetry` / `explore-csv` 的细节见 [`docs/evidence.md`](docs/evidence.md)，
`issue-resolve` 的钩子边界见 [`docs/issue-resolve.md`](docs/issue-resolve.md)，
版本与 release 见 [`docs/release-version.md`](docs/release-version.md)，坑见 [`docs/pitfalls.md`](docs/pitfalls.md)。

## 产物落在哪

| 路径 | 内容 | 谁产生 |
| --- | --- | --- |
| `temp/telemetry/<project>-<period>-<UTC时间戳>.json` | `fetch` 的遥测工件（自描述，带 `schema`） | `fetch` |
| `temp/telemetry/issues/<SHORT-ID>.json`、`index.json` | 本地遥测库（issue 详情与索引） | `telemetry --refresh` |
| `temp/evidence/<issue>/debug/maafw.log` | 那次运行的 trace logs 拼出的日志 | `evidence` |
| `temp/evidence/<issue>/debug/on_error/<run_id>/*.png` | 失败现场图（按失败事件分目录） | `evidence` |
| `temp/evidence/<issue>/evidence.json` | 证据清单（闸门状态、失败字段、写了什么、可复制命令） | `evidence` |
| `temp/flow/<名字>.png`（`--mermaid` 另出 `.mmd`） | 流程图 | `flow` / `explore-csv` 提示的绘图命令 |
| `temp/flow/manifest.json` | 已画过哪些包（同包同参数会跳过） | `flow` |
| `temp/logs/<tool>_<时间戳>_<pid>.log` | 工具运行日志（`--log-file` 可改） | `telemetry`、`flow` |
| `temp/telemetry/report.lock` | 报告并发提示用的 pid 文件（建议性，不互斥） | 报告类子命令 |

`temp/` 已被 `.gitignore` 忽略，产物不会入库。

## 本地验证

零网络、零凭证：

```bash
python -m tools.sentry.cli selftest
python tools/validation/cli.py check-sentry-offline    # 同一套断言 + 渲染一份合成报告方便人眼看
```

联网后建议先确认打通（第一条能同时验证登录、target 与 release 格式）：

```bash
python -m tools.sentry.cli fetch --name probe \
    --field release --field "count_unique(user)" --period 30d
```

工件里应能看到 `MaaAshArms@v1.0.0-beta.<N>` 形式的 release。若 `total_rows == 0`，
说明窗口内没有上报数据，或 `target` / `period` 写错。
