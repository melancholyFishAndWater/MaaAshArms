# tools/perf —— 性能基准（合成下限 + 真实日志）

两个子命令，回答的问题不同：

| 子命令 | 数据源 | 回答什么 | 判断真实优化收益 |
| --- | --- | --- | --- |
| `rounds` | 真实 `maafw.log` 的 `Node.NextList.*` / `Node.Recognition.*` | 一次派发要多久、命中前有几个失败候选、加权完整时间 | **用这个** |
| `bench` | 合成 pipeline + 内存帧 | MaaFW 的框架下限（不含真实识别、真机截图、`rate_limit`） | 只在换 MaaFW 版本/换机器时比 |

判断「改 pipeline 有没有变快」「真机上一轮多久」用 `rounds`；`bench` 的数字不能当成真机一轮的时间
（为什么见 [`docs/methodology.md`](docs/methodology.md)）。

结构与改法见 [`AGENTS.md`](AGENTS.md)；踩过的坑见 [`docs/pitfalls.md`](docs/pitfalls.md)。

## 真实日志：`rounds`

```bash
python tools/perf/cli.py rounds debug/maafw.log
python tools/perf/cli.py rounds debug/                          # 目录里所有 maafw*.log（含 .bak）
python tools/perf/cli.py rounds temp/evidence/PYTHON-2H/debug/maafw.log --x 2
python tools/perf/cli.py rounds debug/ --top 0                   # 派发者表列出全部
python tools/perf/cli.py rounds debug/ --module escort           # 只看 escort.json 这个模块
python tools/perf/cli.py rounds debug/maafw.log --mfa-log D:/MaaAshArms/logs   # 单独拷出来的日志
python tools/perf/cli.py rounds debug/maafw.log --no-pipeline    # 不读 pipeline，不核对也不补平均
python tools/perf/cli.py rounds debug/ --json temp/perf/rounds.json
python tools/perf/cli.py rounds --selftest
```

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `logs`（位置，可多个） | `debug/` | 日志文件、目录或通配；目录只收 `maafw*.log`（含 `.bak`），不给则扫 `debug/` |
| `--mfa-log` | — | MFA 界面日志目录（`logs/log-*.log`）；单独拷出来的 `maafw.log` 用这个指过去 |
| `--x` | `2` | 第三桶的失败候选数；`4+` 是合并桶 |
| `--max-ms` | `10000` | 超过这个耗时的窗口当挂起剔除 |
| `--min-samples` | `3` | 三个桶各自的最低样本数，不够不算完整时间 |
| `--module` | — | 只看某个模块：`pipeline/` 下的文件名去 `.json`（`escort` / `escort.json` 都行），可重复 |
| `--pipeline-dir` | `assets/resource/pipeline` | 当前 pipeline 目录，读顶层节点名用于核对与补平均 |
| `--no-pipeline` | — | 不读当前 pipeline，跳过核对与平均值补齐 |
| `--top` | `10` | 派发者表最多显示几个（`0` = 全部） |
| `--json` | — | 把结果写到这个路径 |
| `--log-level` / `--log-file` | `INFO` / 自动 | 工具日志（stderr + `temp/logs/`） |
| `--selftest` | — | 纯逻辑自检 |

一次实测（`debug/maafw.log`）：

