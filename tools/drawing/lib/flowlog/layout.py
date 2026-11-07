"""版面计算：连续同名折叠、分轮、轮内顺序与父节点分组、重复轮折叠签名。

输入是 `Step` 列表，输出是列/框的排列与坐标，不涉及像素；`draw` 只消费这里
给出的位置。分轮规则变了会直接改产物，改动后按 `--list` 与出图核对。"""

from __future__ import annotations


from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .model import Step

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import ParsedLog
    from .render import FlowOptions


# --------------------------------------------------------------------------- #
# 分轮 + 折叠
# --------------------------------------------------------------------------- #


def collapse_runs(steps: list[Step], collapse: int) -> list[Step]:
    if collapse <= 1:
        return steps
    out: list[Step] = []
    for s in steps:
        if (
            out
            and out[-1].name == s.name
            and out[-1].kind == s.kind
            and out[-1].completed == s.completed
        ):
            out[-1].run_len += 1
            out[-1].texts = list(dict.fromkeys(out[-1].texts + s.texts))
            continue
        out.append(Step(**s.__dict__))
    return out


def build_rounds(
    steps: list[Step],
    min_len: int,
    cut_on: set[str] | None = None,
    hard_cut: set[str] | None = None,
) -> list[list[Step]]:
    """回到本轮已出现过的节点 = 新一轮。

    cut_on 只放父节点（有 next 表的派发者），避免 TrainMoveUp 这类高频叶子把一轮劈开；
    hard_cut 放"本身就是循环节拍"的叶子节点（例如自动代理里的 BattleIsLoading），
    它每出现一次就开一轮。
    """
    rounds: list[list[Step]] = []
    cur: list[Step] = []
    seen: set[str] = set()
    hard = hard_cut or set()
    for s in steps:
        cut = False
        if (
            s.name in hard
            and cur
            or s.name in seen
            and len(cur) >= min_len
            and (cut_on is None or s.name in cut_on)
        ):
            cut = True
        if cut:
            rounds.append(cur)
            cur, seen = [], set()
        cur.append(s)
        seen.add(s.name)
    if cur:
        rounds.append(cur)
    return rounds


def choose_cut_set(steps: list[Step], parents: set[str], min_len: int) -> set[str]:
    """自动选分轮锚点：优先父节点；若这样只有一轮，就把最高频的节点也算上。"""
    counts: dict[str, int] = {}
    for s in steps:
        counts[s.name] = counts.get(s.name, 0) + 1
    cut = {n for n, c in counts.items() if c >= 2 and n in parents}
    if len(build_rounds(steps, min_len, cut)) <= 1 and counts:
        name, c = max(counts.items(), key=lambda kv: kv[1])
        if c >= 3:
            cut.add(name)
    return cut


def roles(steps: list[Step], repeat_threshold: int) -> tuple[set[str], set[str]]:
    """返回 (父节点集合, 重要节点集合)。"""
    counts: dict[str, int] = {}
    for s in steps:
        counts[s.name] = counts.get(s.name, 0) + 1
    important = {n for n, c in counts.items() if c >= repeat_threshold}
    parents = {s.dispatcher for s in steps if s.dispatcher}
    return parents, important


def round_steps(parsed: ParsedLog, opts: FlowOptions) -> list[Step]:
    """要分轮的步骤。

    仅事件模式下 `steps` 本身就是识别记录，每列只有一步，`compose_columns` 里的折叠
    （`--collapse`）永远不会生效——用户会以为这个开关坏了。所以这里先折一次：
    默认把连续同节点的识别并成一格（`未命中 ×N`），`--collapse 1` 保留每次识别各占一列
    （要看每次失败的时间点就用它）。普通日志原样返回。
    """
    if parsed.derived_from:
        return collapse_runs(parsed.steps, opts.collapse)
    return parsed.steps


