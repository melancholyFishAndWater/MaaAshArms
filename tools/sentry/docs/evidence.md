# 证据包：`evidence` / `flow` / `telemetry` / `explore-csv`

README 里这几条命令的**口径与边界**都在这份。改 `lib/evidence*.py`、`lib/telemetry_index.py`、
`lib/explore_csv.py` 前先读。

## `evidence`：失败现场图与那次运行的日志

`fetch` / `task-failure` / `task-trend` 只读聚合数据，附件与日志要单独取——`evidence` 就是这一步，
它把证据落到 `temp/evidence/<issue>/`，并打印能直接跑的绘图命令（**不自己画图**，与"报告不自动画图"一致）。

**两道 Sentry 侧的闸门，先看再下**（`--list` 只报告不下载）：

| 闸门 | 取值 | 含义 |
| --- | --- | --- |
| `attachment.status` | `attached` / `no_evidence` | 有没有失败现场图。实测 2026-10-13~15 的 12 个 issue 里**只有 2 个**是 `attached` |
| `logs.status` | `captured` / … | 有没有**那次运行**的日志。实测 **12 个全是 `captured`**——所以没有附件的 issue 也能画，只是没有画面格 |

这两个字段在 `sentry issue view <ID> --json` 的 `event.context` 里，键名是**扁平的**
`attachment.status`（点号是字符串的一部分，不是层级）。没有批量接口，只能逐个 issue 看，
实测一次 ~7 s，扫 12 个约 90 s（所以有进度输出，也可以 `--issue` 指定）。

**产出刻意照真机 `debug/` 那棵树**（`flow --case` 与 `flow_from_log` 两条路都认）：

```text
temp/evidence/PYTHON-14/
  debug/maafw.log                        ← sentry trace logs 拼出来的那次运行日志（`issue view` 的最新事件；
                                             `--event <id>` 时是指定的那个历史事件）
  debug/on_error/<run_id>/<时间>_<节点>.png  ← **按失败事件分目录**：一个 issue 常有多次失败，
                                            每次一份 `mfa-task-failure-<run_id>-<task_id>-screenshots.zip`，
                                            各存进自己的 `run_id/`，互不覆盖
  evidence.json                          ← 账目：两道闸门的状态、失败字段、重复行数、写了什么、可复制命令；
                                            其中 `event.release`（Sentry 那次运行的版本，形如
                                            `MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61`）会被 `flow --case` 直接
                                            当作「疑似过时」的版本基线——原生 maafw.log 没有「资源版本」那行，
                                            同装 MFA 界面日志也不会被下载，所以这是遥测图唯一可靠的版本来源；
                                            `events[]` 逐条记 run_id / event.id / received / task_id / 图
```

**现场图按失败事件逐个取，不是只取一个事件。** 一个 issue 常有多次失败、每次一份 on_error 包
（实测同一个 issue 有 3 份，其中同一任务 `200000003` 在 10-31 与 11-02 两次运行里都失败）；
`sentry issue view` 只回**一个**事件，只查它就会永远只有那一份。工具先用 `sentry issue events` 枚举
最近 `--max-events`（默认 10）个事件，再逐个查 `api …/events/<eid>/attachments/`，有图的下到
`debug/on_error/<run_id>/`；已下过的 run 跳过（增量，不重下、也不覆盖别的事件）。
只画某一次失败就 `--on-error temp/evidence/<issue>/debug/on_error/<run_id>`。

**默认取最新事件；历史事件用 `--event <id>`（必须配 `--issue`）。** `sentry issue view` 只给
最新事件，所以默认的 `debug/maafw.log` 就是最新那次的 trace；`--event` 走 `sentry event view <id>`
拿该事件的 trace 与失败字段，且**只下这一次**的现场图。事件变了会**重下**日志——不再复用旧事件的
`maafw.log`（否则 `evidence.json` 说最新事件、日志还是几周前的，ISSUES 第 1 条）。画图时顶部会注明
用的是哪个 `event.id` 与运行时间。