```
文件：debug\maafw.log  派发 54（命中 52，未命中轮 2，剔除 >10000 ms 0）
      时间 2026-11-02 15:03 → 2026-11-05 15:39
      基线 时间点 f7959833（附近没有「资源版本」标记，按会话起点的 git 修订）
      改过 1 个派发者：EntrustBranchChapter（只标注，时长仍计入）

按「命中前失败候选数」分桶（窗口 52 个）：
   0 个：n=  26  中位      116 ms  p90     2832 ms
   1 个：n=  18  中位      768 ms  p90     4627 ms
   2 个：n=   6  中位     4262 ms  p90     5980 ms
  4+ 个：n=   2  中位     5309 ms  p90     5848 ms

完整时间 = t0×20% + t1×30% + t2×50% = 2385 ms（n=26/18/6）
整轮空转：命中前空转的窗口 0 个；整轮未命中 2 次（多数以任务结束/超时收尾）

派发者（按窗口总耗时排序）：
  TaskEntrust | 当前 是 | n=16 | 中位 158 ms | p90 4551 ms | 0/1/2+ = 12/0/4
  EntrustBranchChapter | 当前 是 | n=5 | 中位 5029 ms | p90 5980 ms | 0/1/2+ = 0/3/2
  ……
当前 pipeline 共 309 个节点，本次日志派发了 14 个；其余 295 个没跑到或新增（……；不参与统计）。
```

输出里每份日志三行：真实时间跨度、基线（资源版本或时间点 git 修订）、该基线之后改过的派发者
（只标注，不改数字）。多份日志、缺失节点补齐、`--module` 的详细口径见
[`docs/methodology.md`](docs/methodology.md)。

`--json` 里的字段：`buckets`、`weighted_ms`、`modules`、`dropped_windows`、`stale_nodes`、`nodes[]`
（`in_pipeline` / `imputed` / `stale` / `median_ms` / `raw_median_ms` / `p90_ms` / `fails_0` /
`fails_1` / `fails_2plus`）、`missing_in_pipeline`、`unseen_in_pipeline`、`imputed_avg_ms`、
`imputed_windows`；`files[]` 里每份日志另有 `span`（起止时间）与 `baseline`（`version` /
`source` / `updates` / `rev` / `stale` / `changed_modules` / `reason`）。

## 合成下限：`bench`

```bash
python tools/perf/cli.py bench 0                    # 三种都跑，出完整时间
python tools/perf/cli.py bench 1                    # 只测「瞬间命中」
python tools/perf/cli.py bench 2 3 --x 4            # 只测空转；模式 3 空转 4 轮
python tools/perf/cli.py bench 0 --rate-limit 1000  # 带上真实的 1s 等待
python tools/perf/cli.py bench --selftest
```

| 参数 | 含义 |
| --- | --- |
| `modes`（位置，可多个） | `0` = 三种都跑；`1` / `2` / `3` 可多选 |
| `--x` | `2` | 模式 3 的空转轮数 |
| `--repeat` | `5` | 每个模式重复次数（另跑 1 次预热丢弃），取中位数 |
| `--rate-limit` / `--pre-delay` / `--post-delay` | `0` | 毫秒；默认全 0（合成下限） |
| `--work-dir` | `temp/perf/时间戳-pid/` | 产物目录 |
| `--log-level` / `--log-file` | `INFO` / 自动 | 工具日志 |
| `--selftest` | — | 纯逻辑自检（不建资源、不连设备） |

一次实测：

```
MaaFW v5.12.2 · 720×1280 · rate_limit=0 pre_delay=0 post_delay=0 · x=2 · 重复 5 次（中位数）

模式 1  必定命中且瞬间触发  t1 =   26.6 ms  [25.8, 27.4]  n=5  帧 2
模式 2  next 空转 1 轮  t2 =   41.7 ms  [41.1, 43.2]  n=5  帧 3
模式 3  next 空转 2 轮  t3 =   65.0 ms  [60.0, 107.6]  n=5  帧 4

单轮空转：t2 − t1 = 15.1 ms；t3 − t1 = 38.4 ms ÷ 2 = 19.2 ms/轮
完整时间 = t1×20% + t2×30% + t3×50% = 50.3 ms
产物：temp/perf/20261105-204055-4872/
```

`bench.json` 里有同一批数的机器可读版本：`maafw`、`size`、`delays_ms`、`x`、`repeat`、
`modes[]`、`per_round_ms`、`weighted_ms`。

三种模式怎么构造的（帧序列、节点、模板）见 [`docs/methodology.md`](docs/methodology.md)。
