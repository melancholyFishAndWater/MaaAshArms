#!/usr/bin/env python
"""把 MaaFramework 的 maafw.log 还原成"实际执行流程图"。

输入：一份 maafw.log（`[时间][级别][Px..][Tx..][文件][L行][函数] 消息` 格式）+ 可选的 vision 目录
（`debug/vision/<时间>_<节点名>_<reco_id>.jpg`，里面是识别时的截图、ROI 框和模板缩略图）。
输出：一张按时间线排列的 PNG 流程图。

布局约定：
  * 横向 = 轮次（round），从左到右，**不换行**：宽度不够就一直往右延伸（要换行用 --cols-per-band N）；
    纵向 = 一轮内的执行顺序，从上到下。
  * 端口（--ports pos，默认）按相邻方向定：
      - 同一列里上下相邻的节点 → **上入下出**（出口 = 下中，入口 = 上中）；
      - 跨轮左右相邻的节点 → **左入右出**（出口 = 右中，入口 = 左中，竖线走两列之间的空档）。
    --ports role 保留旧规则：重要节点（跨轮重复出现）左下出/右上进，父节点（有 next 的派发者）
    左下出/右下进。
  * 未命中/超时/失败这类框之间留更大的间距（--gap-error），不跟正常步骤挤在一起。

贴图（都可以不给，也可以只给其中一半）：
  * --vision 目录里的 `<时间>_<节点名>_<reco_id>.jpg` 按 reco_id 精确对齐，裁出当时的识别区域贴进框；
  * 未命中的聚合框贴"最后一次尝试"那张；框架没存图的节点（Custom / DirectHit）退到同节点的
    `*_wait_freezes.jpg`、再退到同一时刻的邻近帧，角标写"邻近帧"；
  * --on-error 目录里的 `<时间>_<节点名>.png` 是失败现场图：优先按名字贴回**被记录的那个节点**，
    同时贴到超时/失败事件框上，角标写"失败现场"；
  * 连 --vision 都不给也能出图（纯节点流程图）；只给 --on-error 时，那张/那几张现场图会贴到对应节点上；
  * --vision / --on-error 既能指目录本身，也能指它们的父目录（例如 `debug/`），子目录自动识别。

用法示例：
  python tools/drawing/flow_from_log.py D:/MaaAshArms/debug/maafw.log \
      --vision D:/MaaAshArms/debug/vision \
      --from "2026-10-11 21:39:56" --to "2026-10-11 21:43:29" \
      --out flow_escort.png

只做解析、不画图：加 --list。

输出默认落到**仓库根的 `temp/flow/`**（不再写到日志旁边）；`--out` / `--outdir` 可覆盖。

作为库调用（遥测侧就这么用——远程没有本地日志文件，只有 Sentry 取回的日志正文）：

  from tools.drawing.flow_from_log import FlowOptions, MediaResolver, render_flow

  # 日志正文可以直接传字符串；图片证据可以是目录、zip/多卷、或内存里的字节
  res = render_flow(log_text,
                    vision=MediaResolver(["log-part01.zip", "log-part02.zip"]),
                    options=FlowOptions(t_from=t0, t_to=t1, task_id=200000118))
  print(res.path, res.width, res.height, res.steps)

批量与幂等（同一个证据包重跑会跳过）用 `python tools/drawing/flow_cases.py`，
经 `python -m tools.sentry.cli flow` 也能调用，产出目录一致。

**远程遥测拿不到 vision 识别帧**：MFA 的失败附件显式跳过 `vision/`，且图片包上限
5 MiB（本机 `debug/vision` 有 248.6 MB）。所以从 Sentry 取回的证据只能画
"节点循环 + on_error 失败现场图"；要看识别帧得让对方手动导出（勾"包含 vision 图片"）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 渲染器版本：只要改动会影响产物内容（版面、注记、解析口径）就 +1。
# flow_cases 把它写进 manifest 指纹——不 bump 的话同一个证据包会命中旧记录直接"跳过（已画过）"，
# 渲染器改了却不重画。
# 3：任务判定窗口的右端改用本任务自己的 task end。4：框里多了"父节点"一行。
# 5：父节点从"一行小字"变成浅色分组框 + 左上角实心角标。
RENDERER_VERSION = 5

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
NODE_DONE_RE = re.compile(r"PipelineTask node done \[result=(\{.*\})\] \[task_id_=(\d+)\]\s*$")
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
    r"node\.nextlist\.(?:starting|succeeded|failed)\b.*?\[details(?:_json)?=(\{.*\})\]\s*$",
    re.IGNORECASE,
)
TIMEOUT_RE = re.compile(
    r"Task timeout \[pretask\.name=(?P<name>[^\]]+)\]"
    r"(?: \[duration_since\(start_clock\)=(?P<dur>\d+)(?P<dur_ms>ms)?\])?"
    r"(?: \[pretask\.reco_timeout=(?P<tout>\d+)(?P<tout_ms>ms)?\])?"
)
INVALID_RE = re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]")
NOT_COMPLETED_RE = re.compile(r"node not completed, handle error \[node\.name=([^\]]+)\]")

# 这些行整条匹配才算完整；只对上"头部"说明导出按字节裁过（Explore CSV 的常见下场）
TRUNCATION_HEADS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("reco [result=", RECO_RE),
    ("action [i=", ACTION_RE),
    ("PipelineTask node done", NODE_DONE_RE),
    ("task start:", TASK_START_RE),
    ("task end:", TASK_END_RE),
)

INTERNAL_NAME_RE = re.compile(r"^recognition/(OCR|TemplateMatch|ColorMatch|FeatureMatch)/[0-9a-f\-]+$")


def parse_ts(s: str) -> datetime:
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
    """一条 `Node.NextList.*` 记录：谁在评估 next 表、那一刻排着什么。"""

    ts: datetime
    task_id: int | None
    px: str
    name: str
    candidates: tuple[str, ...]
    jumps: tuple[str, ...]


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
        detail = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(detail, dict):
        return None
    name = detail.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_list = detail.get("list")
    entries = [e for e in raw_list if isinstance(e, dict)] if isinstance(raw_list, list) else []
    candidates = tuple(e["name"] for e in entries if isinstance(e.get("name"), str))
    jumps = tuple(e["name"] for e in entries
                  if e.get("jump_back") and isinstance(e.get("name"), str))
    raw_task = detail.get("task_id")
    task_id = raw_task if isinstance(raw_task, int) else None
    return Dispatcher(ts=ts, task_id=task_id, px=px, name=name, candidates=candidates, jumps=jumps)


def resolve_parent(pending: list[Dispatcher], task_id: int | None, name: str,
                   ts: datetime) -> tuple[Dispatcher | None, float]:
    """找出这一步是被谁派发的：同 task 的记录里，哪一条的候选表里有这个节点名。

    返回 (记录, 间隔秒数)；取不到时给 (None, inf)。规则有三条，都是为了别把父节点认错：

    - 只认**这个 task_id** 的记录。同一条 trace 里常并排跑多个任务，别的任务的候选表里有同名节点
      （就位链的 `LoopErrorByTimes`、`MoveUp` 这类公共节点）时，按名字跨任务匹配会认错爹。
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
        parts = [f"{'√' if i == mark else ''}{n}{'弹回' if n in self.jumps else ''}"
                 for i, n in enumerate(self.candidates)]
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
                bits.append(self.action if self.action_ok is None else f"{self.action}{'' if self.action_ok else '(失败)'}")
        if self.texts:
            bits.append(short(self.texts))
        return "  ".join(b for b in bits if b)


@dataclass
class ParseStats:
    """解析计数。

    存在的理由：一句话"窗口内没有解析到任何节点执行记录"曾经盖住三种完全不同的病因
    （时间戳不单调、输入本来就没有节点级记录、`--from/--to/--task-id` 写错），
    排查的人只能自己切日志试。这些计数让报错能说清"到底看到了什么"。
    """

    source_kind: str = ""            # file | path-missing | text | stream | other
    source_name: str = ""            # 入参的短写，只进报错文案
    lines_total: int = 0             # 匹配 LINE_RE 的行（与窗口无关）
    lines_in_window: int = 0
    lines_other_task: int = 0        # node done 但 task_id 不是要看的那个
    lines_internal_skipped: int = 0  # recognition/OCR/... 这类内部识别，默认过滤
    out_of_order: int = 0            # 时间戳回跳的行数（>0 说明日志不是按时间追加的）
    truncated_skipped: int = 0       # 头对得上、整条读不出，已丢弃
    truncated_kept: int = 0          # 读出来了但尾部缺字段，已标注
    events_dropped: int = 0          # 被 --task-id 排掉的其他任务事件
    # 父节点（派发者）的归属账：`Node.NextList` 事件条数，以及三层退回各命中多少步
    nextlist_seen: int = 0
    parent_resolved: int = 0
    parent_fallback_walk: int = 0
    parent_fallback_entry: int = 0
    parent_unknown: int = 0
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
    derived_clamped: bool = False   # 合成时按目标 task 的起始时间夹住了范围
    # 选中任务的起止（`--task-id` 且日志里有它的步骤时才有）：
    # 未命中聚合与事件挂载都靠它把窗口夹在任务自己的活动区间内，见 task_span()
    span: tuple[datetime, datetime] | None = None


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
        raise TypeError(f"日志入参只接受路径、文本或文本流，收到 {type(source).__name__}")

    if os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")
        return

    # 不是已存在的文件 → 当日志正文。路径写错时会在下面"窗口内没有解析到任何节点执行记录"
    # 那一步报出来，比 open() 的 Errno 22 可读。
    for line in io.StringIO(source):
        yield line.rstrip("\n")


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
        order = sorted(range(len(lines)), key=lambda i: (keys[i] is None, keys[i] or datetime.min, i))
        lines = [lines[i] for i in order]
    return lines


def _count_truncated(msg: str, stats: ParseStats) -> None:
    """行首对上了但整条对不上 → 导出按字节裁过，这条记录读不出来。"""
    for head, pattern in TRUNCATION_HEADS:
        if head in msg and not pattern.search(msg):
            stats.truncated_skipped += 1
            return