三个把路子堵住的 CLI 事实（都实测过，工具已经绕开）：

- **`sentry api` 打不出二进制**（下载端点加 `--silent` 也是空 stdout）→ 工具自己带 Bearer
  `GET /api/0/projects/<org>/<project>/events/<event_id>/attachments/<id>/?download=1`；
  token 用 `sentry auth token --json` 拿（输出的是**裸 JSON 字符串**），**不必去读 `cli.db` 的 `auth` 表**。
- **`sentry trace logs` 的 `--limit` 上限 1000，且没有游标** → 长运行一次拿不全。失败发生在运行末尾，
  所以默认 `--sort newest` 只取最新那段；`--sort` 只认 `newest` / `oldest`（`-timestamp` 在 Windows 上会被当成参数别名报错）。
  拿到的条数、有没有更多（`has_more`）都写进 `evidence.json`。
- **批次之间有逐字节相同的行**（实测 5951 行里 1366 行重复，其中 737 行是 maafw 原生行：24 条 `node done` 里有 6 条是重复的）。
  来源是 MFA 续写日志时带的 `LogPreludeBytes = 64 KB` 前缀重叠，不去重会把图上未命中次数灌水 → 默认去重并记账，`--keep-duplicates` 可关。

**附件 zip 里只有 `debug/on_error/*.png`，没有 `maafw.log`**——而 `flow --case <目录>` 要求根下有 `maafw.log`，
所以直接解包喂 `--case` 会报"找不到 maafw.log"。工具把 `trace logs` 写进 `debug/maafw.log`，这条目录才成为完整证据包
（现场图则按 run 写进 `debug/on_error/<run_id>/`，绘图侧递归扫目录，默认就能认）。
**单份附件下载失败不废整包**：SSL 中断之类只跳过那一份、记进 `attachment_failed` 并给出提示，
日志和其它事件的现场图照常落盘（以前一个附件失败会把整包带没）。
同一条 trace 可能同时挂着多个 issue（实测 PYTHON-14 与 PYTHON-16 就是同一趟运行的两次失败），
日志是**整个运行**的，所以标记可能来自同一运行里的别的任务——按 `--task-id <failure.task_id>` 才只看本次那个。

`evidence.json` 里还有 `failure.*` 那组字段（`task_id` / `node_id` / `message` / `duration_ms`）：**这就是
"失败卡在哪个节点"**——spans 上的 `failure.node` 恒为空（见 `methodology.md`），但事件 context 里有，工具顺手记了下来。

**`evidence.json` 记的是"这个目录里有什么"，不是"本次下了什么"**（`written` / `events` / `hints` 都按这个口径）：
工具不删旧文件，所以 `--force --no-log` / `--force --no-images` 只重下一半时，上一趟的
`debug/maafw.log`、`debug/on_error/<run_id>/*.png` 仍在原地，清单会把它们一并记上并注明"原有的"。
换句话说，**照着清单里的 `hints` 走就一定画得出来**——清单说没有日志时才是真的没有。

**复用旧包时会回显它属于哪个事件**（`提示：已存在（--force 重新下载）（event.id=…，received=…，release=…）`）：
以前只打一句"已存在"，照旧包出图却不知道画的是哪一次（踩过的坑）。事件对不上时不会复用日志，会重新取。

## `flow` 的遥测侧图片边界

**Sentry 里的失败附件拿不到 vision 识别帧**（为什么不硬塞见 AGENTS.md「功能取舍」），所以从遥测画出来的图
只能有**节点循环 + `on_error` 失败现场图**，识别时的画面格是空的。要看识别帧得让复现的人手动导出
（导出对话框的"包含 vision 图片"**默认不勾**，可按"最近 24 小时"限流），再把导出包喂给 `flow --case`。

同理，附件里的 `maafw.log` 只有 **512 KB tail**（每文件；总计 1 MB）。窗口起点早于日志首行时工具会标注
「注意：日志仅含尾部」并印在图上，别把那种图当成完整过程来读。

