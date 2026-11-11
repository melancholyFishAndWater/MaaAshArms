"""日志侧的纯数据结构与小工具：`Step` / `ParsedLog` / `Dispatcher` / `NextListStat` /
`Note`，以及时间戳解析、候选名归一化。

不读文件、不画图；解析与渲染都只依赖这里的形状。字段含义写在各自 docstring 里。"""

from __future__ import annotations


import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from .constants import NEXTLIST_MAX_GAP_SECONDS, NEXTLIST_RE, PARENT_CHOICE_LIMIT


def parse_ts(s: str) -> datetime:
    """`2026-11-11 02:11:48.724` → datetime（毫秒位数不限，也可没有）。

    日志行首那个格式本身就是 ISO 8601，所以走 `fromisoformat`（C 实现）：本机 30 万次
    0.07 s，`strptime` 要 2.18 s——一份 15.5 MB 的日志有 9 万多行，光这一项差 1.2 s。
    `fromisoformat` 不认的写法（罕见）才回退 `strptime`，行为与原来一致。
    """
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")


def collect_texts(detail, out: list[str]) -> None:
    """把识别结果 detail 里的 OCR 文本 / 模板名等抓出来。"""
    if isinstance(detail, dict):
        for k, v in detail.items():
            if k == "text" and isinstance(v, str):
                if v.strip():
                    out.append(v.strip())
            else:
                collect_texts(v, out)
    elif isinstance(detail, list):
        for v in detail:
            collect_texts(v, out)


def short(texts: Iterable[str], limit: int = 26) -> str:
    uniq = list(dict.fromkeys(t for t in texts if t))
    if not uniq:
        return ""
    s = " | ".join(uniq)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# 父节点（派发者）：`Node.NextList.*` 事件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Dispatcher:
    """一条 `Node.NextList.*` 记录：谁在评估 next 表、那一刻排着什么。

    `kind` 是这条事件的类型：`starting` = 开始评估、`succeeded` = 有候选命中、`failed` = 整个候选表
    一个都没命中。**同一条候选表上连续的 `failed` 就是"卡在派发层"的直接证据**（任务一直重试候选、
    没有任何子节点跑起来），所以它要单独统计，不能只用来推父节点。
    """

    ts: datetime
    task_id: int | None
    px: str
    name: str
    candidates: tuple[str, ...]
    jumps: tuple[str, ...]
    kind: str = "starting"


@dataclass
class NextListStat:
    """一个 (task_id, 派发者) 的候选表评估账。

    `candidates` 是第一次见到的候选表（入口那一轮往往是 `[自己]` 的自派发，不代表真实候选），
    `failed_candidates` 记的是**全未命中那一轮**的候选表——排查"卡在派发层"时要看的是这一份。
    """

    name: str
    candidates: tuple[str, ...] = ()
    failed_candidates: tuple[str, ...] = ()
    starting: int = 0
    succeeded: int = 0
    failed: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def real_candidates(self) -> tuple[str, ...]:
        """真实候选表：优先取全未命中那轮的，其次取第一次见到的。"""
        return self.failed_candidates or self.candidates


def parse_nextlist(msg: str, ts: datetime, px: str) -> Dispatcher | None:
    """把一行 `Node.NextList.*` 解析成 Dispatcher；不是这种行或读不出 JSON 时返回 None。

    **agent 进程那种形态判不了父节点**：它的 `details_json` 只有 `focus` + `list`，
    没有 `name`（谁在派发）也没有 `task_id`（实测本机日志 16 条全是这样），所以这里返回 None
    而不是猜一个。能判父节点的只有 MFA 侧带 `name` 的那份。
    """
    match = NEXTLIST_RE.search(msg)
    if not match:
        return None
    try:
        detail = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(detail, dict):
        return None
    name = detail.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_list = detail.get("list")
    entries = (
        [e for e in raw_list if isinstance(e, dict)]
        if isinstance(raw_list, list)
        else []
    )
    candidates = tuple(e["name"] for e in entries if isinstance(e.get("name"), str))
    jumps = tuple(
        e["name"]
        for e in entries
        if e.get("jump_back") and isinstance(e.get("name"), str)
    )
    raw_task = detail.get("task_id")
    task_id = raw_task if isinstance(raw_task, int) else None
    return Dispatcher(
        ts=ts,
        task_id=task_id,
        px=px,
        name=name,
        candidates=candidates,
        jumps=jumps,
        kind=match.group("kind").lower(),
    )


