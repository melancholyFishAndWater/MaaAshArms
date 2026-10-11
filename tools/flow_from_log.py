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
  python tools/flow_from_log.py D:/MaaAshArms/debug/maafw.log \
      --vision D:/MaaAshArms/debug/vision \
      --from "2026-10-11 21:39:56" --to "2026-10-11 21:43:29" \
      --out flow_escort.png

只做解析、不画图：加 --list。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

# --------------------------------------------------------------------------- #
# 日志解析
# --------------------------------------------------------------------------- #

LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]"
    r"\[(?P<lvl>[A-Z]{3})\]"
    r"\[Px\d+\]\[Tx\d+\]"
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
TIMEOUT_RE = re.compile(
    r"Task timeout \[pretask\.name=([^\]]+)\] \[duration_since\(start_clock\)=(\d+)ms\]"
    r" \[pretask\.reco_timeout=(\d+)ms\]"
)
INVALID_RE = re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]")
NOT_COMPLETED_RE = re.compile(r"node not completed, handle error \[node\.name=([^\]]+)\]")

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

    @property
    def detail(self) -> str:
        bits: list[str] = []
        if self.kind == "miss":
            bits.append(f"未命中 ×{self.run_len}")
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
class ParsedLog:
    steps: list[Step] = field(default_factory=list)
    attempts: list[Step] = field(default_factory=list)  # 含未命中
    events: list[Step] = field(default_factory=list)
    tasks: list[tuple[datetime, str, int, str]] = field(default_factory=list)  # start
    task_ends: list[tuple[datetime, str, int, bool]] = field(default_factory=list)
    window: tuple[datetime, datetime] | None = None