**但日志不只有附件那一条路**：`evidence` 用 `sentry trace logs <org>/<project>/<trace-id> --json`
取回**整次运行**的日志（实测一趟 523 条 / 5951 行，含 24 条 `PipelineTask node done` 与失败标记），
写进 `temp/evidence/<issue>/debug/maafw.log`，于是"节点循环 + 失败现场图"能一次拿齐。
限制是 `--limit ≤ 1000` 且没有游标：超长运行只能取最新那段（失败在末尾）。
这份日志里还带着 `Node.NextList` 事件，所以画出来的图上**每一步的父节点（本轮派发者）与它的候选表**也有
（见 [`tools/drawing` 的「解析口径」](../../drawing/docs/parsing.md)）——不需要本机 `debug/` 才能看父子链。

同一个证据包 + 同一套参数重复跑会**跳过**（记在 `temp/flow/manifest.json`），`--force` 才重画。
渲染器是 [`tools/drawing/lib/flow_from_log.py`](../../drawing/lib/flow_from_log.py)（也可 `import` 调用：`render_flow()`）。

## `telemetry`：本地遥测库与反查

用户发来的是**本地日志包**，而画图是离线的。两边唯一对得上的键是日志里的失败信息：
`Node.PipelineNode.Failed` 的 `name` / `node_id` / `task_id`，以及 `Task timeout` /
`node not completed` 的节点名；Sentry 事件 context 里正好有 `failure.task_id` / `failure.node_id`。
所以先把 issue 详情**增量**落一份本地库，画图时在库里查。

```text
temp/telemetry/issues/<SHORT-ID>.json   ← 每个 issue 一份（title/count/lastSeen/release/failure.*）
temp/telemetry/issues/index.json        ← {schema, issues: {<SHORT>: {status, lastSeen, fetched_at}}}
```

画图默认就带这层关联（`flow_cases` / `flow_from_log` 的 `--telemetry` 默认开）：
`--no-telemetry` 关掉，`--telemetry-offline` 只读本地库、不联网。

### 反查给的是候选，不是唯一答案

| 级别 | 判据 | 实测（30 个真实证据包） |
| --- | --- | --- |
| 高 | `task_id` **且** `node_id` 全等 | 25/30 能精确对上 |
| 中 | 节点名相同 + 时间差 ≤ `--telemetry-gap`（默认 3 天） | — |
| 低 | 只有节点名相同 | — |

**一次运行经常产出多个失败事件**（同一个 `BaseTaskStopError` 一小时里失败 4 次），所以
"这个包对应哪个 issue"本来就不唯一：30 个包里期望的 issue 有 26 个出现在候选里、17 个排第一。
工具因此列候选 + 级别，只有"**恰好一个**高置信"时才自动采用；否则取时间最近的那个并明确 warning。
要精确指定就用 `--telemetry-issue PYTHON-2X`。中/低置信只用于"提示有候选"：画图侧不拿它命名产物、
不拿它的 `release` 当「疑似过时」基线、也不往图上挂「遥测问题 / 遥测失败」注记
（判据 `telemetry_index.usable_match()`，2026-11-11 修过一次：低置信的 beta.68 被当成基线，
把当次 beta.73 的节点标成「疑似过时」）。

排序按 **级别 → 时间差 → 事件新旧**，刻意**不用 `count`**：热门问题会把真正的那次压下去。

### 清理（过时 / 已解决）

- `status` 是 resolved / ignored / archived → **删**（`--telemetry-keep-resolved` 才留）；
- `lastSeen` 早于 `--telemetry-days`（默认 14 天）→ 删；
- 不在最近一次 `issue list` 里 → 删；但列表条数到了 `--limit`（可能被截断）时**先留着**，不按缺席删；
- `index.json` 的 `schema` 变了 → 整库重建，不做迁移。

