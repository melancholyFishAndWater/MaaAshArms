# MaaAshArms 遥测分析工具

把 `assets/interface.json` 里配置的 Sentry 项目（`telemetry.sentry`）中**已经上报**的遥测数据
下载到本地、做统计分析、输出结论。结构参照 M9A 的 `tools/sentry/`，统计口径与 M9A 一致，
但针对本仓库的 release 命名与版本发布节奏做了四处改动（见文末「与 M9A 的差异」）。

**边界**：只读已上报的数据（采集与上报在 MFA 侧、由 `assets/interface.json` 的 `telemetry.sentry` 决定）；
只出表格与结论，不画图（图在 [`tools/drawing`](../drawing/README.md)）；只在人调用时跑一次，
不常驻、不自动报警。同一份数据必须能用 `--input` 工件离线复现出同样的结论——这一条决定了下面「功能取舍」里的几条。

> **改完本目录的脚本要校验并修到全绿才结束**：清单见
> [`tools/validation` 的「改完脚本的验收」](../validation/README.md#改完脚本的验收)
> （`check_python` / `validate_refs` / `ruff` / `pyright`，加上本目录的 `selftest` 与 `check_sentry_offline.py`）。

## 功能取舍

| 不做 | 为什么 | 要它怎么办 |
|---|---|---|
| 失败报告里自动画流程图 | 报告要能在 `--input` 工件上离线复现，而画图必须读本地证据（`maafw.log` + 图片），两者不能捆在一起 | 报告末尾会给出线索；`evidence` 拉完证据会打印可复制的 `flow` 命令 |
| 取失败附件的 vision 识别帧 | MFA 的 `TaskDiagnostics.EnumerateEvidenceFiles` 显式跳过 `vision/`，图片包有 `MaxImageBundleBytes = 5 MiB` 硬顶（本机 `debug/vision` 是 1078 张 / 248.6 MB），硬塞会让整包被丢弃 | 让复现的人手动导出（勾「包含 vision 图片」，可按「最近 24 小时」限流），把导出包喂给 `flow --case` |
| 用 spans 上的 `failure.node` / `failure.stage` | 这两个字段只作为 Sentry Issue 事件的 tag 存在，在 spans 上恒为空 | `sentry issue list <target> --sort freq` + `sentry issue view <SHORT-ID>` |
| 定时上报 / 常驻监控 / 自动报警 | 工具只在人调用时运行；采集侧不受这里控制 | 看 MFA 面板与 Sentry 自己的 alert 规则 |
| 保留历史工件 | `fetch` 不删旧文件，也不缓存 Sentry 侧的历史 | 自己清 `temp/telemetry/`；窗口上限见「统计口径」第 6 条 |
| 搬运 M9A 的 `SKILL.md` | `.agents/` 被 gitignore，技能文件无法入库 | 等价内容就是本文档 |
| 把 `trace logs` 翻页取全 | Sentry CLI 限死 `--limit ≤ 1000` 且没有游标；把 newest 与 oldest 拼起来会在中间留洞，比截断更误导 | 默认取 `newest`（失败在运行末尾），`has_more` 写进 `evidence.json` |
| 枚举同一 issue 的其它事件 | `sentry issue view` 只给最近一次，`sentry issue events --json` 是精简视图、没有 context（拿不到 `attachment.*`），也没有批量接口 | 用 `evidence --event <event-id>` 直接指 |
| 把 Sentry CLI 的缓存库自动搬到可写目录 | 那等于由工具替人复制 OAuth 凭据（`cli.db` 里有 token 与 refresh_token），凭据会散落到 `temp/` 且不会自己清掉；而这个问题只需在受限沙箱下处理一次 | 照「前置条件」手动两条命令；报错时 `report_common.authority_error_hint()` 会把这两条打出来 |
| 让画图脚本直接吃 Explore CSV | 导出的正文是**按块切碎**的（每格约 3.3 KB，块首块尾都丢头去尾），格子顺序也不是时间顺序，直接拼会拼出假行 | 先过 `python -m tools.sentry.cli explore-csv <csv>`，再拿它产出的日志正文去画 |
| 还原被截断的行、猜丢失的内容 | 导出按字节裁过（实测 53 条 reco/action/node-done 读不出、104 行断头、1080 块碎片），猜出来的内容会变成假事实 | 只统计并提示；要完整证据用本机 `debug/` 目录 |
| 把 Sentry 按字节块切碎的续行并回原记录 | 实测收益接近零、还有反作用：`PYTHON-18` 落盘的 `maafw.log`（10698 行）有 968 条续行（`内存管理]释放了2.97 MB` / `Adb, resource=B 服…` / `-10-14 13:01:26.284]…` 这类不以 `[时间]` 开头的碎片），并回后 `steps` 仍是 34、轮数与框数不变、只多 2 条 `attempts`（168→170）、`truncated_skipped` 64→61；而 `PYTHON-16` 那份并回后 `truncated` 从 38 变 39——把碎片粘到一条**本来完整**的记录尾巴上会毁掉它（`reco`/`node done` 的正则都以 `$` 收尾）。**"某任务只画出 2 步"不是这个原因**：那 2 条 `PipelineTask node done` 是日志里真有的（`logs_truncated: true`，Sentry 只留 1 MB），968 条续行里没有一条含 `node done` | 要看某任务完整过程：拉更长的窗口（`--limit` 已到上限 1000）或本机 `debug/`；没有 `node done` 时画图会退到"由识别记录还原"模式 |
| 合并 `!!!OnEventNotify!!!` 与 `ctx_event_sink` 两份记录 | 同一次识别在日志里有两行，按行计数会翻倍；两份的字段也不完全一样 | 只计数，需要去重时按 `reco_id`（且注意它是进程内计数） |
| 把 `fetch` 的聚合工件喂给 `flow` / `flow_from_log` 画图 | 聚合行只有 `span.description` / `span.status` / `release` / `p50(...)`，**没有行时间戳也没有节点 id**，本来就不是日志；解析器只认 `[时间] …` 开头的 maafw 行，喂进去必然报「没有一行匹配日志格式」 | 要看单次运行的图走 `evidence --issue <ID>`（它把那次运行的 `trace logs` 写成 `debug/maafw.log`），之后 `flow --case temp/evidence/<ID>/debug` 正常出图 |
| 把「任务 × 版本」的速度矩阵画成一张多序列折线图 | 25 任务 × 39 版本塞进一张图没人能读出单条曲线；且版本轴跳号、每版样本量 5~33 不等，画出来会像"稳定演进" | `speed` 出 **small multiples**（每任务一格、共享 x 轴）+ 表格；表里每格带 `n`，样本不足显示 `n=N` |

## 前置条件

1. **Sentry CLI**（[cli.sentry.dev](https://cli.sentry.dev/)，命令名 `sentry`，不是旧版 `sentry-cli`）：

   ```bash
   pnpm add -g sentry    # 或 npm install -g sentry
   ```

2. **登录一次**（OAuth device flow，需要本人浏览器确认）：

   ```bash
   sentry auth login
   sentry auth status    # 确认已登录
   ```

> **受限沙箱下 `sentry` 会报 `Fatal: Error: unable to open database file`**：CLI 每次
> `explore` 都要写 `~/.config/sentry/cli.db`，而该路径在工作区外只读（`--no-fresh` 也
> 绕不开）。把凭据复制进可写目录再把 `SENTRY_CONFIG_DIR` 指过去即可——OAuth 的
> `token` / `refresh_token` 都在 `cli.db` 的 `auth` 表里，令牌未过期时不需要重新登录：
>
> ```powershell
> copy "$env:USERPROFILE\.config\sentry\cli.db" temp\sentry-cfg\cli.db
> $env:SENTRY_CONFIG_DIR = "$PWD\temp\sentry-cfg"
> ```
>
> 注意 `sentry auth status` 会读库、在只读环境下失败，但 `sentry auth whoami` 不读库、
> 能直接确认登录状态——判断"到底有没有登录"用 `whoami`。

Python 侧零第三方依赖——只用标准库和 `sentry` 命令行。

## 命令一览

统一入口 `python -m tools.sentry.cli <子命令>`，或直接 `python tools/sentry/<文件>.py`。

| 子命令 | 作用 | 默认窗口 |
|---|---|---|
| `fetch` | 下载遥测聚合数据到 JSON 工件，可离线复用 | `7d` |
| `task-failure` | 单版本任务失败率深度体检 | `7d` |
| `task-trend` | 跨版本失败率趋势与 Δpp 对比 | `30d` |
| `speed` | 跨版本耗时趋势与重构前后对比（可出 small multiples 图） | 离线，读工件 |
| `context` | 只看运行环境（os / environment）分布 | `7d` |
| `evidence` | 把失败现场图与**那次运行**的日志拉到本地（联网；不画图，只打印命令） | `7d` |
| `explore-csv` | 把浏览器导出的 Explore CSV 还原成日志正文 + 清单（不联网） | — |
| `flow` | 把本地日志证据包渲染成流程图（不联网） | — |
| `selftest` | 零网络的纯逻辑自检 | — |

典型用法：

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
python tools/drawing/flow_from_log.py temp/flow/Explore_2026-October-15_190806.log --task-id 200000039

# 8. 某个失败要现场图：联网把证据拉下来（图 + 那次运行的日志），再照它打印的命令画
python -m tools.sentry.cli evidence --list                  # 先看哪些 issue 真有证据
python -m tools.sentry.cli evidence --issue PYTHON-14
python -m tools.sentry.cli flow --case temp/evidence/PYTHON-14/debug --task-id 200000062
```

## 失败现场图与那次运行的日志（`evidence`）

`fetch` / `task-failure` / `task-trend` 只读聚合数据，附件与日志要单独取——`evidence` 就是这一步，
它把证据落到 `temp/evidence/<issue>/`，并打印能直接跑的绘图命令（**不自己画图**，与"报告不自动画图"一致）。

**两道 Sentry 侧的闸门，先看再下**（`--list` 只报告不下载）：

| 闸门 | 取值 | 含义 |
|---|---|---|
| `attachment.status` | `attached` / `no_evidence` | 有没有失败现场图。实测 2026-10-13~15 的 12 个 issue 里**只有 2 个**是 `attached` |
| `logs.status` | `captured` / … | 有没有**那次运行**的日志。实测 **12 个全是 `captured`**——所以没有附件的 issue 也能画，只是没有画面格 |

这两个字段在 `sentry issue view <ID> --json` 的 `event.context` 里，键名是**扁平的**
`attachment.status`（点号是字符串的一部分，不是层级）。没有批量接口，只能逐个 issue 看，
实测一次 ~7 s，扫 12 个约 90 s（所以有进度输出，也可以 `--issue` 指定）。

**产出刻意照真机 `debug/` 那棵树**（`flow --case` 与 `flow_from_log` 两条路都认）：

```
temp/evidence/PYTHON-14/
  debug/maafw.log                      ← sentry trace logs 拼出来的那次运行日志
  debug/on_error/2026.10.14-19.20.37.889_EntrustEnterMainLine.png   ← 附件 zip 里的原成员
  evidence.json                        ← 账目：两道闸门的状态、失败字段、重复行数、写了什么、可复制命令
```

```bash
python -m tools.sentry.cli evidence [--period 7d] [--limit 20] [--issue PYTHON-14] [--event <event-id>]
    [--outdir temp/evidence] [--list] [--force] [--no-images] [--no-log]
    [--log-limit 1000] [--sort newest|oldest] [--keep-duplicates] [--verbose] [--timeout 180]
```

三个把路子堵住的 CLI 事实（都实测过，工具已经绕开）：

- **`sentry api` 打不出二进制**（下载端点加 `--silent` 也是空 stdout）→ 工具自己带 Bearer
  `GET /api/0/projects/<org>/<project>/events/<event_id>/attachments/<id>/?download=1`；
  token 用 `sentry auth token --json` 拿（输出的是**裸 JSON 字符串**），**不必去读 `cli.db` 的 `auth` 表**。
- **`sentry trace logs` 的 `--limit` 上限 1000，且没有游标** → 长运行一次拿不全。失败发生在运行末尾，
  所以默认 `--sort newest` 只取最新那段；`--sort` 只认 `newest` / `oldest`（`-timestamp` 在 Windows 上会被当成参数别名报错）。
  拿到的条数、有没有更多（`has_more`）都写进 `evidence.json`。
- **批次之间有逐字节相同的行**（实测 5951 行里 1366 行重复，其中 737 行是 maafw 原生行：24 条 `node done` 里有 6 条是重复的）。
  来源是 MFA 续写日志时带的 `LogPreludeBytes = 64 KB` 前缀重叠，不去重会把图上未命中次数灌水 → 默认去重并记账，`--keep-duplicates` 可关。

**附件里只有 `debug/on_error/*.png`，没有 `maafw.log`**——而 `flow --case <目录>` 要求根下有 `maafw.log`，
所以直接解包喂 `--case` 会报"找不到 maafw.log"。工具把 `trace logs` 写进 `debug/maafw.log`，这条目录才成为完整证据包。
同一条 trace 可能同时挂着多个 issue（实测 PYTHON-14 与 PYTHON-16 就是同一趟运行的两次失败），
日志是**整个运行**的，所以标记可能来自同一运行里的别的任务——按 `--task-id <failure.task_id>` 才只看本次那个。

`evidence.json` 里还有 `failure.*` 那组字段（`task_id` / `node_id` / `message` / `duration_ms`）：**这就是
"失败卡在哪个节点"**——spans 上的 `failure.node` 恒为空（见「功能取舍」），但事件 context 里有，工具顺手记了下来。

**`evidence.json` 记的是"这个目录里有什么"，不是"本次下了什么"**（`written` 与 `hints` 都按这个口径）：
工具不删旧文件，所以 `--force --no-log` / `--force --no-images` 只重下一半时，上一趟的
`debug/maafw.log`、`debug/on_error/*.png` 仍在原地，清单会把它们一并记上并注明"原有的"。
换句话说，**照着清单里的 `hints` 走就一定画得出来**——清单说没有日志时才是真的没有。

## Explore CSV：浏览器导出的那份怎么用

Sentry 网页上「Explore → 导出 CSV」拿到的是 `timestamp,message` 两列的日志导出。三件事必须知道，
不然会得出错结论（都是实测 `Explore_2026-October-15_190806.csv`，3.35 MB / 934 行）：

1. **`timestamp` 不是行时间**，是这一批日志的**上报时刻**（UTC；整份文件只有 3 个不同值，每批约 310 行），
   批内也不按时间排。行时间只认行首的 `[2026-10-14 13:03:48.225]`。
2. **正文是按块切碎的**：`message` 每格约 3.3 KB，实测 934 格**每格首行都是半截行**、209 格末行是断头。
   所以既不能逐格解析，也不能直接把格子首尾相接——两格之间毫无关系时会拼出 `[DBGceeded]` 这种假行。
   `explore-csv` 只接回"上一行断头 + 本格首行能拼成完整日志行"的那 85 处，其余按碎片/断头计数。
3. **导出里没有图片**（`images: false`），所以画出来的图上不会有画面格；要看识别帧得用本机 `debug/` 目录。

```bash
python -m tools.sentry.cli explore-csv <csv> [--log-out PATH] [--json-out PATH] [--no-write]
```

产出两个文件，终端还会直接打印可复制的下一步绘图命令：

| 产物 | 默认路径 | 给谁用 |
|---|---|---|
| 日志正文 | `temp/flow/<csv 名>.log` | `flow_from_log.py` / `flow_cases.py --case`（按行内时间排好序，不用再排） |
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

```bash
python -m tools.sentry.cli task-failure --format markdown > temp/telemetry/report.md
```

`task-failure` 与 `task-trend` 都有两个覆盖 `config.json` 的门槛开关，用来现场调松紧：

| 开关 | 覆盖 | 什么时候用 |
|---|---|---|
| `--min-users N` | `min_release_unique_users` | 自动选版本时把口径调严（只看有足够装机量的版本） |
| `--min-samples N` | `min_samples` | 想让更多行显示 Wilson 区间，或反过来把噪声全标成「样本不足」 |

```bash
# 只把样本量 ≥ 60 的行当可靠结论看
python -m tools.sentry.cli task-failure --min-samples 60
```

## flow：从日志画执行流程图

`flow` 子命令把某次失败还原成"横向=轮次、纵向=轮内顺序"的流程图，产出落到 `temp/flow/`。
它读的是**本地证据**（`maafw.log` + `on_error/` + 可选 `vision/`），所以是唯一不联网的子命令。

```bash
# 本机安装根
python -m tools.sentry.cli flow --case D:/MaaAshArms/debug --task-id 200000118 \
    --from "2026-10-14 00:30:00" --to "2026-10-14 00:33:00"

# 别人给的导出包（多卷要一起传，会被当同一棵逻辑目录树）
python -m tools.sentry.cli flow --case log_20261014-part01.zip log_20261014-part02.zip --task-id 200000118

# 独立入口（等价），以及只列证据包
python tools/drawing/flow_cases.py --case D:/MaaAshArms/debug --list-cases
```

同一个证据包 + 同一套参数重复跑会**跳过**（记在 `temp/flow/manifest.json`），`--force` 才重画。
渲染器是 [`tools/drawing/flow_from_log.py`](../flow_from_log.py)（也可 `import` 调用：`render_flow()`）。

### 遥测侧的图片边界（重要）

**Sentry 里的失败附件拿不到 vision 识别帧**（为什么不硬塞见「功能取舍」），所以从遥测画出来的图
只能有**节点循环 + `on_error` 失败现场图**，识别时的画面格是空的。要看识别帧得让复现的人手动导出
（导出对话框的"包含 vision 图片"**默认不勾**，可按"最近 24 小时"限流），再把导出包喂给 `flow --case`。

同理，附件里的 `maafw.log` 只有 **512 KB tail**（每文件；总计 1 MB）。窗口起点早于日志首行时工具会标注
「注意：日志仅含尾部」并印在图上，别把那种图当成完整过程来读。

**但日志不只有附件那一条路**：`evidence` 用 `sentry trace logs <org>/<project>/<trace-id> --json`
取回**整次运行**的日志（实测一趟 523 条 / 5951 行，含 24 条 `PipelineTask node done` 与失败标记），
写进 `temp/evidence/<issue>/debug/maafw.log`，于是"节点循环 + 失败现场图"能一次拿齐。
限制是 `--limit ≤ 1000` 且没有游标：超长运行只能取最新那段（失败在末尾）。
这份日志里还带着 `Node.NextList` 事件，所以画出来的图上**每一步的父节点（本轮派发者）与它的候选表**也有
（见 [`tools/drawing` 的「解析口径」](../drawing/README.md#解析口径读图前先看这里)）——不需要本机 `debug/` 才能看父子链。

## speed：跨版本耗时趋势

`speed` 读 `fetch` 取回的耗时聚合（`--field "p50(span.duration)"` / `p95` / `avg`），把
「任务 × 版本」这个二维矩阵变成可读的表，并支持**两版对比**与 **small multiples 图**。

```bash
# 取数（一次性，联网）。四个条件缺一不可，少一个口径就变了：
#   --period 30d      默认 7d 只覆盖最近 9 个版本，--last 8 之外的历史不在工件里
#   span.op:mfa.task  否则 55 个内部节点 span（TaskGift / TrainEnsureHome…）混进任务表
#   span.status:ok    mfa.task 的 internal_error 是毫秒级"快速失败"，混进 p50 会把正常耗时压低
#   span.duration:<1800000  排除"挂着不动几小时再继续"的运行（见下面「已知坑」里的 avg）
python -m tools.sentry.cli fetch --name task_speed_op --period 30d \
    --query "span.op:mfa.task span.status:ok span.duration:<1800000" \
    --field span.description --field release \
    --field "count_unique(trace)" \
    --field "p50(span.duration)" --field "p95(span.duration)" --field "avg(span.duration)" \
    -o temp/speed/task_speed_op.json

# 看表（离线）
python -m tools.sentry.cli speed --input temp/speed/task_speed_op.json

# 只看最近 5 个版本 / 只看某几个任务
python -m tools.sentry.cli speed --input temp/speed/task_speed_op.json --last 5 --task Training,Gift

# 重构前后对比：哪几个任务真的变慢/变快
python -m tools.sentry.cli speed --input temp/speed/task_speed_op.json \
    --compare v1.0.0-beta.46..v1.0.0-beta.49

# 再画一张图（small multiples，每任务一格；需要 Pillow）
python -m tools.sentry.cli speed --input temp/speed/task_speed_op.json \
    --chart temp/speed/task_speed_op.png
```

三个关键设计，每条都对应一个实测事实（数据来自 30 天窗口、按上面的命令取到的 588 行 / 39 个版本，
去掉系统伪任务后 25 个业务任务共 434 个格子）：

1. **样本量门槛**（`--min-count`，默认 2）。p50 在 `n=1` 时没有稳定含义——实测 `Training`
   相邻版本的 p50 波动中位数 **95.4%**、最大 11737%，且 **61.5% 的格子是 n=1**。低于门槛的
   版本**显示 `n=N` 而不是耗时**，图上留白不连线。门槛是参数：用户量上来后调高即可。
2. **窗口截断**（`--last`，默认 8）。全量 434 行没人读；最近 8 版是实测里还能看到一些达标
   数据的窗口。版本按**版本号**排序（不是字典序，`beta.9` 不会排到 `beta.10` 后面），
   跳号版本（`beta.49` 之后没有 `beta.48`）保持在其位置。
3. **重构对比**（`--compare A..B`）输出 p50 比值与四种判定：**改善 / 变慢 / 噪声内 / 证据不足**。
   判定要**两侧都达到 `--compare-min-count`（默认 3）**才给方向——实测 `Gift` 从 128.4s 到
   801ms（0.01x）看着快了 160 倍，但两侧只有 n=4 / n=3，更可能是那几趟跑的不是同一关卡。
   把"证据不足"和"噪声内"分开是有出处的：FrontierRefactor 那篇基准里，1080 次重构有
   **21.9% 的"提升"其实落在测量噪声内**、19.3% 是真的变慢，两者必须分开报。

表格底部会报**样本密度**（`36/250 个格子达到 n≥2`）——当前装机量下样本极稀，这个数不写出来，
读者会以为空格是"没有这个版本"。

图用 **small multiples**（每任务一格、共享 x 轴）而不是一张多序列折线图：25 条序列塞进一张图
没人能读出单条曲线，这是业界处理多序列的标准做法（trellising）。只画**有 ≥2 个达标点**的任务，
按点数降序排；每格独立 y 轴、标出该格的极值。`v1.0.0-` 前缀在轴上省略。

> `speed` 的判定阈值（`--min-count` / `--compare-min-count` / `--noise-factor`）都是参数，
> **为用户量增长留了余地**：样本上来后把门槛调高、噪声阈值收紧到 1.1，报告结构不用改。
> 耗时阈值刻意**不读 `config.json`**——速度与失败率的口径不同（前者跟平台噪声，后者跟业务）。

## 统计口径

四块数据口径不同，不能混成一个「失败率」来读。

1. **所有计数按唯一 trace 去重**（Sentry 侧 `count_unique(trace)`）：一次运行内同一事件触发多次不重复计数。
2. **任务失败率** = `internal_error` 的 trace 数 / 总 trace 数。这是最接近「这个任务失败概率」的口径。
   附 Wilson 95% 置信区间；样本数少于 `min_samples`（默认 20）时标 `样本不足 n=N`，不给区间。
3. **失败节点标记**：只在失败时上报的内部 span（如 `HomeFlag`、`ReturnMain`）与系统级失败
   （控制器初始化失败、连接失败等）。它们没有成功样本，**只计触发次数，不参与失败率计算**；
   混进失败率排名会得出错误结论。
4. **运行失败率**：umbrella span（`mfa.task_run`）的失败占比，即「一次完整运行失败的概率」。
   按 release 原文分组显示，因为同一资源版本下可能有多条渠道 release。
5. **环比用百分点 Δpp**：格式 `10.0% (+7.5pp)` 表示相对更早一个版本上升 7.5 个百分点。
   **先看样本量**：`--min-latest-runs` 门槛以下的任务不进大表，进了表的也可能标着「样本不足」——
   榜单末尾个位数样本的 `+33pp` 是噪声，不是回归。这正是要显示 Wilson 区间的原因。
6. **时间窗口上限约 30 天**：`sentry explore` 对超过 30 天的窗口不再做真实聚合，只返回截断样本
   （不同 `--period` 拿到相同结果），此时**绝对计数不可用**。`--period` 超过 30 天时工具会向 stderr 警告一次。

## 数据源与字段映射

MFA（MFAAvalonia）的实现在 `MFAAvalonia/Helper/TelemetryService.cs`，本工具依赖的 span 形状：

| span | 含义 | 关键列 |
|---|---|---|
| `mfa.task_run` | 一次完整运行的根 span（op `mfa.run`） | `release` |
| `mfa.task` | 单个顶层任务，description = `interface.json` 里的任务 **label**（如 `训练所`） | `span.description`、`span.status`（`ok` / `internal_error` / `cancelled`） |
| `mfa.node` | 失败节点标记，只在失败时上报 | `span.description` |

`config.json` 的 `ignored_prefixes: ["mfa."]` 把 `mfa.task_run` 与 `mfa.node` 从任务表里排掉，
任务表只留 `mfa.task`；任务 label 不以 `mfa.` 开头，不会误伤。

**`failure.node` / `failure.stage` 拿不到**：它们只作为 Sentry Issue 事件的 tag 存在，在 spans 上恒为空。
需要看「失败卡在哪个节点」时走 Issue 通道：

```bash
sentry issue list 5e4d413b64a7/python --period 7d --limit 30 --sort freq \
    --json --fields shortId,title,count,userCount,firstSeen,lastSeen
sentry issue view <SHORT-ID>
```

其他可用维度：`release`、`environment`、`os.name`、`user.id`（匿名机器码 SHA256）、
`count_unique(user)`、`count_unique(trace)`。

## 遥测工件格式

`fetch` 落盘的 JSON 是自描述的，schema 版本写在 `schema` 字段里：

```json
{
  "schema": 1,
  "generated_at": "2026-10-14T09:00:00Z",
  "target": "5e4d413b64a7/python",
  "period": "7d",
  "measurements": {
    "task_status": {
      "fields": ["span.description", "span.status", "release", "count_unique(trace)"],
      "query": "",
      "sort": "-count_unique(trace)",
      "rows": [
        {
          "description": "训练所",
          "status": "ok",
          "release": "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38",
          "_count": 41
        }
      ]
    }
  }
}
```

落盘时 Sentry 的字段名会换成中性名（`span.description` → `description`、
`count_unique(trace)` → `_count`、`count_unique(user)` → `_count_user`、`os.name` → `os_name`），
所以读回时不依赖 Sentry 的字段拼写。默认路径 `temp/telemetry/<project>-<period>-<UTC时间戳>.json`；
`temp/` 已被 `.gitignore` 忽略，工件不会入库。

`--preset` 决定拉哪些 measurement：

| preset | measurement | 供哪个命令离线使用 |
|---|---|---|
| `task-failure` | `task_total`、`task_status`、`release_users`、`run_total`、`run_failed`、`runtime_os`、`runtime_env` | `task-failure`、`context` |
| `task-trend` | `task_total`、`task_status`、`release_users` | `task-trend` |
| `context` | `runtime_os`、`runtime_env` | `context` |
| `all`（默认） | 以上全部 | 全部 |

## `config.json` 字段

`tools/sentry/config.json`，换项目只改这个文件：

| 字段 | 说明 |
|---|---|
| `target` | Sentry 的 `<org>/<project>`。当前 `5e4d413b64a7/python` |
| `project_prefix` | 项目名，用于自动构造 release 正则（本仓库配了 `release_pattern`，此项仅作提示） |
| `release_pattern` | 自定义 release 正则，**必须恰好 5 个捕获组**（major, minor, patch, prerelease, prerelease_number）。刻意不带 `^`/`$` 锚点 |
| `task_run_spans` | umbrella span 名称，当前 `["mfa.task_run"]` |
| `ignored_prefixes` | 任务表里忽略的 span 前缀，当前 `["mfa."]` |
| `ignored_suffixes` | 任务表里忽略的 span 后缀，当前 `[".task_run"]` |
| `ignored_descriptions` | 按描述名忽略的 span，当前 `["connection_failed","controller_initialization_failed"]` |
| `system_labels` | 系统级/框架内部 span 描述 → 可读标签（`{原名: 显示名}`）。当前排掉 `连接`/`开始前脚本`/`性能基准`/`结束后脚本` |
| `min_release_unique_users` | 认定一个版本「有足够用户」的最低独立用户数，当前 1（真实装机量小） |
| `min_samples` | 展示失败率与区间所需的最低样本数，当前 10（真实每任务 7–17 次） |

## 与 M9A 的差异

四处改动，每一处都是照抄会静默出错或直接跑不动的地方：

1. **release 解析取「最后一个匹配」。** 本仓库的 release 是双 `@` 拼接
   `MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38`。M9A 的 `search()` 取第一个匹配，
   在 `project_prefix: "MFA"` 下会解析成 **MFA 版本 2.16.1**，于是同一资源版本被按客户端版本切碎，
   趋势全是假的。`report_common.release_match()` 改用 `finditer` 取最后一个匹配——
   拼接格式下它永远是资源版本，裸 release 下也是唯一匹配。
2. **`task-trend` 默认包含预发布版**，用 `--stable-only` 反向排除。本项目至今只发 beta，
   照抄 M9A 的「默认仅正式版」会让趋势表一个版本都选不出来。
3. **用户数门槛可调且默认调低**（`min_release_unique_users`，默认 3；M9A 硬编码 10）。
   找不到达标版本时不抛错，而是放宽到口径内出现过的全部版本并提示；`--stable-only` 下
   如果确实没有正式版，会明确报「该口径下一个版本都没有」，不会悄悄忽略这个开关。
4. **失败率附 Wilson 95% 区间**，样本不足直接标注。M9A 的 SKILL 只写了「小样本不可当真」，
   这里给出可量化的判据。

另外没有搬运 M9A 的 `.agents/skills/maafw-sentry-analysis/SKILL.md`——本仓库 `.agents/` 被
gitignore，技能文件无法入库；等价内容就是本文档。

## 真实数据校正：框架伪任务与版本选择

用 30 天真实数据跑过一遍后修了三处（2026-10-14，工件见 `temp/telemetry/python-30d-*.json`）：

1. **MFA 会报出一批界面不可选的框架 span**：`连接`（64 次）、`开始前脚本`（64）、
   `性能基准`（62）、`结束后脚本`（31）——**失败率恒为 0**。它们不匹配任何
   `ignored_prefixes`，会以"运行数最多 + 0% 失败率"占住排行榜头部。现在由
   `config.json` 的 `system_labels` 排掉（改这个文件即可，不用改代码）。同一原因加了
   `ignored_descriptions`（`connection_failed` 等系统级标记）。
2. **版本选择改「按数据量」而不是「按版本号最新」**。真实分布是 `beta.37` 有 64 次运行、
   其后四个版本各只有 1–2 次；按版本号选会把每个任务的有效样本从 9–17 次压到 1 次，
   算出来的 Δpp 全是噪声（下游版本的 Δpp 恰好是 β37 的相反数，一眼能看出是小样本抖动）。
   现在按"该版本运行数"降序选，`min_release_unique_users` 仍作门槛。
3. **`min_samples` 默认从 20 降到 10**：真实数据里最常跑的任务也只有 9–17 次，
   20 会让整张表都不给区间（区间反而比点估计更有信息，比如 `Training 9/14` 的
   `64.3%（95%CI 38.8%–83.7%）`）。20 仍可通过 `--min-samples` 或 config 调回。

### 真实数据的当前读数（beta.37，30 天）

| 任务 | 运行 | 失败 | 取消 | 失败率 |
|---|---:|---:|---:|---|
| Training | 14 | 9 | 5 | **64.3%**（95%CI 38.8%–83.7%） |
| Entrust | 11 | 4 | 4 | 36.4%（95%CI 15.2%–64.6%） |
| Gift | 9 | 2 | 0 | 22.2%（样本不足） |
| Escort | 9 | 2 | 2 | 22.2%（样本不足） |
| Reward | 7 | 1 | 0 | 14.3%（样本不足） |
| Friend | 13 | 1 | 3 | 7.7%（95%CI 1.4%–33.3%） |
| Mission / Factory | 10 | 1 | 4 / 0 | 10.0%（95%CI 1.8%–40.4%） |
| StartUp | 17 | 0 | 7 | 0.0%（95%CI 0.0%–18.4%） |
| Train | 10 | 0 | 4 | 0.0%（95%CI 0.0%–27.8%） |
| Maintenance | 9 | 0 | 2 | 0.0%（样本不足） |

读这份表的注意点：**取消数不能忽略**——`Training` 的 14 次里有 5 次是取消（手动停），
`StartUp` 17 次里 7 次取消。取消不是失败，但会缩小有效样本，所以工具把取消单列一栏。
据此可下的结论只有一条：**`Training` 的失败率显著高于其他任务**（区间下界 38.8% 仍很高）；
`Entrust` 的区间跨到 15%，属于"值得看但还不能定论"。`failure.node` 只存在于 Issue 事件
的 tag 上（spans 上恒为空），要看具体卡在哪个节点得走 `sentry issue list`。

## 本地验证

零网络、零凭证，校验 release 解析、任务表与失败标记分流、Δpp 方向、工件往返、三种渲染：

```bash
python -m tools.sentry.cli selftest
python tools/validation/check_sentry_offline.py    # 同一套断言 + 渲染一份合成报告方便人眼看
```

自检覆盖 release 解析、任务表与失败标记分流、Δpp 方向、工件往返、三种渲染，以及
**流程图解析器的文本入参**与**诊断行为**（时间戳回跳重排、只有识别事件时的合成、导出截断的行、
空窗口的三种说法、`--task-id` 严格过滤与任务判定窗口），再加 **Explore CSV 提取**（块边界接续、
五分类计数、任务可画性、截断按类型拆、入口报错）、**失败证据的纯逻辑**（解包白名单、附件名取 task_id、
去重、扁平 context 键、失败标记、可复制命令）、**证据清单与磁盘一致**（`--force --no-log` 留下的
日志/现场图仍进 `written` 与 `hints`）与**并发锁的持有者判据**（残留锁不提示且自动清掉、真并发仍提示）——
远程日志没有本地文件、只有正文，这几条都在遥测路径上。
它不需要 Pillow——画图才需要；也不需要网络与登录。

> 画图（`flow`）依赖 Pillow，而**发布包自带的解释器不含它**（`.python-runtime/` 与包内
> `python/` 都 `import PIL` 失败）。用装了 Pillow 的系统 Python 跑即可；绘图不进发布包。

联网后建议按这个顺序确认打通（第一条能同时验证登录、target 与 release 格式）：

```bash
python -m tools.sentry.cli fetch --name probe \
    --field release --field "count_unique(user)" --period 30d
```

工件里应能看到 `MaaAshArms@v1.0.0-beta.<N>` 形式的 release。若 `total_rows == 0`，
说明窗口内没有上报数据，或 `target` / `period` 写错。

## 读报告时的注意点

- **时间窗口受 Sentry 限制**：超过约 30 天绝对计数不可用；更早的数据由 Sentry 端保留策略决定，
  脚本不缓存历史（「功能取舍」里有对应条目）。
- **`avg(span.duration)` 不能当耗时用**：存在"挂着不动几小时再继续"的运行（实测某版本 `Friend` 的
  p95 = 9.9e6 ms，合出来的 `avg` 是 21.3 分钟而 p50 只有 6.4 秒）。全表 `avg/p50` 的中位数是
  1.0x 但**最大 2845x**——中位数正常只是因为大多数版本没有长尾跑。**只看 p50 / p95**，
  或取数时加 `span.duration:<1800000` 排除挂机跑（上面那条 `fetch` 命令已经带了）。
- **`--query` 按 release 过滤必须带通配符**：`release:MaaAshArms@v1.0.0-beta.37`、`release:beta.37`、
  裸写 `MaaAshArms@v1.0.0-beta.37` **都返回 0 行且不报错**，只有 `release:*beta.37` 命中（同窗口实测 83 行）。
  按版本切数据前先用 `--field release` 核对写法，否则会把"过滤没生效"读成"该版本没数据"。
- **配额**：失败附件实测 0.97 / 1.24 MB（`failure_attachments_sample_rate: 1`），12 个 issue 里只有 2 个带附件。
  读报告时若看到某天失败数暴增，先确认是不是一次启动失败后队列继续跑出一串失败。
- **受限沙箱下 `sentry` 一律 `Fatal: Error: unable to open database file`**（连 `--version` 也一样）：
  照「前置条件」把 `cli.db` 复制到可写目录并设 `SENTRY_CONFIG_DIR`。`evidence` 走的是同一个 CLI，同样需要它。
- **并发提示看锁文件里的 pid 还活不活**（`report_common.lock_owner_alive()`）：报告会往
  `temp/telemetry/report.lock` 写自己的 pid，下一条报告只在那个进程**还在**时才提示并发。
  持锁进程已退出（或锁是旧版本工具写的、读不出 pid）时：前者顺手删掉残留锁，后者照旧提示——
  这条锁只是建议性的，不做互斥，也不会拦截任何命令。