def parse_log(path: str, t_from: datetime | None, t_to: datetime | None,
              task_id: int | None, keep_internal: bool) -> ParsedLog:
    out = ParsedLog()
    recl_by_id: dict[int, dict] = {}
    act_by_id: dict[int, dict] = {}
    dispatcher = ""
    task_names: dict[int, str] = {}

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            m = LINE_RE.match(raw.rstrip("\n"))
            if not m:
                continue
            ts = parse_ts(m.group("ts"))
            if t_from and ts < t_from:
                continue
            if t_to and ts > t_to:
                break
            msg = m.group("msg")

            # 当前在走谁的 next 表
            w = WALK_RE.search(msg)
            if w:
                dispatcher = w.group(1).strip()
                continue

            # 任务起止
            ms = TASK_START_RE.search(msg)
            if ms:
                d = json.loads(ms.group(1))
                task_names[int(d.get("task_id", 0))] = d.get("entry", "")
                out.tasks.append((ts, d.get("entry", ""), int(d.get("task_id", 0)), d.get("hash", "")))
                out.events.append(Step(ts=ts, name=f"任务开始 {d.get('entry','')}", completed=True,
                                       kind="event", action=f"task_id={d.get('task_id')}",
                                       task_id=int(d.get("task_id", 0))))
                continue
            me = TASK_END_RE.search(msg)
            if me:
                d = json.loads(me.group(1))
                ret = me.group(2) == "true"
                out.task_ends.append((ts, d.get("entry", ""), int(d.get("task_id", 0)), ret))
                out.events.append(Step(ts=ts, name=f"任务结束 {d.get('entry','')} {'成功' if ret else '失败'}",
                                       completed=ret, kind="event", action=f"ret={me.group(2)}",
                                       task_id=int(d.get("task_id", 0))))
                continue

            # 识别结果（命中或不命中都记）
            mr = RECO_RE.search(msg)
            if mr:
                try:
                    j = json.loads(mr.group(1))
                except json.JSONDecodeError:
                    continue
                rid = j.get("reco_id")
                if isinstance(rid, int):
                    recl_by_id[rid] = j
                name = j.get("name", "")
                if INTERNAL_NAME_RE.match(name) and not keep_internal:
                    continue
                texts: list[str] = []
                collect_texts(j.get("detail"), texts)
                out.attempts.append(Step(
                    ts=ts, name=name, completed=bool(j.get("box")),
                    reco_id=rid if isinstance(rid, int) else None,
                    algorithm=j.get("algorithm", ""), box=j.get("box"), texts=texts,
                    dispatcher=dispatcher, kind="hit" if j.get("box") else "miss",
                ))
                continue

            # 动作结果
            ma = ACTION_RE.search(msg)
            if ma:
                try:
                    j = json.loads(ma.group(1))
                except json.JSONDecodeError:
                    continue
                aid = j.get("action_id")
                if isinstance(aid, int):
                    act_by_id[aid] = j
                continue

            # 节点执行完毕（主流程的一步）
            md = NODE_DONE_RE.search(msg)
            if md:
                try:
                    j = json.loads(md.group(1))
                except json.JSONDecodeError:
                    continue
                tid = int(md.group(2))
                name = j.get("name", "")
                if INTERNAL_NAME_RE.match(name) and not keep_internal:
                    continue
                if task_id is not None and tid != task_id:
                    continue
                reco = recl_by_id.get(j.get("reco_id"), {})
                act = act_by_id.get(j.get("action_id"), {})
                texts: list[str] = []
                collect_texts(reco.get("detail"), texts)
                out.steps.append(Step(
                    ts=ts, name=name, completed=bool(j.get("completed")),
                    reco_id=j.get("reco_id"), action_id=j.get("action_id"),
                    node_id=j.get("node_id"), task_id=tid,
                    algorithm=reco.get("algorithm", ""), box=reco.get("box"), texts=texts,
                    action=act.get("action", ""), action_ok=act.get("success"),
                    dispatcher=dispatcher,
                ))
                continue

            # 异常
            mt = TIMEOUT_RE.search(msg)
            if mt:
                out.events.append(Step(
                    ts=ts, name=f"Task timeout {mt.group(1)}", completed=False, kind="event",
                    action=f"{mt.group(2)}ms > timeout {mt.group(3)}ms"))
                continue
            mi = INVALID_RE.search(msg)
            if mi:
                out.events.append(Step(ts=ts, name="invalid node id", completed=False,
                                       kind="event", action=mi.group(1)))
                continue
            mn = NOT_COMPLETED_RE.search(msg)
            if mn:
                out.events.append(Step(ts=ts, name="node not completed", completed=False,
                                       kind="event", action=mn.group(1)))
                continue

    if task_id is not None:
        out.events = [e for e in out.events if e.task_id in (None, task_id)] or out.events
    return out


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
        out.append(Step(**{**s.__dict__}))
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
        if s.name in hard and cur:
            cut = True
        elif s.name in seen and len(cur) >= min_len and (cut_on is None or s.name in cut_on):
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
# vision 目录
# --------------------------------------------------------------------------- #


class VisionIndex:
    """`debug/vision/<时间>_<节点名>_<reco_id>.jpg`。

    MaaFramework 只给"有东西可画"的识别（TemplateMatch / OCR / ColorMatch…）存图，
    `Custom` / `DirectHit` 节点没有自己的图。所以取图有三级回退：
      1. (节点名, reco_id) 精确命中 —— 那张图里带 ROI 框和模板缩略图；
      2. 同一节点的 `*_wait_freezes.jpg`（该节点执行期间的画面）；
      3. 同一时刻任意节点的邻近帧（只当作"当时画面"，会标注出来）。
    """

    NAME_RE = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.*?)_(?P<tail>wait_freezes|\d+)\.jpg$")

    def __init__(self, root: str | None):
        self.root = root
        self.by_reco: dict[tuple[str, int], str] = {}
        self.freezes: list[tuple[datetime, str, str]] = []
        self.frames: list[tuple[datetime, str]] = []
        if not root or not os.path.isdir(root):
            return
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                m = self.NAME_RE.match(fn)
                if not m:
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
                except ValueError:
                    continue
                if m.group("tail") == "wait_freezes":
                    self.freezes.append((ts, m.group("name"), path))
                else:
                    self.by_reco[(m.group("name"), int(m.group("tail")))] = path
                self.frames.append((ts, path))
        self.freezes.sort()
        self.frames.sort()

    def find(self, step: Step, tol: float = 3.0, neighbor_tol: float = 2.0):
        """返回 (图片路径, 是否该节点自己的识别图)。找不到返回 (None, False)。"""
        if step.reco_id is not None:
            p = self.by_reco.get((step.name, int(step.reco_id)))
            if p:
                return p, True
        best, best_d = None, tol
        for ts, name, path in self.freezes:
            if name != step.name:
                continue
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = path, d
        if best:
            return best, False
        best, best_d = None, neighbor_tol
        for ts, path in self.frames:
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = path, d
        return best, False


