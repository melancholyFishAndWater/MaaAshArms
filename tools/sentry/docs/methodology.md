# 统计口径与数据形状

README 只讲怎么用；本文件是**口径与数据形状**：数字怎么算出来的、Sentry 那边有哪些字段、
工件长什么样、`config.json` 每个字段干什么。要改分析逻辑先读这份。

## 统计口径

四块数据口径不同，不能混成一个「失败率」来读。

1. **所有计数按唯一 trace 去重**（Sentry 侧 `count_unique(trace)`）：一次运行内同一事件触发多次不重复计数。
2. **任务失败率** = `internal_error` 的 trace 数 / 总 trace 数。这是最接近「这个任务失败概率」的口径。
   附 Wilson 95% 置信区间；样本数少于 `min_samples`（默认 20）时标 `样本不足 n=N`，不给区间。
3. **失败节点标记**：只在失败时上报的内部 span（如 `HomeFlag`、`BaseReturnHome`）与系统级失败
   （控制器初始化失败、连接失败等）。它们没有成功样本，**只计触发次数，不参与失败率计算**；
   混进失败率排名会得出错误结论。
4. **运行失败率**：umbrella span（`mfa.task_run`）的失败占比，即「一次完整运行失败的概率」。
   按 release 原文分组显示，因为同一资源版本下可能有多条渠道 release。
5. **环比用百分点 Δpp**：格式 `10.0% (+7.5pp)` 表示相对更早一个版本上升 7.5 个百分点。
   **先看样本量**：`--min-latest-runs` 门槛以下的任务不进大表，进了表的也可能标着「样本不足」——
   榜单末尾个位数样本的 `+33pp` 是噪声，不是回归。这正是要显示 Wilson 区间的原因。
6. **时间窗口上限约 30 天**：`sentry explore` 对超过 30 天的窗口不再做真实聚合，只返回截断样本
   （不同 `--period` 拿到相同结果），此时**绝对计数不可用**。`--period` 超过 30 天时工具会向 stderr 警告一次。

`task-failure` 与 `task-trend` 都有两个覆盖 `config.json` 的门槛开关，用来现场调松紧：

| 开关 | 覆盖 | 什么时候用 |
| --- | --- | --- |
| `--min-users N` | `min_release_unique_users` | 自动选版本时把口径调严（只看有足够装机量的版本） |
| `--min-samples N` | `min_samples` | 想让更多行显示 Wilson 区间，或反过来把噪声全标成「样本不足」 |

```bash
# 只把样本量 ≥ 60 的行当可靠结论看
python -m tools.sentry.cli task-failure --min-samples 60
```

## 数据源与字段映射

MFA（MFAAvalonia）的实现在 `MFAAvalonia/Helper/TelemetryService.cs`，本工具依赖的 span 形状：

| span | 含义 | 关键列 |
| --- | --- | --- |
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
| --- | --- | --- |
| `task-failure` | `task_total`、`task_status`、`release_users`、`run_total`、`run_failed`、`runtime_os`、`runtime_env` | `task-failure`、`context` |
| `task-trend` | `task_total`、`task_status`、`release_users` | `task-trend` |
| `context` | `runtime_os`、`runtime_env` | `context` |
| `all`（默认） | 以上全部 | 全部 |

## `config.json` 字段

`tools/sentry/config.json`，换项目只改这个文件：

| 字段 | 说明 |
| --- | --- |
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

## `speed` 的设计

`speed` 读 `fetch` 取回的耗时聚合（`--field "p50(span.duration)"` / `p95` / `avg`），把
「任务 × 版本」这个二维矩阵变成可读的表，并支持**两版对比**与 **small multiples 图**。

取数命令（一次性联网；四个条件缺一不可，少一个口径就变了）：

```bash
python -m tools.sentry.cli fetch --name task_speed_op --period 30d \
    --query "span.op:mfa.task span.status:ok span.duration:<1800000" \
    --field span.description --field release \
    --field "count_unique(trace)" \
    --field "p50(span.duration)" --field "p95(span.duration)" --field "avg(span.duration)" \
    -o temp/speed/task_speed_op.json
```

- `--period 30d`：默认 7d 只覆盖最近 9 个版本，`--last 8` 之外的历史不在工件里；
- `span.op:mfa.task`：否则 55 个内部节点 span（TaskGift / TrainEnsureHome…）混进任务表；
- `span.status:ok`：`mfa.task` 的 `internal_error` 是毫秒级"快速失败"，混进 p50 会把正常耗时压低；
- `span.duration:<1800000`：排除"挂着不动几小时再继续"的运行（见 `pitfalls.md` 的 `avg`）。

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