def resolve_parent(
    pending: list[Dispatcher], task_id: int | None, name: str, ts: datetime
) -> tuple[Dispatcher | None, float]:
    """找出这一步是被谁派发的：同 task 的记录里，哪一条的候选表里有这个节点名。

    返回 (记录, 间隔秒数)；取不到时给 (None, inf)。规则有三条，都是为了别把父节点认错：

    - 只认**这个 task_id** 的记录。同一条 trace 里常并排跑多个任务，别的任务的候选表里有同名节点
      （`BaseProbeBlackPage`、`BaseMoveUp` 这类各任务共用的节点）时，按名字跨任务匹配会认错爹。
    - 优先取**不晚于这一步**的最近一条：晚于它的记录说明那时候选表已经换过，不再是它的父节点。
    - 间隔超过 `NEXTLIST_MAX_GAP_SECONDS` 就不认，免得拿几十分钟前那一轮的记录当父节点。
    """
    if not pending or not name or task_id is None:
        # `task_id is None` 直接不认：没有 task 就没法排除"别的任务里同名的公共节点"，
        # 而框架级失败事件（Task timeout 等）正是 task_id 为空的那批。
        return None, float("inf")
    same_task = [d for d in pending if d.task_id == task_id]
    past = [d for d in same_task if d.ts <= ts and name in d.candidates]
    pick = max(past, key=lambda d: d.ts) if past else None
    if pick is None:
        # 这一步的父节点记录在本窗口里没留下（日志被截断 / 只导出了一段）
        return None, float("inf")
    gap = (ts - pick.ts).total_seconds()
    return (pick, gap) if gap <= NEXTLIST_MAX_GAP_SECONDS else (None, float("inf"))


@dataclass(frozen=True)
class CustomMissing:
    """一次「Custom 回调没注册」：框架打的那两条 ERR。

    `kind` = reco（识别未注册，该候选判 miss）/ act（动作未注册，该节点动作返回 false）。
    `node` 是**节点名**不是注册名（已与 pipeline 的 `custom_recognition`/`custom_action` 对照），
    所以归属不需要绕注册名。
    """

    ts: datetime
    px: str
    node: str
    reg: str
    kind: str