def anchor_of(steps: list[Step], rounds: list[list[Step]]) -> str:
    """每轮开头的节点里出现次数最多的那个，作为图的"循环锚点"。"""
    heads: dict[str, int] = {}
    for r in rounds:
        if r:
            heads[r[0].name] = heads.get(r[0].name, 0) + 1
    if not heads:
        return ""
    return max(
        heads.items(),
        key=lambda kv: (kv[1], -min(i for i, s in enumerate(steps) if s.name == kv[0])),
    )[0]


def classify(step: Step) -> str:
    if step.kind == "miss":
        return "miss"
    if step.kind == "event":
        if (
            not step.completed
            or "timeout" in step.name
            or step.name in ("invalid node id", "node not completed")
        ):
            return "error"
        if step.name.startswith("任务"):
            return "task"
        return "event"
    if step.algorithm == "Custom":
        return "custom"
    if step.algorithm == "DirectHit":
        return "direct"
    return "hit"


def task_span(
    parsed: ParsedLog, task_id: int | None
) -> tuple[datetime, datetime] | None:
    """选中任务的**判定窗口** `[起, 止]`；没给 `--task-id`、或日志里没有它的步骤时返回 None。

    存在的理由：识别记录（`reco [result=]`）里没有 `task_id`，框架级事件（`Task timeout` /
    `invalid node id` / `node not completed`）也没有，于是未命中聚合与事件挂载只能用时间窗。
    时间窗右端原来是"整份日志最后一个动作/事件"，一份装了多次运行的导出就会把**别的任务**的
    未命中与失败事件算进这一轮——实测 13:11 那次维修的末轮里混进了 23:59 那趟训练的
    `未命中 ×9/×10` 与 `node not completed`（那是 task 200000056 的事）。

    右端优先取**该任务自己的 `task end`**：任务失败后框架不再为它记任何东西，所以它之后出现的
    框架级事件一定属于别的任务（实测 200000054 的图里混进了同一运行里 200000062 的
    `Task timeout EntrustEnterMainLine` 与 `invalid node id`——那两条是 28 秒后另一个任务的事）。
    日志里没有它的 `task end` 时退回"本任务最后一步之后、别的任务的第一步"（自检里那条 00:35:02 的
    `invalid node id` 就是这么保住的：任务失败恰恰发生在最后一步之后，按自己最后一步切会剪掉它）；
    也没有别的任务的步骤时右端取 `datetime.max`（等于不设右界，与改动前一致）。
    """
    if task_id is None:
        return None
    own = [s.ts for s in parsed.steps if s.task_id == task_id]
    if not own:
        return None
    lo, last_step = min(own), max(own)
    ends = [ts for ts, _entry, tid, _ret in parsed.task_ends if tid == task_id]
    if ends:
        return lo, max(last_step, max(ends))
    later = [ts for ts in parsed.stats.other_task_ts if ts > last_step]
    return lo, min(later) if later else datetime.max


