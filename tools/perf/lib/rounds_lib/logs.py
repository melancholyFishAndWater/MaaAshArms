"""读日志与分桶口径：一次派发怎么配对、失败候选怎么数、一轮多长。

输入是 `maafw.log` 的行，输出是 `Dispatch`（一次派发）与 `Window`（一次命中连同它之前的空转轮）。
这里不做 pipeline 核对，也不管报告怎么排版。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .consts import DETAILS, EVENT_MSG, LINE, MAX_BUCKET, MAX_GAP_S, WEIGHTS

log = logging.getLogger("tools.perf.rounds")

@dataclass
class Dispatch:
    parent: str
    names: list[str]
    t0: datetime
    task_id: str
    ms: int = 0
    ok: bool = False
    closed: bool = False
    fails: int = 0


@dataclass
class Window:
    parent: str
    fails: int
    empty_rounds: int
    ms: float
    raw_ms: float


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def load(path: Path) -> tuple[list[Dispatch], int]:
    """解析一份日志，返回 (派发列表, 跳过的 OnEventNotify 行数)。"""
    dispatches: list[Dispatch] = []
    open_d: dict[tuple[str, str], Dispatch] = {}
    stacks: dict[tuple[str, str], list[tuple[str, bool]]] = defaultdict(list)
    skipped = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if "OnEventNotify" not in raw or "EventDispatcher" not in raw:
                continue
            match = LINE.match(raw.rstrip("\r\n"))
            if not match:
                skipped += 1
                continue
            ts_text, _level, px, tx, _file, _func, body = match.groups()
            msg_match = EVENT_MSG.search(body)
            detail_match = DETAILS.search(body)
            if not msg_match or not detail_match:
                skipped += 1
                continue
            try:
                detail = json.loads(detail_match.group(1))
            except json.JSONDecodeError:
                skipped += 1
                continue

            msg = msg_match.group(1)
            key = (px, tx)
            ts = parse_ts(ts_text)
            task_id = str(detail.get("task_id"))

            if msg == "Node.NextList.Starting":
                dispatch = Dispatch(
                    parent=detail.get("name") or "?",
                    names=[entry.get("name") for entry in detail.get("list") or []],
                    t0=ts,
                    task_id=task_id,
                )
                open_d[key] = dispatch
                dispatches.append(dispatch)
            elif msg in ("Node.NextList.Succeeded", "Node.NextList.Failed"):
                dispatch = open_d.pop(key, None)
                if dispatch and dispatch.task_id == task_id:
                    dispatch.ms = max(0, int((ts - dispatch.t0).total_seconds() * 1000))
                    dispatch.ok = msg.endswith("Succeeded")
                    dispatch.closed = True
            elif msg == "Node.Recognition.Starting":
                name = detail.get("name") or "?"
                stacks[key].append((name, not stacks[key]))
            elif msg in ("Node.Recognition.Succeeded", "Node.Recognition.Failed"):
                name = detail.get("name") or "?"
                stack = stacks[key]
                position = next(
                    (i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name),
                    None,
                )
                if position is None:
                    continue
                top_level = stack[position][1]
                del stack[position:]
                if top_level and msg.endswith("Failed"):
                    dispatch = open_d.get(key)
                    if dispatch and dispatch.task_id == task_id:
                        dispatch.fails += 1
    return dispatches, skipped


def chain_windows(dispatches: list[Dispatch], max_gap_s: float = MAX_GAP_S) -> list[Window]:
    """把「连续未命中轮 + 最后一次命中」并成一个窗口。"""
    ordered = sorted(dispatches, key=lambda d: d.t0)
    windows: list[Window] = []
    for index, dispatch in enumerate(ordered):
        if not dispatch.ok or not dispatch.closed:
            continue
        empty_rounds = 0
        first = dispatch
        cursor = index - 1
        while cursor >= 0:
            previous = ordered[cursor]
            if (
                previous.ok
                or not previous.closed
                or previous.task_id != dispatch.task_id
                or previous.parent != dispatch.parent
            ):
                break
            gap = (first.t0 - (previous.t0 + timedelta(milliseconds=previous.ms))).total_seconds()
            if gap < -1.0 or gap > max_gap_s:
                break
            empty_rounds += 1
            first = previous
            cursor -= 1
        end = dispatch.t0 + timedelta(milliseconds=dispatch.ms)
        total = (end - first.t0).total_seconds() * 1000
        windows.append(
            Window(
                parent=dispatch.parent,
                fails=dispatch.fails,
                empty_rounds=empty_rounds,
                ms=total,
                raw_ms=total,
            )
        )
    return windows


def bucket_of(fails: int) -> int:
    return min(fails, MAX_BUCKET)


def bucket_label(bucket: int) -> str:
    return f"{bucket}+" if bucket >= MAX_BUCKET else str(bucket)


def weighted(t0: float, t1: float, tx: float) -> float:
    return t0 * WEIGHTS[0] + t1 * WEIGHTS[1] + tx * WEIGHTS[2]


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]