def parse_log(path, t_from: datetime | None, t_to: datetime | None,
              task_id: int | None, keep_internal: bool) -> ParsedLog:
    """解析日志。`path` 可以是文件路径、日志正文，或任何可逐行的文本流。

    全量读入（不再在第一条晚于 `--to` 的行上提前结束），时间戳回跳时先按时间重排行；
    `steps` 为空而 `attempts` 非空时，用识别记录合成步骤（"仅事件模式"）。
    """
    out = ParsedLog()
    stats = out.stats
    stats.source_kind = source_kind_of(path)
    stats.source_name = _source_label(path)
    # 键带进程号：reco_id / action_id 是**每个进程各自**的计数器，一个装了多次运行的
    # maafw.log 里会重复（本机实测 930 个 reco_id 有 142 个重复，全部跨进程）。
    # 只按 id 作键会把别的运行的识别结果贴到这一步上。
    recl_by_id: dict[tuple[str, int], dict] = {}
    act_by_id: dict[tuple[str, int], dict] = {}
    # 派发者（`[cur_node_=X]`）同样按进程存：多会话日志里两个进程都会写这种行
    # （本机 maafw.log 是 106 / 248 条），共用一个变量会让别的进程的派发者粘到本进程的步骤上，
    # 进而把 roles() → 分轮算错。
    dispatcher_by_px: dict[str, str] = {}
    task_names: dict[int, str] = {}
    # 解析到当前这一行时正在跑哪个 task：`task start` 记下、`task end` 清掉。
    # 只给框架级失败事件（自己不带 task_id）判断父节点用——它们的 task_id 必须留空，
    # 否则 parse_log() 末尾那段区间过滤会把别的运行的失败事件一起收进来。
    # 用单元列表而不是裸变量：`event_step()` 闭包要读它，裸变量在循环里赋值会被当成循环变量捕获。
    active_task: list[int | None] = [None]
    # 父节点：`Node.NextList.*` 记录的 (派发者, 候选表)。与 `[cur_node_=X]` 不同，这类记录**带 task_id**，
    # 所以按 task 归属而不是按进程——MFA 侧发事件、agent 进程写 node done，天然跨进程。
    nextlists: list[Dispatcher] = []

    def attach_parent(step: Step, px: str, *, allow_walk: bool = True) -> Step:
        """给一步贴上父节点与候选表；三层退回，取不到就留空（图上写"未记录"）。

        这里只贴，不记账：`--task-id` 会把别的任务的步骤滤掉，父节点覆盖率的账要等滤完再算
        （见 `parse_log()` 末尾），否则图上会报"79 步的父节点没找到"而图里其实只有 18 步。
        """
        record, _gap = resolve_parent(nextlists, step.task_id, step.name, step.ts)
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
        entry = task_names.get(step.task_id if step.task_id is not None else -1, "")
        if entry:
            step.dispatcher = entry
            step.parent_source = "entry"
            return step
        step.parent_source = "none"
        return step

    def event_step(ts: datetime, name: str, action: str, node: str | None,
                   task_ref: int | None = None, *, kind: str = "event", completed: bool = False) -> Step:
        """框架级事件也认一次父节点。

        `invalid node id` / `node not completed` 这几条事件自己不带 task_id，但 `action` 里就是出问题的
        节点名（`[node.name=X]`），而它刚跑完、多半在 `Node.NextList` 记录里出现过（实测
        `node not completed [node.name=TrainingBattleWin]` 落在 `BattleWinSuccess -> [TrainingBattleWin]`
        之后 0.4 s）→ 框里能直接给出派发者与候选表，而不是写"父 未记录"。

        `task_ref` 优先用调用方知道的那个 task（`task end` 那行自带 task_id），否则用**解析到这一行时
        正在跑的那个 task**（`active_task`）。框架级失败事件的 `task_id` 字段仍然留空：`parse_log()`
        末尾的区间过滤靠它把别的运行的失败事件排掉，填上就过滤不掉了。
        `Task timeout` 的 pretask 是**正在跑的那个**节点，还没被选中进候选表，查不到属正常。
        """
        step = Step(ts=ts, name=name, completed=completed, kind=kind, action=action,
                    task_id=task_ref if kind == "task" else None)
        if node:
            record, _gap = resolve_parent(nextlists,
                                          task_ref if task_ref is not None else active_task[0],
                                          node, ts)
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
        px = m.group("px")
        msg = m.group("msg")

        # 谁在评估 next 表（父节点 + 候选表）
        record = parse_nextlist(msg, ts, px)
        if record is not None:
            nextlists.append(record)
            stats.nextlist_seen += 1
            continue

        # 当前在走谁的 next 表（只知道派发者、不知道候选表，作为退回）
        w = WALK_RE.search(msg)
        if w:
            dispatcher_by_px[px] = w.group(1).strip()
            continue

        # 任务起止
        ms = TASK_START_RE.search(msg)
        if ms:
            try:
                d = json.loads(ms.group(1))
            except json.JSONDecodeError:
                stats.truncated_skipped += 1
                continue
            task_names[int(d.get("task_id", 0))] = d.get("entry", "")
            active_task[0] = int(d.get("task_id", 0))
            entry = d.get("entry", "")
            tid = int(d.get("task_id", 0))
            out.tasks.append((ts, entry, tid, d.get("hash", "")))
            # 任务起始也算一步：入口那一轮框架合成的自派发（`TaskX -> [TaskX]`）在这里就能标出来
            out.events.append(event_step(ts, f"任务开始 {entry}", f"task_id={tid}", entry, tid,
                                         kind="task", completed=True))
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
            out.events.append(event_step(ts, f"任务结束 {entry} {'成功' if ret else '失败'}",
                                         f"ret={me.group(2)}", entry, tid,
                                         kind="task", completed=ret))
            if tid == active_task[0]:
                active_task[0] = None
            out.task_ends.append((ts, entry, tid, ret))
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
                recl_by_id[(px, rid)] = j
            name = j.get("name", "")
            if INTERNAL_NAME_RE.match(name) and not keep_internal:
                stats.lines_internal_skipped += 1
                continue
            texts: list[str] = []
            collect_texts(j.get("detail"), texts)
            out.attempts.append(attach_parent(Step(
                ts=ts, name=name, completed=bool(j.get("box")),
                reco_id=rid if isinstance(rid, int) else None,
                algorithm=j.get("algorithm", ""), box=j.get("box"), texts=texts,
                kind="hit" if j.get("box") else "miss",
            ), px))
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
                act_by_id[(px, aid)] = j
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
            if task_id is not None and tid != task_id:
                stats.lines_other_task += 1
                stats.other_task_ts.append(ts)
                continue
            active_task[0] = tid  # 这份日志里可能一条 `task start` 都没有（实测 Sentry 拉回来的就没有），
            #                       所以"正在跑哪个 task"以 node done 为准，给框架级事件判父节点用
            reco = recl_by_id.get((px, j.get("reco_id")), {})
            act = act_by_id.get((px, j.get("action_id")), {})
            texts: list[str] = []
            collect_texts(reco.get("detail"), texts)
            out.steps.append(attach_parent(Step(
                ts=ts, name=name, completed=bool(j.get("completed")),
                reco_id=j.get("reco_id"), action_id=j.get("action_id"),
                node_id=j.get("node_id"), task_id=tid,
                algorithm=reco.get("algorithm", ""), box=reco.get("box"), texts=texts,
                action=act.get("action", ""), action_ok=act.get("success"),
            ), px))
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
                    seen.append(f"duration_since(start_clock)={dur}{'ms' if dur_ok else '(截断)'}")
                if tout:
                    seen.append(f"reco_timeout={tout}{'ms' if tout_ok else '(截断)'}")
                detail = "行尾被截断" + ("，" + "  ".join(seen) if seen else "，两个数字都缺失")
            out.events.append(event_step(ts, f"Task timeout {mt.group('name')}", detail, mt.group("name")))
            continue
        mi = INVALID_RE.search(msg)
        if mi:
            out.events.append(event_step(ts, "invalid node id", mi.group(1), mi.group(1)))
            continue
        mn = NOT_COMPLETED_RE.search(msg)
        if mn:
            out.events.append(event_step(ts, "node not completed", mn.group(1), mn.group(1)))
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
            inside = not out.span or event.task_id is not None or (
                out.span[0] <= event.ts <= out.span[1])
            if owned and inside:
                kept.append(event)
            elif not event.completed:
                dropped += 1
        stats.events_dropped = dropped
        out.events = kept

    _synthesize_steps(out, task_id)
    # 父节点覆盖率的账按**最终进图的步骤**算（过滤与合成都已结束）
    for step in out.steps:
        if step.parent_source == "nextlist":
            stats.parent_resolved += 1
        elif step.parent_source == "walk":
            stats.parent_fallback_walk += 1
        elif step.parent_source == "entry":
            stats.parent_fallback_entry += 1
        else:
            stats.parent_unknown += 1
    return out


def _synthesize_steps(out: ParsedLog, task_id: int | None) -> None:
    """没有 `PipelineTask node done` 时，用识别记录合成步骤（"仅事件模式"）。

    这种日志真实存在：MFA 的 Sentry 导出里只有 `Node.*` 事件与 `reco [result=]`，
    `PipelineTask node done` 一条没有。以前直接抛"窗口内没有解析到任何节点执行记录"，
    而同一批识别记录已经足够还原"卡在哪个节点、连续多少次没命中"。
    合成出来的框就是识别记录本身，所以 `compose_columns` 不再另做未命中聚合（会画重）。
    """
    if out.steps or not out.attempts:
        return
    attempts = out.attempts
    if task_id is not None:
        # reco 行里没有 task_id，只能用该 task 自己的起始行夹住范围（取不到就不夹）。
        starts = [t for t, _entry, tid, _hash in out.tasks if tid == task_id]
        if starts:
            lo = max(starts)
            ends = [t for t, _entry, tid, _ret in out.task_ends if tid == task_id and t >= lo]
            hi = min(ends) if ends else None
            attempts = [a for a in attempts if a.ts >= lo and (hi is None or a.ts <= hi)]
            out.derived_clamped = True
    if not attempts:
        return
    out.steps = [Step(**a.__dict__) for a in attempts]
    out.derived_from = "attempts"