def compose_columns(
    parsed: ParsedLog,
    rounds: list[list[Step]],
    miss_min: int,
    collapse: int = 1,
    miss_max: int = 8,
) -> list[list[Step]]:
    """每轮 = 该轮命中步骤（按需折叠 ×N）与"连续未命中"聚合框按时间合并；异常事件挂到所属轮末尾。"""
    tail = max(
        [a.ts for a in parsed.attempts]
        + [e.ts for e in parsed.events]
        + [rounds[-1][-1].ts]
    )
    span = None if parsed.derived_from else parsed.span

    def miss_rows(i: int) -> list[Step]:
        if parsed.derived_from:
            # 仅事件模式下 steps 本身就是识别记录，再聚合一次同一批未命中会画重
            return []
        lo = rounds[i][0].ts
        hi = rounds[i + 1][0].ts if i + 1 < len(rounds) else tail
        if span:
            lo, hi = max(lo, span[0]), min(hi, span[1])
        agg: dict[tuple[str, str], list[Step]] = {}
        for a in parsed.attempts:
            if a.kind != "miss" or not (lo <= a.ts < hi):
                continue
            agg.setdefault((a.dispatcher, a.name), []).append(a)
        rows: list[Step] = []
        for (disp, name), items in agg.items():
            if len(items) < miss_min:
                continue
            rows.append(
                Step(
                    ts=items[-1].ts,
                    name=name,
                    completed=False,
                    kind="miss",
                    run_len=len(items),
                    dispatcher=disp,
                    candidates=list(items[-1].candidates),
                    jumps=list(items[-1].jumps),
                    parent_source=items[-1].parent_source,
                    reco_id=items[-1].reco_id,
                    box=items[-1].box,
                    # 这一组里只要有一次踩在「Custom 回调没注册」上就标出来（取第一条非空的）
                    agent_missing=next(
                        (a.agent_missing for a in items if a.agent_missing), ""
                    ),
                )
            )
        rows.sort(key=lambda s: (-s.run_len, s.ts))
        rows = rows[:miss_max] if miss_max else rows
        rows.sort(key=lambda s: s.ts)
        return rows

    def merge_by_ts(hits: list[Step], misses: list[Step]) -> list[Step]:
        """把未命中聚合框按时间插回本轮，而不是一律缀在末尾。

        末轮常见形状是「命中几步 → 连续多轮全未命中 → 某个候选终于命中 → 失败」，
        一律缀在末尾会让图上读成"已经失败了还在重试"，时序颠倒。`sorted` 稳定，
        时间戳相同的命中框仍排在未命中框前面。
        """
        return sorted(hits + misses, key=lambda s: s.ts)

    columns = [
        merge_by_ts(collapse_runs(list(r), collapse), miss_rows(i))
        for i, r in enumerate(rounds)
    ]
    for ev in parsed.events:
        if ev.kind != "event" or classify(ev) != "error":
            continue
        for col in reversed(columns):
            if col and col[0].ts <= ev.ts:
                # 按时间插回去，不是一律缀在列末：`Node.Action.Failed` 比节点跑完更早
                # （实测早 3.4 s），缀在末尾会让图上读成"节点跑完才失败"。时间戳相同的
                # 仍排在命中框之后（`insert` 只找第一个更晚的），与 merge_by_ts 的口径一致。
                at = next((k for k, s in enumerate(col) if s.ts > ev.ts), len(col))
                col.insert(at, ev)
                break
    return columns


def parent_groups(column: list[Step]) -> list[tuple[str, int, int]]:
    """把一列里**连续同父**的步骤归成一组，返回 [(父, 起始下标, 结束下标)]。

    图上给每组套一个浅色框、左上角压一个实心角标写父节点名——父子关系直接看得见，不用逐框读小字。
    连续才算一组：同一轮里 `TrainingEnterOne` 派发 3 步、`TrainingEnterFormation` 派发 1 步，
    就是两组；同一个父节点跨轮重复出现（`BattleWin` 在第 2/3/4 轮各派发一次）就是 3 个各自独立的组。

    不归组的情况三种：没有父节点（`父 未记录`）、父节点就是自己（自派发/自循环，角标会写自己的名字）、
    任务起止框（`kind == "task"`，它们不是被派发出来的）。
    """
    groups: list[tuple[str, int, int]] = []
    start = -1
    current = ""  # 当前这一组的父节点；只有它变了才收组
    for i, st in enumerate(column):
        if st.parent and not st.dispatched and st.kind != "task":
            if start < 0:
                start, current = i, st.parent
                continue
            if st.parent == current:
                continue
            # 父节点换了：收上一组，本步开新组
            groups.append((current, start, i - 1))
            start, current = i, st.parent
            continue
        if start >= 0:
            groups.append((current, start, i - 1))
            start, current = -1, ""
    if start >= 0:
        groups.append((current, start, len(column) - 1))
    return groups


