"""把日志读成 `ParsedLog`：行切分、事件匹配（NextList / Recognition / Action /
PipelineNode / 任务起止 / 超时与失败）、仅事件模式的步骤合成、任务扫描。

只做解析，不排版不上色。口径细节（哪些行算步骤、截断怎么记）都写在各函数
与 `Step` 字段上；`--list` 的输出就是本模块的直接结果。"""

from __future__ import annotations


import io
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .constants import (
    ACTION_FAILED_DEDUP_SECONDS,
    ACTION_FAILED_RE,
    ACTION_RE,
    AGENT_CONNECT_FUNC,
    AGENT_DESTROY_FUNC,
    CUSTOM_MISS_WINDOW_SECONDS,
    CUSTOM_NULL_ACT_RE,
    CUSTOM_NULL_RECO_RE,
    INTERNAL_NAME_RE,
    INVALID_RE,
    LINE_RE,
    NODE_DONE_RE,
    NOT_COMPLETED_RE,
    PIPE_FAILED_DETAILS_RE,
    PIPE_FAILED_MARK_RE,
    PIPE_FAILED_NAME_FIELD_RE,
    PIPE_FAILED_NAME_RE,
    PIPE_FAILED_TASK_FIELD_RE,
    RECO_RE,
    TASKER_TASK_RE,
    TASK_END_RE,
    TASK_START_RE,
    TIMEOUT_RE,
    TRUNCATION_HEADS,
    WALK_RE,
)

from .layout import task_span

