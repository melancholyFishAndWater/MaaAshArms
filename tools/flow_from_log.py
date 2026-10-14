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

输出默认落到**仓库根的 `temp/flow/`**（不再写到日志旁边）；`--out` / `--outdir` 可覆盖。

作为库调用（遥测侧就这么用——远程没有本地日志文件，只有 Sentry 取回的日志正文）：

  from tools.flow_from_log import FlowOptions, MediaResolver, render_flow

  # 日志正文可以直接传字符串；图片证据可以是目录、zip/多卷、或内存里的字节
  res = render_flow(log_text,
                    vision=MediaResolver(["log-part01.zip", "log-part02.zip"]),
                    options=FlowOptions(t_from=t0, t_to=t1, task_id=200000118))
  print(res.path, res.width, res.height, res.steps)

批量与幂等（同一个证据包重跑会跳过）用 `python tools/flow_cases.py`，
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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator

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


def _open_log_lines(source) -> Iterator[str]:
    """把 stdin / 文本 / 路径三种入参统一成"逐行"。

    判据顺序是 ``read`` 属性 → **存在的文件路径** → 其余当文本：
    前者是文本流（含 StringIO、stdin）；中间那步用 ``os.path.isfile`` 而不是"含不含换行"，
    因为**单行日志正文也不含换行**，按换行判会把它当路径去 ``open()``，
    抛的是 `OSError Invalid argument`（不是 FileNotFoundError），很难看出原因。
    """
    if hasattr(source, "read"):
        for line in source:
            yield line.rstrip("\n")
        return

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


def parse_log(path, t_from: datetime | None, t_to: datetime | None,
              task_id: int | None, keep_internal: bool) -> ParsedLog:
    """解析日志。`path` 可以是文件路径、日志正文，或任何可逐行的文本流。"""
    out = ParsedLog()
    recl_by_id: dict[int, dict] = {}
    act_by_id: dict[int, dict] = {}
    dispatcher = ""
    task_names: dict[int, str] = {}

    for raw in _open_log_lines(path):
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


def draw_flow(parsed: ParsedLog, options: "FlowOptions", out_path: str) -> tuple[int, int, int, int, int]:
    from PIL import Image, ImageDraw

    f_name = load_font(max(11, int(options.font_size * options.scale)), bold=True)
    f_det = load_font(max(9, int((options.font_size - 3) * options.scale)))
    f_title = load_font(int((options.font_size + 8) * options.scale), bold=True)
    f_leg = load_font(max(10, int((options.font_size - 2) * options.scale)))

    raw = parsed.steps
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

    # ---- 先把每个框要贴的图解析出来（决定框高：有无图）----
    box_w = int(options.box_width * options.scale)
    pad = int(10 * options.scale)
    slot_w = int(box_w * 0.46)                       # 右侧图槽
    slot_h = int(options.thumb_height * options.scale) if options.thumbs else 0
    box_h = max(int(options.box_height * options.scale), slot_h + int(18 * options.scale)) if slot_h else int(options.box_height * options.scale)
    event_h = int(options.box_height * options.scale)
    gap_y = int(options.gap_y * options.scale)
    gap_err = int(options.gap_error * options.scale)
    gap_x = int(options.gap_x * options.scale)
    margin = int(34 * options.scale)
    title_h = int(64 * options.scale)
    header_h = int(34 * options.scale)
    cols_per_band = max(1, options.cols_per_band) if options.cols_per_band else len(columns)

    vision = VisionIndex(options.vision) if options.vision else None
    on_err = OnErrorIndex(options.on_error) if options.on_error else None
    # (列, 行) -> (证据条目, 是否该节点自己的识别图, 角标文字)
    thumb_of: dict[tuple[int, int], tuple[MediaEntry, bool, str]] = {}
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
        y_band = max(probe[ci] for ci in band) + int(96 * options.scale)

    height = y_band + int(64 * options.scale)
    width = margin * 2 + max(len(b) for b in bands) * (box_w + gap_x)
    width = max(width, int(900 * options.scale))

    img = Image.new("RGB", (width, height), (250, 251, 253))
    d = ImageDraw.Draw(img)

    # ---- 标题 ----
    t0 = rounds[0][0].ts if rounds and rounds[0] else None
    t1 = rounds[-1][-1].ts if rounds and rounds[-1] else None
    title = options.title or f"执行流程  ·  {os.path.basename(options.log)}"
    sub = f"{t0.strftime('%Y-%m-%d %H:%M:%S') if t0 else '?'} → {t1.strftime('%H:%M:%S') if t1 else '?'}"
    sub += f"   |   {len(steps)} 步 / {len(rounds)} 轮"
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
        """返回 (接点坐标, 'top'|'left'|'right')，'left'/'right' 表示从左右边缘水平进入。"""
        x, y, w, h = pos[ci][ri]
        if options.ports == "pos":
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

    def fit_text(s: str, font, maxw: int) -> str:
        if d.textlength(s, font=font) <= maxw:
            return s
        out = s
        while out and d.textlength(out + "…", font=font) > maxw:
            out = out[:-1]
        return out + "…"

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
            d.text((x + pad, y + int(6 * options.scale)), st.ts.strftime("%H:%M:%S.%f")[:-3],
                   font=f_det, fill=(126, 132, 148))
            label = st.name if st.run_len <= 1 else f"{st.name}  ×{st.run_len}"
            d.text((x + pad, y + int(22 * options.scale)), fit_text(label, f_name, text_w), font=f_name, fill=ink)
            det = st.detail
            if det:
                d.text((x + pad, y + int(22 * options.scale) + int(options.font_size * options.scale) + int(6 * options.scale)),
                       fit_text(det, f_det, text_w), font=f_det, fill=(88, 94, 110))
            if got:
                try:
                    draw_thumb(st, got[0], got[1], got[2], x, y, w, h)
                except Exception:
                    pass

    # ---- 图例 ----
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
    return img.width, img.height, len(steps), shown, len(rounds)


# --------------------------------------------------------------------------- #
# 可复用的调用接口
# --------------------------------------------------------------------------- #

# 仓库根：tools/flow_from_log.py 上溯两级。默认输出目录挂在它下面，避免写到日志旁边。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
    # 证据来源：目录 / zip / zip 列表 / MediaResolver / None
    vision: Any = None
    on_error: Any = None
    # 日志名，只用于默认标题；由 render_flow 按入参填好，调用方不用管
    log: str = ""
    # 日志只剩尾部（第一行晚于请求的 --from）：会印在图的副标题上，别省成"完整日志"
    truncated_start: bool = False
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
        skip = {"vision", "on_error", "t_from", "t_to"}
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


def render_flow(log,
                *,
                vision: Any = None,
                on_error: Any = None,
                out: str | None = None,
                outdir: str | None = None,
                options: "FlowOptions | None" = None,
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
    raw = parsed.steps
    if not raw:
        raise ValueError("窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id")

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
        lines.append(f"\n共 {len(raw)} 步 / {len(rounds)} 轮；锚点={opts.anchor or anchor_of(raw, rounds)}；"
                     f"分轮节点={','.join(sorted(cut))}")
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

    return FlowResult(path=target, mermaid=mermaid_path, width=w, height=h,
                      steps=n, boxes=boxes, rounds=nr, truncated_start=truncated_start)


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
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