class OnErrorIndex:
    """`debug/on_error/<时间>_<节点名>.png`：节点失败时框架抓的现场图。"""

    NAME_RE = re.compile(r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.+)\.png$")

    def __init__(self, root: str | None):
        self.items: list[tuple[datetime, str, str]] = []
        if not root or not os.path.isdir(root):
            return
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                m = self.NAME_RE.match(fn)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
                except ValueError:
                    continue
                self.items.append((ts, m.group("name"), os.path.join(dirpath, fn)))
        self.items.sort()

    def find(self, step: Step, tol: float = 8.0) -> str | None:
        """按时间就近找；`Task timeout X` / `invalid node id` 事件的 action 里存着失败节点名，优先同名。"""
        target = step.action.strip()
        best, best_d = None, tol
        for ts, name, path in self.items:
            d = abs((ts - step.ts).total_seconds())
            if target and name == target:
                d *= 0.15
            if d < best_d:
                best, best_d = path, d
        return best


# --------------------------------------------------------------------------- #
# 画图
# --------------------------------------------------------------------------- #


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


PALETTE = {
    "hit": ((232, 241, 255), (72, 110, 180), (26, 43, 74)),
    "custom": ((240, 235, 255), (122, 92, 180), (52, 32, 92)),
    "direct": ((238, 238, 240), (130, 130, 140), (52, 52, 58)),
    "miss": ((255, 236, 233), (200, 92, 74), (110, 30, 20)),
    "error": ((255, 224, 224), (190, 40, 40), (110, 0, 0)),
    "event": ((228, 246, 236), (56, 140, 96), (20, 70, 46)),
    "task": ((226, 232, 240), (60, 74, 100), (18, 26, 42)),
}


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


def compose_columns(parsed: ParsedLog, rounds: list[list[Step]], miss_min: int,
                    collapse: int = 1, miss_max: int = 8) -> list[list[Step]]:
    """每轮 = 该轮命中步骤（按需折叠 ×N）+ 该轮内的"连续未命中"聚合框；异常事件挂到所属轮末尾。"""
    tail = max([a.ts for a in parsed.attempts] + [e.ts for e in parsed.events] + [rounds[-1][-1].ts])

    def miss_rows(i: int) -> list[Step]:
        lo = rounds[i][0].ts
        hi = rounds[i + 1][0].ts if i + 1 < len(rounds) else tail
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


