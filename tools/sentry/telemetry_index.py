"""本地遥测库：把 Sentry issue 的详情**增量**存到 `temp/`，供画图离线查"这个包对应哪个问题"。

## 为什么需要它

用户发来的是**本地日志包**，而画图是离线的（`flow` 不联网）。两边唯一对得上的键是日志里的
失败信息：`Node.PipelineNode.Failed` 的 `name` / `node_id` / `task_id`，以及 `Task timeout` /
`node not completed` 的节点名。Sentry 事件 context 里正好有 `failure.task_id` / `failure.node_id`
（实测 30 个真实包里 25 个能靠这两项精确对上）。

所以做法是：联网时把 issue 详情**增量**落一份本地库（只补新增或 `lastSeen` 变了的），
画图时在库里查——查询零网络、可复现。

## 产出

    temp/telemetry/issues/<SHORT-ID>.json   ← 每个 issue 一份（issue view 的解析结果）
    temp/telemetry/index.json               ← {schema, issues: {<SHORT>: {status, lastSeen, fetched_at}}}

## 清理（用户明确要求：过时 / 已解决的数据不能留）

- `status` 是 resolved/ignored/archived → 默认**删**（`--keep-resolved` 才留）；
- `lastSeen` 早于 `--telemetry-days`（默认 14 天）→ 删；
- 不在最近一次 `issue list` 返回里 → 删（服务器上已消失）；
- `index.json` 的 `schema` 变了 → 整库重建，不做迁移。

**不做**：不缓存 spans/聚合数据（那是 `fetch` 的活）、不自动删别的工具写的 `temp/` 文件、
不猜测"哪个 issue 一定对"——匹配永远给候选和级别，不硬猜（实测一次运行常对应多个 issue）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools._logging import configure_tool_logging

log = logging.getLogger("tools.telemetry_index")

try:
    from . import report_common
    from .config import CONFIG
    from .evidence import issue_context, release_version
except ImportError:  # 直接跑文件时
    import report_common  # type: ignore[no-redef]
    from config import CONFIG  # type: ignore[no-redef]
    from evidence import issue_context, release_version  # type: ignore[no-redef]

SCHEMA = 1
STORE_DIR = "temp/telemetry/issues"
INDEX_NAME = "index.json"
DEFAULT_DAYS = 14
DEFAULT_GAP_DAYS = 3
# 匹配级别：越小越强。排序与展示都用它
TIER_RANK = {"high": 0, "mid": 1, "low": 2}
TIER_LABEL = {"high": "高", "mid": "中", "low": "低"}

# issue title：`Maa task failed: Entrust (EntrustBranchChapter/recognition)`
TITLE_RE = re.compile(r"^Maa task failed:\s*(.*?)\s*\(([^/]+)/([^/)]+)\)\s*$")
# 日志里的行首时间（与 maafw 的 LINE_RE 同格式，本地自带一份避免反向依赖绘图模块）
LINE_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]")
# `Node.PipelineNode.Failed` 两种写法：带 details 的（有 node_id/task_id），和只有 [name=] 的
PIPE_FAILED_DETAILS_RE = re.compile(
    r"\[msg=Node\.PipelineNode\.Failed\]\s*\[details=(\{.*?\})\]\s*$"
)
PIPE_FAILED_NAME_RE = re.compile(r"\[msg=Node\.PipelineNode\.Failed\]\s*\[name=([^\]]+)\]")
# 其它失败标记（与 evidence.MARKER_RES 同一批判据）
MARKER_RES = (
    re.compile(r"Task timeout \[pretask\.name=([^\]]+)\]"),
    re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]"),
    re.compile(r"node not completed, handle error \[node\.name=([^\]]+)\]"),
)
CB_ENTRY_RE = re.compile(r'\[cb_detail=(\{[^}]*"entry":"[^"]*"[^}]*\})\]')
NODE_DONE_RE = re.compile(r'PipelineTask node done \[result=(\{.*?\})\] \[task_id_=(\d+)\]')

ISSUE_OPEN_STATUSES = {"", "open", "unresolved", "archived"}
ISSUE_CLOSED_STATUSES = {"resolved", "ignored", "muted", "deleted"}


# --------------------------------------------------------------------------- #
# 卡片与候选
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class IssueCard:
    """一个 issue 的详情（从 `sentry issue view --json` 解析）。"""

    short_id: str
    title: str = ""
    count: Any = None
    status: str = ""
    last_seen: str = ""
    received: str = ""
    release: str = ""  # 归一成版本字符串（MFA release 可能是对象）
    failure_task_id: int | None = None
    failure_node_id: int | None = None
    failure_message: Any = None
    attachment_status: Any = None
    logs_status: Any = None
    event_id: str = ""
    permalink: str = ""

    @property
    def label(self) -> str:
        """title 里的任务名（`Maa task failed: Entrust (…)` → `Entrust`）。"""
        match = TITLE_RE.match(self.title)
        return match.group(1).strip() if match else ""

    @property
    def node_name(self) -> str:
        match = TITLE_RE.match(self.title)
        return match.group(2) if match else ""

    @property
    def stage(self) -> str:
        match = TITLE_RE.match(self.title)
        return match.group(3) if match else ""

    def to_json(self) -> dict:
        return {
            "short_id": self.short_id,
            "title": self.title,
            "count": self.count,
            "status": self.status,
            "last_seen": self.last_seen,
            "received": self.received,
            "release": self.release,
            "failure_task_id": self.failure_task_id,
            "failure_node_id": self.failure_node_id,
            "failure_message": self.failure_message,
            "attachment_status": self.attachment_status,
            "logs_status": self.logs_status,
            "event_id": self.event_id,
            "permalink": self.permalink,
        }


@dataclass(frozen=True)
class FailureCandidate:
    """本地日志里的一次失败：节点名 +（可能有的）node_id / task_id + 本地时刻。"""

    name: str
    ts: datetime
    node_id: int | None = None
    task_id: int | None = None
    kind: str = ""  # pipeline-node-failed | timeout | invalid-node | not-completed
    # 失败任务的入口名（`TaskEscort` 这类）：通用兜底节点被多个任务共用，按节点名匹配时
    # 还要拿它跟问题标题里的任务名对一遍，否则 A 任务的问题会匹配到 B 任务的同名失败。
    entry: str = ""


@dataclass(frozen=True)
class Match:
    card: IssueCard
    tier: str  # high | mid | low
    detail: str
    gap_seconds: float | None = None


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #


def _parse_time(text: str) -> datetime | None:
    """Sentry 的时刻（UTC）：ISO `...Z` 或 `YYYY/MM/DD HH:MM:SS`。解析不出返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _utc_offset() -> timedelta:
    """本机时区偏移：本地日志是 naive 本地时间，Sentry 是 UTC，比较前要换算。"""
    offset = datetime.now().astimezone().utcoffset()
    return offset or timedelta(0)


