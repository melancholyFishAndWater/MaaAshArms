"""解析与绘制共用的常量：日志行/事件正则、渲染器版本、候选列数上限、派发记录有效期。

`RENDERER_VERSION` 只在产物内容会变时 +1：`flow_cases` 把它写进 manifest 指纹，
不 bump 会让同一证据包命中旧记录直接跳过。正则按日志的真实行格式写，
改之前先拿一份日志核对（`--list` 的步骤数就是它的直接结果）。"""

from __future__ import annotations


import re


# 渲染器版本：只要改动会影响产物内容（版面、注记、解析口径）就 +1。
# flow_cases 把它写进 manifest 指纹——不 bump 的话同一个证据包会命中旧记录直接"跳过（已画过）"，
# 渲染器改了却不重画。
# 3：任务判定窗口的右端改用本任务自己的 task end。4：框里多了"父节点"一行。
# 5：父节点从"一行小字"变成浅色分组框 + 左上角实心角标。
# 6：图第二行多了"卡在派发层"的注记（候选表连续全未命中）；`--list` 多了父节点评估账。
# 7：连续、同形、整轮无截图且无失败框的轮次合并成一列（列头写「第 a–b 轮 · ×N」+ 右侧橙条，
#    `--no-overlay` 关掉）。默认开启，所以是产物变化。
# 8：`VisionIndex` 的两处 `sort` 加 member 次键——同时间戳的多张 vision 图不再抛
#    `TypeError`，且"同时间戳取哪张"从"插入顺序"改成"member 字典序"（产物可能换一张缩略图）。
# 9：节点名后标「疑似过时」——日志所属版本之后，该节点所在模块有提交（产物内容变化）。
# 10：产物文件名去掉 `flow_` 前缀；画完后显式打印「疑似过时」提示（产物名变化）。
# 11：解析按 `(进程, task_id)` 归属——`active_task` 从全局改成按 `Px<pid>` 存，
#     多进程混画的临时父节点不再跨进程串（默认渲染的父节点可能变准）；新增 `--pid` / `--list-tasks`。
# 12：把「Custom 回调没注册」（agent 缺位）标到对应步骤上，并在注记里说明——识别缺位原本与
#     "纯识别超时"、动作缺位原本与"pipeline 自己的问题"在图上分不出来（见 README「agent 缺位」）。
# 13：轮内「未命中（聚合）」框改为按时间戳与命中步骤合并，不再一律缀在轮末——末轮"命中→多轮
#     全未命中→终于命中→失败"的形状原本被画成"失败之后还在重试"。
# 14：补回 `Node.Action.Failed`——它比框架随后补的 `node not completed` 早且精确（带动作名、
#     action_id、box），单独画一类失败框；动作没派发（`action=""`、`action_id=0`）时命中框里
#     也补「动作未派发」，否则 `detail()` 的 `if self.action` 会把失败整段吞掉。
# 15：「疑似过时」的基线版本改取日志里的 `资源版本：vX`（MFA 自己报的），安装目录名只作兜底
#     并标"仅参考"——目录名是用户机器上的文件夹名，可以被改（实测目录名 beta.42 / 资源版本 beta.58）。
# 16：不再拿安装目录名当版本（更新资源后一定是错的），判不出来就照实说；第二行注记按类型上色、
#     一条一行，判出资源版本时在顶部标出来。
# 17：原生 maafw.log 里没有 `资源版本`（那是 MFAAvalonia 写的），取不到时改去同装
#     `logs/log-*.log` 借，并按本次日志起点挑"那一刻生效"的那条（一份日志里可能先后两个版本）。
# 18：资源版本来源再加"遥测证据包"——从 `evidence.json` 的 `release`（Sentry 事件字段）取，
#     比从 maafw.log 里抠更稳（原生 maafw.log 根本没有那行，同装 MFA 界面日志也不会被下载）。
#     同时支持 `--version` 手动指定基线。
# 19：失败现场图（`on_error/`）限定"只贴回时间紧挨着的那一步"，且一格只留最近那张——原来没有
#     时间上限、按列表顺序覆盖，同名节点一天失败多次时贴成了别的跑次的图；失败现场图改红框 +
#     红底角标、框里写「现场图 HH:MM:SS」，并把本窗口的图名写进顶部注记。
# 20：现场图的"本窗口"改按**选中任务的判定窗口**（`parsed.span`）夹，不再用全局 min/max——
#     框架级失败事件没有 task 归属，同一份滚动日志里别的跑次会把窗口撑到跨天；左端不放宽，
#     免得把首尾只差 51 ms 的**上一个** task 的图算进来。
# 21：`Node.PipelineNode.Failed` 只在"该 task 终止失败"时出失败框（取该 task 最后一条），
#     可恢复的节点失败不再全画（一份证据日志 105–491 条，全画会满屏红）；`Tasker.Task.Failed`
#     作为补充的终止信号（有些 task 没有 `task end: ... [ret=false]`）。
# 22：仅事件模式（由识别记录还原）下 `--task-id` 改用带 task_id 的 NextList/事件推断区间夹 reco，
#     导出日志没有 `task start` 时也不再混入别的 task（实测原来 26 轮 / 13048×8084）。
# 23：包里有 on_error 图、但都不在本窗口时，顶部注记点出文件名与时刻（"属于另一次失败"），
#     不再只字不提（实测 PYTHON-2H 的图是 66 s 前另一次失败，被 ±8 s 窗口正确排除）。
RENDERER_VERSION = 24

