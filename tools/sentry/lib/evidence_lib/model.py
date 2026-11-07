"""数据结构与事件判据：`Issue` / `Evidence`，以及从事件 context 里取字段的规则。

只做"解释 Sentry 返回的 JSON"，不发起网络请求、不碰磁盘；下载在 download.py，落盘在 layout.py。
"""

from __future__ import annotations


from typing import Any
from pathlib import Path
from dataclasses import dataclass, field

def issue_context(event: dict[str, Any]) -> dict[str, Any]:
    """`event.context` 里那几个扁平键取出来，缺的补 None。

    键名的点号是**字符串的一部分**（`attachment.status`），不是层级——写成 `ctx["attachment"]["status"]`
    会永远取到 None。
    """
    ctx = event.get("context") or {}
    return {
        "attachment_status": ctx.get("attachment.status"),
        "attachment_image_count": ctx.get("attachment.image_count"),
        "attachment_bundle_bytes": ctx.get("attachment.bundle_bytes"),
        "attachment_selected_raw_bytes": ctx.get("attachment.selected_raw_bytes"),
        "logs_status": ctx.get("logs.status"),
        "logs_count": ctx.get("logs.count"),
        "logs_selected_raw_bytes": ctx.get("logs.selected_raw_bytes"),
        "logs_truncated": ctx.get("logs.truncated"),
        "failure_task_id": ctx.get("failure.task_id"),
        "failure_node_id": ctx.get("failure.node_id"),
        "failure_message": ctx.get("failure.message"),
        "failure_duration_ms": ctx.get("failure.duration_ms"),
        "instance_task_count": ctx.get("instance.task_count"),
    }


def release_version(event: dict[str, Any]) -> str:
    """`event.release` 归一成版本字符串。

    Sentry 的 `event.release` 是**对象**（`{"id":…, "version":"MFA@v…+MaaAshArms@v1.0.0-beta.55", …}`），
    不是字符串。直接 `str()` 会把 Python repr 写进 `evidence.json`（`"{'id': …}"`），
    下游 `version_label()` 虽然碰巧能从中抠出 `vX.Y.Z`，但那是脏数据。这里统一取 `version`。
    """
    rel = event.get("release")
    if isinstance(rel, dict):
        return str(rel.get("version") or "")
    return str(rel or "")


def wants_images(evidence: dict[str, Any]) -> bool:
    return evidence.get("attachment_status") == "attached"


def wants_log(evidence: dict[str, Any]) -> bool:
    return evidence.get("logs_status") == "captured"


@dataclass
class Issue:
    """一次 survey 的结论：这个 issue 值不值得下、下什么。"""

    short_id: str
    title: str
    count: Any
    last_seen: str
    event_id: str
    trace_id: str
    # Sentry 事件顶层字段 `release`（如 `MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38`），
    # 画图时它是「疑似过时」最可靠的版本来源——原生 maafw.log 没有「资源版本」那行。
    release: str = ""
    # 事件时间（`dateCreated`/`timestamp`）：复用旧包时要能说出"这是哪一次失败"
    received: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    # True = 按 `--event` 指定的**单个**事件建出来的：现场图只取该事件的附件，
    # 不再枚举该 issue 的其它运行（否则历史事件包里会混进最近几次的图）。
    event_only: bool = False

    @property
    def images(self) -> bool:
        return wants_images(self.evidence)

    @property
    def logs(self) -> bool:
        return wants_log(self.evidence)

    @property
    def has_anything(self) -> bool:
        return self.images or self.logs


@dataclass
class Evidence:
    """一个 issue 的产出。"""

    directory: Path
    inventory: dict[str, Any]
    hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