from .model import (
    CustomMissing,
    NextListStat,
    ParseStats,
    ParsedLog,
    Step,
    _fmt_ts,
    _source_label,
    collect_texts,
    parse_nextlist,
    parse_ts,
    resolve_parent,
    source_kind_of,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Dispatcher


def _open_log_lines(source) -> Iterator[str]:
    """把 stdin / 文本 / 路径三种入参统一成"逐行"。

    判据顺序是 ``read`` 属性 → **存在的文件路径** → 其余当文本：
    前者是文本流（含 StringIO、stdin）；中间那步用 ``os.path.isfile`` 而不是"含不含换行"，
    因为**单行日志正文也不含换行**，按换行判会把它当路径去 ``open()``，
    抛的是 `OSError Invalid argument`（不是 FileNotFoundError），很难看出原因。

    路径写错时会在 ``parse_log`` 的统计里表现为"一行都没匹配上"，
    由 ``describe_empty_window`` 报"既不是文件也不是日志正文"——不再混进"窗口内没有记录"。
    """
    if hasattr(source, "read"):
        for line in source:
            yield line.rstrip("\n")
        return

    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    if not isinstance(source, str):
        raise TypeError(
            f"日志入参只接受路径、文本或文本流，收到 {type(source).__name__}"
        )

    if os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")
        return

    # 不是已存在的文件 → 当日志正文。路径写错时会在下面"窗口内没有解析到任何节点执行记录"
    # 那一步报出来，比 open() 的 Errno 22 可读。
    for line in io.StringIO(source):
        yield line.rstrip("\n")


def _read_lines(source: Any, stats: ParseStats) -> list[str]:
    """读全部行；时间戳回跳就直接把行按时间重排。

    为什么要读全量与重排：`--from/--to` 原来是"遇到第一条晚于 --to 的行就 break"，
    这在按时间追加的 maafw.log 上没问题；但 Explore 导出的 CSV 是**分块拼接**的，
    块与块之间时间会回跳，于是窗口里的记录（实测 11 步 + 30 条识别）全被跳过，
    还报成"窗口内没有解析到任何节点执行记录"。
    排序只按时间戳、稳定排序，未带时间戳的行排在末尾（它们本来也会被跳过），
    与手工"先排序再喂工具"的结果逐字段一致。
    """
    lines: list[str] = []
    keys: list[datetime | None] = []
    last: datetime | None = None
    for raw in _open_log_lines(source):
        lines.append(raw)
        m = LINE_RE.match(raw.rstrip("\n"))
        ts = parse_ts(m.group("ts")) if m else None
        keys.append(ts)
        if ts is not None:
            if last is not None and ts < last:
                stats.out_of_order += 1
            if last is None or ts > last:
                last = ts
    if stats.out_of_order:
        order = sorted(
            range(len(lines)),
            key=lambda i: (keys[i] is None, keys[i] or datetime.min, i),
        )
        lines = [lines[i] for i in order]
    return lines


def _count_truncated(msg: str, stats: ParseStats) -> None:
    """行首对上了但整条对不上 → 导出按字节裁过，这条记录读不出来。"""
    for head, pattern in TRUNCATION_HEADS:
        if head in msg and not pattern.search(msg):
            stats.truncated_skipped += 1
            return


_OVERRIDE_MARKS = ('"pipeline_override":', "[pipeline_override=")


def _override_names(msg: str) -> set[str]:
    """从运行期覆写事件的日志行里取出被覆写的节点名（解析不出就返回空集，不猜）。

    agent 的 `BaseErrorNext`（`agent/shared/generic.py:BaseErrorNextReco`）在同一个 next 连续
    10 轮没命中时会调 `context.override_pipeline()`，建出 `<父节点>ErrorNext` 并把父节点的
    `next` 改成它。这个节点只存在于**运行期**，`assets/resource/pipeline/` 里永远没有它，
    所以「疑似过时」不能把它当成"已改名/删除"（实测 `EscortEnterAcceptErrorNext`）。

    框架会把覆写内容原样打进日志，两种写法都见过：
    `handle_context_override_pipeline … [req={…"pipeline_override":{…}}]`、
    `Context::override_pipeline … [pipeline_override={…}]`。导出裁过的行解析不出就跳过。
    """
    start = -1
    if "override" not in msg:
        # 快筛：绝大多数行没有覆写，别对它们做两次 find + JSON 括号扫描
        return set()
    for mark in _OVERRIDE_MARKS:
        at = msg.find(mark)
        if at >= 0:
            start = at + len(mark)
            break
    if start < 0:
        return set()
    text = msg[start:].lstrip()
    if not text.startswith("{"):
        return set()
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end < 0:
        return set()
    try:
        data = json.loads(text[:end])
    except ValueError:
        return set()
    # 内部识别与内部动作（`recognition/OCR/<uuid>`、`action/Swipe/<uuid>`）是 MFA 自己建的，
    # 不在 pipeline JSON 里，也不能算"运行期合成"的 pipeline 节点——一律剔掉，免得把
    # missing/synthesized 搅浑（顺带让这份名字集合不掺 uuid）
    return {
        key
        for key, value in data.items()
        if isinstance(value, dict)
        and not INTERNAL_NAME_RE.match(key)
        and not key.startswith(("recognition/", "action/"))
    }


_PARSE_CACHE: dict[tuple, ParsedLog] = {}
_PARSE_CACHE_MAX = 4
# 只缓存 16 MB 以内的日志：`cases` 批量时同一份**证据包日志**会被 `_evidence_task_id`、
# `_terminal_dispatcher`、`render_flow` 各解析一次；本地 `--case` 画历史运行时还要再解析一次
# 来定"失败在哪一刻"。缓存里只存解析结果（不含日志正文），15.5 MB 的日志解析出来也就几千个
# `Step`，占用与体积无关；用 `_PARSE_CACHE_MAX_STEPS` 再兜一道，别让满屏 reco 的日志吃内存。
_PARSE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_PARSE_CACHE_MAX_STEPS = 20000


def _parse_cache_key(
    path: Any,
    t_from: datetime | None,
    t_to: datetime | None,
    task_id: int | None,
    keep_internal: bool,
    px: str | None,
) -> tuple | None:
    """同一份日志 + 同样参数只解析一次：给出缓存键；不能缓存时返回 None。

    只认**文件路径**输入（正文/文本流每次都可能不同），并且按 `mtime + 大小` 认身份——
    日志被轮转或重写后键就变了，不会拿到旧结果。

    **返回的 `ParsedLog` 在进程内是共享的**：调用方只读；要就地改先 `copy.deepcopy`。
    """
    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if stat.st_size > _PARSE_CACHE_MAX_BYTES:
        return None
    return (
        os.path.abspath(path),
        stat.st_mtime_ns,
        stat.st_size,
        t_from,
        t_to,
        task_id,
        keep_internal,
        px,
    )


def parse_log(
    path,
    t_from: datetime | None,
    t_to: datetime | None,
    task_id: int | None,
    keep_internal: bool,
    px: str | None = None,
) -> ParsedLog:
    """解析日志。`path` 可以是文件路径、日志正文，或任何可逐行的文本流。

    全量读入（不再在第一条晚于 `--to` 的行上提前结束），时间戳回跳时先按时间重排行；
    `steps` 为空而 `attempts` 非空时，用识别记录合成步骤（"仅事件模式"）。

    同一份**文件**日志配同样参数时，进程内复用上一次的结果（见 `_parse_cache_key`）。
    """
    cache_key = _parse_cache_key(path, t_from, t_to, task_id, keep_internal, px)
    if cache_key is not None and cache_key in _PARSE_CACHE:
        return _PARSE_CACHE[cache_key]
    out = ParsedLog()
    stats = out.stats
    stats.source_kind = source_kind_of(path)
    stats.source_name = _source_label(path)
    target_px = px  # 过滤条件：只取这个进程；None = 全文
    # 键带进程号：reco_id / action_id 是**每个进程各自**的计数器，一个装了多次运行的
    # maafw.log 里会重复（本机实测 930 个 reco_id 有 142 个重复，全部跨进程）。
    # 只按 id 作键会把别的运行的识别结果贴到这一步上。
    recl_by_id: dict[tuple[str, int], dict] = {}
    act_by_id: dict[tuple[str, int], dict] = {}
    # 派发者（`[cur_node_=X]`）同样按进程存：多会话日志里两个进程都会写这种行
    # （本机 maafw.log 是 106 / 248 条），共用一个变量会让别的进程的派发者粘到本进程的步骤上，
    # 进而把 roles() → 分轮算错。
    dispatcher_by_px: dict[str, str] = {}
    task_names: dict[tuple[str, int], str] = {}
    # 解析到当前这一行时正在跑哪个 task：`task start` 记下、`task end` 清掉。
    # 只给框架级失败事件（自己不带 task_id）判断父节点用——它们的 task_id 必须留空，
    # 否则 parse_log() 末尾那段区间过滤会把别的运行的失败事件一起收进来。
    # 用单元列表而不是裸变量：`event_step()` 闭包要读它，裸变量在循环里赋值会被当成循环变量捕获。
    active_task: dict[str, int | None] = {}
    # 父节点：`Node.NextList.*` 记录的 (派发者, 候选表)。与 `[cur_node_=X]` 不同，这类记录**带 task_id**，
    # 所以按 task 归属而不是按进程——MFA 侧发事件、agent 进程写 node done，天然跨进程。
    nextlists: list[Dispatcher] = []
    # `Node.Action.Failed` 同一事件会有两条通知（MFA 进程 + agent 进程，相差 1 ms），
    # 按 (节点, task, action_id, 动作名) 记最后一次的时间做去重，否则一张图会画出两个同样的失败框。
    seen_action_failed: dict[tuple[str, int | None, int | None, str], datetime] = {}
    # 终止失败归集（见 parse_log 末尾）：
    #   pipeline_failures = 全部 Node.PipelineNode.Failed，元素是 (ts, 节点名, task_id, 进程)；
    #   task_failed = 任务确实终止失败的时刻，元素是 (ts, task_id)。
    pipeline_failures: list[tuple[datetime, str, int | None, str]] = []
    task_failed: list[tuple[datetime, int | None]] = []

    def attach_parent(step: Step, px: str, *, allow_walk: bool = True) -> Step:
        """给一步贴上父节点与候选表；三层退回，取不到就留空（图上写"未记录"）。

        这里只贴，不记账：`--task-id` 会把别的任务的步骤滤掉，父节点覆盖率的账要等滤完再算
        （见 `parse_log()` 末尾），否则图上会报"79 步的父节点没找到"而图里其实只有 18 步。
        """
        record, _gap = resolve_parent(
            nextlists,
            step.task_id if step.task_id is not None else active_task.get(px),
            step.name,
            step.ts,
        )
        if record is not None:
            step.dispatcher = record.name
            step.candidates = list(record.candidates)
            step.jumps = list(record.jumps)
            step.parent_source = "nextlist"
            return step
        walk = dispatcher_by_px.get(px, "")
        if allow_walk and walk and step.task_id is not None:
            # 只给**知道是哪个 task** 的步骤用这条退回：`[cur_node_=X]` 是"本进程当前在走谁的表"这个
            # 全局状态，而识别记录（`reco` 行）没有 task_id，直接套会把别的任务的派发者粘上来。
            step.dispatcher = walk
            step.parent_source = "walk"
            return step
        entry = task_names.get(
            (px, step.task_id if step.task_id is not None else -1), ""
        )
        if entry:
            step.dispatcher = entry
            step.parent_source = "entry"
            return step
        step.parent_source = "none"
        return step

    def event_step(
        ts: datetime,
        name: str,
        action: str,
        node: str | None,
        task_ref: int | None = None,
        *,
        kind: str = "event",
        completed: bool = False,
        px: str = "",
        task_id: int | None = None,
    ) -> Step:
        """框架级事件也认一次父节点。

        `invalid node id` / `node not completed` 这几条事件自己不带 task_id，但 `action` 里就是出问题的
        节点名（`[node.name=X]`），而它刚跑完、多半在 `Node.NextList` 记录里出现过（实测
        `node not completed [node.name=TrainingBattleWin]` 落在 `BattleWinSuccess -> [TrainingBattleWin]`
        之后 0.4 s）→ 框里能直接给出派发者与候选表，而不是写"父 未记录"。

        `task_ref` 优先用调用方知道的那个 task（`task end` 那行自带 task_id），否则用**解析到这一行时
        正在跑的那个 task**（`active_task`）。框架级失败事件的 `task_id` 字段仍然留空：`parse_log()`
        末尾的区间过滤靠它把别的运行的失败事件排掉，填上就过滤不掉了。
        `Task timeout` 的 pretask 是**正在跑的那个**节点，还没被选中进候选表，查不到属正常。

        `task_id` 只给**自带 task_id 的事件**用（`Node.Action.Failed` 的 details 里就有）：
        这类事件归属明确，不必退回时间窗，也能被 `--task-id` 精确排掉别的任务。
        """
        step = Step(
            ts=ts,
            name=name,
            completed=completed,
            kind=kind,
            action=action,
            task_id=(
                task_id
                if task_id is not None
                else (task_ref if kind == "task" else None)
            ),
        )
        if node:
            record, _gap = resolve_parent(
                nextlists,
                task_ref if task_ref is not None else active_task.get(px),
                node,
                ts,
            )
            if record is not None:
                step.dispatcher = record.name
                step.candidates = list(record.candidates)
                step.jumps = list(record.jumps)
                step.parent_source = "nextlist"
        return step

    for raw in _read_lines(path, stats):
        m = LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts = parse_ts(m.group("ts"))
        stats.lines_total += 1
        if stats.first_ts is None:
            stats.first_ts = ts
        stats.last_ts = ts
        if t_from and ts < t_from:
            continue
        if t_to and ts > t_to:
            continue
        stats.lines_in_window += 1
        line_px = m.group("px")
        func = m.group("func")
        msg = m.group("msg")

        # 运行期覆写建出来的节点（BaseErrorNext 的 `<父节点>ErrorNext`）：只记名字，
        # 「疑似过时」据此把"pipeline 里找不到"的节点分成"合成"与"真的没了"两类。
        # 不 continue：覆写行本身不含步骤，但没有必要改变后续解析的路径
        override_names = _override_names(msg)
        if override_names:
            out.override_nodes.update(override_names)

        # 谁在评估 next 表（父节点 + 候选表）；同时记一份"评估账"——
        # 连续的 failed 就是卡在派发层的证据，见 NextListStat 的注释。
        record = parse_nextlist(msg, ts, line_px)
        if record is not None and (target_px is None or record.px == target_px):
            nextlists.append(record)
            stats.nextlist_seen += 1
            key = (record.task_id, record.name)
            stat = out.nextlist_stats.get(key)
            if stat is None:
                stat = NextListStat(
                    name=record.name, candidates=record.candidates, first_ts=ts
                )
                out.nextlist_stats[key] = stat
            stat.last_ts = ts
            if record.kind == "succeeded":
                stat.succeeded += 1
            elif record.kind == "failed":
                stat.failed += 1
                stat.failed_candidates = record.candidates
            else:
                stat.starting += 1
            continue

        # agent 缺位：Custom 回调没注册（框架打 ERR），以及 agent 客户端的生死
        cm = CUSTOM_NULL_RECO_RE.search(msg)
        if cm:
            out.custom_missing.append(
                CustomMissing(ts, line_px, cm.group("node"), cm.group("reg"), "reco")
            )
            continue
        cm = CUSTOM_NULL_ACT_RE.search(msg)
        if cm:
            out.custom_missing.append(
                CustomMissing(ts, line_px, cm.group("node"), cm.group("reg"), "act")
            )
            continue
        if func == AGENT_DESTROY_FUNC and "| enter" in msg:
            out.agent_state.append((ts, line_px, False))
            continue
        if func == AGENT_CONNECT_FUNC and "| leave" in msg:
            out.agent_state.append((ts, line_px, True))
            continue

        # 当前在走谁的 next 表（只知道派发者、不知道候选表，作为退回）
        w = WALK_RE.search(msg)
        if w:
            dispatcher_by_px[line_px] = w.group(1).strip()
            continue

        # 任务起止
        ms = TASK_START_RE.search(msg)
        if ms:
            try:
                d = json.loads(ms.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            key = (line_px, int(d.get("task_id", 0)))
            task_names.setdefault(key, d.get("entry", ""))
            active_task[line_px] = int(d.get("task_id", 0))
            entry = d.get("entry", "")
            tid = int(d.get("task_id", 0))
            out.tasks.append((ts, entry, tid, d.get("hash", "")))
            # 任务起始也算一步：入口那一轮框架合成的自派发（`TaskX -> [TaskX]`）在这里就能标出来
            out.events.append(
                event_step(
                    ts,
                    f"任务开始 {entry}",
                    f"task_id={tid}",
                    entry,
                    tid,
                    kind="task",
                    completed=True,
                    px=line_px,
                )
            )
            continue
        me = TASK_END_RE.search(msg)
        if me:
            try:
                d = json.loads(me.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            ret = me.group(2) == "true"
            tid = int(d.get("task_id", 0))
            entry = d.get("entry", "")
            # 任务收尾也带上父节点：入口节点（`TaskTraining -> [TaskTraining]`）会显示成自派发，
            # 失败收尾就能看出"是谁结束了这个任务"。要赶在清 `active_task` 之前算。
            out.events.append(
                event_step(
                    ts,
                    f"任务结束 {entry} {'成功' if ret else '失败'}",
                    f"ret={me.group(2)}",
                    entry,
                    tid,
                    kind="task",
                    completed=ret,
                    px=line_px,
                )
            )
            if tid == active_task.get(line_px):
                active_task[line_px] = None
            out.task_ends.append((ts, entry, tid, ret))
            continue
        # 事件总线版任务收尾：有些 task 只有这条、没有 `task end:`（实测 PYTHON-2C 的
        # TaskMaintenance）。只用于判断"任务确实终止失败"，不额外造一条「任务结束」框。
        mtf = TASKER_TASK_RE.search(msg)
        if mtf:
            try:
                jf = json.loads(mtf.group(2))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            if mtf.group(1) == "Failed":
                tf_tid = jf.get("task_id")
                if isinstance(tf_tid, int):
                    task_failed.append((ts, tf_tid))
            continue

        # 识别结果（命中或不命中都记）
        mr = RECO_RE.search(msg)
        if mr:
            try:
                j = json.loads(mr.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            rid = j.get("reco_id")
            if isinstance(rid, int):
                recl_by_id[(line_px, rid)] = j
            name = j.get("name", "")
            if INTERNAL_NAME_RE.match(name) and not keep_internal:
                stats.lines_internal_skipped += 1
                continue
            texts: list[str] = []
            collect_texts(j.get("detail"), texts)
            out.attempts.append(
                attach_parent(
                    Step(
                        ts=ts,
                        name=name,
                        completed=bool(j.get("box")),
                        reco_id=rid if isinstance(rid, int) else None,
                        algorithm=j.get("algorithm", ""),
                        box=j.get("box"),
                        texts=texts,
                        kind="hit" if j.get("box") else "miss",
                    ),
                    line_px,
                )
            )
            continue

        # 动作失败事件（Node.Action.Failed）：比框架随后补的 `node not completed` 早且精确
        mf = ACTION_FAILED_RE.search(msg)
        if mf:
            try:
                jf = json.loads(mf.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            node_name = jf.get("name") or ""
            ad = jf.get("action_details") or {}
            act_name = ad.get("action") or ""
            act_aid = ad.get("action_id")
            tid = jf.get("task_id")
            tid = tid if isinstance(tid, int) else None
            key = (node_name, tid, act_aid, act_name)
            prev = seen_action_failed.get(key)
            if (
                prev is not None
                and (ts - prev).total_seconds() <= ACTION_FAILED_DEDUP_SECONDS
            ):
                continue
            seen_action_failed[key] = ts
            # 动作没派发时 `action` 与 `action_details.name` 都是空串、`action_id` 是 0
            label = f"动作失败 {act_name}" if act_name else "动作未派发"
            out.events.append(
                event_step(
                    ts,
                    label,
                    node_name,
                    node_name,
                    task_ref=tid,
                    px=line_px,
                    task_id=tid,
                )
            )
            continue

        # 动作结果
        ma = ACTION_RE.search(msg)
        if ma:
            try:
                j = json.loads(ma.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            aid = j.get("action_id")
            if isinstance(aid, int):
                act_by_id[(line_px, aid)] = j
            continue

        # 节点执行完毕（主流程的一步）
        md = NODE_DONE_RE.search(msg)
        if md:
            try:
                j = json.loads(md.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            tid = int(md.group(2))
            name = j.get("name", "")
            if INTERNAL_NAME_RE.match(name) and not keep_internal:
                stats.lines_internal_skipped += 1
                continue
            if target_px is not None and line_px != target_px:
                # 别的进程的同号任务：task_id 在各进程内复用，只按编号切会把它混进来
                stats.lines_other_task += 1
                stats.other_task_ts.append(ts)
                continue
            if task_id is not None and tid != task_id:
                stats.lines_other_task += 1
                stats.other_task_ts.append(ts)
                continue
            active_task[line_px] = (
                tid  # 正在跑哪个 task：给框架级事件判父节点用（line_px 取自本行）
            )
            # 这份日志里可能一条 `task start` 都没有（实测 Sentry 拉回来的就没有），
            # 所以"正在跑哪个 task"以 node done 为准，给框架级事件判父节点用
            reco = recl_by_id.get((line_px, j.get("reco_id")), {})
            act = act_by_id.get((line_px, j.get("action_id")), {})
            texts: list[str] = []
            collect_texts(reco.get("detail"), texts)
            out.steps.append(
                attach_parent(
                    Step(
                        ts=ts,
                        name=name,
                        completed=bool(j.get("completed")),
                        reco_id=j.get("reco_id"),
                        action_id=j.get("action_id"),
                        node_id=j.get("node_id"),
                        task_id=tid,
                        algorithm=reco.get("algorithm", ""),
                        box=reco.get("box"),
                        texts=texts,
                        action=act.get("action", ""),
                        action_ok=act.get("success"),
                    ),
                    line_px,
                )
            )
            continue

        # 节点级失败事件：**只记录，不立即出框**——可恢复的失败也会发，全画会刷屏。
        # 末尾按"任务确实终止"挑每个 task 的最后一条出框（见 parse_log 末尾）。
        if PIPE_FAILED_MARK_RE.search(msg):
            pf_name, pf_tid = "", None
            mj = PIPE_FAILED_DETAILS_RE.search(msg)
            if mj:
                try:
                    jpf = json.loads(mj.group(1))
                except json.JSONDecodeError:
                    jpf = {}
                pf_name = str(jpf.get("name") or "")
                raw_tid = jpf.get("task_id")
                pf_tid = raw_tid if isinstance(raw_tid, int) else None
            if not pf_name:
                mn = PIPE_FAILED_NAME_RE.search(msg)
                if mn:
                    pf_name = mn.group(1)
            if not pf_name:
                # 被导出行尾裁断（没有收尾 `}]`）：依次尝试取**顶层** `"name"`：
                # 1) `"focus":null,"name":"…"`；2) focus 是对象时 `},"name":"…"`；
                # 3) 实在没有才退回第一个 `"name"`（可能拿到 focus 自己的名字）。
                mrel = (
                    re.search(r'"focus":\s*null\s*,\s*"name":"([^"\\]+)"', msg)
                    or re.search(r'\},"name":"([^"\\]+)"', msg)
                    or PIPE_FAILED_NAME_FIELD_RE.search(msg)
                )
                if mrel:
                    pf_name = mrel.group(1)
            if pf_tid is None:
                mtid = PIPE_FAILED_TASK_FIELD_RE.search(msg)
                if mtid:
                    pf_tid = int(mtid.group(1))
            if pf_name:
                pipeline_failures.append((ts, pf_name, pf_tid, line_px))
            continue

        # 异常
        mt = TIMEOUT_RE.search(msg)
        if mt:
            dur, tout = mt.group("dur"), mt.group("tout")
            dur_ok = bool(dur and mt.group("dur_ms"))
            tout_ok = bool(tout and mt.group("tout_ms"))
            if dur_ok and tout_ok:
                detail = f"{dur}ms > timeout {tout}ms"
            else:
                # 导出按字节裁剪会把行尾的两个字段切掉一半（本机实测 1 条）。
                # 如实标注，不猜数字、也不把这条超时事件整条丢掉。
                stats.truncated_kept += 1
                seen = []
                if dur:
                    seen.append(
                        f"duration_since(start_clock)={dur}{'ms' if dur_ok else '(截断)'}"
                    )
                if tout:
                    seen.append(f"reco_timeout={tout}{'ms' if tout_ok else '(截断)'}")
                detail = "行尾被截断" + (
                    "，" + "  ".join(seen) if seen else "，两个数字都缺失"
                )
            out.events.append(
                event_step(
                    ts,
                    f"Task timeout {mt.group('name')}",
                    detail,
                    mt.group("name"),
                    px=line_px,
                )
            )
            continue
        mi = INVALID_RE.search(msg)
        if mi:
            out.events.append(
                event_step(ts, "invalid node id", mi.group(1), mi.group(1), px=line_px)
            )
            continue
        mn = NOT_COMPLETED_RE.search(msg)
        if mn:
            out.events.append(
                event_step(
                    ts, "node not completed", mn.group(1), mn.group(1), px=line_px
                )
            )
            continue

        # 上面全不匹配：可能是被导出裁断的记录
        _count_truncated(msg, stats)

    if task_id is not None:
        # 严格过滤，不回退成"保留全部事件"：兜底会把别的任务的失败收尾画进单任务图里。
        # 框架级事件（task_id=None：Task timeout / invalid node id / node not completed）本身没法
        # 归属，改用**选中任务的活动区间**排掉别的运行——装了多次运行的日志里它们的 task_id 全是空，
        # 只按 task_id 过滤会漏。
        # 只记"本来会画进图里"的那些（进图的只有失败事件），否则正常窗口也会刷一条提示。
        out.span = task_span(out, task_id)
        kept: list[Step] = []
        dropped = 0
        for event in out.events:
            owned = event.task_id in (None, task_id)
            inside = (
                not out.span
                or event.task_id is not None
                or (out.span[0] <= event.ts <= out.span[1])
            )
            if owned and inside:
                kept.append(event)
            elif not event.completed:
                dropped += 1
        stats.events_dropped = dropped
        out.events = kept

    _drop_sub_recognition_attempts(out, task_id)
    _synthesize_steps(out, task_id)
    # 父节点覆盖率的账按**最终进图的步骤**算（过滤与合成都已结束）
    for step in out.steps:
        if step.parent_source == "nextlist":
            stats.parent_resolved += 1
        elif step.parent_source == "walk":
            stats.parent_fallback_walk += 1
        elif step.parent_source == "entry":
            stats.parent_fallback_entry += 1
        elif step.parent_source == "chain":
            stats.parent_derived += 1
        else:
            stats.parent_unknown += 1
    # 终止失败框：只在「任务确实终止失败」时，取该 task 最后一条
    # `Node.PipelineNode.Failed` 出框。可恢复的节点失败（占绝大多数）不出框，避免满屏红。
    if task_failed and pipeline_failures:
        seen_terminal: set[tuple[str, datetime]] = set()
        for tf_ts, tf_tid in task_failed:
            cands = [
                pf
                for pf in pipeline_failures
                if (pf[2] is None or pf[2] == tf_tid)
                and pf[0] <= tf_ts
                and (tf_ts - pf[0]).total_seconds() <= 30.0
                and (target_px is None or pf[3] == target_px)
                and (task_id is None or pf[2] is None or pf[2] == task_id)
            ]
            if not cands:
                continue
            best = max(cands, key=lambda p: p[0])
            key = (best[1], best[0])
            if key in seen_terminal:
                continue
            seen_terminal.add(key)
            # 同节点、同秒已经有失败事件（超时 / invalid / not completed / 动作失败）就不重复
            if any(
                e.name.endswith(best[1])
                and abs((e.ts - best[0]).total_seconds()) <= 2.0
                for e in out.events
            ):
                continue
            out.events.append(
                event_step(
                    best[0],
                    f"失败 {best[1]}",
                    "PipelineNode.Failed",
                    best[1],
                    best[2],
                    px=best[3],
                )
            )
        out.events.sort(key=lambda s: s.ts)

    _mark_agent_missing(out)
    if cache_key is not None and len(out.attempts) <= _PARSE_CACHE_MAX_STEPS:
        _PARSE_CACHE[cache_key] = out
        while len(_PARSE_CACHE) > _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
    return out


@dataclass
class TaskRef:
    """日志里的一个任务：`(进程, task_id)` 唯一定位。

    `task_id` 在每个进程里从同一个基数重新分配（本机实测 200000001 在 Px15316 与 Px6448
    各出现一次），所以单靠编号指不明确切是哪一个——`--pid` + `--task-id` 成对才是键。
    """

    px: str
    task_id: int
    entry: str = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    steps: int = 0

    def label(self) -> str:
        return f"{self.px}/{self.task_id}"

    def window(self) -> str:
        lo = _fmt_ts(self.first_ts)
        hi = _fmt_ts(self.last_ts)
        return f"{lo} ~ {hi}"


def scan_tasks(
    log, t_from: datetime | None = None, t_to: datetime | None = None
) -> list[TaskRef]:
    """列出日志里所有 `(进程, task_id)`，按开始时间排序。

    只扫 `PipelineTask node done`（任务靠它带 `task_id_`）与 `task start`（拿入口名），
    不复用 `parse_log()`：那个要建步骤、认父节点，为了列一张表不值得。
    多进程日志里同一编号会出现在不同进程下，`--pid` + `--task-id` 才能唯一定位一个。
    """
    refs: dict[tuple[str, int], TaskRef] = {}
    stats = ParseStats()
    for raw in _read_lines(log, stats):
        m = LINE_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts = parse_ts(m.group("ts"))
        if t_from and ts < t_from:
            continue
        if t_to and ts > t_to:
            continue
        px = m.group("px")
        msg = m.group("msg")
        md = NODE_DONE_RE.search(msg)
        if md:
            tid = int(md.group(2))
            ref = refs.get((px, tid))
            if ref is None:
                ref = refs[(px, tid)] = TaskRef(px=px, task_id=tid)
            ref.steps += 1
            ref.first_ts = ts if ref.first_ts is None else min(ref.first_ts, ts)
            ref.last_ts = ts if ref.last_ts is None else max(ref.last_ts, ts)
            continue
        ms = TASK_START_RE.search(msg)
        if ms:
            try:
                d = json.loads(ms.group(1))
            except json.JSONDecodeError:
                continue
            tid = int(d.get("task_id", 0))
            ref = refs.get((px, tid))
            if ref is None:
                ref = refs[(px, tid)] = TaskRef(px=px, task_id=tid)
            ref.entry = d.get("entry", "") or ref.entry
            ref.first_ts = ts if ref.first_ts is None else min(ref.first_ts, ts)
    # 只留真有节点执行的：`MaaTaskerPostStop` 那种停止指令只有 task start、0 步，
    # 画不出图，列在表里只是噪音（本机实测 6 条）。
    return sorted(
        (r for r in refs.values() if r.steps),
        key=lambda r: (r.first_ts or datetime.min, r.px, r.task_id),
    )


def _drop_sub_recognition_attempts(out: ParsedLog, task_id: int | None) -> int:
    """丢掉**不是本任务候选**的识别记录——复合识别（`Or` / `And` 引用别的节点）的子识别。

    框架真的会跑一次被引用节点的识别：`BaseBranchError` 的 `Or` 里引用 `BaseProbeReward`、
    `StartUpClickLogin`、`BaseClickRewardTap`…，日志里就落了它们各自的 `reco [result=…]` 行
    （实测 `PYTHON-3H` 的 task 200000107：这类名字 17 个、共 150 次）。把它们当成候选评估画成
    「未命中 ×9」「未命中 ×10」的框，读图的人会以为任务在等这些页面，其实那一步在评估的是
    `BattleClickReplenish` 的候选表。

    判据只看 `Node.NextList`：候选表（`real_candidates`）+ 派发者本身才叫"本任务评估过的东西"，
    且只丢**落在本任务活动区间**（`out.span`：首步 → 任务结束/下一步）里的那些。

    两条限定都是为了避免误删：

    * **没有 NextList 就全留**（Sentry 导出的包常见这种形态）——那时没有别的依据，宁可保留；
    * **整段日志模式（`task_id` 为空）不动**：那种视图本来就混着多个任务，`known` 只是并集，
      拿它判"是不是候选"会把别的任务的正常识别也删掉。

    返回丢掉的条数（调用方目前不展示，靠这条 docstring 与 `docs/parsing.md` 说清）。
    """
    if task_id is None or out.span is None:
        return 0
    known: set[str] = set()
    for (tid, name), stat in out.nextlist_stats.items():
        if tid != task_id:
            continue
        known.add(name)
        known.update(stat.real_candidates)
    if not known:
        return 0
    lo, hi = out.span
    before = len(out.attempts)
    out.attempts = [
        attempt
        for attempt in out.attempts
        if attempt.name in known or not (lo <= attempt.ts <= hi)
    ]
    return before - len(out.attempts)


def _synthesize_steps(out: ParsedLog, task_id: int | None) -> None:
    """没有 `PipelineTask node done` 时，用识别记录合成步骤（"仅事件模式"）。

    这种日志真实存在：MFA 的 Sentry 导出里只有 `Node.*` 事件与 `reco [result=]`，
    `PipelineTask node done` 一条没有。以前直接抛"窗口内没有解析到任何节点执行记录"，
    而同一批识别记录已经足够还原"卡在哪个节点、连续多少次没命中"。
    合成出来的框就是识别记录本身，所以 `compose_columns` 不再另做未命中聚合（会画重）。

    连 `reco` 行都没有时（Sentry 只留了事件总线的一段：实测 PYTHON-31 的包在失败的那 95 秒里
    只有 `Node.*` 事件、一条 reco 都没有），退回该 task 的 `Node.NextList.*` 派发记录，
    按派发顺序排成一条链（`derived_from="nextlist"`）。这时能还原的只是"依次派发了哪些节点"，
    不是完整执行顺序，所以图上要写清来源。

    反过来，日志里能看出**别的** task（`Node.NextList` 或任务事件带 task_id）、却没有这个 task
    的任何记录时，**不给步骤**：那段识别记录全属于别的任务，硬画出来就是同一份"全任务混在一起"
    的日志（实测 PYTHON-33/35/36 的包只留了 MFA 面板行，三张图除了标题一模一样）。
    """
    if out.steps:
        return
    attempts = out.attempts
    lo: datetime | None = None
    hi: datetime | None = None
    if task_id is not None:
        # reco 行里没有 task_id，只能用带 task_id 的事件推断这个 task 的活动区间。
        # 优先 `task start` / `task end`；导出日志往往一条都没有（Sentry 拉回的就是），
        # 于是退回 `Node.NextList.*`（details 里带 task_id）与带 task_id 的事件。
        starts = [t for t, _entry, tid, _hash in out.tasks if tid == task_id]
        if starts:
            lo = max(starts)
            ends = [
                t
                for t, _entry, tid, _ret in out.task_ends
                if tid == task_id and t >= lo
            ]
            if ends:
                hi = min(ends)
        else:
            lo_marks = [
                stat.first_ts
                for (tid, _name), stat in out.nextlist_stats.items()
                if tid == task_id and stat.first_ts is not None
            ]
            hi_marks = [
                stat.last_ts
                for (tid, _name), stat in out.nextlist_stats.items()
                if tid == task_id and stat.last_ts is not None
            ]
            for event in out.events:
                if event.task_id == task_id:
                    lo_marks.append(event.ts)
                    hi_marks.append(event.ts)
            if lo_marks:
                lo = min(lo_marks)
            if hi_marks:
                hi = max(hi_marks)
        if lo is not None:
            attempts = [
                a for a in attempts if a.ts >= lo and (hi is None or a.ts <= hi)
            ]
            out.derived_clamped = True
    if task_id is not None and lo is None:
        # 日志里有别的 task 的归属记录、却没有一条属于这个 task：说明这个 task 没跑在这段日志里
        # （实测 PYTHON-33/35/36 的包只留了 MFA 面板行，框架记录从 4 分钟后那次运行才有）。
        # 这时不能把整段识别记录当成它的步骤——那会画出同一份"全任务混在一起"的日志
        # （三张图除了标题几乎一模一样），比退概览卡更没有信息量。
        other_ids = {tid for (tid, _name) in out.nextlist_stats if tid is not None}
        other_ids |= {e.task_id for e in out.events if e.task_id is not None}
        if other_ids:
            return
    if attempts:
        out.steps = [Step(**a.__dict__) for a in attempts]
        out.derived_from = "attempts"
        return
    if task_id is not None:
        chain = _dispatch_chain(out, task_id, lo, hi)
        if chain:
            out.steps = chain
            out.derived_from = "nextlist"


def _dispatch_chain(
    out: ParsedLog, task_id: int, lo: datetime | None, hi: datetime | None
) -> list[Step]:
    """该 task 派发过的节点，按**第一次派发**排成一条链（同名只留一步）。

    `Node.NextList.*` 的 `name` 是派发者：它刚跑完、正在评估自己的 `next` 表，所以这条链就是
    任务依次走过的节点（节点失败时它自己也留下一条 `failed` 记录）。只在"这段日志没有该 task
    的 reco / node-done 记录"时用（见 `_synthesize_steps` 的说明）——那时 `attempts` 是空的，
    没有别的步骤来源。

    - `completed`：这个派发者的候选表成功过就为 True；只有 `failed`、一次都没成功 = 卡在它上面
      （渲染成未命中框）。
    - 父节点按链上前一步填，`parent_source="chain"`：那是**派发顺序**推出来的，不是
      `Node.NextList` 里真的记着的那条，所以单列一个来源，不算进"父节点没找到"的账。
    """
    marks = [
        (stat.first_ts, name, stat)
        for (tid, name), stat in out.nextlist_stats.items()
        if tid == task_id and stat.first_ts is not None
    ]
    if lo is not None:
        marks = [m for m in marks if m[0] >= lo]
    if hi is not None:
        marks = [m for m in marks if m[0] <= hi]
    marks.sort(key=lambda m: m[0])
    steps: list[Step] = []
    for ts, name, stat in marks:
        completed = bool(stat.succeeded) or not stat.failed
        steps.append(
            Step(
                ts=ts,
                name=name,
                completed=completed,
                task_id=task_id,
                kind="hit" if completed else "miss",
                dispatcher=steps[-1].name if steps else "",
                candidates=list(stat.real_candidates),
                parent_source="chain" if steps else "",
            )
        )
    return steps


def _match_custom_missing(
    records: list[CustomMissing], ts: datetime, name: str, kind: str
) -> CustomMissing | None:
    """找「这条记录之前 `CUSTOM_MISS_WINDOW_SECONDS` 内、同名、同类」的那次未注册。

    只比节点名与时间，**不比进程号**：同一份日志里进程在时间上是串行的（实测 6 次启动
    的首末任务区间两两不重叠），不会出现"两个进程同时跑同名节点"。
    """
    best: CustomMissing | None = None
    for record in records:
        if record.kind != kind or record.node != name:
            continue
        delta = (ts - record.ts).total_seconds()
        if 0 <= delta <= CUSTOM_MISS_WINDOW_SECONDS and (
            best is None or record.ts > best.ts
        ):
            best = record
    return best


def _mark_agent_missing(out: ParsedLog) -> None:
    """把「Custom 回调没注册」标到它影响的记录上。

    两条挂法，方向都是"ERR 在前"（框架先判回调为空、再落记录）：

    * 识别缺位 → 挂在它后面那条 `algorithm=Custom`、`box=null` 的未命中记录上
      （实测 `TrainInit` 的 ERR 之后 29 ms 就是 `reco … "box":null`）；
    * 动作缺位 → 挂在它后面那一步 `completed=false` 上（实测 `EntrustInit` 之后 201 ms 是
      `PipelineTask node done … "completed":false`）。
    """
    if not out.custom_missing:
        return
    for attempt in out.attempts:
        if attempt.box or attempt.algorithm != "Custom":
            continue
        hit = _match_custom_missing(
            out.custom_missing, attempt.ts, attempt.name, "reco"
        )
        if hit is not None:
            attempt.agent_missing = hit.reg
    for step in out.steps:
        if step.completed:
            continue
        hit = _match_custom_missing(out.custom_missing, step.ts, step.name, "act")
        if hit is not None:
            step.agent_missing = hit.reg
