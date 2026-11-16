# 绘图与遥测联动工作流

面向维护者、日志分析人员与 AI 助手。所有命令从仓库根执行；示例中的问题号、任务号和目录须换成实际值。
本页描述已有能力，不自动下载数据、不执行设备任务、不修改任务配置。

- 绘图参数：[drawing README](../README.md)
- 遥测命令：[sentry README](../../sentry/README.md)
- 取证细节：[evidence](../../sentry/docs/evidence.md)
- 身份核验与完整度：[证据包与本地辅助](packs.md#本地-release-辅助默认开)

## 先选工作流

| 你要回答的问题 | 首选工作流 | 不应据此断言 |
| --- | --- | --- |
| 用户说某些任务没正常运行，给了完整日志 | A：本地运行诊断 | 没有失败 issue 就是正常 |
| 已知一个线上 issue，要看失败经过 | B：单问题取证与绘图 | 最新卡片与旧证据包必属同一次运行 |
| 巡检某版本所有线上问题 | C：批量 issue 绘图 | 输出覆盖了用户日志中的所有任务 |
| 遥测片段截断，手头另有完整日志 | D：本地辅助与完整度选择 | 时间相近或任务同名就属于同一次运行 |
| 只有浏览器导出的 Explore CSV | E：CSV 还原与绘图 | 还原能补回被截掉的内容 |
| 只想知道日志可能对应哪个 issue | F：只匹配、不画图 | 每个候选都已确认身份 |
| 修复完成，管理 issue 状态与图名 | G：修复状态联动 | “已修复待发版”意味着验收通过 |

**三种产物必须分开**：统计报告回答失败率和趋势；遥测图集围绕 issue 出图；本地诊断检查用户实际运行。
`fetch` 生成的聚合工件不是逐行日志，不能直接喂给流程绘图。

## A：本地运行诊断（用户反馈的首选）

### 1. 先确认来源与任务

同时检查 `logs/` 面板日志、`debug/maafw*.log`（含轮转）、`debug/agent/`、`vision/` 与 `on_error/`。
目录名不是资源版本证据，轮转文件名也不是其内容起止时间。不同实例、设备或进程的记录不能混成一次运行。

任务名先从 `assets/interface.json` 对应 `name / label / entry`，不要猜：

| 用户任务 | 遥测 name | pipeline entry |
| --- | --- | --- |
| 每日赠礼 | Gift | TaskGift |
| 武装押运 | Train | TaskTrain |
| 防卫局 | Escort | TaskEscort |
| 领取奖励 | Reward | TaskReward |
| 训练所 | Training | TaskTraining |
| 自动代理 | Replays | TaskReplays |

上表仅供定位，配置变化后以当前 interface 和运行日志为准。

### 2. 列任务，再精确选择运行

多个本地目录先用 [inspect](inspect.md) 一次扫描全部轮转与 agent 日志，再从输出中选择实际源文件：

```powershell
python tools/drawing/cli.py inspect D:/Downloads/logs D:/Downloads/debug
python tools/drawing/cli.py inspect D:/Downloads/debug --view tasks
python tools/drawing/cli.py inspect D:/Downloads/logs --view search --contains "任务失败" --context 2
```

```powershell
python tools/drawing/cli.py flow D:/Downloads/debug/maafw.log --list-tasks
python tools/drawing/cli.py flow D:/Downloads/debug/maafw.log --task-id 200000096 --pid Px7192 --list --no-telemetry
```

日志已轮转时，对实际包含该运行的文件执行上述命令。`task_id` 可跨进程复用，必要时同时限制 `--pid`、`--from` 和 `--to`。

### 3. 先保证本地任务出图，不以 issue 是否存在为门槛

```powershell
python tools/drawing/cli.py cases --case D:/Downloads/debug/maafw.log --task-id 200000096 --pid Px7192 --no-telemetry --outdir temp/flow-local-review
```

要检查一个日志中的全部任务，可用：

```powershell
python tools/drawing/cli.py cases --case D:/Downloads/debug/maafw.log --all-tasks --no-telemetry --outdir temp/flow-local-review
```

`--all-tasks` 按现有任务扫描能力列任务，不承诺发现没有节点记录的投递失败。仍须用面板任务清单补核。
单个目录含多份轮转日志时，不要假设 `--case <目录>` 会自动把所有轮转日志分别画出；先列文件并逐份指定。

### 4. 可选关联遥测

去掉 `--no-telemetry` 即启用关联；只读现有问题库用 `--telemetry-offline`。
先预览匹配可加 `--telemetry-only`。没有可靠 issue 身份也应保留本地诊断图。

### 5. 判断任务是否真的完成

逐项核对：面板开始/结束/停止记录 → task 结果 → 执行节点 → 识别帧/失败现场 → 业务完成标志。

- `ret=true` 不保证业务完成；停止任务也可能以成功收尾。
- Custom 回调异常不一定转换为框架动作失败，要同时读 agent stderr。
- “无奖励，跳过”是程序判据，不是游戏实际无奖励的独立证据。
- 用户报告业务异常但 task 成功时，仍需诊断，不等待遥测产生失败 issue。

## B：单问题取证与绘图

```powershell
python -m tools.sentry.cli evidence --list
python -m tools.sentry.cli evidence --issue PYTHON-XX
python -m tools.sentry.cli flow --case temp/evidence/PYTHON-XX/debug --task-id 200000096 --telemetry-offline --outdir temp/flow-issue-review
```

`evidence` 联网下载，不绘图；产物包括日志、现场图和 `evidence.json`。
先读取实际清单/任务列表，再填写任务号，不直接沿用示例数字。调查历史事件可用 `evidence --issue ... --event ...`。
当前卡片的标题、event_id、任务号或版本与包不符时，不能把旧包画成最新事件。

## C：批量 issue 绘图

```powershell
python -m tools.sentry.cli flow --telemetry-bulk --telemetry-version ">=v1.0.0-beta.73" --outdir temp/flow
```

固定本地问题集合重跑、不联网拉证据：

```powershell
python -m tools.sentry.cli flow --telemetry-bulk --telemetry-offline --no-fetch-evidence --outdir temp/flow
```

流程：本地 issue 库（必要时刷新）→ 版本筛选 → 找/下载证据 → 定位失败 task → 完整流程或概览卡 → 增量更新与清理。

注意：

- 默认增量，不为“重跑”习惯性加 `--force` 或 `--clean-flow`。
- `--telemetry-offline` 固定的是当前本地快照，不代表最新线上状态。
- 这是一组 issue 的图，不是所有执行任务的图。成功、取消、未上报业务异常可能不在集合内。
- 批量清理会删除不属于本次 issue 集合的旧批量产物；版本筛选也影响清理范围。
- 需要保留某次调查成果时另用 `--outdir`，不要把用户诊断图与滚动 issue 图集混放。无范围过滤时可用 `--no-prune`；它与版本/issue/task/进程/时间窗过滤及 `--clean-flow` 互斥。`--clean-flow` 也不能与 `--telemetry-only` / `--list-cases` 预览组合。
- 无可画节点、无法定位任务、身份错配时可退概览卡。概览卡不等于完整执行证据。

## D：本地辅助与更完整来源选择

```powershell
python -m tools.sentry.cli flow --telemetry-bulk --telemetry-offline --no-fetch-evidence --local-release D:/Downloads/二合一 --local-release D:/Downloads --outdir temp/flow-issue-review
```

`--local-release` 指向含 `debug/`、通常还有 `logs/` 的根，可重复；默认本地安装根仍作为兜底。
这只是辅助候选来源，不代表目录里的所有任务都会被绘制。

身份核验：

1. 遥测包可用：以完整时间戳的独特日志行逐字对账，并复核 task 归属；事件号可作本机身份信号。
2. 遥测包缺失/错配：当前事件号必须出现在本地面板日志，并在其前 10 秒内找到同入口唯一失败收尾。
3. 多个身份候选、只有同名成功任务或无当前事件证据时拒绝，不按同时间同名字猜。

核验后比较同 task、同窗口的真实 node-done 记录、步骤/事件覆盖、无法解析的截断条数；本地更完整才替换，持平保留原来源。
选定本地日志后使用对应图片；本地也无法绘制才尝试原包，均不可用时退卡。独立事件回退不使用错配包的版本、task 或附件。

详见 [本地辅助判据](packs.md#本地-release-辅助默认开)。找遍明确提供的目录与本地安装仍没有对应运行时，结论就是“现有材料缺失”，不拿历史运行替代。

## E：Explore CSV 还原与绘图

```powershell
python -m tools.sentry.cli explore-csv D:/Downloads/Explore.csv
```

按命令输出的实际日志路径，再运行 `drawing flow` 或 `cases`，先列任务再给 task_id。
CSV 应含逐行日志正文；失败率/耗时聚合表不能还原流程。被裁掉的日志片段不会凭空恢复。

## F：只匹配、不绘图

```powershell
python -m tools.sentry.cli telemetry --refresh
python -m tools.sentry.cli telemetry --case D:/Downloads/debug
```

或在 `cases` 命令中使用 `--telemetry-only`。匹配结果须区分可靠身份与仅节点名/时间邻近的候选。
刷新问题库需要网络与已登录的 Sentry CLI；纯离线分析不必刷新。

## G：修复状态与图名联动

提交正文使用 `Fixes / Closes / Resolves PYTHON-XX` 后：

- `issue-resolve` 从提交范围提取问题号；默认列清单，`--status` 核对远端，`--apply` 才写远端。
- 绘图可给相关产物加“已修复待发版”后缀，只影响名称，不等于重画或验收通过。
- pre-push hook 是否写远端，取决于安装时是否明确启用了 apply；普通调查不应运行写操作。

```powershell
python -m tools.sentry.cli issue-resolve
python -m tools.sentry.cli issue-resolve --status
```

具体边界见 [issue-resolve](../../sentry/docs/issue-resolve.md)。

## AI 与人工共用的收尾清单

1. 写清来源目录、实例、资源版本、运行时间、entry、task_id、进程；缺失字段注明未知。
2. 写清所用日志是本地完整记录、遥测片段还是合成步骤，不将历史事件冒充当前事件。
3. 区分调用成功、任务成功、业务完成；将停止、跳过、回调异常独立列出。
4. 失败原因须有日志/图像证据；“疑似”与“确认”分开，超时节点不一定是真因。
5. 对账面板任务清单与已出图任务；没有图的逐项说明：未选中、无 issue、被清理、身份错配、日志缺失或无法解析。
6. 查看工具运行日志与 manifest，确认实际采用的来源、步骤数、截断提示、回退原因和清理结果。
7. 调查请求只给结论与建议，不修改脚本、任务或用户原始证据；修复与重画按用户授权另行执行。

## 参数冲突与前置检查

冲突在联网、工具日志写入、目录创建和清理之前以参数错误退出（exit 2），不再按参数顺序静默覆盖。

- `flow` / `cases`：`--telemetry` 与 `--no-telemetry`、`--telemetry-refresh` 与 `--telemetry-offline` 互斥；关闭遥测不能同时指定仅匹配、问题号或刷新。
- `cases`：批量模式与关闭遥测互斥；显式添加本地辅助与禁用本地辅助互斥；下载证据的正反开关互斥；全部任务与指定 task_id 互斥；两种预览互斥。
- `flow`：`--list`、`--list-tasks`、`--telemetry-only` 三种输出模式互斥；关闭疑似过时检查不能同时指定其版本。
- `flow` / `cases`：关闭重复轮合并与显式配置 `--overlay-min` 互斥。
- `cases`：`--clean-flow` 必须配合 `--telemetry-bulk`；保留、清空与过滤/预览的限制见工作流 C。
- `evidence`：`--no-images` 与 `--no-log`、`--list` 与 `--force` 互斥；不取日志时不能显式给日志条数、排序或保留重复行参数。

依赖型规则只检查显式传入的参数（支持 `--key=value`），不因程序默认值报冲突。

## 实现入口索引

| 职责 | 入口 |
| --- | --- |
| Sentry flow 转发 | `tools/sentry/cli.py` 的 REPORTS |
| 联动编排与完整度选择 | `tools/drawing/lib/cases/cli.py` |
| 包身份与失败任务定位 | `tools/drawing/lib/cases/evidence.py` |
| 本地内容对账/独立事件核验 | `tools/drawing/lib/cases/local_release.py` |
| 渲染与增量 | `tools/drawing/lib/cases/runner.py`、`tools/drawing/lib/flowlog/render.py` |
| 遥测匹配 | `tools/sentry/lib/telemetry_index.py` |
| 证据下载 | `tools/sentry/lib/evidence_lib/` |
| 图名修复标记 | `tools/drawing/lib/cases/release_mark.py` |

实现索引用于查代码，不代替 CLI 帮助；参数变化时同时维护本页及对应工具 README。
