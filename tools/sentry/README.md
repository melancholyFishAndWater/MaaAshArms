# MaaAshArms 遥测分析工具

把 `assets/interface.json` 里配置的 Sentry 项目（`telemetry.sentry`）中已上报的遥测数据
下载到本地、做统计分析、输出结论。结构参照 M9A 的 `tools/sentry/`，统计口径与 M9A 一致，
但针对本仓库的 release 命名与版本发布节奏做了四处改动（见文末「与 M9A 的差异」）。

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
| `context` | 只看运行环境（os / environment）分布 | `7d` |
| `flow` | 把本地日志证据包渲染成流程图（唯一不联网的子命令） | — |
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
```

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
python tools/flow_cases.py --case D:/MaaAshArms/debug --list-cases
```

同一个证据包 + 同一套参数重复跑会**跳过**（记在 `temp/flow/manifest.json`），`--force` 才重画。
渲染器是 [`tools/flow_from_log.py`](../flow_from_log.py)（也可 `import` 调用：`render_flow()`）。

### 遥测侧的图片边界（重要）

**Sentry 里的失败附件拿不到 vision 识别帧**，原因有两条，都在 MFA 源码里：

- `TaskDiagnostics.EnumerateEvidenceFiles` 显式跳过 `vision/` 子目录，只收 `on_error/*.png`；
- 图片包有 `MaxImageBundleBytes = 5 MiB` 硬顶，而本机 `debug/vision` 是 1078 张 / **248.6 MB**，
  硬塞会让整包被丢弃（连现有的 `on_error` 图与日志一起丢）。

所以从遥测画出来的图，只能有**节点循环 + `on_error` 失败现场图**，识别时的画面格是空的。
要看识别帧得让复现的人手动导出（导出对话框的"包含 vision 图片"**默认不勾**，可按"最近 24 小时"
限流），再把导出包喂给 `flow --case`。

同理，附件里的 `maafw.log` 只有 **512 KB tail**（每文件；总计 1 MB），`sentry log list <trace-id>`
拿到的也是同一份 tail 文本。窗口起点早于日志首行时工具会标注「注意：日志仅含尾部」并印在图上，
别把那种图当成完整过程来读。

`task-failure` 报告在失败节点标记表后会打一行指向本命令的线索，但**不自动画图**——报告要能在
`--input` 工件上离线复现，而画图必须读本地证据，两者不能捆在一起。

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
python tools/ci/check_sentry_offline.py    # 同一套断言 + 渲染一份合成报告方便人眼看
```

自检覆盖 release 解析、任务表与失败标记分流、Δpp 方向、工件往返、三种渲染，以及
**流程图解析器的文本入参**（远程日志没有本地文件，只有正文）。它不需要 Pillow——画图才需要。

> 画图（`flow`）依赖 Pillow，而**发布包自带的解释器不含它**（`.python-runtime/` 与包内
> `python/` 都 `import PIL` 失败）。用装了 Pillow 的系统 Python 跑即可；绘图不进发布包。

联网后建议按这个顺序确认打通（第一条能同时验证登录、target 与 release 格式）：

```bash
python -m tools.sentry.cli fetch --name probe \
    --field release --field "count_unique(user)" --period 30d
```

工件里应能看到 `MaaAshArms@v1.0.0-beta.<N>` 形式的 release。若 `total_rows == 0`，
说明窗口内没有上报数据，或 `target` / `period` 写错。

## 已知限制

- **没有保留策略**：`fetch` 不会删除历史工件，需要时自己清 `temp/telemetry/`。
- **不做定时上报**：脚本只在人调用时运行，不常驻、不自动跑。
- **时间窗口受 Sentry 限制**：超过约 30 天绝对计数不可用；更早的数据由 Sentry 端保留策略决定，
  脚本不缓存历史。
- **配额**：失败附件每个约 384 KB（`failure_attachments_sample_rate: 1`）。读报告时若看到
  某天失败数暴增，先确认是不是一次启动失败后队列继续跑出一串失败。