@dataclass(frozen=True)
class ColumnSpan:
    """一列在原始轮次里覆盖的范围；`first != last` 说明这一列是合并出来的。"""

    first: int
    last: int

    @property
    def count(self) -> int:
        return self.last - self.first + 1

    @property
    def merged(self) -> bool:
        return self.last > self.first


def column_signature(column: list[Step]) -> tuple[tuple[str, str, int, bool], ...]:
    """一列的可比较形状：名字、种类、折叠次数、是否完成。

    四个都要进签名，少一个就会把不同的事折成一列：
      * 名字 + 种类：`未命中（聚合）` 与"真跑过这个节点"是两件事；
      * `run_len`：折叠成一格的 `×3` 与原样三格不是同一个东西；
      * `completed`：跑完与被中止的同一节点也该分开。

    **不做子序列匹配**：只从列首逐项比。`A,B,C,D,E` 与 `B,C,D,E` 因此是两种形状
    （真日志里"前缀差一步"很常见，但把短的那轮塞进长的那轮会让列头写的轮号区间与实际首尾不符）。
    """
    return tuple((st.name, st.kind, st.run_len, st.completed) for st in column)


def _has_failure(column: list[Step]) -> bool:
    """这一列里有失败框（`node not completed` / `Task timeout` / `invalid node id` 等）。

    失败的轮一律不折叠：折进 `×N` 里等于把"这次崩了"藏起来，而读图的人恰恰先找它。
    """
    return any(classify(st) == "error" for st in column)


def overlay_repeats(
    columns: list[list[Step]],
    rounds: list[list[Step]],
    has_thumb: Callable[[int], bool],
    min_span: int = 2,
) -> list[tuple[list[Step], ColumnSpan]]:
    """把连续、同签名、整轮无贴图、无失败框的轮次合并成一列，返回 [(记这一列的步骤, 轮次范围)]。

    四个条件缺一不可，且**任一侧不满足就在那里断开**（不是整段作废）：
      1. 签名相同（`column_signature()`，从列首对齐）；
      2. 相邻（中间夹了别的形状的轮就断）；
      3. 组里每一轮都没贴到图——有图本身就是信息，折掉就看不出"这几轮有画面、那几轮没有"；
      4. 组里每一轮都没有失败框。
    组内轮数 < `min_span` 的不合并（`min_span = 2` 表示"连续两轮才开始折"）。

    `has_thumb(i)` 问的是**第 i 轮的实际贴图结果**，所以只能在 `draw_flow()` 算完 `thumb_of`
    之后调用：贴图取决于 `--vision` / `--on-error` 的真实命中（含失败现场图回贴到命中节点那条规则），
    提前按"有没有传 --vision"判断会把其实有图的轮折掉。

    合并列直接用**首轮**的步骤列表（时间戳是真实的，不改 `Step`、不复制），所以分组框、候选行、
    连线都按首轮画——组内各轮同形，这是预期行为。
    """
    if min_span < 2:
        return [(list(col), ColumnSpan(i, i)) for i, col in enumerate(columns)]

    out: list[tuple[list[Step], ColumnSpan]] = []
    i = 0
    while i < len(columns):
        sig = column_signature(columns[i])
        end = i
        while end + 1 < len(columns):
            k = end + 1
            if column_signature(columns[k]) != sig:
                break
            if has_thumb(k) or has_thumb(end):
                break
            if _has_failure(columns[k]) or _has_failure(columns[end]):
                break
            end = k
        if end - i + 1 < min_span or has_thumb(i) or _has_failure(columns[i]):
            # 只有一轮可合并（或首轮自己就有图/有失败）→ 原样留着，下一轮重新起头
            out.append((list(columns[i]), ColumnSpan(i, i)))
            i += 1
            continue
        out.append((list(columns[i]), ColumnSpan(i, end)))
        i = end + 1
    return out