def _local_as_utc(ts: datetime) -> datetime:
    return (ts - _utc_offset()).replace(tzinfo=timezone.utc)


def _fmt_time(value: datetime) -> str:
    return value.strftime("%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 本地库
# --------------------------------------------------------------------------- #


def store_dir() -> Path:
    return report_common.repo_root() / STORE_DIR


def index_path() -> Path:
    return store_dir() / INDEX_NAME


def card_path(short_id: str) -> Path:
    return store_dir() / f"{short_id}.json"


def load_index() -> dict:
    path = index_path()
    if not path.is_file():
        return {"schema": SCHEMA, "issues": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": SCHEMA, "issues": {}}
    if not isinstance(data, dict) or not isinstance(data.get("issues"), dict):
        return {"schema": SCHEMA, "issues": {}}
    return data


def save_index(index: dict) -> None:
    store_dir().mkdir(parents=True, exist_ok=True)
    with open(index_path(), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(index, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_card(short_id: str) -> IssueCard | None:
    path = card_path(short_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("short_id"):
        return None
    return IssueCard(**{k: v for k, v in data.items() if k in IssueCard.__dataclass_fields__})


def _drop(short_id: str, notes: list[str], index: dict | None = None) -> None:
    """把一个 issue 从本地库清掉：卡片文件 + `index["issues"]` 里的条目。

    只删卡片文件不够——索引项还在的话，"库里有几个 issue"（索引）和"实际能画几个"
    （卡片）会对不上，`load_cards` 也会静默跳过（实测 13 vs 11）。
    """
    path = card_path(short_id)
    if path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            notes.append(f"{short_id} 的卡片删不掉（{exc}）")
            return
    if index is not None:
        index.get("issues", {}).pop(short_id, None)
    notes.append(f"{short_id} 从本地遥测库清掉")


def write_card(card: IssueCard) -> None:
    store_dir().mkdir(parents=True, exist_ok=True)
    with open(card_path(card.short_id), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(card.to_json(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# 联网取数（增量）
# --------------------------------------------------------------------------- #


def list_issues(
    sentry: str,
    target: str,
    *,
    period: str,
    limit: int,
    verbose: bool,
    timeout: float,
    query: str | None = None,
) -> list[dict]:
    """`sentry issue list` → 清单（shortId/title/count/lastSeen/status）。

    `query` 走 Sentry 的搜索语法（`is:unresolved` 这类）。服务端过滤比在本地猜 `status`
    可靠——`issue list --json` 不一定回 `status` 字段。
    """
    arguments = [
        "issue",
        "list",
        target,
        "--period",
        period,
        "--limit",
        str(limit),
        "--json",
    ]
    if query:
        arguments += ["--query", query]
    payload = report_common.run_sentry_json_value(
        sentry, arguments, verbose=verbose, timeout_seconds=timeout
    )
    rows = payload.get("data") if isinstance(payload, dict) else payload
    return [row for row in rows or [] if isinstance(row, dict)]


def issue_view(
    sentry: str, short_id: str, *, verbose: bool, timeout: float
) -> dict:
    raw = report_common.run_sentry_json_value(
        sentry,
        ["issue", "view", short_id, "--json"],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"sentry issue view {short_id} 返回了非对象 JSON。")
    return raw


def parse_issue_view(raw: dict, short_id: str) -> IssueCard:
    """`issue view --json` → IssueCard。

    `event.release` 在 Sentry 里是**对象**（含 `version`），不是字符串——直接 `str()` 会写成
    Python repr（上一版就这么错了）。这里统一取 `version`。
    """
    event = raw.get("event") or {}
    ctx = issue_context(event)
    return IssueCard(
        short_id=str(raw.get("shortId") or short_id),
        title=str(raw.get("title") or ""),
        count=raw.get("count"),
        status=str(raw.get("status") or event.get("status") or ""),
        last_seen=str(raw.get("lastSeen") or raw.get("lastEvent") or ""),
        received=str(event.get("dateCreated") or event.get("timestamp") or ""),
        release=release_version(event),
        failure_task_id=_as_int(ctx.get("failure_task_id")),
        failure_node_id=_as_int(ctx.get("failure_node_id")),
        failure_message=ctx.get("failure_message"),
        attachment_status=ctx.get("attachment_status"),
        logs_status=ctx.get("logs_status"),
        event_id=str(event.get("id") or ""),
        permalink=str(raw.get("permalink") or ""),
    )


def refresh(
    sentry: str,
    target: str,
    *,
    verbose: bool = False,
    timeout: float = 180.0,
    days: int = DEFAULT_DAYS,
    keep_resolved: bool = False,
    period: str = "30d",
    limit: int = 200,
) -> tuple[dict, list[str]]:
    """把本地遥测库补到最新，顺手清理过时/已解决的。返回 (index, 变化说明)。"""
    notes: list[str] = []
    index = load_index()
    if index.get("schema") != SCHEMA:
        notes.append(f"本地遥测库 schema {index.get('schema')} ≠ {SCHEMA}，整库重建")
        for old in list(index.get("issues", {})):
            _drop(old, notes, index)
        index = {"schema": SCHEMA, "issues": {}}

    # 先让服务端只回未解决的（本地 `status` 字段不一定有）；老 CLI 不认 `--query` 就退回全量
    rows: list[dict] = []
    server_filtered = False
    if not keep_resolved:
        try:
            rows = list_issues(
                sentry,
                target,
                period=period,
                limit=limit,
                verbose=verbose,
                timeout=timeout,
                query="is:unresolved",
            )
            server_filtered = True
        except RuntimeError:
            server_filtered = False
    if not server_filtered:
        rows = list_issues(
            sentry, target, period=period, limit=limit, verbose=verbose, timeout=timeout
        )
        if not keep_resolved:
            notes.append(
                "这个 sentry CLI 不认 `--query is:unresolved`，已解决的 issue 只能靠 status/时间清"
            )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    live: dict[str, dict] = {}
    for row in rows:
        short = str(row.get("shortId") or "")
        if not short:
            continue
        status = str(row.get("status") or "").lower()
        last_seen_raw = str(row.get("lastSeen") or row.get("lastEvent") or "")
        last_seen = _parse_time(last_seen_raw)
        # 已解决/已忽略：默认清掉（它不再代表"当前问题"，留着会让匹配误命中）
        if status in ISSUE_CLOSED_STATUSES and not keep_resolved:
            _drop(short, notes, index)
            continue
        # 过时：最后事件早于保留期
        if last_seen is not None and last_seen < cutoff:
            _drop(short, notes, index)
            continue
        live[short] = {"status": status, "lastSeen": last_seen_raw}

    # 服务器上已经没了 → 清掉
    for short in list(index.get("issues", {})):
        if short not in live:
            _drop(short, notes, index)

    fetched = 0
    for short, meta in live.items():
        cached = index["issues"].get(short) or {}
        if (
            cached.get("lastSeen") == meta["lastSeen"]
            and card_path(short).is_file()
        ):
            continue  # 增量：没变就不重下
        raw = issue_view(sentry, short, verbose=verbose, timeout=timeout)
        card = parse_issue_view(raw, short)
        write_card(card)
        index["issues"][short] = {
            "status": meta["status"],
            "lastSeen": meta["lastSeen"],
            "fetched_at": now.isoformat(),
        }
        fetched += 1
    if fetched:
        notes.append(f"本地遥测库新增/更新 {fetched} 个 issue")
    save_index(index)
    return index, notes


def load_cards(index: dict, *, short_ids: list[str] | None = None) -> list[IssueCard]:
    """读出卡片；`short_ids` 为空时读全部。读不了的跳过（不抛）。

    索引里有、卡片缺失或读不出时打一条 warning：不然"库里有几个 issue"（索引）和
    "实际能画几个"（卡片）会对不上，批量出图会静默少画（实测 13 vs 11）。
    """
    keys = short_ids if short_ids is not None else list(index.get("issues", {}))
    out: list[IssueCard] = []
    missing: list[str] = []
    for short in keys:
        card = read_card(short)
        if card is not None:
            out.append(card)
        else:
            missing.append(short)
    if missing and short_ids is None:
        shown = "、".join(missing[:8])
        more = f" 等 {len(missing)} 个" if len(missing) > 8 else ""
        log.warning("索引里有、卡片缺失或读不出，已跳过：%s%s", shown, more)
    return out


# --------------------------------------------------------------------------- #
# 本地日志 → 失败候选
# --------------------------------------------------------------------------- #


def extract_candidates(log: Any) -> list[FailureCandidate]:
    """从本地日志里抽失败候选（零网络）。

    `log` 可以是路径、日志正文或文本流（与 `flow_from_log._open_log_lines` 同口径）。
    只认四种硬证据：`Node.PipelineNode.Failed`（带 details 时有 node_id/task_id）、
    `Task timeout`、`invalid node id`、`node not completed`。
    """
    if hasattr(log, "read"):
        lines = [str(line).rstrip("\n") for line in log]
    elif isinstance(log, os.PathLike):
        lines = _read_file_lines(os.fspath(log))
    elif isinstance(log, str) and "\n" not in log and os.path.isfile(log):
        lines = _read_file_lines(log)
    elif isinstance(log, str):
        lines = log.splitlines()
    else:
        return []

    entries: dict[int, str] = {}
    cands: list[FailureCandidate] = []
    seen: set[tuple] = set()
    for raw in lines:
        m = CB_ENTRY_RE.search(raw)
        if m:
            try:
                j = json.loads(m.group(1))
            except json.JSONDecodeError:
                j = {}
            tid, entry = _as_int(j.get("task_id")), j.get("entry")
            if tid is not None and isinstance(entry, str) and entry:
                entries.setdefault(tid, entry)
        m = NODE_DONE_RE.search(raw)
        if m:
            try:
                j = json.loads(m.group(1))
            except json.JSONDecodeError:
                j = {}
            name = str(j.get("name") or "")
            if name.startswith("Task"):
                entries.setdefault(int(m.group(2)), name)

        ts_match = LINE_TS_RE.match(raw)
        ts = (
            datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S.%f")
            if ts_match
            else None
        )
        if ts is None:
            continue

        m = PIPE_FAILED_DETAILS_RE.search(raw)
        if m:
            try:
                j = json.loads(m.group(1))
            except json.JSONDecodeError:
                j = {}
            name = str(j.get("name") or "")
            tid = _as_int(j.get("task_id"))
            key = (name, j.get("node_id"), j.get("task_id"))
            if name and key not in seen:
                seen.add(key)
                cands.append(
                    FailureCandidate(
                        name=name,
                        ts=ts,
                        node_id=_as_int(j.get("node_id")),
                        task_id=tid,
                        kind="pipeline-node-failed",
                        entry=entries.get(tid, "") if tid is not None else "",
                    )
                )
            continue
        m = PIPE_FAILED_NAME_RE.search(raw)
        if m:
            key = (m.group(1), None, None)
            if key not in seen:
                seen.add(key)
                cands.append(
                    FailureCandidate(
                        name=m.group(1), ts=ts, kind="pipeline-node-failed"
                    )
                )
            continue
        for pattern in MARKER_RES:
            mm = pattern.search(raw)
            if mm:
                key = (mm.group(1), None, None)
                if key not in seen:
                    seen.add(key)
                    cands.append(
                        FailureCandidate(name=mm.group(1), ts=ts, kind="marker")
                    )
                break
    return cands


def _read_file_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


def _gap_seconds(card: IssueCard, cand: FailureCandidate) -> float | None:
    """卡片时刻（UTC）与候选本地时刻的差（秒）；卡片没时刻返回 None。"""
    card_ts = _parse_time(card.received) or _parse_time(card.last_seen)
    if card_ts is None:
        return None
    return abs((card_ts - _local_as_utc(cand.ts)).total_seconds())


def match(
    candidates: list[FailureCandidate],
    cards: list[IssueCard],
    *,
    gap_days: int = DEFAULT_GAP_DAYS,
) -> list[Match]:
    """把候选和卡片对上，给级别。级别定义见 README：

    - 高：`task_id` 与 `node_id` 都相等（实测 25/30 的真实包能中）；
    - 中：节点名相同，且卡片时刻与失败时刻差 ≤ `gap_days`；
    - 低：只有节点名相同。

    一张卡只保留最强的那条命中；结果按级别、时间差排序。
    """
    limit = gap_days * 86400
    out: list[Match] = []
    for card in cards:
        best: Match | None = None
        for cand in candidates:
            tier = ""
            detail = ""
            gap: float | None = None
            if (
                cand.task_id is not None
                and cand.node_id is not None
                and cand.task_id == card.failure_task_id
                and cand.node_id == card.failure_node_id
            ):
                tier, detail = "high", "task_id + node_id 全等"
                # 同一级别里用时间差排序：同一次运行可能有多个失败都精确对上
                gap = _gap_seconds(card, cand)
            elif card.node_name and cand.name == card.node_name:
                # 通用兜底节点（BaseTaskStopError / BaseErrorNext / BaseProbeConfirm）被多个
                # 任务共用：只有节点名对得上不够，失败任务的入口名也要跟标题里的任务名一致
                # （`TaskEntrust` ↔ 标题 `Entrust`）。两边缺一头就不强判，保留原匹配。
                if cand.entry and card.label:
                    norm = (
                        cand.entry[4:]
                        if cand.entry.startswith("Task")
                        else cand.entry
                    )
                    if norm != card.label:
                        continue
                gap = _gap_seconds(card, cand)
                if gap is not None and gap <= limit:
                    tier = "mid"
                    detail = f"同节点名，时间差 {_fmt_gap(gap)}"
                else:
                    tier, detail = "low", "只有节点名相同"
            if not tier:
                continue
            current = Match(card=card, tier=tier, detail=detail, gap_seconds=gap)
            if best is None or TIER_RANK[tier] < TIER_RANK[best.tier]:
                best = current
            elif tier == best.tier and gap is not None:
                if best.gap_seconds is None or gap < best.gap_seconds:
                    best = current
        if best is not None:
            out.append(best)
    # 排序：级别 → 时间差 → 事件新旧（**不用 count**：热门问题会压过真正的那次）。
    def _recency(m: Match) -> float:
        stamp = _parse_time(m.card.last_seen) or _parse_time(m.card.received)
        return -stamp.timestamp() if stamp else 0.0

    out.sort(
        key=lambda m: (
            TIER_RANK[m.tier],
            m.gap_seconds if m.gap_seconds is not None else float("inf"),
            _recency(m),
        )
    )
    return out


def _fmt_gap(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}min"
    return f"{seconds / 3600:.1f}h"


# --------------------------------------------------------------------------- #
# 注记（纯函数，画图侧直接用）
# --------------------------------------------------------------------------- #


def card_notes(
    card: IssueCard | None,
    matches: list[Match],
    *,
    chosen: str = "",
) -> list[tuple[str, str]]:
    """把遥测关联写成图上注记（`(文本, kind)`，与 `flow_from_log.Note` 同口径）。"""
    notes: list[tuple[str, str]] = []
    if card is not None:
        rel = ""
        if card.release:
            try:
                label = report_common.version_label(card.release)
            except Exception:
                label = None
            rel = f" · {label}" if label else ""
        when = _fmt_seen(card.last_seen)
        notes.append(
            (
                f"遥测问题 {card.short_id}：{card.count} 次"
                + (f" · 最后 {when}" if when else "")
                + rel,
                "version",
            )
        )
        if card.title:
            notes.append((f"遥测失败：{card.title}", "fail"))
    if matches:
        ids = "、".join(m.card.short_id for m in matches[:4])
        more = f" 等 {len(matches)} 个" if len(matches) > 4 else ""
        head = f"（取 {chosen}）" if chosen else ""
        notes.append(
            (f"匹配到 {len(matches)} 个遥测问题{head}：{ids}{more}", "info")
        )
    return notes


def _fmt_seen(text: str) -> str:
    """Sentry 的时刻是 UTC，注记里要显示**本地时间**（日志是本地时间，读者对的是日志）。"""
    parsed = _parse_time(text)
    if parsed is None:
        return ""
    return _fmt_time((parsed + _utc_offset()).replace(tzinfo=None))


# --------------------------------------------------------------------------- #
# CLI（可单独跑：刷新 / 只查）
# --------------------------------------------------------------------------- #


def refresh_default(**kwargs: Any) -> tuple[dict, list[str]]:
    """刷新本地库，自己解析 sentry 命令与 target（画图侧不想 import 这些）。"""
    sentry = report_common.resolve_sentry_command()
    return refresh(sentry, CONFIG.target, **kwargs)


@dataclass
class Annotation:
    """一次"本地包 → 遥测问题"的结论。"""

    card: IssueCard | None = None
    matches: list[Match] = field(default_factory=list)
    chosen: str = ""
    version: str = ""  # 归一成 `vX.Y.Z`（给「疑似过时」当基线）
    notes: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0


def annotate(
    log: Any,
    *,
    index: dict | None = None,
    offline: bool = False,
    issue: str | None = None,
    task_id: int | None = None,
    days: int = DEFAULT_DAYS,
    gap: int = DEFAULT_GAP_DAYS,
    keep_resolved: bool = False,
    verbose: bool = False,
    timeout: float = 180.0,
) -> Annotation:
    """拿本地日志在本地遥测库里查对应的 issue；返回注记 + 版本基线。

    `index` 给了就不再刷新（调用方已刷过一次）；`offline=True` 只读本地库、不联网。
    刷新失败（没装 sentry CLI / 没登录 / 网络不通）只记一条警告，**不影响画图**。
    """
    out = Annotation()
    if index is None:
        if offline:
            index = load_index()
        else:
            try:
                sentry = report_common.resolve_sentry_command()
                index, _notes = refresh(
                    sentry,
                    CONFIG.target,
                    verbose=verbose,
                    timeout=timeout,
                    days=days,
                    keep_resolved=keep_resolved,
                )
            except (RuntimeError, OSError) as exc:
                out.warnings.append(f"遥测库没刷新成（{exc}），本次用本地已有数据")
                index = load_index()

    cards = load_cards(index, short_ids=[issue] if issue else None)
    if not cards:
        out.warnings.append(
            "本地遥测库是空的：先跑一次 `python -m tools.sentry.cli telemetry --refresh`（需联网）"
        )
        return out

    # 只读一遍日志：完整的 maafw.log 有几十 MB，读两次纯属浪费
    candidates = extract_candidates(log)
    if task_id is not None:
        # 指定了 task 就只认**它自己**的失败：一份包常含多个 task 的失败，
        # 用整份日志去查会把别的 task 的问题算到这张图上（`--all-tasks` 尤其明显）。
        owned = [c for c in candidates if c.task_id == task_id]
        if owned:
            candidates = owned
    out.candidates = len(candidates)
    if not candidates:
        out.warnings.append("日志里没找到失败标记（PipelineNode.Failed / Task timeout 等），无法反查")
        return out

    out.matches = match(candidates, cards, gap_days=gap)
    if not out.matches:
        out.warnings.append(
            "本地遥测库里没有对得上的问题（可能还没上报，或已被清理）"
        )
        return out

    highs = [m for m in out.matches if m.tier == "high"]
    if issue:
        picked = next((m for m in out.matches if m.card.short_id == issue), None)
    elif len(highs) == 1:
        picked = highs[0]
    else:
        picked = out.matches[0]

    if picked is not None:
        out.card = picked.card
        # 只有「高」置信才用它命名产物；中/低置信只进注记（显式 --telemetry-issue 例外）。
        # 通用兜底节点会让中置信的问题号挂到别的任务上，写进文件名就是把问题张冠李戴。
        out.chosen = (
            picked.card.short_id if (picked.tier == "high" or issue) else ""
        )
        if picked.card.release:
            try:
                out.version = report_common.version_label(picked.card.release) or ""
            except Exception:
                out.version = ""
        if len(highs) > 1:
            out.warnings.append(
                f"同一次运行里有 {len(highs)} 个失败都精确对上（"
                + "、".join(m.card.short_id for m in highs[:4])
                + f"），先按时间取了 {picked.card.short_id}；用 --telemetry-issue 指定"
            )
        elif picked.tier != "high":
            out.warnings.append(
                f"{picked.card.short_id} 只是「{TIER_LABEL[picked.tier]}」置信（{picked.detail}），"
                "别把它当唯一答案；不以它命名产物"
            )
    out.notes = card_notes(out.card, out.matches, chosen=out.chosen)
    return out


def format_matches(matches: list[Match], output: TextIO) -> None:
    if not matches:
        print("没有匹配的遥测问题。", file=output)
        return
    print(f"\n匹配到 {len(matches)} 个遥测问题：", file=output)
    for m in matches:
        card = m.card
        print(
            f"  [{TIER_LABEL[m.tier]}] {card.short_id:<12} "
            f"{card.title[:44]:<46} {card.count or 0:>3} 次  {m.detail}",
            file=output,
        )


def create_argument_parser(prog: str | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog,
        description="本地遥测库：增量下载 issue 详情，并按本地日志反查对应的问题",
    )
    parser.add_argument("--refresh", action="store_true", help="只刷新本地遥测库，不查")
    parser.add_argument("--case", default=None, help="本地日志/证据包，反查它对应哪个 issue")
    parser.add_argument("--issue", default=None, help="只报告这个 issue（不反查）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"保留最近 N 天（默认 {DEFAULT_DAYS}）")
    parser.add_argument("--gap", type=int, default=DEFAULT_GAP_DAYS, help=f"匹配的时间窗（默认 {DEFAULT_GAP_DAYS} 天）")
    parser.add_argument("--keep-resolved", action="store_true", help="保留已解决/已忽略的 issue")
    parser.add_argument("--period", default="30d", help="列表窗口（默认 30d）")
    parser.add_argument("--limit", type=int, default=200, help="列表上限（默认 200）")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次调用超时秒数")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="日志文件路径（默认按运行时间戳建 temp/logs/telemetry_<时间戳>_<pid>.log）；"
        "传 none / 空 只走控制台",
    )
    return parser


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    # `cli.py` 统一按 `(argv, prog)` 调各子命令的 main，签名要对齐
    args = create_argument_parser(prog).parse_args(argv)
    _, log_path = configure_tool_logging(
        "telemetry", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)
    sentry = report_common.resolve_sentry_command()
    try:
        index, notes = refresh(
            sentry,
            CONFIG.target,
            verbose=args.verbose,
            timeout=args.timeout,
            days=args.days,
            keep_resolved=args.keep_resolved,
            period=args.period,
            limit=args.limit,
        )
    except RuntimeError as exc:
        log.error(
            "刷新本地遥测库失败：%s；若报「unable to open database file」，"
            "把 SENTRY_CONFIG_DIR 指到含可写 cli.db 的目录",
            exc,
        )
        return 1
    for note in notes:
        log.info("提示：%s", note)

    if args.refresh:
        print(f"本地遥测库：{len(index['issues'])} 个 issue")
        return 0

    if args.issue:
        card = read_card(args.issue)
        if card is None:
            log.error("%s 不在本地遥测库里。", args.issue)
            return 1
        for text, kind in card_notes(card, []):
            print(text)
        return 0

    if not args.case:
        log.error("要给 --case <日志/证据包> 才会反查。")
        return 1

    candidates = extract_candidates(args.case)
    matches = match(candidates, load_cards(index), gap_days=args.gap)
    format_matches(matches, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