# 框里最多列几个候选，多的折成"另 N 项"（`--list` 不受这个限制）。
# 默认只列 2 个：父节点名字 + 本次真跑的那个候选是这一行最要紧的两件事，
# 列到 3 个时实测会被 `fit_text` 截掉尾巴（带图槽的框文本区只有 224px）。
PARENT_CHOICE_LIMIT = 2
# 这条派发记录离这一步最多多久还算数。同一份日志里同一个候选表会反复出现，取最近的就没问题；
# 这个上限只是防止上一条记录在几十分钟前、中间隔了别的任务时把父节点认错。
NEXTLIST_MAX_GAP_SECONDS = 1800.0

# --------------------------------------------------------------------------- #
# 日志解析
# --------------------------------------------------------------------------- #

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]"
    r"\[(?P<lvl>[A-Z]{3})\]"
    r"\[(?P<px>Px\d+)\]\[Tx\d+\]"
    r"\[(?P<src>[^\]]+)\]\[L\d+\]\[(?P<func>[^\]]*)\]\s?(?P<msg>.*)$"
)
JSON_TAIL_RE = re.compile(r"(\{.*\})\s*$")
RECO_RE = re.compile(r"reco \[result=(\{.*\})\]\s*$")
ACTION_RE = re.compile(r"action \[i=\d+\] \[data\.repeat=\d+\] \[result=(\{.*\})\]\s*$")
NODE_DONE_RE = re.compile(
    r"PipelineTask node done \[result=(\{.*\})\] \[task_id_=(\d+)\]\s*$"
)
TASK_START_RE = re.compile(r"task start: \[cb_detail=(\{.*\})\]\s*$")
TASK_END_RE = re.compile(r"task end: \[cb_detail=(\{.*\})\] \[ret=(\w+)\]\s*$")
JUMPBACK_RE = re.compile(r"(push|pop) jumpback_stack: (\S+)\s*$")
WALK_RE = re.compile(r"\[cur_node_=([^\]]+)\] \[list=(\[.*\])\]")
# `name` = 正在评估 next 表的节点 = **父节点**，`list` = 它那一刻排着的候选。
# 只有 MFA 进程那份带 `name` 与 `task_id`；agent 进程那份（`[message=Node.NextList.X]
# [details_json={…}]`）只有 focus + list，判不了父节点 → `parse_nextlist()` 返回 None。
# 大小写不统一（日志写 `node.nextlist.starting`），所以带 IGNORECASE。
#
# 收尾那个 `\]` 不能省：本机 Python 3.14 的 re 在裸 `(\{.*\})$` 上**不会**回溯 `.*`（实测
# `re.search(r'\[details=(\{.*\})\s*$', 'x [details={"a":1}] ')` 返回 None，加上 `\]` 就正常），
# 只按 `$` 收尾会静默匹配不上——与仓库里 RECO_RE / NODE_DONE_RE 的写法保持一致。
NEXTLIST_RE = re.compile(
    r"node\.nextlist\.(?P<kind>starting|succeeded|failed)\b.*?\[details(?:_json)?=(\{.*\})\]\s*$",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(
    r"Task timeout \[pretask\.name=(?P<name>[^\]]+)\]"
    r"(?: \[duration_since\(start_clock\)=(?P<dur>\d+)(?P<dur_ms>ms)?\])?"
    r"(?: \[pretask\.reco_timeout=(?P<tout>\d+)(?P<tout_ms>ms)?\])?"
)
INVALID_RE = re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]")
NOT_COMPLETED_RE = re.compile(
    r"node not completed, handle error \[node\.name=([^\]]+)\]"
)
# 节点级失败事件：`[msg=Node.PipelineNode.Failed] [details={…}]`（或只有 `[name=…]`）。
# 日志里很多——可恢复的失败也发（实测 PYTHON-2C 一份证据日志 105 条、PYTHON-2R 274 条），
# **不能全画成失败框**，否则满屏红框。只在"该 task 终止失败"时取它最后一条出框，
# 见 parse_log 末尾的终止失败归集。
PIPE_FAILED_MARK_RE = re.compile(r"\[msg=Node\.PipelineNode\.Failed\]")
PIPE_FAILED_DETAILS_RE = re.compile(
    r"\[msg=Node\.PipelineNode\.Failed\]\s*\[details=(\{.*\})\]\s*$"
)
PIPE_FAILED_NAME_RE = re.compile(
    r"\[msg=Node\.PipelineNode\.Failed\]\s*\[name=([^\]]+)\]"
)
# 导出按字节裁剪时 `[details={…}]` 可能没有收尾：直接抠第一个 `"name"` / `"task_id"`。
PIPE_FAILED_NAME_FIELD_RE = re.compile(r'"name":"([^"\\]+)"')
PIPE_FAILED_TASK_FIELD_RE = re.compile(r'"task_id":(\d+)')
# 任务级失败事件（事件总线版）：`[msg=Tasker.Task.Failed] [details={entry,task_id,…}]`。
# 有些 task 只有这一条、没有 `task end: … [ret=false]`（实测 PYTHON-2C 的 TaskMaintenance），
# 所以它是判断"任务确实终止"的补充来源。
TASKER_TASK_RE = re.compile(
    r"\[msg=Tasker\.Task\.(Failed|Succeeded)\] \[details=(\{.*\})\]\s*$"
)
# 动作失败事件：`!!!OnEventNotify!!! [msg=Node.Action.Failed] [details={…}]`。
# 它比框架随后补的 `node not completed` 早（实测 3.4 s / 0.2 s），且带节点名、动作名、
# `action_id` 与 `box`——两者对应完全相反的排查方向（动作/坐标 vs 识别/ROI）。
# 同一事件日志里会出现两条（MFA 进程与 agent 进程各发一次 EventDispatcher 通知，相差 1 ms），
# 所以只认 `[msg=…][details=…]` 这一种写法，再按 (节点, task, action_id, 动作名) + 时间窗去重。
ACTION_FAILED_RE = re.compile(
    r"\[msg=Node\.Action\.Failed\]\s*\[details=(\{.*\})\]\s*$"
)
ACTION_FAILED_DEDUP_SECONDS = 1.0
# agent 缺位：Custom 回调没注册时框架打的两条 ERR。只在"引用了回调名但没人注册"时出现，
# 所以有它就是硬证据；反过来没有它不代表 agent 在场（回调注册了但抛异常/返回 false 不打这两条）。
# 判据与三个反例见 README 的「agent 缺位」一节。
CUSTOM_NULL_RECO_RE = re.compile(
    r"recognition is null \[name_=(?P<node>[^\]]+)\] \[param_\.name=(?P<reg>[^\]]+)\]"
)
CUSTOM_NULL_ACT_RE = re.compile(
    r"Action is null \[node_name=(?P<node>[^\]]+)\] \[param\.name=(?P<reg>[^\]]+)\]"
)
# agent 客户端的生死：Destroy 之后到下一次 Connect 返回之间，注册过的回调都不在。
# 判定用 `LINE_RE` 的 `func` 组（函数名在方括号里，**不在 `msg` 里**），见 `parse_log`。
# 注意 `clear_custom_registration` **不是**断开信号——它是每次 Connect 的准备工作
# （本机实测 23 次入口对应 22 次 Connect），拿它当"断开"会误报。
AGENT_DESTROY_FUNC = "MaaAgentClientDestroy"
AGENT_CONNECT_FUNC = "MaaAgentClientConnect"
# 未注册的 ERR 与它影响的记录之间隔多久还算同一次：实测识别侧约 30 ms、动作侧约 200 ms
CUSTOM_MISS_WINDOW_SECONDS = 2.0
# `on_error/` 的现场图贴回"同名命中节点"时允许的时间差（秒）。现场图是**这一步**失败时抓的，
# 隔了几分钟的是另一次失败，不是它的现场。实测同一个 issue 里同名的 `BaseTaskStopError` 一天
# 失败好几次（08:50 / 08:52 / 09:02 / 09:03…），不设上限时按列表顺序覆盖，最后遍历到的那张
# （45 分钟后）会赢，于是本次失败框贴着别的跑次的图——读图的人根本分不出哪张是真的。
# 与 `OnErrorIndex.find` 的默认 `tol` 同一个量级。
ON_ERROR_BACKFILL_SECONDS = 8.0

# 这些行整条匹配才算完整；只对上"头部"说明导出按字节裁过（Explore CSV 的常见下场）
TRUNCATION_HEADS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("reco [result=", RECO_RE),
    ("action [i=", ACTION_RE),
    ("PipelineTask node done", NODE_DONE_RE),
    ("task start:", TASK_START_RE),
    ("task end:", TASK_END_RE),
)

INTERNAL_NAME_RE = re.compile(
    r"^recognition/(OCR|TemplateMatch|ColorMatch|FeatureMatch)/[0-9a-f\-]+$"
)
