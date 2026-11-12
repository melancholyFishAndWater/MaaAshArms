"""本地日志失败候选抽取：纯本地、零网络。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


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


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class FailureCandidate:
    """本地日志里的一次失败：节点名 +（可能有的）node_id / task_id + 本地时刻。"""

    name: str
    ts: datetime
    node_id: int | None = None
    task_id: int | None = None
    kind: str = ""  # pipeline-node-failed | timeout | invalid-node | not-completed
    entry: str = ""




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


