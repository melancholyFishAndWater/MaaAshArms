"""图上第二行与 `--list` 的诊断注记：空窗口原因、整轮空转统计、agent 缺位、
疑似过时、时间戳回跳。

只产 `Note`（文本 + kind），上色与排版由 `render`/`draw` 负责；判据尽量只依赖
`ParsedLog` 的事实，不猜。"""

from __future__ import annotations


from datetime import datetime
from typing import Any

from .model import Note, _fmt_ts

from .stale import VERSION_SOURCE_CLI, VERSION_SOURCE_MFA_LOG, VERSION_SOURCE_TELEMETRY

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import NextListStat, ParsedLog
    from .render import FlowOptions
    from .stale import StaleCheck


def describe_empty_window(parsed: ParsedLog, opts: FlowOptions, source: Any) -> str:
    """窗口内画不出东西时，按实际原因给不同的话。

    这句话曾经只有一种写法（"窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id"），
    实际盖住三种病因：日志时间戳不单调、输入本来就没有节点级记录、参数真写错了。
    前两种现在分别由"按时间重排"和"用识别记录合成"处理掉了，剩下能走到这里的都能说清原因，
    所以文案必须报出**看到了什么**（行数、时间范围、被过滤掉的是什么），而不是继续让人猜。
    """
    st = parsed.stats
    window = f"请求的窗口 {_fmt_ts(opts.t_from)} ~ {_fmt_ts(opts.t_to)}"
    if st.lines_total == 0:
        if st.source_kind == "path-missing":
            return (
                f"入参像路径但磁盘上没有这个文件：{st.source_name}；"
                f"它也不含任何一行日志正文"
            )
        if st.source_kind == "file":
            return (
                f"文件读到了，但没有一行匹配日志格式（不是 maafw.log？"
                f"MFA 的 `logs/log-*.log` 只有界面日志、没有 pipeline 执行记录，"
                f"流程在安装目录的 `debug/maafw.log` 里）：{st.source_name}"
            )
        return f"入参里没有一行匹配日志格式，不是日志正文：{st.source_name}"
    if st.lines_in_window == 0:
        return (
            f"窗口内没有日志行：日志时间范围 {_fmt_ts(st.first_ts)} ~ {_fmt_ts(st.last_ts)}；"
            f"{window}"
        )
    if st.lines_other_task:
        return (
            f"窗口内 {st.lines_in_window} 行里属于本 task（--task-id={opts.task_id}）的节点级记录"
            f"一条都没有，有 {st.lines_other_task} 行是其他 task_id 的："
            f"这个 task 在窗口内没跑过，或 task_id 写错了。{window}"
        )
    if st.lines_internal_skipped:
        return (
            f"窗口内 {st.lines_in_window} 行只有内部识别记录（{st.lines_internal_skipped} 条，"
            f"形如 recognition/OCR/<uuid>），默认过滤掉了；加 --keep-internal 可以看"
        )
    extra = (
        f"，其中 {st.truncated_skipped} 条因导出截断读不出"
        if st.truncated_skipped
        else ""
    )
    return (
        f"窗口内 {st.lines_in_window} 行里没有节点级记录（PipelineTask node done），"
        f"也没有可用的识别结果（reco [result=...]）{extra}。{window}"
    )


def nextlist_stats_of(parsed: ParsedLog, task_id: int | None) -> list[NextListStat]:
    """按 task 取"候选表评估账"，没命中轮次多的排前面。

    `task_id is None` 时返回全部（整份日志里所有任务），调用方自己决定要不要限定。
    """
    stats = [
        s
        for (tid, _name), s in parsed.nextlist_stats.items()
        if task_id is None or tid == task_id
    ]
    return sorted(stats, key=lambda s: (-s.failed, -(s.starting + s.succeeded)))


def stall_notes(
    parsed: ParsedLog, task_id: int | None, step_names: set[str], limit: int = 2
) -> list[Note]:
    """把"卡在派发层"的任务说清楚：某派发者的候选表连续 N 轮一个都没命中。

    这是"node done 只有两三步、时间却过了十几秒"那类任务的**唯一可复现证据**——`reco [result=]`
    行不带 `task_id`，只能由带 `task_id` 的 `Node.NextList` 事件来说明"这段时间在反复试哪些候选"。

    只报 `failed >= 2` 的（1 轮无命中是循环推进的正常现象），按没命中轮数取前 `limit` 个。
    """
    notes: list[Note] = []
    for stat in nextlist_stats_of(parsed, task_id):
        if len(notes) >= limit:
            break
        if stat.failed < 2:
            continue
        ran = [c for c in stat.real_candidates if c in step_names]
        head = f"{stat.name} 的候选表 {stat.failed} 轮全未命中"
        if stat.real_candidates:
            shown = "、".join(stat.real_candidates[:4])
            more = (
                f" 另 {len(stat.real_candidates) - 4} 项"
                if len(stat.real_candidates) > 4
                else ""
            )
            head += f"（候选 {shown}{more}）"
        if not ran:
            head += "，一个候选都没跑起来：任务一直停在派发层"
        else:
            head += f"，跑起来的候选：{'、'.join(ran[:4])}"
        notes.append(Note(head, "stale"))
    return notes


