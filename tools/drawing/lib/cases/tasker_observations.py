"""批量概览卡专用的 Tasker 未初始化日志观察。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from tools.drawing.lib.flow_from_log import _open_log_lines

from .pack import Case, _evidence_manifest

_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]"
    r"\[[^]]+\]\[(?P<process>Px[^]]+)\]\[(?P<thread>Tx[^]]+)\]"
)
_ENTRY_RE = re.compile(r"MaaNS::Tasker::post_task\] \[entry=(?P<entry>[^\]]+)\]")
_TASKER_ERROR = "Tasker not inited"
_ENTRY_WINDOW_SECONDS = 1.0
_MARKER_WINDOW_SECONDS = 30.0
_NEARBY_WINDOW_SECONDS = 2.0
_MARKER_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+")


def _record(line: str) -> tuple[datetime, str, str] | None:
    match = _LINE_RE.match(line)
    if match is None:
        return None
    try:
        stamp = datetime.fromisoformat(match.group("ts"))
    except ValueError:
        return None
    return stamp, match.group("process"), match.group("thread")


def _marker_times(manifest: dict[str, Any]) -> list[datetime]:
    trace = manifest.get("trace") or {}
    raw_markers = trace.get("markers") if isinstance(trace, dict) else None
    result: list[datetime] = []
    for marker in raw_markers or []:
        if not isinstance(marker, str):
            continue
        match = _MARKER_RE.match(marker.strip())
        if match is None:
            continue
        try:
            result.append(datetime.fromisoformat(match.group("ts")))
        except ValueError:
            continue
    return result


def tasker_observations(case: Case | None, card: Any) -> list[tuple[str, str]]:
    """仅提供同证据包内的日志观察，不将时间邻近当成事件归属证据。"""
    if case is None:
        return []
    event_id = str(getattr(card, "event_id", "") or "")
    label = str(getattr(card, "label", "") or "")
    manifest = _evidence_manifest(case)
    event = manifest.get("event") or {}
    if not event_id or not label or not isinstance(event, dict):
        return []
    if str(event.get("id") or "") != event_id:
        return []
    markers = _marker_times(manifest)
    if not markers:
        return []
    latest: dict[tuple[str, str], tuple[datetime, str]] = {}
    candidates: set[tuple[datetime, str, str]] = set()
    lines: list[tuple[str, datetime, str, str]] = []
    try:
        for line in _open_log_lines(case.log_source()):
            parsed = _record(line)
            if parsed is None:
                continue
            stamp, process, thread = parsed
            lines.append((line, stamp, process, thread))
            match = _ENTRY_RE.search(line)
            if match:
                latest[(process, thread)] = (stamp, match.group("entry"))
            if _TASKER_ERROR not in line:
                continue
            entry = latest.get((process, thread))
            if entry is None or entry[1] != f"Task{label}":
                continue
            delta = (stamp - entry[0]).total_seconds()
            if 0 <= delta <= _ENTRY_WINDOW_SECONDS and any(
                abs((stamp - marker).total_seconds()) <= _MARKER_WINDOW_SECONDS
                for marker in markers
            ):
                candidates.add((stamp, process, thread))
    except (OSError, TypeError):
        return []
    if len(candidates) != 1:
        return [("观察：同名任务有多个投递候选，无法确定事件归属", "info")] if candidates else []
    error_stamp, process, thread = next(iter(candidates))
    notes = [
        (f"附近日志：Task{label} 投递时 Tasker not inited", "info"),
        (f"时刻 {error_stamp:%H:%M:%S}；事件归属尚未确认；关联不等于因果", "info"),
    ]
    nearby = [
        line.lower() for line, stamp, px, tx in lines
        if px == process and tx == thread
        and abs((stamp - error_stamp).total_seconds()) <= _NEARBY_WINDOW_SECONDS
    ]
    if any("screencap failed" in line for line in nearby):
        notes.append(("附近同线程出现截图失败；不代表因果关系", "info"))
    if any("re-connect failed" in line or "reconnect failed" in line for line in nearby):
        notes.append(("附近同线程出现重连失败；不代表因果关系", "info"))
    return notes
