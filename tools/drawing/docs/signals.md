# 图上的信号：agent 缺位、失败框、缩略图角标、报错

## agent 缺位（自动标，没有开关）

日志里出现下面任意一条 ERR，就说明框架引用了 Custom 回调名、但**没有任何进程注册它**：

| 来源 | 行 |
| --- | --- |
| `CustomRecognition.cpp` `L33` | `recognition is null [name_=<节点>] [param_.name=<Reco 名>]` |
| `CustomAction.cpp` `L19` | `Action is null [node_name=<节点>] [param.name=<Act 名>]` |

只在 agent 缺位时出现，所以是硬证据。命中的两条按"ERR 在前"挂到受影响的记录上：

| 缺位类型 | 图上表现 |
| --- | --- |
| 识别未注册 | 那条 `algorithm=Custom`、`box=null` 的记录标 `agent 缺位`（实测 ERR 之后约 30 ms），通常并进「未命中（聚合）」框 |
| 动作未注册 | 那一步（`completed=false`）的 detail 后标 `agent 缺位`（实测 `EntrustInit` 之后约 200 ms） |

标题注记给"多少处、哪些节点"；全部命中都落在同一段缺位区间时，再补一句区间
（`MaaAgentClientDestroy` 之后没有 `MaaAgentClientConnect` 返回，就是"销毁了却没重连"）。

**反向不成立（也印在图的注记里）**：没有 `agent 缺位` 标记**不代表** agent 在场——回调注册了但抛异常
或返回 false 时框架**不打**这两条 ERR（源码 `CustomAction.cpp:18-21`、`CustomRecognition.cpp:32-35`
只在回调指针为空时打），那种情况只有 `debug/agent/agent.log` 能说清。

三个**不要**当成 agent 缺位的反例：

1. 只有 `[pretask.reco_timeout=…]`、一个 Custom 参数都没有 → 纯识别超时；
2. agent 在场但回调自己返回 `false` → 就是上面那条反向情形，不打 ERR；
3. `failed to get_reco_result [reco_id=…]`（`MaaTasker.cpp:359`）单独出现 → 多份日志里成百条地出现，
   噪声太大，只能当辅证。

实现上的两个坑（都写在代码注释里）：`name_` 是**节点名**而不是注册名（已与 pipeline 的
`custom_recognition` 对照），所以归属不用绕注册名；`clear_custom_registration` **不是**断开信号，
它是每次 `MaaAgentClientConnect` 的准备工作（本机实测 23 次入口对应 22 次 Connect），拿它当
"断开"会误报。

## 失败框的原因与证据等级

错误框优先显示原因，标题和明细自动换行，避免现场缩略图挤掉节点名或诊断。

- `识别超时 <节点>`：有框架 `Task timeout` 日志，明细保留实际耗时和 timeout 数值；截断数字明确标注。
- `疑似识别超时 <节点>`：仅有终止失败事件，但终止前同任务、同进程、同派发者的同候选表连续至少两轮全未命中，跨度至少 1 秒且最后一轮距失败不超过 3 秒。明细显示轮数、观测跨度及“缺少明确超时日志”，不把推断当事实。
- `节点失败（原因未知）<节点>`：只有通用 `PipelineNode.Failed`，无足够原因证据；不再只写“失败 + 节点名”。
- 动作失败、动作未派发、invalid node id 等已有明确事件继续独立显示；同节点近时刻有明确事件时不再补重复的通用失败框。

连续未命中只取终止前最后一段记录，遇到成功、派发者改变或候选表改变即停止；不以整个任务累计失败轮数推断超时。

## 失败框（`动作失败` / `动作未派发` / `node not completed` / `识别超时`）

图上红色框有四种来源，按**时间戳**插进轮内（不是一律缀在列末）：

| 框 | 来自 | 说明 |
| --- | --- | --- |
| `动作失败 <动作名>` | `Node.Action.Failed` 的 `action_details.action` | 动作派发了但返回 `success=false` |
| `动作未派发` | 同上，但 `action` 为空、`action_details.action_id=0` | 动作根本没派发（常见于识别给了零矩形、或任务正在停止） |
| `node not completed` | `PipelineTask.cpp` | 框架级收尾，**比上两条晚**（实测 0.2 s / 3.4 s） |
| `Task timeout` / `invalid node id` | 框架 | 同属框架级收尾 |

`Node.Action.Failed` 与 `node not completed` 对应**相反**的排查方向（动作/坐标 vs 识别/ROI），
所以前者不再被后者吞掉。同一事件日志里会出现两条（MFA 进程与 agent 进程各发一次通知，相差 1 ms），
按 `(节点, task, action_id, 动作名)` + 1 s 窗口去重，图上只画一个。

命中框里也会标出"动作没派发"（`detail()` 里 `action` 为空时原本整段跳过，失败一点痕迹不留）：
`GiftChooseFirstGift` 那种 `action=""`、`action_id=0` 的步骤现在显示 `动作未派发`。

**`BaseTaskStopError` 的失败是设计如此**：`BaseTaskStopErrorAct` 恒返回 `false`
（`agent/shared/generic.py:124`），用来让任务报错结束。所以它那个 `动作失败 Custom` 框是
**正常收尾**，不是动作坏了。

## 缩略图角标：哪张才是失败现场

一次失败里大部分框贴的是**识别帧**（`debug/vision/`），同一次卡住的十几步往往是同一个画面的截图，
看起来一模一样；只有 `debug/on_error/` 那几张才是**失败那一刻**的现场。两者必须能一眼分开：

| 角标 | 图来自 | 画法 |
| --- | --- | --- |
| `失败现场 HH:MM:SS.mmm` | `on_error/<时间>_<节点>.png` | **红框 + 实心红底白字**角标；时刻就是文件名里那一段，直接对上 `debug/on_error/<文件>` |
| `邻近帧` | `vision/` 里**别的节点**的帧（本节点没存图时的最近帧） | 淡琥珀小字，不抢眼 |
| （无角标） | `vision/` 里本节点自己的识别帧（`reco_id` 精确命中） | 就是"这个节点当时看到了什么" |

顶部注记还会点名本窗口内的现场图文件名
（`提示：失败现场图：2026.11.03-08.52.54.334_BaseTaskStopError.png`），
同名节点一天失败好几次时以它为准。

失败现场图贴回同名命中节点有两条硬限制（`backfill_failure_thumbs()`）：

- **时间上限 8 s**：`on_error/` 是"这一步失败时"抓的，隔几分钟的是另一次失败。实测同一个
  `BaseTaskStopError` 一小时里失败 4 次（08:50 / 08:52 / 09:02 / 09:03），不设上限时按列表顺序
  覆盖 → 最后遍历到的 09:03 那张赢，本次失败框就贴着别的跑次的图。
- **一格只留最近那张，且不覆盖该节点自己的精确识别帧**。

## 画不出来时的报错

`--from/--to/--task-id` 写错、路径写错、窗口落在日志时间范围外，报的话各不相同，且都会报出
**看到了什么**（行数、日志的时间范围、被过滤掉的是哪一类）：

```text
错误：入参像路径但磁盘上没有这个文件：D:/MaaAshArms/debug/vision；它也不含任何一行日志正文
错误：窗口内没有日志行：日志时间范围 2026-10-13 14:52:37.678 ~ 2026-10-14 00:35:24.413；请求的窗口 2027-01-01 00:00:00.000 ~ 2027-01-01 01:00:00.000
错误：窗口内 20 行里属于本 task（--task-id=999999）的节点级记录一条都没有，有 1 行是其他 task_id 的
```