def _agent_gap_of(parsed: ParsedLog, ts: datetime) -> datetime | None:
    """`ts` 之前最后一次 agent 生死变更；那次是"销毁"就返回销毁时刻，否则 None。

    只认 `MaaAgentClientDestroy` 与 `MaaAgentClientConnect` 的返回：前者之后回调全部失效，
    后者返回说明重连（并重新注册）成功。
    """
    before = [state for state in parsed.agent_state if state[0] <= ts]
    if not before:
        return None
    gap_ts, _px, present = max(before, key=lambda s: s[0])
    return None if present else gap_ts


def agent_missing_note(parsed: ParsedLog, opts: FlowOptions) -> list[Note]:
    """agent 缺位那件事怎么说：命中数、涉及节点、缺位区间（判得出来才写）、以及反向提醒。

    **只数窗口内的**：ERR 行不带 `task_id`，选中某个任务时按它的判定窗口夹（与框架级事件同一个
    口径）。打标那一侧不能夹——ERR 与它影响的记录可能分属"步骤区间"内外。
    """
    records = parsed.custom_missing
    if opts.task_id is not None and parsed.span is not None:
        lo, hi = parsed.span
        records = [m for m in records if lo <= m.ts <= hi]
    if not records:
        return []
    nodes = sorted({m.node for m in records})
    shown = "、".join(nodes[:5])
    more = f" 等 {len(nodes)} 个" if len(nodes) > 5 else ""
    notes = [
        Note(
            f"{len(records)} 处 Custom 回调未注册（agent 缺位）：{shown}{more}"
            f"——图里这些步骤的注记写了「agent 缺位」，属环境故障，不要往 ROI / UI 方向查",
            "agent",
        )
    ]
    gaps = [_agent_gap_of(parsed, m.ts) for m in records]
    real = [g for g in gaps if g is not None]
    if real and len(real) == len(gaps):
        # 全部命中都落在"销毁之后、重连之前"，才能说成一段区间；参差不齐就只说现象
        start = min(real).strftime("%H:%M:%S")
        notes.append(
            Note(
                f"agent 客户端在 {start} 被销毁，之后没有重新连上"
                f"（`MaaAgentClientConnect` 未返回），这批缺位都落在这个区间内",
                "agent",
            )
        )
    # 这条必须写：这两条 ERR 是**单向证据**，没有它不代表 agent 在场（回调抛异常/返回 false 不打）
    notes.append(
        Note(
            "注意：**没有**「agent 缺位」标记不代表 agent 在场——回调注册了但抛异常或返回 false 时"
            "框架不打这两条 ERR，那种情况要看 debug/agent/agent.log",
            "agent",
        )
    )
    return notes