# --------------------------------------------------------------------------- #
# 分轮 + 折叠
# --------------------------------------------------------------------------- #


def collapse_runs(steps: list[Step], collapse: int) -> list[Step]:
    if collapse <= 1:
        return steps
    out: list[Step] = []
    for s in steps:
        if out and out[-1].name == s.name and out[-1].kind == s.kind and out[-1].completed == s.completed:
            out[-1].run_len += 1
            out[-1].texts = list(dict.fromkeys(out[-1].texts + s.texts))
            continue
        out.append(Step(**s.__dict__))
    return out


def build_rounds(steps: list[Step], min_len: int, cut_on: set[str] | None = None,
                 hard_cut: set[str] | None = None) -> list[list[Step]]:
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
        if s.name in hard and cur or s.name in seen and len(cur) >= min_len and (cut_on is None or s.name in cut_on):
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
    return max(heads.items(), key=lambda kv: (kv[1], -min(i for i, s in enumerate(steps) if s.name == kv[0])))[0]


# --------------------------------------------------------------------------- #
# 证据来源：目录 / zip / 内存
# --------------------------------------------------------------------------- #


def _is_zip(path: str) -> bool:
    """按文件头魔数判断 zip，不按扩展名（MFA 导出分卷叫 archive-part01.zip，名字不可靠）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


@dataclass(frozen=True)
class MediaEntry:
    """一条图片证据。`path` 为 None 时用 `data`（bytes 或 `_ZipMember`）。"""

    member: str          # 归档内相对路径或文件绝对路径，仅用于日志与去重
    path: str | None = None
    data: Any = None


@dataclass(frozen=True)
class _ZipMember:
    """归档里的一个条目：真正要画时才解压，避免一次性把整包图片读进内存。"""

    package: str
    member: str

    def read(self) -> bytes:
        with zipfile.ZipFile(self.package) as zf:
            return zf.read(self.member)


class MediaResolver:
    """把"证据来源"统一成可枚举的条目，来源可以是：

      * 一个目录（`debug/vision`、`debug/on_error`，本机常态）；
      * 一个 zip 或**多个** zip（MFA 导出包，超过 23.75 MB 会分成 archive-partNN.zip）；
      * 内存字节（遥测从 Sentry 取回的图片，本地没有文件）。

    两类索引都只依赖 `iter_entries()`，因此加一种来源不用改渲染代码。
    """

    def __init__(self, source: Any = None, *, members: Iterable[tuple[str, Any]] | None = None):
        """`members` 的值可以给 bytes，也可以给**可调用对象**（延迟读，返回 bytes）。

        延迟读很重要：MFA 导出包里的图片动辄几百 KB、一包几百张；一次性
        `zf.read()` 全部条目会把内存顶起来，而实际只有命中节点的几张会被画。
        """
        self.roots: list[tuple[str, str]] = []      # (kind, locator)；kind = dir | zip
        self.members: list[tuple[str, Any]] = list(members or [])
        if source is None:
            return
        if isinstance(source, MediaResolver):        # 已经是 resolver，直接接管
            self.roots = list(source.roots)
            self.members.extend(source.members)
            return
        if hasattr(source, "read"):                  # 字节流
            self.members.append(("stream.bin", source.read()))
            return
        for item in (source if isinstance(source, (list, tuple)) else [source]):
            item = str(item)
            if os.path.isdir(item):
                self.roots.append(("dir", os.path.abspath(item)))
            elif _is_zip(item):
                self.roots.append(("zip", os.path.abspath(item)))
            # 既不是目录也不是 zip 的路径直接忽略，交给上层报"没有证据"

    def __bool__(self) -> bool:
        return bool(self.roots or self.members)

    def iter_entries(self) -> Iterator[MediaEntry]:
        """按**成员名**字典序产出全部条目；zip 分卷因此天然合并成一棵逻辑树。"""
        seen: set[str] = set()
        for member, data in sorted(self.members, key=lambda kv: kv[0]):
            if member in seen:
                continue
            seen.add(member)
            yield MediaEntry(member=member, data=data)
        for kind, locator in self.roots:
            if kind == "dir":
                for dirpath, _dirs, files in os.walk(locator):
                    for fn in sorted(files):
                        full = os.path.join(dirpath, fn)
                        member = os.path.relpath(full, locator).replace("\\", "/")
                        if member in seen:
                            continue
                        seen.add(member)
                        yield MediaEntry(member=member, path=full)
            else:
                try:
                    with zipfile.ZipFile(locator) as zf:
                        for info in sorted(zf.infolist(), key=lambda i: i.filename):
                            if info.is_dir() or info.filename in seen:
                                continue
                            seen.add(info.filename)
                            yield MediaEntry(member=info.filename,
                                             data=_ZipMember(locator, info.filename))
                except (OSError, zipfile.BadZipFile) as exc:
                    print(f"提示：跳过读不了的 zip {locator}：{exc}", file=sys.stderr)

    @staticmethod
    def open_image(entry: MediaEntry):
        """把一条证据打开成 PIL.Image。"""
        from PIL import Image

        data = entry.data
        if isinstance(data, _ZipMember):
            return Image.open(io.BytesIO(data.read())).convert("RGB")
        if data is not None:
            return Image.open(io.BytesIO(data)).convert("RGB")
        if entry.path is None:
            raise ValueError(f"证据 {entry.member} 既没有文件路径也没有字节内容，打不开。")
        return Image.open(entry.path).convert("RGB")


class VisionIndex:
    """`debug/vision/<时间>_<节点名>_<reco_id>.jpg`。

    MaaFramework 只给"有东西可画"的识别（TemplateMatch / OCR / ColorMatch…）存图，
    `Custom` / `DirectHit` 节点没有自己的图。所以取图有三级回退：
      1. (节点名, reco_id) 精确命中 —— 那张图里带 ROI 框和模板缩略图；
      2. 同一节点的 `*_wait_freezes.jpg`（该节点执行期间的画面）；
      3. 同一时刻任意节点的邻近帧（只当作"当时画面"，会标注出来）。

    **遥测侧没有这一层**：MFA 的失败附件显式跳过 `vision/`（`TaskDiagnostics` 的目录
    遍历里 `vision` 直接 continue），且 `MaxImageBundleBytes` 只有 5 MiB，而本机
    `debug/vision` 有 1078 张 / 248.6 MB。所以远程只能出"节点流程图 + 失败现场图"，
    要看识别帧得让对方手动导出（导出对话框里勾"包含 vision 图片"）。
    """

    NAME_RE = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.*?)_(?P<tail>wait_freezes|\d+)\.jpg$")

    def __init__(self, source: Any = None):
        self.source = source if isinstance(source, MediaResolver) else MediaResolver(source)
        self.by_reco: dict[tuple[str, int], MediaEntry] = {}
        self.freezes: list[tuple[datetime, str, MediaEntry]] = []
        self.frames: list[tuple[datetime, MediaEntry]] = []
        for entry in self.source.iter_entries():
            m = self.NAME_RE.match(os.path.basename(entry.member))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
            except ValueError:
                continue
            if m.group("tail") == "wait_freezes":
                self.freezes.append((ts, m.group("name"), entry))
            else:
                self.by_reco[(m.group("name"), int(m.group("tail")))] = entry
            self.frames.append((ts, entry))
        self.freezes.sort()
        self.frames.sort()

    def find(self, step: Step, tol: float = 3.0, neighbor_tol: float = 2.0):
        """返回 (MediaEntry, 是否该节点自己的识别图)。找不到返回 (None, False)。"""
        if step.reco_id is not None:
            p = self.by_reco.get((step.name, int(step.reco_id)))
            if p:
                return p, True
        best, best_d = None, tol
        for ts, name, entry in self.freezes:
            if name != step.name:
                continue
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = entry, d
        if best:
            return best, False
        best, best_d = None, neighbor_tol
        for ts, entry in self.frames:
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = entry, d
        return best, False


class OnErrorIndex:
    """`debug/on_error/<时间>_<节点名>.png`：节点失败时框架抓的现场图。

    这是**遥测里唯一有的图片证据**（失败附件就是这个目录），所以远程路径上它
    是"能贴的全部"。
    """

    NAME_RE = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.+)\.png$")

    def __init__(self, source: Any = None):
        self.source = source if isinstance(source, MediaResolver) else MediaResolver(source)
        self.items: list[tuple[datetime, str, MediaEntry]] = []
        for entry in self.source.iter_entries():
            m = self.NAME_RE.match(os.path.basename(entry.member))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
            except ValueError:
                continue
            self.items.append((ts, m.group("name"), entry))
        self.items.sort()

    def find(self, step: Step, tol: float = 8.0) -> MediaEntry | None:
        """按时间就近找；`Task timeout X` / `invalid node id` 事件的 action 里存着失败节点名，优先同名。"""
        target = step.action.strip()
        best, best_d = None, tol
        for ts, name, entry in self.items:
            d = abs((ts - step.ts).total_seconds())
            if target and name == target:
                d *= 0.15
            if d < best_d:
                best, best_d = entry, d
        return best


# --------------------------------------------------------------------------- #
# 画图
# --------------------------------------------------------------------------- #


try:                                  # pill​ow 只为取一个异常类型，缺了也不该 import 就炸
    from PIL import UnidentifiedImageError as _UnidentifiedImage
except ImportError:                   # pragma: no cover - 没装 Pillow 时走到这里
    class _UnidentifiedImage(Exception):  # type: ignore[no-redef]
        """Pillow 缺失时的占位：真正画图时会在别处报出"需要 Pillow"。"""


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for c in cands:
        if os.path.isfile(c):
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def text_width(font: Any, text: str) -> float:
    """字体测宽。FreeType 字体用 getlength；退回位图字体（没有这个方法）时按字符数估算。"""
    try:
        return float(font.getlength(text))
    except AttributeError:
        return len(text) * 10.0


def _wrap_notes(notes: list[str], font, maxw: int) -> list[str]:
    """把诊断注记折成若干行。

    先按注记边界换行、单条本身超宽再按字符硬折：这些字是"这份图不是完整过程"的前提，
    超出画布右边被裁掉就等于没说。
    """
    if not notes:
        return []
    sep = "   |   "
    packed: list[str] = []
    cur = ""
    for note in notes:
        piece = f"注：{note}" if not cur else note
        cand = f"{cur}{sep}{piece}" if cur else piece
        if not cur or text_width(font, cand) <= maxw:
            cur = cand
            continue
        packed.append(cur)
        cur = piece
    if cur:
        packed.append(cur)

    lines: list[str] = []
    for line in packed:
        while text_width(font, line) > maxw and len(line) > 1:
            cut = len(line) - 1
            while cut > 1 and text_width(font, line[:cut]) > maxw:
                cut -= 1
            lines.append(line[:cut])
            line = "    " + line[cut:]
        lines.append(line)
    return [line for line in lines if line.strip()]


PALETTE = {
    "hit": ((232, 241, 255), (72, 110, 180), (26, 43, 74)),
    "custom": ((240, 235, 255), (122, 92, 180), (52, 32, 92)),
    "direct": ((238, 238, 240), (130, 130, 140), (52, 52, 58)),
    "miss": ((255, 236, 233), (200, 92, 74), (110, 30, 20)),
    "error": ((255, 224, 224), (190, 40, 40), (110, 0, 0)),
    "event": ((228, 246, 236), (56, 140, 96), (20, 70, 46)),
    "task": ((226, 232, 240), (60, 74, 100), (18, 26, 42)),
}

# 父节点分组的三色一套：(分组框底色, 分组框边框, 角标底色)。角标上是白字，所以角标色要够深。
# 同名的父节点永远拿同一套（一眼看出"又是它派发的"），不同父节点按首次出现顺序轮换这 6 套。
PARENT_COLORS: tuple[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]], ...] = (
    ((234, 239, 250), (124, 140, 186), (74, 88, 140)),     # 蓝
    ((234, 246, 241), (86, 156, 132), (34, 110, 86)),      # 青绿
    ((248, 239, 228), (190, 148, 84), (146, 102, 32)),     # 琥珀
    ((244, 234, 245), (158, 114, 170), (114, 70, 128)),    # 紫
    ((233, 246, 233), (104, 158, 94), (58, 116, 52)),      # 绿
    ((250, 235, 238), (186, 112, 124), (146, 66, 82)),     # 玫红
)


def classify(step: Step) -> str:
    if step.kind == "miss":
        return "miss"
    if step.kind == "event":
        if not step.completed or "timeout" in step.name or step.name in ("invalid node id", "node not completed"):
            return "error"
        if step.name.startswith("任务"):
            return "task"
        return "event"
    if step.algorithm == "Custom":
        return "custom"
    if step.algorithm == "DirectHit":
        return "direct"
    return "hit"


def task_span(parsed: ParsedLog, task_id: int | None) -> tuple[datetime, datetime] | None:
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


def compose_columns(parsed: ParsedLog, rounds: list[list[Step]], miss_min: int,
                    collapse: int = 1, miss_max: int = 8) -> list[list[Step]]:
    """每轮 = 该轮命中步骤（按需折叠 ×N）+ 该轮内的"连续未命中"聚合框；异常事件挂到所属轮末尾。"""
    tail = max([a.ts for a in parsed.attempts] + [e.ts for e in parsed.events] + [rounds[-1][-1].ts])
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
            rows.append(Step(ts=items[-1].ts, name=name, completed=False, kind="miss",
                             run_len=len(items), dispatcher=disp,
                             candidates=list(items[-1].candidates), jumps=list(items[-1].jumps),
                             parent_source=items[-1].parent_source,
                             reco_id=items[-1].reco_id, box=items[-1].box))
        rows.sort(key=lambda s: (-s.run_len, s.ts))
        rows = rows[:miss_max] if miss_max else rows
        rows.sort(key=lambda s: s.ts)
        return rows

    columns = [collapse_runs(list(r), collapse) + miss_rows(i) for i, r in enumerate(rounds)]
    for ev in parsed.events:
        if ev.kind != "event" or classify(ev) != "error":
            continue
        for col in reversed(columns):
            if col and col[0].ts <= ev.ts:
                col.append(ev)
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
    current = ""      # 当前这一组的父节点；只有它变了才收组
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


def draw_flow(parsed: ParsedLog, options: FlowOptions, out_path: str) -> tuple[int, int, int, int, int]:
    from PIL import Image, ImageDraw

    f_name = load_font(max(11, int(options.font_size * options.scale)), bold=True)
    f_det = load_font(max(9, int((options.font_size - 3) * options.scale)))
    # 父节点那一行比明细再小一档：实测最长的组合（`父 BattleWinSuccess：√TrainingBattleWin`）
    # 在 13px 下要 255px，而带图槽的框只有 216px 可写；11px 下 219px，刚好放得下。
    f_par = load_font(max(9, int((options.font_size - 5) * options.scale)))
    f_title = load_font(int((options.font_size + 8) * options.scale), bold=True)
    f_leg = load_font(max(10, int((options.font_size - 2) * options.scale)))

    raw = round_steps(parsed, options)
    if not raw:
        raise SystemExit("窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id")

    # 父节点 = 派发过子节点的节点（日志里出现 [cur_node_=X] 的那些），它们拥有 next 表
    # 重要节点 = 全局重复出现的节点
    parents, important = roles(raw, options.repeat_threshold)
    if options.cut_on:
        cut = {n.strip() for n in options.cut_on.split(",") if n.strip()}
    else:
        cut = choose_cut_set(raw, parents, options.min_round_len)
    hard_cut = {n for n in cut if n not in parents}
    rounds = build_rounds(raw, options.min_round_len, cut, hard_cut)
    if len(rounds) > options.max_rounds:
        rounds = rounds[: options.max_rounds]
    anchor = options.anchor or anchor_of(raw, rounds)

    # 每轮末尾追加该轮内"连续未命中"的聚合框；窗口右端延伸到下一轮开始（末轮延伸到最后一个动作/事件）
    columns = compose_columns(parsed, rounds, options.miss_min, options.collapse, options.miss_max)
    steps = [s for r in rounds for s in r]
    # 仅事件模式下 raw 已经折过：图上报的步数要按识别记录条数算，与"折叠后 N 个框"对齐
    total_steps = len(parsed.steps) if parsed.derived_from else len(steps)

    # ---- 先把每个框要贴的图解析出来（决定框高：有无图）----
    box_w = int(options.box_width * options.scale)
    pad = int(10 * options.scale)
    slot_w = int(box_w * 0.46)                       # 右侧图槽
    slot_h = int(options.thumb_height * options.scale) if options.thumbs else 0
    # 候选那一行要占高度：本图一条候选都没有（日志没有 NextList 事件）时整行不加——那种情况下每个框
    # 都写"父 未记录"只是刷屏，同一件事由图上第二行的注记说一次（`build_notes()`）。
    any_candidates = any(st.candidates for col in columns for st in col) if options.parents else False

    def parent_details(st: Step) -> str:
        if not options.parents or not any_candidates:
            return ""
        if st.candidates:
            return st.parent_detail()
        return "" if st.parent else "父 未记录"

    parent_h = int(options.font_size * options.scale) + int(6 * options.scale) if any_candidates else 0
    box_h = max(int(options.box_height * options.scale) + parent_h, slot_h + int(18 * options.scale)) if slot_h else int(options.box_height * options.scale) + parent_h
    event_h = int(options.box_height * options.scale) + parent_h
    gap_y = int(options.gap_y * options.scale)
    gap_err = int(options.gap_error * options.scale)
    gap_x = int(options.gap_x * options.scale)
    margin = int(34 * options.scale)
    header_h = int(34 * options.scale)
    cols_per_band = max(1, options.cols_per_band) if options.cols_per_band else len(columns)

    vision = VisionIndex(options.vision) if options.vision else None
    on_err = OnErrorIndex(options.on_error) if options.on_error else None
    # (列, 行) -> (证据条目, 是否该节点自己的识别图, 角标文字)
    thumb_of: dict[tuple[int, int], tuple[MediaEntry, bool, str]] = {}
    # 贴图失败的记录与打印上限：读图失败要能看见，但不能刷满屏幕
    thumb_errors: list[str] = []
    WARN_THUMB_LIMIT = 5
    if slot_h:
        for ci, col in enumerate(columns):
            for ri, st in enumerate(col):
                path, exact, note = None, False, ""
                if st.kind in ("hit", "miss") and vision:
                    path, exact = vision.find(st)
                    note = "" if exact else "邻近帧"
                elif st.kind == "event":
                    if on_err:
                        path, exact, note = on_err.find(st), False, "失败现场"
                    if not path and vision:
                        path, exact = vision.find(st)
                        note = "邻近帧"
                if path:
                    thumb_of[(ci, ri)] = (path, exact, note)
        # 只有 on_error 图时（没给 --vision 或该节点本来就没存图）：把现场图贴回"被记录的那个节点"
        if on_err and on_err.items:
            by_name: dict[str, list[tuple[int, int, Step]]] = {}
            for ci, col in enumerate(columns):
                for ri, st in enumerate(col):
                    if st.kind == "hit":
                        by_name.setdefault(st.name, []).append((ci, ri, st))
            for ts, name, entry in on_err.items:
                cands = [x for x in by_name.get(name, []) if x[2].ts <= ts]
                if not cands:
                    continue
                ci, ri, _st = max(cands, key=lambda x: x[2].ts)
                thumb_of[(ci, ri)] = (entry, False, "失败现场")

    bands: list[list[int]] = [list(range(i, min(i + cols_per_band, len(columns))))
                             for i in range(0, len(columns), cols_per_band)]
    # 父节点分组：每列的 [(父, 首, 末)]，套框与角标都用它
    groups_of: dict[int, list[tuple[str, int, int]]] = {
        ci: parent_groups(col) for ci, col in enumerate(columns)}
    group_badge_h = int(20 * options.scale)   # 角标高度（实心矩形 + 白字）
    group_pad = int(6 * options.scale)        # 分组框四边的内边距
    # 组内相邻两步贴紧一点：默认 36px 的间隔叠上分组框的上下内边距会显得很空
    group_gap = int(12 * options.scale)
    # 相邻两个分组框之间留的空：上一组的框底还带 group_pad 的内边距，只留 group_gap 会让两个框贴到一起
    group_gap_between = int(26 * options.scale)
    # 每个分组在列里要额外占的高度。角标画在"第一格上边界的上方"（badge_h + pad），而上一格与本格之间
    # 只有 group_gap 的空档——所以必须把整个角标走廊 + 上下内边距都预留出来，否则角标会压住上一格的文字。
    group_extra_one = group_badge_h + group_pad * 2 + group_gap

    # 画布宽度只由列数与版式决定，先算出来：诊断注记要在定标题高度之前折行
    width = margin * 2 + max(len(b) for b in bands) * (box_w + gap_x)
    width = max(width, int(900 * options.scale))
    note_lines = _wrap_notes(options.notes, f_leg, width - margin * 2)
    title_h = int(64 * options.scale) + len(note_lines) * int(20 * options.scale)

    def box_height_for(ci: int, ri: int) -> int:
        return box_h if (ci, ri) in thumb_of else event_h

    # 每列的高度：框高 + 间隔（未命中/异常相邻处用更大的间隔，别挤在一起）+ 分组框的净增高。
    # 不预先撑开这些空隙的话，后画的浅色分组框会糊住上一个框的文字；相邻两组的框之间也要留够
    # （上一组的框底带 group_pad 内边距，只留 group_gap 会让两个框贴到一起）。
    def column_geo(ci: int, x: int, y0: int) -> tuple[list[tuple[int, int, int, int]], int]:
        span: dict[int, tuple[int, int]] = {}
        for _name, first, last in (groups_of[ci] if options.parents else []):
            for ri in range(first, last + 1):
                span[ri] = (first, last)
        y = y0 + header_h
        boxes: list[tuple[int, int, int, int]] = []
        for ri, st in enumerate(columns[ci]):
            if ri:
                prev_st = columns[ci][ri - 1]
                noisy = prev_st.kind in ("miss", "event") or st.kind in ("miss", "event")
                if ri in span and span[ri][0] == ri:
                    # 新的一组的首格：让出上一组的框底内边距 + 角标走廊
                    y += group_extra_one + (group_gap_between - group_gap if span.get(ri - 1) else 0)
                elif noisy:
                    y += gap_err
                elif span.get(ri) and span.get(ri) == span.get(ri - 1):
                    y += group_gap          # 同一组内的相邻两步
                else:
                    y += gap_y
            boxes.append((x, y, box_w, box_height_for(ci, ri)))
            y += boxes[-1][3]
        return boxes, y

    # 先按"同一行"的纵坐标依次算一遍，再定画布高度
    col_x: dict[int, int] = {}
    probe: dict[int, int] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            col_x[ci] = margin + (ci - band[0]) * (box_w + gap_x)
            _boxes, probe[ci] = column_geo(ci, col_x[ci], y_band)
        y_band = max(probe[ci] for ci in band) + int(96 * options.scale)

    height = y_band + int(64 * options.scale)

    img = Image.new("RGB", (width, height), (250, 251, 253))
    d = ImageDraw.Draw(img)

    # ---- 标题 ----
    t0 = rounds[0][0].ts if rounds and rounds[0] else None
    t1 = rounds[-1][-1].ts if rounds and rounds[-1] else None
    title = options.title or f"执行流程  ·  {os.path.basename(options.log)}"
    sub = f"{t0.strftime('%Y-%m-%d %H:%M:%S') if t0 else '?'} → {t1.strftime('%H:%M:%S') if t1 else '?'}"
    sub += f"   |   {total_steps} 步 / {len(rounds)} 轮"
    if anchor:
        sub += f"   |   循环锚点 {anchor}"
    if options.task_id:
        sub += f"   |   task_id={options.task_id}"
    if options.truncated_start:
        # 日志只剩尾巴时必须印在图上，否则读图的人会以为任务从这里开始。
        # 只用中文字符：⚠ 这类符号在 msyh 里没有字形，会画成方框。
        sub += "   |   注意：日志仅含尾部"
    d.text((margin, int(14 * options.scale)), title, font=f_title, fill=(20, 26, 40))
    d.text((margin, int(40 * options.scale)), sub, font=f_leg, fill=(96, 104, 122))
    # 诊断注记单独一行：由识别记录还原、时间戳已重排、多少条被裁断——这些是读图前提，
    # 挤在副标题里会被一起读过去（也可能超出画布右边被裁掉）
    for i, note in enumerate(note_lines):
        d.text((margin, int(58 * options.scale) + i * int(20 * options.scale)), note,
               font=f_leg, fill=(168, 112, 30))

    # 三种字体共用的截断（画父节点角标时就要用，所以定义在这里，不放在"框"那一段）
    def fit_text(s: str, font, maxw: int) -> str:
        if d.textlength(s, font=font) <= maxw:
            return s
        out = s
        while out and d.textlength(out + "…", font=font) > maxw:
            out = out[:-1]
        return out + "…"

    # ---- 每列的框位置 + 列底色/表头 ----
    pos: dict[int, list[tuple[int, int, int, int]]] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            boxes, bottom = column_geo(ci, col_x[ci], y_band)
            pos[ci] = boxes
            x = col_x[ci]
            if ci % 2 == 1:
                d.rounded_rectangle([x - int(14 * options.scale), y_band,
                                     x + box_w + int(14 * options.scale), bottom + int(12 * options.scale)],
                                    radius=int(12 * options.scale), fill=(244, 246, 250))
            r = rounds[ci]
            label = f"第 {ci + 1} 轮 · {r[0].ts.strftime('%H:%M:%S')}–{r[-1].ts.strftime('%H:%M:%S')} · {len(r)} 步"
            d.rounded_rectangle([x, y_band, x + box_w, y_band + int(24 * options.scale)],
                                radius=int(6 * options.scale), fill=(226, 230, 238))
            d.text((x + int(8 * options.scale), y_band + int(4 * options.scale)), label, font=f_leg, fill=(58, 66, 84))
        y_band = max(pos[ci][-1][1] + pos[ci][-1][3] for ci in band) + int(96 * options.scale)

    # ---- 父节点分组框（在框与连线之前画，只当背景）----
    # 浅蓝底 + 同色系边框套住"同一个派发者连着派发的那几步"，左上角压一个深色实心角标写父节点名。
    # 角标压在第一格左上、与它重叠：重叠必须能读出来，所以不透明 + 白字，不给连线留穿透的余地
    # （连线在下一步才画，会绕不过角标——那一格的上边框端口因此改从角标右边出）。
    group_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    badge_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}   # (ci, 末行) -> 角标矩形
    if options.parents:
        inset = int(7 * options.scale)
        # 同名父节点用同一套颜色（一眼看出"又是它派发的"），不同父节点按**首次出现顺序**轮换配色
        color_of: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {}
        for cols in groups_of.values():
            for name, _first, _last in cols:
                if name not in color_of:
                    color_of[name] = PARENT_COLORS[len(color_of) % len(PARENT_COLORS)]
        for ci, cols in groups_of.items():
            for name, first, last in cols:
                fill, edge, badge_color = color_of[name]
                fx, fy, _fw, _fh = pos[ci][first]
                lx, ly, _lw, lh = pos[ci][last]
                left, top = fx - inset, fy - group_badge_h - inset
                right = fx + box_w + inset
                bottom = ly + lh + inset + group_badge_h
                d.rounded_rectangle([left, top, right, bottom], radius=int(9 * options.scale),
                                    fill=fill, outline=edge, width=2)
                # 角标按文字宽度收紧（不铺满整格宽，否则读起来像标题栏而不是标签）
                text_w_badge = int(d.textlength(name, font=f_par))
                bw = min(box_w - inset, text_w_badge + int(16 * options.scale))
                d.rounded_rectangle([left, top, left + bw, top + group_badge_h],
                                    radius=int(6 * options.scale), fill=badge_color)
                d.text((left + int(7 * options.scale), top + int(3 * options.scale)),
                       fit_text(name, f_par, bw - int(12 * options.scale)), font=f_par,
                       fill=(255, 255, 255))
                group_boxes[(ci, first)] = (left, top, right, bottom)
                badge_boxes[(ci, last)] = (left, top, left + bw, top + group_badge_h)

    # ---- 端口与连线（先画线，再画框，线不会压住文字）----
    # 端口规则（--ports pos，默认）：
    #   同列相邻（竖直方向） —— 上入下出：出口 = 下中，入口 = 上中
    #   跨轮相邻（水平方向） —— 左入右出：出口 = 右中，入口 = 左中
    # --ports role 是旧规则：重要节点左下出/右上进，父节点左下出/右下进。
    pad_port = int(12 * options.scale)
    lane_gap = int(34 * options.scale)

    def role_of(name: str) -> str:
        if name in parents and name in important:
            return "parent+repeat"
        if name in parents:
            return "parent"
        if name in important:
            return "repeat"
        return "plain"

    def out_port(ci: int, ri: int, horizontal: bool = False) -> tuple[int, int]:
        x, y, w, h = pos[ci][ri]
        if options.ports == "pos":
            return (x + w, y + h // 2) if horizontal else (x + w // 2, y + h)
        if role_of(columns[ci][ri].name) == "plain":
            return (x + w // 2, y + h)
        return (x + pad_port, y + h)

    def in_port(ci: int, ri: int, cross_round: bool = False) -> tuple[tuple[int, int], str]:
        """返回 (接点坐标, 'top'|'left'|'right')，'left'/'right' 表示从左右边缘水平进入。

        这一格如果顶着父节点分组框的角标（角标压在第一格左上），入口就挪到角标**右边**：
        角标是不透明的，从正中间上去会被它盖住，箭头看着像断的。
        """
        x, y, w, h = pos[ci][ri]
        badge = badge_boxes.get((ci, ri))
        entry_x = x + w // 2
        if badge is not None and badge[2] > entry_x:
            # 角标压住了中点：改从"角标右缘 ~ 框右缘"的中点上去
            entry_x = x + (w + badge[2]) // 2
        if options.ports == "pos":
            if cross_round:
                return (x, y + h // 2), "left"
            return (entry_x, y), "top"
        r = role_of(columns[ci][ri].name)
        if cross_round or r == "repeat":
            return (x + w - pad_port, y), "top"
        if r in ("parent", "parent+repeat"):
            return (x + w, y + h - pad_port), "right"
        return (x + w // 2, y), "top"

    def arrow(points: list[tuple[int, int]], color, width: int = 2):
        d.line(points, fill=color, width=width, joint="curve")
        (x0, y0), (x1, y1) = points[-2], points[-1]
        ah = int(9 * options.scale)
        if abs(x1 - x0) >= abs(y1 - y0):  # 水平进入
            s = 1 if x1 > x0 else -1
            d.polygon([(x1, y1), (x1 - s * ah, y1 - ah // 2), (x1 - s * ah, y1 + ah // 2)], fill=color)
        else:  # 垂直进入
            s = 1 if y1 > y0 else -1
            d.polygon([(x1, y1), (x1 - ah // 2, y1 - s * ah), (x1 + ah // 2, y1 - s * ah)], fill=color)

    def route(a: tuple[int, int], b: tuple[int, int], kind: str, bx: int, bw: int) -> None:
        if kind == "top":  # 竖直方向：上入下出
            y_mid = a[1] + max(int(14 * options.scale), (b[1] - a[1]) // 2)
            arrow([a, (a[0], y_mid), (b[0], y_mid), b], (150, 158, 174))
        elif kind == "left":  # 水平方向：左入右出（竖线走两列之间的空档）
            x_lane = (a[0] + bx) // 2
            arrow([a, (x_lane, a[1]), (x_lane, b[1]), b], (120, 130, 150), width=3)
        else:  # 旧的"右下进"：从右边缘进
            x_lane = bx + bw + lane_gap
            arrow([a, (a[0], a[1] + int(14 * options.scale)), (x_lane, a[1] + int(14 * options.scale)),
                   (x_lane, b[1]), b], (150, 158, 174))

    band_of = {ci: bi for bi, band in enumerate(bands) for ci in band}
    over = int(26 * options.scale)
    for ci, boxes in enumerate(columns):
        for ri in range(len(boxes) - 1):  # 列内：竖直相邻
            a = out_port(ci, ri)
            b, kind = in_port(ci, ri + 1)
            bx, _by, bw, _bh = pos[ci][ri + 1]
            route(a, b, kind, bx, bw)
        nxt = ci + 1
        if nxt >= len(columns) or not boxes:
            continue
        a = out_port(ci, len(boxes) - 1, horizontal=True)  # 跨轮：水平相邻
        bx, _by, bw, _bh = pos[nxt][0]
        b, kind = in_port(nxt, 0, True)
        if band_of[ci] == band_of[nxt] and kind == "left":
            route(a, b, kind, bx, bw)
        else:  # 换行时改从上边进，避免竖线穿过下一排的框
            b2 = (bx + bw // 2, pos[nxt][0][1])
            corridor = pos[ci][-1][1] + pos[ci][-1][3] + over
            x_lane = (a[0] + b2[0]) // 2
            arrow([a, (x_lane, a[1]), (x_lane, corridor), (b2[0], corridor), b2], (120, 130, 150), width=3)

    # ---- 框 ----
    from PIL import ImageOps

    def draw_thumb(st: Step, entry: MediaEntry, exact: bool, note: str, x: int, y: int, w: int, h: int) -> None:
        im = MediaResolver.open_image(entry)
        frame = im.crop((0, 0, min(1280, im.width), min(720, im.height)))
        if exact and st.box and st.box[2] > 0 and st.box[3] > 0:
            bx, by, bw, bh = st.box
            p = int(26 * options.scale)
            crop = frame.crop((max(0, bx - p), max(0, by - p),
                               min(frame.width, bx + bw + p), min(frame.height, by + bh + p)))
        else:
            crop = frame
        sx = x + w - pad - slot_w
        sy = y + (h - slot_h) // 2
        d.rectangle([sx - 1, sy - 1, sx + slot_w + 1, sy + slot_h + 1], fill=(255, 255, 255), outline=(198, 204, 216))
        thumb = ImageOps.contain(crop, (slot_w - 2, slot_h - 2))
        img.paste(thumb, (sx + (slot_w - thumb.width) // 2, sy + (slot_h - thumb.height) // 2))
        if note:
            d.text((sx + 2, sy + slot_h - int(13 * options.scale)), note, font=f_det, fill=(168, 120, 40))

    for ci, col in enumerate(columns):
        for ri, (x, y, w, h) in enumerate(pos[ci]):
            st = col[ri]
            cls = classify(st)
            fill, edge, ink = PALETTE[cls]
            d.rounded_rectangle([x, y, x + w, y + h], radius=int(10 * options.scale),
                                fill=fill, outline=edge, width=2)
            got = thumb_of.get((ci, ri))
            text_w = (w - pad * 2) if not got else (w - slot_w - pad * 3)
            # 父节点那一行的右边界单独算：有图时**从图槽左边**过字（`draw_thumb` 只占右槽，
            # 字压在缩略图上是原来就有的行为），只按 `text_w` 量会把够宽的一行也截掉。
            row_w = (w - pad * 2) if not got else (w - slot_w - pad * 2)
            d.text((x + pad, y + int(6 * options.scale)), st.ts.strftime("%H:%M:%S.%f")[:-3],
                   font=f_det, fill=(126, 132, 148))
            label = st.name if st.run_len <= 1 else f"{st.name}  ×{st.run_len}"
            d.text((x + pad, y + int(22 * options.scale)), fit_text(label, f_name, text_w), font=f_name, fill=ink)
            det = st.detail
            if det:
                d.text((x + pad, y + int(22 * options.scale) + int(options.font_size * options.scale) + int(6 * options.scale)),
                       fit_text(det, f_det, text_w), font=f_det, fill=(88, 94, 110))
            detail_line = parent_details(st)
            if detail_line:
                # 父节点那部分已经进了分组框角标，这一行写的是"候选"（或"未记录"）
                d.text((x + pad, y + h - parent_h + int(1 * options.scale)),
                       fit_text(detail_line, f_par, row_w), font=f_par, fill=(120, 104, 148))
            if got:
                try:
                    draw_thumb(st, got[0], got[1], got[2], x, y, w, h)
                except (OSError, ValueError, _UnidentifiedImage) as exc:
                    # 不吞异常：图贴不上必须能看出来（遥测的证据包可能是被截断的分卷、
                    # 也可能图片已被 log_dir 的 7 天清理删掉，两种都表现为读图失败）。
                    # 但有界——一次几百张图读不了时不能刷满屏幕。
                    thumb_errors.append(f"{got[0].member}: {type(exc).__name__}: {exc}")
                    if len(thumb_errors) <= WARN_THUMB_LIMIT:
                        print(f"[警告] 贴图失败 {thumb_errors[-1]}", file=sys.stderr)

    # ---- 图例 ----
    if len(thumb_errors) > WARN_THUMB_LIMIT:
        print(f"[警告] 另有 {len(thumb_errors) - WARN_THUMB_LIMIT} 张贴图失败（已省略）", file=sys.stderr)
    lx = margin
    ly = height - int(46 * options.scale)
    d.text((lx, ly), "图例：", font=f_leg, fill=(60, 66, 80))
    lx += int(52 * options.scale)
    for key, text in (("hit", "命中并执行"), ("custom", "自定义识别/动作"), ("direct", "DirectHit"),
                      ("miss", "未命中（聚合）"), ("error", "超时/失败"), ("task", "任务起止")):
        fill, edge, _ = PALETTE[key]
        d.rounded_rectangle([lx, ly - int(2 * options.scale), lx + int(16 * options.scale), ly + int(14 * options.scale)],
                            radius=3, fill=fill, outline=edge)
        d.text((lx + int(22 * options.scale), ly), text, font=f_leg, fill=(70, 76, 92))
        lx += int(22 * options.scale) + int(d.textlength(text, font=f_leg)) + int(22 * options.scale)

    img.save(out_path)
    shown = sum(1 for c in columns for s in c if s.kind == "hit")
    return img.width, img.height, total_steps, shown, len(rounds)


# --------------------------------------------------------------------------- #
# 可复用的调用接口
# --------------------------------------------------------------------------- #

# 仓库根：tools/drawing/flow_from_log.py 上溯**三级**（改了目录层级就要同步改这里，
# 否则默认产出会悄悄落到 tools/drawing/temp/flow）。默认输出目录挂在它下面，避免写到日志旁边。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OUTDIR = os.path.join(REPO_ROOT, "temp", "flow")


@dataclass
class FlowOptions:
    """画图参数。默认值与命令行默认值一致，调用方只改需要的项。"""

    # 解析/分轮
    t_from: datetime | None = None
    t_to: datetime | None = None
    task_id: int | None = None
    keep_internal: bool = False
    anchor: str | None = None
    cut_on: str | None = None
    collapse: int = 3
    repeat_threshold: int = 2
    min_round_len: int = 2
    miss_min: int = 5
    miss_max: int = 8
    max_rounds: int = 40
    ports: str = "pos"
    cols_per_band: int = 0
    # 框里写"父 X：候选表"（父节点 = 本轮的派发者，来自 Node.NextList 事件）
    parents: bool = True
    # 证据来源：目录 / zip / zip 列表 / MediaResolver / None
    vision: Any = None
    on_error: Any = None
    # 日志名，只用于默认标题；由 render_flow 按入参填好，调用方不用管
    log: str = ""
    # 日志只剩尾部（第一行晚于请求的 --from）：会印在图的副标题上，别省成"完整日志"
    truncated_start: bool = False
    # 诊断注记（由 render_flow 填好并印在图上的第二行）：由识别记录还原、时间戳回跳已重排、
    # 有多少记录被导出裁断……读图的人必须先知道这些，否则会把图当作完整过程
    notes: list[str] = field(default_factory=list)
    # 版面
    thumbs: bool = True
    thumb_height: int = 132
    box_width: int = 470
    box_height: int = 66
    gap_y: int = 36
    gap_error: int = 64
    gap_x: int = 120
    font_size: int = 16
    scale: float = 1.0
    title: str | None = None

    def summary(self) -> dict[str, Any]:
        """可序列化的选项摘要，用于 manifest 判断"同一份输入 + 同一套参数"。"""
        # notes 由 render_flow 按输入填写，同一份 options 会在多个 case 之间复用，
        # 留在摘要里会把上一个 case 的注记带进下一个 case 的指纹。
        skip = {"vision", "on_error", "t_from", "t_to", "notes"}
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in self.__dict__.items()
            if k not in skip
        }


@dataclass
class FlowResult:
    """一次渲染的产出。"""

    path: str
    mermaid: str | None
    width: int
    height: int
    steps: int
    boxes: int
    rounds: int
    truncated_start: bool = False   # 日志只剩尾巴：第一行晚于请求的 --from
    notes: list[str] = field(default_factory=list)      # 与图上第二行相同的诊断注记
    derived_from: str = ""          # 非空 = 步骤由识别记录合成（仅事件模式）
    out_of_order: int = 0           # 时间戳回跳的行数（>0 表示已按时间重排）


def _log_basename(log: Any) -> str:
    """给输出文件取名用的日志名；文本输入没有文件名时退回 'maafw'。"""
    if isinstance(log, str) and "\n" not in log and "\r" not in log:
        return os.path.splitext(os.path.basename(log))[0]
    # 先取到局部变量再判类型：`name` 存在但不是 str 时（例如包装文件描述符的对象
    # 给的是 int）直接 `basename(log.name)` 会抛 TypeError。
    name = getattr(log, "name", None)
    if isinstance(name, str) and name:
        return os.path.splitext(os.path.basename(name))[0]
    return "maafw"


def _norm_source(source: Any) -> Any:
    """把 --vision/--on-error 允许的入参归一化。

    接受：目录、zip（可给 bash 展开后的多个文件，用列表传）、父目录（例如 debug/）、
    已经是 MediaResolver 的对象、内存字节。给的是父目录时自动往下找同名子目录。
    """
    if source is None or isinstance(source, MediaResolver):
        return source
    if isinstance(source, (list, tuple)):
        return MediaResolver(source)
    if hasattr(source, "read"):
        return MediaResolver(source)
    text = str(source)
    if os.path.isdir(text):
        return MediaResolver(text)
    return source


def _pick_subdir(path: Any, name: str) -> Any:
    """`--vision debug/` 这种父目录入参：往下找 `debug/vision`。支持列表与 zip。"""
    if path is None:
        return None
    items = path if isinstance(path, (list, tuple)) else [path]
    out: list[str] = []
    for item in items:
        item = str(item)
        if os.path.isdir(os.path.join(item, name)):
            out.append(os.path.join(item, name))
        elif os.path.isdir(item) or _is_zip(item):
            out.append(item)
    if not out:
        return None
    return out[0] if len(out) == 1 else out


def _first_log_ts(log: Any) -> datetime | None:
    """日志里第一条能解析出时间戳的行。

    用于判断"这份日志是不是只剩尾巴"：遥测附件只带 `maafw.log` 的 512 KB 尾部，
    也可能用户手动截断过。判据是**日志第一行**晚于请求的 `--from`，而不是
    "第一个节点执行晚于 --from"——后者在窗口开头本来就没有动作时也会成立（误报）。
    """
    try:
        for raw in _open_log_lines(log):
            m = LINE_RE.match(raw)
            if m:
                return parse_ts(m.group("ts"))
            if raw.strip():
                return None      # 第一行有内容但不是日志行，说明这不是 maafw.log
    except (OSError, TypeError):
        return None
    return None


def _fmt_ts(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if ts else "?"


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
            return (f"入参像路径但磁盘上没有这个文件：{st.source_name}；"
                    f"它也不含任何一行日志正文")
        if st.source_kind == "file":
            return (f"文件读到了，但没有一行匹配日志格式（不是 maafw.log？）：{st.source_name}")
        return f"入参里没有一行匹配日志格式，不是日志正文：{st.source_name}"
    if st.lines_in_window == 0:
        return (f"窗口内没有日志行：日志时间范围 {_fmt_ts(st.first_ts)} ~ {_fmt_ts(st.last_ts)}；"
                f"{window}")
    if st.lines_other_task:
        return (f"窗口内 {st.lines_in_window} 行里属于本 task（--task-id={opts.task_id}）的节点级记录"
                f"一条都没有，有 {st.lines_other_task} 行是其他 task_id 的："
                f"这个 task 在窗口内没跑过，或 task_id 写错了。{window}")
    if st.lines_internal_skipped:
        return (f"窗口内 {st.lines_in_window} 行只有内部识别记录（{st.lines_internal_skipped} 条，"
                f"形如 recognition/OCR/<uuid>），默认过滤掉了；加 --keep-internal 可以看")
    extra = f"，其中 {st.truncated_skipped} 条因导出截断读不出" if st.truncated_skipped else ""
    return (f"窗口内 {st.lines_in_window} 行里没有节点级记录（PipelineTask node done），"
            f"也没有可用的识别结果（reco [result=...]）{extra}。{window}")


def build_notes(parsed: ParsedLog, opts: FlowOptions) -> list[str]:
    """图上第二行与终端提示共用的诊断注记。

    只用中文字符：`⚠` 这类符号在 msyh 里没有字形，会画成方框。
    """
    st = parsed.stats
    notes: list[str] = []
    if parsed.derived_from:
        notes.append("由识别记录还原（日志没有 PipelineTask node done）")
        if parsed.derived_clamped:
            notes.append("识别范围已按该 task 的起始时间夹住")
        elif opts.task_id:
            notes.append("识别记录里没有 task_id，同窗口其他任务可能混入")
    # 父节点只在"日志里确实有派发记录、却有步骤没归上"时才提示；全归上了就不刷屏
    if st.nextlist_seen and (st.parent_fallback_entry or st.parent_unknown):
        missed = st.parent_fallback_entry + st.parent_unknown
        notes.append(f"{missed} 步的父节点没落在本窗口的派发记录里，已退回任务入口或留空")
    if opts.parents and st.nextlist_seen == 0 and (parsed.steps or parsed.attempts):
        notes.append("日志里没有 Node.NextList 事件，父节点只能留空")
    if st.out_of_order:
        notes.append(f"日志时间戳有回跳，已按时间重排（{st.out_of_order} 行）")
    if st.truncated_skipped:
        notes.append(f"{st.truncated_skipped} 条记录因导出截断读不出，已跳过")
    if st.truncated_kept:
        notes.append(f"{st.truncated_kept} 条记录尾部被截断，数字可能不全")
    if st.events_dropped:
        notes.append(f"已排除其他 task 的 {st.events_dropped} 条失败事件（本会画进图里）")
    return notes


def render_flow(log,
                *,
                vision: Any = None,
                on_error: Any = None,
                out: str | None = None,
                outdir: str | None = None,
                options: FlowOptions | None = None,
                mermaid: bool = False,
                list_only: bool = False) -> FlowResult | str:
    """解析日志并画流程图；库调用入口，与命令行共用同一实现。

    ``log`` 可以是文件路径、日志正文，或任何可逐行的文本流——遥测从 Sentry 取回的
    日志没有本地文件，直接传文本即可。``list_only=True`` 时返回分轮文本（与 ``--list``
    输出一致），不画图。
    """
    opts = options or FlowOptions()
    vision = _norm_source(opts.vision if vision is None else vision)
    on_error = _norm_source(opts.on_error if on_error is None else on_error)
    if on_error is None and vision is not None:
        # 顺手带上同级 on_error（与 CLI 的既有行为一致）
        for root in (vision.roots if isinstance(vision, MediaResolver) else []):
            if root[0] == "dir":
                cand = os.path.join(os.path.dirname(root[1]), "on_error")
                if os.path.isdir(cand):
                    on_error = MediaResolver(cand)
                    break

    parsed = parse_log(log, opts.t_from, opts.t_to, opts.task_id, opts.keep_internal)
    raw = round_steps(parsed, opts)
    if not raw:
        raise ValueError(describe_empty_window(parsed, opts, log))

    notes = build_notes(parsed, opts)

    truncated_start = False
    if opts.t_from is not None:
        first_ts = _first_log_ts(log)
        if first_ts is not None and first_ts > opts.t_from:
            # 日志被截断（遥测只带 tail）时，请求的窗口起点早于日志第一行。
            # 如实标注，而不是当成"任务从这里开始"来解读。
            truncated_start = True

    if list_only:
        parents, _ = roles(raw, opts.repeat_threshold)
        cut = ({n.strip() for n in opts.cut_on.split(",") if n.strip()} if opts.cut_on
               else choose_cut_set(raw, parents, opts.min_round_len))
        hard_cut = {n for n in cut if n not in parents}
        rounds = build_rounds(raw, opts.min_round_len, cut, hard_cut)
        cols = compose_columns(parsed, rounds, opts.miss_min, opts.collapse, opts.miss_max)
        lines: list[str] = []
        for i, col in enumerate(cols):
            lines.append(f"--- 轮 {i + 1} ---")
            for s in col:
                tag = {"miss": "[未命中]", "event": "[事件]"}.get(s.kind, "")
                lines.append(f"  {s.ts.strftime('%H:%M:%S.%f')[:-3]}  {s.name:<28}{tag} {s.detail}")
                if opts.parents:
                    # 纯文本不省候选（图上会折成"另 N 项"，这里给全，排查时省一次翻日志）
                    lines.append(f"  {'':<12}└ {s.parent_detail(full=True)}")
        lines.append(f"\n共 {len(parsed.steps)} 步 / {len(rounds)} 轮；"
                     f"锚点={opts.anchor or anchor_of(raw, rounds)}；"
                     f"分轮节点={','.join(sorted(cut))}")
        for note in notes:
            lines.append(f"提示：{note}")
        return "\n".join(lines)

    if out:
        target = os.path.abspath(out)
    else:
        base_dir = os.path.abspath(outdir) if outdir else DEFAULT_OUTDIR
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(base_dir, f"flow_{_log_basename(log)}_{stamp}.png")
    os.makedirs(os.path.dirname(target), exist_ok=True)

    opts.vision, opts.on_error = vision, on_error
    opts.truncated_start = truncated_start
    opts.notes = notes
    opts.log = str(log) if isinstance(log, str) and "\n" not in log else _log_basename(log) + ".log"
    try:
        w, h, n, _shown, nr = draw_flow(parsed, opts, target)
    except ModuleNotFoundError as exc:
        if (exc.name or "").startswith("PIL"):
            raise ModuleNotFoundError(
                "画图需要 Pillow，当前解释器里没有。发布包自带的 python 也不含它——"
                "请用装了 Pillow 的系统 Python 跑（或先 pip install pillow）。"
            ) from exc
        raise

    parents, _ = roles(raw, opts.repeat_threshold)
    cut = ({n.strip() for n in opts.cut_on.split(",") if n.strip()} if opts.cut_on
           else choose_cut_set(raw, parents, opts.min_round_len))
    hard_cut = {n for n in cut if n not in parents}
    rounds = build_rounds(raw, opts.min_round_len, cut, hard_cut)[: opts.max_rounds]
    boxes = sum(len(c) for c in compose_columns(parsed, rounds, opts.miss_min, opts.collapse, opts.miss_max))

    mermaid_path: str | None = None
    if mermaid:
        mermaid_path = os.path.splitext(target)[0] + ".mmd"
        # 父节点那一行同时作为虚线"派发"边（实线仍是执行顺序，两者含义不同，别混着读）
        with open(mermaid_path, "w", encoding="utf-8") as fh:
            fh.write("flowchart TD\n")
            for i, r in enumerate(rounds):
                fh.write(f"  subgraph R{i + 1}[第 {i + 1} 轮]\n    direction TB\n")
                for j, s in enumerate(r):
                    fh.write(f'    n{i}_{j}["{s.name}"]\n')
                for j in range(len(r) - 1):
                    fh.write(f"    n{i}_{j} --> n{i}_{j + 1}\n")
                fh.write("  end\n")
                if i:
                    fh.write(f"  n{i - 1}_{len(rounds[i - 1]) - 1} --> n{i}_0\n")
            if opts.parents:
                index: dict[str, str] = {}
                for i, r in enumerate(rounds):
                    for j, s in enumerate(r):
                        index.setdefault(s.name, f"n{i}_{j}")
                for i, r in enumerate(rounds):
                    for j, s in enumerate(r):
                        if not s.parent or s.dispatched:
                            continue  # 自派发/自循环画出来只是噪音
                        src = index.get(s.parent)
                        if src and src != f"n{i}_{j}":
                            fh.write(f"  {src} -.->|派发| n{i}_{j}\n")

    return FlowResult(path=target, mermaid=mermaid_path, width=w, height=h,
                      steps=n, boxes=boxes, rounds=nr, truncated_start=truncated_start,
                      notes=list(notes), derived_from=parsed.derived_from,
                      out_of_order=parsed.stats.out_of_order)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            today = datetime.now().date()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt
    raise SystemExit(f"看不懂的时间格式：{s}")


def default_output_dir() -> str:
    """默认输出目录：仓库根的 temp/flow。"""
    return DEFAULT_OUTDIR


def build_flow_options(args: argparse.Namespace) -> FlowOptions:
    """命令行参数 → FlowOptions（库调用方可以直接构造，不必过 CLI）。"""
    return FlowOptions(
        t_from=parse_time(args.t_from),
        t_to=parse_time(args.t_to),
        task_id=args.task_id,
        keep_internal=args.keep_internal,
        anchor=args.anchor,
        cut_on=args.cut_on,
        collapse=args.collapse,
        repeat_threshold=args.repeat_threshold,
        min_round_len=args.min_round_len,
        miss_min=args.miss_min,
        miss_max=args.miss_max,
        max_rounds=args.max_rounds,
        ports=args.ports,
        cols_per_band=args.cols_per_band,
        parents=args.parents,
        vision=_pick_subdir(args.vision, "vision") if args.vision else None,
        on_error=_pick_subdir(args.on_error, "on_error") if args.on_error else None,
        thumbs=args.thumbs,
        thumb_height=args.thumb_height,
        box_width=args.box_width,
        box_height=args.box_height,
        gap_y=args.gap_y,
        gap_error=args.gap_error,
        gap_x=args.gap_x,
        font_size=args.font_size,
        scale=args.scale,
        title=args.title,
    )


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="maafw.log → 实际执行流程图（横向=轮次、纵向=轮内顺序；"
                    "默认输出到仓库根的 temp/flow/）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("log", help="maafw.log 路径（也可以是 MFA 导出的 zip 里的那份，见 --vision 的说明）")
    ap.add_argument("--vision", default=None,
                    help="vision 目录，或 MFA 导出包（zip / 多个 archive-partNN.zip），或它们的父目录（如 debug/）")
    ap.add_argument("--on-error", dest="on_error", default=None,
                    help="on_error 目录或导出包（可选，默认取 --vision 同级的 on_error；给超时/失败框贴现场图）")
    ap.add_argument("--out", default=None, help=f"输出 PNG（默认 {DEFAULT_OUTDIR}/flow_<日志名>_<时间戳>.png）")
    ap.add_argument("--outdir", default=None, help=f"只改输出目录（默认 {DEFAULT_OUTDIR}）")
    ap.add_argument("--from", dest="t_from", default=None, help="起始时间，支持 'HH:MM:SS' 或完整时间戳")
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间")
    ap.add_argument("--task-id", type=int, default=None, help="只看某个 task_id")
    ap.add_argument("--anchor", default=None, help="手动指定循环锚点（只影响标题显示，默认自动）")
    ap.add_argument("--cut-on", default=None,
                    help="手动指定分轮节点，逗号分隔（默认自动：父节点里重复出现的；只有一轮时再取最高频节点）")
    ap.add_argument("--collapse", type=int, default=3, help="连续同名节点折叠为 ×N 的阈值（默认 3，1=不折叠）")
    ap.add_argument("--repeat-threshold", type=int, default=2, help="出现次数 >= N 视为重要节点（左下出/右上进，默认 2）")
    ap.add_argument("--min-round-len", type=int, default=2, help="一轮至少多少步（默认 2）")
    ap.add_argument("--miss-min", type=int, default=5, help="连续未命中几次才画成聚合框（默认 5，0=全部显示）")
    ap.add_argument("--miss-max", type=int, default=8, help="每轮最多画几个未命中聚合框（默认 8，0=不限）")
    ap.add_argument("--max-rounds", type=int, default=40, help="最多画多少轮")
    ap.add_argument("--ports", choices=["pos", "role"], default="pos",
                    help="端口规则：pos=按方向（竖直相邻上入下出、水平相邻左入右出，默认）；"
                         "role=按节点角色（重要节点左下出/右上进，父节点左下出/右下进）")
    ap.add_argument("--cols-per-band", type=int, default=0,
                    help="一行放几轮；默认 0 = 不换行，空间不够就一直往右延伸")
    ap.add_argument("--keep-internal", action="store_true", help="保留 agent 内联识别（recognition/OCR/<uuid>）")
    ap.add_argument("--no-thumbs", dest="thumbs", action="store_false", help="不贴截图")
    ap.add_argument("--no-parents", dest="parents", action="store_false",
                    help="框里不写父节点那一行。父节点 = **本轮的派发者**（来自 Node.NextList 事件），"
                         "不是配置里的层级：next 是有序候选集，同一节点可被多处派发，所以是调用图不是树")
    ap.add_argument("--thumb-height", type=int, default=132, help="截图高度 px（默认 132）")
    ap.add_argument("--box-width", type=int, default=470)
    ap.add_argument("--box-height", type=int, default=66)
    ap.add_argument("--gap-y", type=int, default=36, help="同一列里相邻框的间距（默认 36）")
    ap.add_argument("--gap-error", type=int, default=64, help="未命中/异常框相邻时的间距（默认 64，别挤在一起）")
    ap.add_argument("--gap-x", type=int, default=120, help="列间距（默认 120）")
    ap.add_argument("--font-size", type=int, default=16)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--list", action="store_true", help="只打印解析出的步骤，不画图")
    ap.add_argument("--mermaid", action="store_true", help="额外输出同名 .mmd（Mermaid 文本流程图）")
    ap.set_defaults(thumbs=True, parents=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = create_argument_parser().parse_args(argv)
    options = build_flow_options(args)

    try:
        result = render_flow(args.log, out=args.out, outdir=args.outdir,
                             options=options, mermaid=args.mermaid, list_only=args.list)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ModuleNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.list:
        print(result)
        return 0

    assert isinstance(result, FlowResult)
    print(f"步骤 {result.steps}（折叠后 {result.boxes} 个框）/ 轮次 {result.rounds}"
          f" → {result.path}  ({result.width}×{result.height})")
    if result.truncated_start:
        print("提示：日志第一行晚于 --from，这份日志只包含了窗口的后半段（遥测附件只带 tail），"
              "窗口开头的过程不在图里", file=sys.stderr)
    for note in result.notes:
        # 与图上第二行同一批注记：终端里再打一遍，免得只看 stdout 的人漏掉读图前提
        print(f"提示：{note}", file=sys.stderr)
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
