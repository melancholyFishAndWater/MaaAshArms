"""仅事件日志的步骤合成：由识别记录或 Node.NextList 派发链恢复步骤。"""

from __future__ import annotations

from datetime import datetime

from .model import ParsedLog, Step


def _drop_sub_recognition_attempts(out: ParsedLog, task_id: int | None) -> int:
    """丢掉**不是本任务候选**的识别记录——复合识别（`Or` / `And` 引用别的节点）的子识别。

    判据只看 `Node.NextList` 的候选表与派发者，并且只丢活动区间内的记录；没有 NextList
    或 task_id 为空时全留，避免把别的任务识别误删。返回丢掉的条数。
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