@dataclass
class Step:
    """一次"节点跑完"的记录。"""

    ts: datetime
    name: str
    completed: bool
    reco_id: int | None = None
    action_id: int | None = None
    node_id: int | None = None
    task_id: int | None = None
    algorithm: str = ""
    box: list[int] | None = None
    texts: list[str] = field(default_factory=list)
    action: str = ""
    action_ok: bool | None = None
    dispatcher: str = ""
    run_len: int = 1  # 折叠后代表连续执行了几次
    kind: str = "hit"  # hit | miss | event
    # 这一步是被谁的 next 表派发出来的（父节点）+ 那一刻排着的完整候选表。
    # 来源是 `Node.NextList.*` 事件（`name` = 派发者、`list` = 候选），`[cur_node_=X]` 只作退回。
    candidates: list[str] = field(default_factory=list)
    jumps: list[str] = field(default_factory=list)  # 候选里带 `[JumpBack]` 的那些
    # nextlist | walk | entry | none：归到父节点靠的是哪一层证据（读图的人要能看出可信度）
    parent_source: str = ""
    # 非空 = 这一步踩在「Custom 回调没注册」上（值是该回调的注册名）。由 `_mark_agent_missing` 填。
    # 存在理由见 README 的「agent 缺位」：识别缺位与"纯超时"、动作缺位与"pipeline 自己的问题"
    # 在图上一模一样，读图的人会往 ROI/UI 方向查。
    agent_missing: str = ""

    # 疑似过时的节点名（整张图共用一份，由 `_apply_stale` 填）。不写成 dataclass 字段：
    # 它不属于"这一步"，而 `Step` 会被 `overlay_repeats` 大量重造，加字段要处处带参数。
    stale_names: ClassVar[frozenset[str]] = frozenset()

    @property
    def parent(self) -> str:
        """父节点 = 本轮派发者。**不是配置里的层级**：`next` 是有序候选集，
        同一节点可被多处派发、`[JumpBack]` 会弹回上层，所以这是调用图而不是树。"""
        return self.dispatcher

    @property
    def dispatched(self) -> bool:
        """父节点就是这个节点自己：入口那一轮的框架合成自派发，或配置的自循环。"""
        return bool(self.dispatcher) and self.dispatcher == self.name

    def parent_detail(self, full: bool = False) -> str:
        """框里那一行要写什么：派发者名字已经进了分组框角标，这里只留候选（或"未记录"）。

        符号只用 msyh 里**确实有字形**的：`√` 与 `弹回`（实测 `✓` `↩` 这类大多画成方框，
        见 `.agents/project.md` 那条"图上不要用 ⚠ 这类符号"）。本次跑的是候选表里**第一个同名项**，
        重名时标不准（`collapse_runs` 合并不了同名候选的归属）。
        """
        if not self.parent:
            return "父 未记录"
        if self.dispatched:
            return f"父 {self.parent}（自派发/自循环）"
        if not self.candidates:
            return f"父 {self.parent}"
        # 按**位置**标本次跑的那一项（同一个名字可能在候选表里出现多次），不能按名字 index()
        mark = next((i for i, n in enumerate(self.candidates) if n == self.name), -1)
        parts = [
            f"{'√' if i == mark else ''}{n}{'弹回' if n in self.jumps else ''}"
            for i, n in enumerate(self.candidates)
        ]
        if not full and len(parts) > PARENT_CHOICE_LIMIT:
            rest = len(parts) - PARENT_CHOICE_LIMIT
            parts = parts[:PARENT_CHOICE_LIMIT] + [f"另 {rest} 项"]
        return "候选：" + " ・ ".join(parts)

    @property
    def detail(self) -> str:
        bits: list[str] = []
        if self.kind == "miss":
            bits.append(f"未命中 ×{self.run_len}" if self.run_len > 1 else "未命中")
        elif self.kind == "event":
            bits.append(self.action or "")
        else:
            bits.append(f"{self.ts.strftime('%H:%M:%S.%f')[:-3]}")
            if self.run_len > 1:
                bits.append(f"×{self.run_len}")
            if self.algorithm:
                bits.append(self.algorithm)
            if self.action and self.action not in ("DoNothing",):
                bits.append(
                    self.action
                    if self.action_ok is None
                    else f"{self.action}{'' if self.action_ok else '(失败)'}"
                )
            elif self.action_ok is False:
                # 动作没派发时结果里没有动作名（`action=""`、`action_id=0`），上面那支整段跳过，
                # 失败就一点痕迹都不留（实测 `GiftChooseFirstGift` 只剩一条 `node not completed`）。
                bits.append("动作未派发" if self.action_id == 0 else "动作失败")
        if self.texts:
            bits.append(short(self.texts))
        if self.agent_missing:
            # 只写这四个字：框宽有限，注册名进注记（`agent_missing_note`）里说
            bits.append("agent 缺位")
        return "  ".join(b for b in bits if b)


@dataclass
class ParseStats:
    """解析计数。

    存在的理由：一句话"窗口内没有解析到任何节点执行记录"曾经盖住三种完全不同的病因
    （时间戳不单调、输入本来就没有节点级记录、`--from/--to/--task-id` 写错），
    排查的人只能自己切日志试。这些计数让报错能说清"到底看到了什么"。
    """

    source_kind: str = ""  # file | path-missing | text | stream | other
    source_name: str = ""  # 入参的短写，只进报错文案
    lines_total: int = 0  # 匹配 LINE_RE 的行（与窗口无关）
    lines_in_window: int = 0
    lines_other_task: int = 0  # node done 但 task_id 不是要看的那个
    lines_internal_skipped: int = 0  # recognition/OCR/... 这类内部识别，默认过滤
    out_of_order: int = 0  # 时间戳回跳的行数（>0 说明日志不是按时间追加的）
    truncated_skipped: int = 0  # 头对得上、整条读不出，已丢弃
    truncated_kept: int = 0  # 读出来了但尾部缺字段，已标注
    events_dropped: int = 0  # 被 --task-id 排掉的其他任务事件
    # 父节点（派发者）的归属账：`Node.NextList` 事件条数，以及三层退回各命中多少步
    nextlist_seen: int = 0
    parent_resolved: int = 0
    parent_fallback_walk: int = 0
    parent_fallback_entry: int = 0
    parent_unknown: int = 0
    # 由派发顺序推出来的父节点（`derived_from="nextlist"` 时才有）：不是 NextList 里记着的那条，
    # 单列一项，不算进"父节点没找到"的账。
    parent_derived: int = 0
    # 被 task 过滤掉的步骤时间：判定窗口要用"下一个别的任务的第一步"当右界，而 steps 里只剩本任务的
    other_task_ts: list[datetime] = field(default_factory=list)
    first_ts: datetime | None = None
    last_ts: datetime | None = None