def draw_flow(parsed: ParsedLog, args, out_path: str) -> tuple[int, int, int, int, int]:
    from PIL import Image, ImageDraw

    f_name = load_font(max(11, int(args.font_size * args.scale)), bold=True)
    f_det = load_font(max(9, int((args.font_size - 3) * args.scale)))
    f_title = load_font(int((args.font_size + 8) * args.scale), bold=True)
    f_leg = load_font(max(10, int((args.font_size - 2) * args.scale)))

    raw = parsed.steps
    if not raw:
        raise SystemExit("窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id")

    # 父节点 = 派发过子节点的节点（日志里出现 [cur_node_=X] 的那些），它们拥有 next 表
    # 重要节点 = 全局重复出现的节点
    parents, important = roles(raw, args.repeat_threshold)
    if args.cut_on:
        cut = {n.strip() for n in args.cut_on.split(",") if n.strip()}
    else:
        cut = choose_cut_set(raw, parents, args.min_round_len)
    hard_cut = {n for n in cut if n not in parents}
    rounds = build_rounds(raw, args.min_round_len, cut, hard_cut)
    if len(rounds) > args.max_rounds:
        rounds = rounds[: args.max_rounds]
    anchor = args.anchor or anchor_of(raw, rounds)

    # 每轮末尾追加该轮内"连续未命中"的聚合框；窗口右端延伸到下一轮开始（末轮延伸到最后一个动作/事件）
    columns = compose_columns(parsed, rounds, args.miss_min, args.collapse, args.miss_max)
    steps = [s for r in rounds for s in r]

    # ---- 先把每个框要贴的图解析出来（决定框高：有无图）----
    box_w = int(args.box_width * args.scale)
    pad = int(10 * args.scale)
    slot_w = int(box_w * 0.46)                       # 右侧图槽
    slot_h = int(args.thumb_height * args.scale) if args.thumbs else 0
    box_h = max(int(args.box_height * args.scale), slot_h + int(18 * args.scale)) if slot_h else int(args.box_height * args.scale)
    event_h = int(args.box_height * args.scale)
    gap_y = int(args.gap_y * args.scale)
    gap_err = int(args.gap_error * args.scale)
    gap_x = int(args.gap_x * args.scale)
    margin = int(34 * args.scale)
    title_h = int(64 * args.scale)
    header_h = int(34 * args.scale)
    cols_per_band = max(1, args.cols_per_band) if args.cols_per_band else len(columns)

    vision = VisionIndex(args.vision) if args.vision else None
    on_err = OnErrorIndex(args.on_error) if args.on_error else None
    # (列, 行) -> (图片路径, 是否该节点自己的识别图, 角标文字)
    thumb_of: dict[tuple[int, int], tuple[str, bool, str]] = {}
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
            for ts, name, path in on_err.items:
                cands = [x for x in by_name.get(name, []) if x[2].ts <= ts]
                if not cands:
                    continue
                ci, ri, _st = max(cands, key=lambda x: x[2].ts)
                thumb_of[(ci, ri)] = (path, False, "失败现场")

    bands: list[list[int]] = [list(range(i, min(i + cols_per_band, len(columns))))
                             for i in range(0, len(columns), cols_per_band)]

    def box_height_for(ci: int, ri: int) -> int:
        return box_h if (ci, ri) in thumb_of else event_h

    # 每列的高度：框高 + 间隔（未命中/异常相邻处用更大的间隔，别挤在一起）
    def column_geo(ci: int, x: int, y0: int) -> tuple[list[tuple[int, int, int, int]], int]:
        y = y0 + header_h
        boxes: list[tuple[int, int, int, int]] = []
        prev: Step | None = None
        for ri, st in enumerate(columns[ci]):
            h = box_height_for(ci, ri)
            if prev is not None:
                noisy = prev.kind in ("miss", "event") or st.kind in ("miss", "event")
                y += gap_err if noisy else gap_y
            boxes.append((x, y, box_w, h))
            y += h
            prev = st
        return boxes, y

    # 先按"同一行"的纵坐标依次算一遍，再定画布高度
    col_x: dict[int, int] = {}
    probe: dict[int, int] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            col_x[ci] = margin + (ci - band[0]) * (box_w + gap_x)
            _boxes, probe[ci] = column_geo(ci, col_x[ci], y_band)
        y_band = max(probe[ci] for ci in band) + int(96 * args.scale)

    height = y_band + int(64 * args.scale)
    width = margin * 2 + max(len(b) for b in bands) * (box_w + gap_x)
    width = max(width, int(900 * args.scale))

    img = Image.new("RGB", (width, height), (250, 251, 253))
    d = ImageDraw.Draw(img)

    # ---- 标题 ----
    t0 = rounds[0][0].ts if rounds and rounds[0] else None
    t1 = rounds[-1][-1].ts if rounds and rounds[-1] else None
    title = args.title or f"执行流程  ·  {os.path.basename(args.log)}"
    sub = f"{t0.strftime('%Y-%m-%d %H:%M:%S') if t0 else '?'} → {t1.strftime('%H:%M:%S') if t1 else '?'}"
    sub += f"   |   {len(steps)} 步 / {len(rounds)} 轮"
    if anchor:
        sub += f"   |   循环锚点 {anchor}"
    if args.task_id:
        sub += f"   |   task_id={args.task_id}"
    d.text((margin, int(14 * args.scale)), title, font=f_title, fill=(20, 26, 40))
    d.text((margin, int(40 * args.scale)), sub, font=f_leg, fill=(96, 104, 122))

    # ---- 每列的框位置 + 列底色/表头 ----
    pos: dict[int, list[tuple[int, int, int, int]]] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            boxes, bottom = column_geo(ci, col_x[ci], y_band)
            pos[ci] = boxes
            x = col_x[ci]
            if ci % 2 == 1:
                d.rounded_rectangle([x - int(14 * args.scale), y_band,
                                     x + box_w + int(14 * args.scale), bottom + int(12 * args.scale)],
                                    radius=int(12 * args.scale), fill=(244, 246, 250))
            r = rounds[ci]
            label = f"第 {ci + 1} 轮 · {r[0].ts.strftime('%H:%M:%S')}–{r[-1].ts.strftime('%H:%M:%S')} · {len(r)} 步"
            d.rounded_rectangle([x, y_band, x + box_w, y_band + int(24 * args.scale)],
                                radius=int(6 * args.scale), fill=(226, 230, 238))
            d.text((x + int(8 * args.scale), y_band + int(4 * args.scale)), label, font=f_leg, fill=(58, 66, 84))
        y_band = max(pos[ci][-1][1] + pos[ci][-1][3] for ci in band) + int(96 * args.scale)

    # ---- 端口与连线（先画线，再画框，线不会压住文字）----
    # 端口规则（--ports pos，默认）：
    #   同列相邻（竖直方向） —— 上入下出：出口 = 下中，入口 = 上中
    #   跨轮相邻（水平方向） —— 左入右出：出口 = 右中，入口 = 左中
    # --ports role 是旧规则：重要节点左下出/右上进，父节点左下出/右下进。
    pad_port = int(12 * args.scale)
    lane_gap = int(34 * args.scale)

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
        if args.ports == "pos":
            return (x + w, y + h // 2) if horizontal else (x + w // 2, y + h)
        if role_of(columns[ci][ri].name) == "plain":
            return (x + w // 2, y + h)
        return (x + pad_port, y + h)

    def in_port(ci: int, ri: int, cross_round: bool = False) -> tuple[tuple[int, int], str]:
        """返回 (接点坐标, 'top'|'left'|'right')，'left'/'right' 表示从左右边缘水平进入。"""
        x, y, w, h = pos[ci][ri]
        if args.ports == "pos":
            return ((x, y + h // 2), "left") if cross_round else ((x + w // 2, y), "top")
        r = role_of(columns[ci][ri].name)
        if cross_round or r == "repeat":
            return (x + w - pad_port, y), "top"
        if r in ("parent", "parent+repeat"):
            return (x + w, y + h - pad_port), "right"
        return (x + w // 2, y), "top"

    def arrow(points: list[tuple[int, int]], color, width: int = 2):
        d.line(points, fill=color, width=width, joint="curve")
        (x0, y0), (x1, y1) = points[-2], points[-1]
        ah = int(9 * args.scale)
        if abs(x1 - x0) >= abs(y1 - y0):  # 水平进入
            s = 1 if x1 > x0 else -1
            d.polygon([(x1, y1), (x1 - s * ah, y1 - ah // 2), (x1 - s * ah, y1 + ah // 2)], fill=color)
        else:  # 垂直进入
            s = 1 if y1 > y0 else -1
            d.polygon([(x1, y1), (x1 - ah // 2, y1 - s * ah), (x1 + ah // 2, y1 - s * ah)], fill=color)

    def route(a: tuple[int, int], b: tuple[int, int], kind: str, bx: int, bw: int) -> None:
        if kind == "top":  # 竖直方向：上入下出
            y_mid = a[1] + max(int(14 * args.scale), (b[1] - a[1]) // 2)
            arrow([a, (a[0], y_mid), (b[0], y_mid), b], (150, 158, 174))
        elif kind == "left":  # 水平方向：左入右出（竖线走两列之间的空档）
            x_lane = (a[0] + bx) // 2
            arrow([a, (x_lane, a[1]), (x_lane, b[1]), b], (120, 130, 150), width=3)
        else:  # 旧的"右下进"：从右边缘进
            x_lane = bx + bw + lane_gap
            arrow([a, (a[0], a[1] + int(14 * args.scale)), (x_lane, a[1] + int(14 * args.scale)),
                   (x_lane, b[1]), b], (150, 158, 174))

    band_of = {ci: bi for bi, band in enumerate(bands) for ci in band}
    over = int(26 * args.scale)
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

    def fit_text(s: str, font, maxw: int) -> str:
        if d.textlength(s, font=font) <= maxw:
            return s
        out = s
        while out and d.textlength(out + "…", font=font) > maxw:
            out = out[:-1]
        return out + "…"

    def draw_thumb(st: Step, path: str, exact: bool, note: str, x: int, y: int, w: int, h: int) -> None:
        im = Image.open(path).convert("RGB")
        frame = im.crop((0, 0, min(1280, im.width), min(720, im.height)))
        if exact and st.box and st.box[2] > 0 and st.box[3] > 0:
            bx, by, bw, bh = st.box
            p = int(26 * args.scale)
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
            d.text((sx + 2, sy + slot_h - int(13 * args.scale)), note, font=f_det, fill=(168, 120, 40))

    for ci, col in enumerate(columns):
        for ri, (x, y, w, h) in enumerate(pos[ci]):
            st = col[ri]
            cls = classify(st)
            fill, edge, ink = PALETTE[cls]
            d.rounded_rectangle([x, y, x + w, y + h], radius=int(10 * args.scale),
                                fill=fill, outline=edge, width=2)
            got = thumb_of.get((ci, ri))
            text_w = (w - pad * 2) if not got else (w - slot_w - pad * 3)
            d.text((x + pad, y + int(6 * args.scale)), st.ts.strftime("%H:%M:%S.%f")[:-3],
                   font=f_det, fill=(126, 132, 148))
            label = st.name if st.run_len <= 1 else f"{st.name}  ×{st.run_len}"
            d.text((x + pad, y + int(22 * args.scale)), fit_text(label, f_name, text_w), font=f_name, fill=ink)
            det = st.detail
            if det:
                d.text((x + pad, y + int(22 * args.scale) + int(args.font_size * args.scale) + int(6 * args.scale)),
                       fit_text(det, f_det, text_w), font=f_det, fill=(88, 94, 110))
            if got:
                try:
                    draw_thumb(st, got[0], got[1], got[2], x, y, w, h)
                except Exception:
                    pass

    # ---- 图例 ----
    lx = margin
    ly = height - int(46 * args.scale)
    d.text((lx, ly), "图例：", font=f_leg, fill=(60, 66, 80))
    lx += int(52 * args.scale)
    for key, text in (("hit", "命中并执行"), ("custom", "自定义识别/动作"), ("direct", "DirectHit"),
                      ("miss", "未命中（聚合）"), ("error", "超时/失败"), ("task", "任务起止")):
        fill, edge, _ = PALETTE[key]
        d.rounded_rectangle([lx, ly - int(2 * args.scale), lx + int(16 * args.scale), ly + int(14 * args.scale)],
                            radius=3, fill=fill, outline=edge)
        d.text((lx + int(22 * args.scale), ly), text, font=f_leg, fill=(70, 76, 92))
        lx += int(22 * args.scale) + int(d.textlength(text, font=f_leg)) + int(22 * args.scale)

    img.save(out_path)
    shown = sum(1 for c in columns for s in c if s.kind == "hit")
    return img.width, img.height, len(steps), shown, len(rounds)


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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="maafw.log → 实际执行流程图（横向=轮次、纵向=轮内顺序；父节点左下出/右下进，"
                    "重要节点（跨轮重复）左下出/右上进）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("log", help="maafw.log 路径")
    ap.add_argument("--vision", default=None, help="vision 目录（可选；不传就只画节点框，不贴截图）")
    ap.add_argument("--on-error", dest="on_error", default=None,
                    help="on_error 目录（可选，默认取 --vision 同级的 on_error；用来给超时/失败框贴现场图）")
    ap.add_argument("--out", default=None, help="输出 PNG（默认 <log目录>/flow_<日志名>.png）")
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
    ap.set_defaults(thumbs=True)
    args = ap.parse_args()

    def pick_dir(p: str | None, name: str) -> str | None:
        """允许直接给 vision / on_error 目录，也允许给它们的父目录（例如 debug/）。"""
        if not p:
            return None
        p = os.path.abspath(p.rstrip("/\\"))
        sub = os.path.join(p, name)
        if os.path.isdir(sub):
            return sub
        return p if os.path.isdir(p) else None

    args.vision = pick_dir(args.vision, "vision")
    args.on_error = pick_dir(args.on_error, "on_error")
    # --vision 给了就顺手带上同级的 on_error（超时/失败现场图）
    if args.on_error is None and args.vision:
        cand = os.path.join(os.path.dirname(args.vision), "on_error")
        if os.path.isdir(cand):
            args.on_error = cand

    parsed = parse_log(args.log, parse_time(args.t_from), parse_time(args.t_to),
                       args.task_id, args.keep_internal)
    raw = parsed.steps
    if not raw:
        raise SystemExit("窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id")
    parents, _important = roles(raw, args.repeat_threshold)
    cut = ({n.strip() for n in args.cut_on.split(",") if n.strip()} if args.cut_on
           else choose_cut_set(raw, parents, args.min_round_len))
    hard_cut = {n for n in cut if n not in parents}
    rounds = build_rounds(raw, args.min_round_len, cut, hard_cut)

    tids = sorted({s.task_id for s in raw if s.task_id})
    if len(tids) > 1 and args.task_id is None:
        print(f"提示：窗口里有多个 task_id {tids}，用 --task-id 可只画其中一个", file=sys.stderr)

    if args.list:
        cols = compose_columns(parsed, rounds, args.miss_min, args.collapse, args.miss_max)
        for i, col in enumerate(cols):
            print(f"--- 轮 {i + 1} ---")
            for s in col:
                tag = {"miss": "[未命中]", "event": "[事件]"}.get(s.kind, "")
                print(f"  {s.ts.strftime('%H:%M:%S.%f')[:-3]}  {s.name:<28}{tag} {s.detail}")
        print(f"\n共 {len(raw)} 步 / {len(rounds)} 轮；锚点={args.anchor or anchor_of(raw, rounds)}；"
              f"分轮节点={','.join(sorted(cut))}")
        return

    out = args.out
    if not out:
        base = os.path.splitext(os.path.basename(args.log))[0]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = os.path.join(os.path.dirname(os.path.abspath(args.log)), f"flow_{base}_{stamp}.png")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    w, h, n, shown, nr = draw_flow(parsed, args, out)
    print(f"步骤 {n}（折叠后 {shown} 个框）/ 轮次 {nr} → {out}  ({w}×{h})")

    if args.mermaid:
        mmd = os.path.splitext(out)[0] + ".mmd"
        with open(mmd, "w", encoding="utf-8") as fh:
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
        print(f"mermaid → {mmd}")


if __name__ == "__main__":
    main()