def build_notes(parsed: ParsedLog, opts: FlowOptions) -> list[Note]:
    """图上第二行与终端提示共用的诊断注记（每条带 `kind`，画的时候按类型上色）。

    只用中文字符：`⚠` 这类符号在 msyh 里没有字形，会画成方框。
    """
    st = parsed.stats
    notes: list[Note] = []
    if parsed.derived_from == "nextlist":
        notes.append(
            Note(
                "由 NextList 派发记录还原（这段日志里没有该 task 的识别记录）："
                "节点顺序 = 派发顺序",
                "derive",
            )
        )
    elif parsed.derived_from:
        notes.append(Note("由识别记录还原（日志没有 PipelineTask node done）", "derive"))
        if parsed.derived_clamped:
            notes.append(Note("识别范围已按该 task 的活动区间夹住", "derive"))
        elif opts.task_id:
            notes.append(Note("识别记录里没有 task_id，同窗口其他任务可能混入", "derive"))
    # 卡在派发层：只在"图只涉及一个 task"时报（多任务混在一张图上点名某个 task 的候选表会误导）
    task_ids = {s.task_id for s in parsed.steps if s.task_id is not None}
    if opts.task_id is not None:
        notes.extend(stall_notes(parsed, opts.task_id, {s.name for s in parsed.steps}))
    elif len(task_ids) == 1:
        notes.extend(
            stall_notes(parsed, next(iter(task_ids)), {s.name for s in parsed.steps})
        )
    # 父节点只在"日志里确实有派发记录、却有步骤没归上"时才提示；全归上了就不刷屏
    if st.nextlist_seen and (st.parent_fallback_entry or st.parent_unknown):
        missed = st.parent_fallback_entry + st.parent_unknown
        notes.append(
            Note(f"{missed} 步的父节点没落在本窗口的派发记录里，已退回任务入口或留空")
        )
    if opts.parents and st.nextlist_seen == 0 and (parsed.steps or parsed.attempts):
        notes.append(Note("日志里没有 Node.NextList 事件，父节点只能留空"))
    if st.out_of_order:
        notes.append(Note(f"日志时间戳有回跳，已按时间重排（{st.out_of_order} 行）", "time"))
    if st.truncated_skipped:
        notes.append(Note(f"{st.truncated_skipped} 条记录因导出截断读不出，已跳过", "stale"))
    if st.truncated_kept:
        notes.append(Note(f"{st.truncated_kept} 条记录尾部被截断，数字可能不全", "stale"))
    if st.events_dropped:
        notes.append(
            Note(f"已排除其他 task 的 {st.events_dropped} 条失败事件（本会画进图里）")
        )
    notes.extend(stale_notes(opts.stale))
    notes.extend(agent_missing_note(parsed, opts))
    return notes


def stale_notes(check: StaleCheck | None) -> list[Note]:
    """疑似过时那件事怎么说（图上第二行）。

    三类，各自一个颜色：

    - 绿色 `version`：这次用的**资源版本**（判出来了就总要标一行，读图的人才知道拿哪版比的）；
    - 琥珀 `stale`：哪些节点疑似过时（只列名字 + 模块@短 hash，长提交信息留给终端）；
    - 灰色 `info`：哪些节点在当前 pipeline 里已找不到。

    做不成时只出一条琥珀 `stale`，把"为什么查不了"写清楚——"查不了"与"没有过时节点"必须能分开读。
    """
    if check is None:
        return []
    if not check.ok:
        return [Note(f"疑似过时检查跳过：{check.reason}", "stale")]
    src = (
        "（取自 MFA 日志 logs/log-*.log）"
        if check.source == VERSION_SOURCE_MFA_LOG
        else "（取自遥测：evidence.json 或本地遥测库的 release）"
        if check.source == VERSION_SOURCE_TELEMETRY
        else "（手动指定）"
        if check.source == VERSION_SOURCE_CLI
        else ""
    )
    notes: list[Note] = [Note(f"资源版本 {check.version}{src}", "version")]
    if check.nodes:
        parts = []
        for node in check.nodes[:4]:
            short = node.last_commit.split(" ")[0] if node.last_commit else "?"
            parts.append(f"{node.name}（{node.module}@{short}）")
        more = f"；另 {len(check.nodes) - 4} 个" if len(check.nodes) > 4 else ""
        notes.append(
            Note(
                f"{len(check.nodes)} 个节点疑似过时：{'、'.join(parts)}{more}",
                "stale",
            )
        )
    if check.missing:
        shown = "、".join(check.missing[:6])
        more = f" 另 {len(check.missing) - 6} 个" if len(check.missing) > 6 else ""
        notes.append(
            Note(
                f"{len(check.missing)} 个节点在当前 pipeline 里已找不到：{shown}{more}",
                "info",
            )
        )
    return notes


def stale_summary(check: StaleCheck | None) -> str | None:
    """「疑似过时」命中时的一句话提示；没命中（或没做这项检查）返回 None。

    与 `stale_notes` 的分工：notes 是图上第二行的注记（带模块与短 hash），
    这条是画完图后在终端显式打的一行——**只看 stdout 的人也能知道这张图有存疑的节点**。
    终端能贴长文，所以这里把每个节点的"哪次提交"写全。
    """
    if check is None or not check.ok or not check.nodes:
        return None
    lines = [
        f"  {n.name}（{n.module}，最新 {n.last_commit}）" for n in check.nodes[:8]
    ]
    more = f"\n  …另 {len(check.nodes) - 8} 个" if len(check.nodes) > 8 else ""
    return (
        f"疑似过时：{len(check.nodes)} 个节点在资源版本 {check.version} 之后改过"
        f"——图里这些节点名带「疑似过时」，改动有没有影响它们的行为要自己核对\n"
        + "\n".join(lines)
        + more
    )