@dataclass
class ParsedLog:
    steps: list[Step] = field(default_factory=list)
    attempts: list[Step] = field(default_factory=list)  # 含未命中
    events: list[Step] = field(default_factory=list)
    tasks: list[tuple[datetime, str, int, str]] = field(default_factory=list)  # start
    task_ends: list[tuple[datetime, str, int, bool]] = field(default_factory=list)
    window: tuple[datetime, datetime] | None = None
    stats: ParseStats = field(default_factory=ParseStats)
    # 非空表示 steps 是由识别记录（attempts）合成的：这份日志没有 PipelineTask node done
    derived_from: str = ""
    derived_clamped: bool = False  # 合成时按目标 task 的起始时间夹住了范围
    # 选中任务的起止（`--task-id` 且日志里有它的步骤时才有）：
    # 未命中聚合与事件挂载都靠它把窗口夹在任务自己的活动区间内，见 task_span()
    span: tuple[datetime, datetime] | None = None
    # (task_id, 派发者) -> 候选表评估账。`failed` 连续出现 = 卡在派发层（候选一个都没命中），
    # 是"node done 很少却过了很久"那类任务唯一能拿到的证据（`reco` 行没有 task_id）。
    nextlist_stats: dict[tuple[int | None, str], NextListStat] = field(
        default_factory=dict
    )
    # agent 缺位：日志里的「Custom 回调没注册」记录，以及 agent 客户端的生死变更
    # （`(时刻, 进程, 是否在场)`）。两者都只进注记，不需要进图。
    custom_missing: list[CustomMissing] = field(default_factory=list)
    agent_state: list[tuple[datetime, str, bool]] = field(default_factory=list)
    # 运行期 `override_pipeline` 覆写里出现过的节点名（`BaseErrorNextReco` 会建出
    # `<父节点>ErrorNext`）。只用于「疑似过时」：这些节点在 pipeline JSON 里没有属正常，
    # 不能和"真的改名/删除了"混成一条"已找不到"。见 `logparse._override_names`。
    override_nodes: set[str] = field(default_factory=set)


def source_kind_of(source: Any) -> str:
    """入参形态，只用于报错文案：file / path-missing / text / stream / other。"""
    if hasattr(source, "read"):
        return "stream"
    text: Any = os.fspath(source) if isinstance(source, os.PathLike) else source
    if not isinstance(text, str):
        return "other"
    if os.path.isfile(text):
        return "file"
    if "\n" not in text and "\r" not in text and len(text) <= 4096:
        return "path-missing"
    return "text"


def _source_label(source: Any) -> str:
    """入参的短写，只进报错文案：路径给路径，正文给前 60 字。"""
    if hasattr(source, "read"):
        return f"<文本流 {type(source).__name__}>"
    text: Any = os.fspath(source) if isinstance(source, os.PathLike) else source
    if not isinstance(text, str):
        return f"<{type(source).__name__}>"
    if os.path.isfile(text) or ("\n" not in text and len(text) <= 260):
        return text
    return text[:60].replace("\n", " ") + "…"


@dataclass(frozen=True)
class Note:
    """图上第二行的一条诊断注记。

    带 `kind` 是为了**按类型上色**：顶部注记一多，全用一个颜色就读不出哪句是"前提"、
    哪句是"警告"、哪句只是"这次用的资源版本"。
    """

    text: str
    kind: str = "info"  # info | version | stale | agent | derive | time | fail

    def __str__(self) -> str:
        """插值时给正文，不给 dataclass 的 repr。

        `--list` 末尾那几行是 `f"提示：{note}"`（`render.render_flow` 的 `list_only` 分支），
        没有 `__str__` 时打出来的是 `Note(text='…', kind='stale')`，读的人看不到正文。
        `repr(note)` 仍是 dataclass 自动生成的那个，排查时不影响。
        """
        return self.text


def _fmt_ts(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ts else "?"
