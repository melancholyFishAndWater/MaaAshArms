"""Custom agent 缺位记录与实际识别/动作记录的关联。"""

from __future__ import annotations

from datetime import datetime

from .constants import CUSTOM_MISS_WINDOW_SECONDS
from .model import CustomMissing, ParsedLog


def _match_custom_missing(
    records: list[CustomMissing], ts: datetime, name: str, kind: str
) -> CustomMissing | None:
    """找「这条记录之前 `CUSTOM_MISS_WINDOW_SECONDS` 内、同名、同类」的那次未注册。

    只比节点名与时间，**不比进程号**：同一份日志里进程在时间上是串行的（实测 6 次启动
    的首末任务区间两两不重叠），不会出现"两个进程同时跑同名节点"。
    """
    best: CustomMissing | None = None
    for record in records:
        if record.kind != kind or record.node != name:
            continue
        delta = (ts - record.ts).total_seconds()
        if 0 <= delta <= CUSTOM_MISS_WINDOW_SECONDS and (
            best is None or record.ts > best.ts
        ):
            best = record
    return best


def _mark_agent_missing(out: ParsedLog) -> None:
    """把「Custom 回调没注册」标到它影响的记录上。

    两条挂法，方向都是"ERR 在前"（框架先判回调为空、再落记录）：

    * 识别缺位 → 挂在它后面那条 `algorithm=Custom`、`box=null` 的未命中记录上
      （实测 `TrainInit` 的 ERR 之后 29 ms 就是 `reco … "box":null`）；
    * 动作缺位 → 挂在它后面那一步 `completed=false` 上（实测 `EntrustInit` 之后 201 ms 是
      `PipelineTask node done … "completed":false`）。
    """
    if not out.custom_missing:
        return
    for attempt in out.attempts:
        if attempt.box or attempt.algorithm != "Custom":
            continue
        hit = _match_custom_missing(
            out.custom_missing, attempt.ts, attempt.name, "reco"
        )
        if hit is not None:
            attempt.agent_missing = hit.reg
    for step in out.steps:
        if step.completed:
            continue
        hit = _match_custom_missing(out.custom_missing, step.ts, step.name, "act")
        if hit is not None:
            step.agent_missing = hit.reg