每一条清除都会在提示里带**原因**（`status=resolved` / `lastSeen 27 天前，超过 --telemetry-days 14` /
`服务端 30d 的列表里已经没有它`），新增/更新的那一批会列出问题号——批量出图的张数变了要能一眼看出
是"库被清了"还是"新问题进来了"。清单里本来就没有的问题（例如一直在保留期外被扫到的）不再每轮刷一条"清掉"。

不清理的后果很直接：一个已解决的旧 issue 留在库里，就会凭节点名抢走本该属于新 issue 的匹配。

### 默认开、失败不影响画图

刷新失败（没装 `sentry` CLI / 没登录 / 网络不通）只打一条提示，然后用本地已有数据；
本地库为空时再补一条"先跑 `telemetry --refresh`"。**遥测是加分项，不是画图的前提**。

## `explore-csv`：浏览器导出的那份怎么用

Sentry 网页上「Explore → 导出 CSV」拿到的是 `timestamp,message` 两列的日志导出。三件事必须知道，
不然会得出错结论（都是实测 `Explore_2026-October-15_190806.csv`，3.35 MB / 934 行）：

1. **`timestamp` 不是行时间**，是这一批日志的**上报时刻**（UTC；整份文件只有 3 个不同值，每批约 310 行），
   批内也不按时间排。行时间只认行首的 `[2026-10-14 13:03:48.225]`。
2. **正文是按块切碎的**：`message` 每格约 3.3 KB，实测 934 格**每格首行都是半截行**、209 格末行是断头。
   所以既不能逐格解析，也不能直接把格子首尾相接——两格之间毫无关系时会拼出 `[DBGceeded]` 这种假行。
   `explore-csv` 只接回"上一行断头 + 本格首行能拼成完整日志行"的那 85 处，其余按碎片/断头计数。
3. **导出里没有图片**（`images: false`），所以画出来的图上不会有画面格；要看识别帧得用本机 `debug/` 目录。

产出两个文件，终端还会直接打印可复制的下一步绘图命令：

| 产物 | 默认路径 | 给谁用 |
| --- | --- | --- |
| 日志正文 | `temp/flow/<csv 名>.log` | `tools/drawing/cli.py flow`（按行内时间排好序，不用再排） |
| 清单 | `temp/telemetry/<csv 名>.json` | 分析：`tasks`（含 `drawable`、`misses`、`stalled_at`）、`markers`、`panel_lines`、`processes`、丢失量 |

清单里 `recovered` 是还原账（行数、完整行、面板行、断头、碎片、接回几处、排序前回跳几处），
`parse` 是解析账（node done / 识别 / 事件 / 被截断的条数，**按记录类型拆**）。实测那份导出：
10744 行 = 完整 8785 + 面板 766 + 断头 104 + 碎片 1080，接回 85 处；31 条 node done 分属 3 个任务
（`200000039` 14 条、`200000056` 9 条、`200000013` 8 条）→ 三个都能画。

**`tasks[]` 里还有"卡住"的账**（`node_done` 少不等于没内容）：`window`（任务的活动窗口 =
它的 `Node.NextList` 记录首末 ∪ node done 首末 ∪ `task end`）、`misses` / `miss_first` / `miss_last`
（窗口内的识别未命中）、`stalled_at` / `stalled_misses`（最长一段"连续未命中、其间没有任何 node done"，
≥ 5 次才记）。命中这段时清单 `notes` 会点名，例如
「task 200000167 只有 2 步 node done，但 09:39:23 起有连续 88 次识别未命中……画它时要带 `--from/--to`」——
实测那个任务默认窗口只画得出末尾两步，真正的主体是 19 秒的候选全未命中。

**MFA 面板行单独统计**（`[ts][WRN] [cfg=Default] 中文消息`，级别括号后有空格）：截图方式不兼容、
跨平台解密失败、焦点协议失败这些只在面板里说，maafw 原生日志没有；画图时它们会被解析器跳过，
分析时要单独看 `panel_lines`。

报告有 `console`（默认）、`markdown`、`json` 三种输出；`json` 便于丢给别的工具二次处理。
