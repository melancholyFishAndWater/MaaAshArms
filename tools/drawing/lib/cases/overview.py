"""概览卡：没有可画日志时的降级产物（问题号/标题/失败节点/次数/现场图/链接）。

只有 `run_overview` 与文件名分配；遥测卡字段来自 `tools.sentry.telemetry_index`。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from tools.drawing.lib.flow_from_log import (
    FlowResult,
    Note,
    render_overview,
)

from .evidence import _evidence_on_error
from .output import BULK_STEM_SUFFIXES, artifact_issue, find_existing, rename_artifact
from .pack import _fmt_local, _telemetry_module
from .release_mark import mark_suffix
from .signature import code_stages

log = logging.getLogger("tools.flow_cases")


def overview_filename(
    short_id: str, outdir: str, manifest: dict, fingerprint: dict
) -> str:
    """概览卡文件名：`<SHORT>[_已修复待发版].png`；被**别的**产物占用时追加指纹短哈希。

    占用判据只看 `fingerprint["short_id"]`：概览卡的身份就是问题号，`last_seen` /
    `release` / 渲染器版本变了都不该换名字——否则库里每来一次新事件就多一个
    `<SHORT>_<哈希>.png`，与增量清理互相打架（这一轮写哈希名、下一轮又删掉写回原名）。
    同一个名字上是别的 issue、或是不带 `short_id` 的记录（单包流程图）时才算被占用。

    「已修复待发版」的后缀由 `cases/release_mark.py` 决定（提交里已 `Fixes`、还没发版）。
    """
    import hashlib
    import json

    base = f"{short_id}{mark_suffix(short_id)}.png"

    def taken(name: str) -> bool:
        """这个名字被**别人**的产物占着吗？

        有记录时看它属于哪个 issue；没有记录、又是本工具给这个 issue 用的名字
        （`<SHORT>.png` / `_已修复待发版.png` 这类形状）就当成自己的旧图，直接覆盖——
        否则记录一旦丢了（改名、旧版本留下的），旧卡会永远占着名字，新卡只能写哈希名
        （实测 PYTHON-V：旧卡 25 次/.65 一直躺在 temp/flow，新卡叫 PYTHON-V_2c6cb52faaaf.png）。
        别的无主文件才算占用。
        """
        record = manifest["cases"].get(name)
        if record is not None:
            return record.get("fingerprint", {}).get("short_id") != short_id
        issue, rest = artifact_issue(name)
        return not (issue and rest in BULK_STEM_SUFFIXES)

    if not taken(base):
        return base
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{short_id}_{digest}.png"


def run_overview(
    card: Any,
    *,
    outdir: str,
    mermaid: bool,
    force: bool,
    manifest: dict,
    no_log_hint: bool = True,
    origin: str = "case",
    kept: set[str] | None = None,
) -> FlowResult | None:
    """画一条遥测 issue 的概览卡（没有可画的完整日志时的降级产物）。

    `no_log_hint=True` 时在卡上补一句"怎么补完整输出"；证据包在但解析不出流程时传 False。
    卡片内容与绘制实现都没变时按轨迹跳过（`render_overview` 里判），产物名登记进 `kept`。
    """

    rel = card.release or ""
    fields = [
        ("问题号", card.short_id),
        ("标题", card.title or "—"),
        ("失败节点", f"{card.node_name}/{card.stage}" if card.node_name else "—"),
        ("任务入口", card.label or "—"),
        ("次数", str(card.count if card.count is not None else "—")),
        ("状态", card.status or "—"),
        ("最后出现", _fmt_local(card.last_seen)),
        ("首次收到", _fmt_local(card.received)),
        ("资源版本", rel or "—"),
        ("链接", card.permalink or "—"),
    ]
    images = _evidence_on_error(card.short_id)
    notes = [Note(text, kind) for text, kind in _telemetry_module().card_notes(card, [])]
    if no_log_hint:
        notes.append(
            Note(
                f"无本地日志：批量默认会自动尝试拉取；也可手动跑 "
                f"`python -m tools.sentry.cli evidence {card.short_id}`（需联网）",
                "fail",
            )
        )
    fingerprint = {
        "short_id": card.short_id,
        "last_seen": card.last_seen,
        "release": rel,
        "images": len(images),
    }
    name = overview_filename(card.short_id, outdir, manifest, fingerprint)
    target = os.path.join(outdir, name)
    old = None if force else find_existing(outdir, manifest, name, fingerprint)
    if old is not None and old[0] != name:
        # 只有「已修复待发版」标记（或日志时间标签）那段变了：先把上一轮的卡改名、
        # 不重画，之后 `render_overview` 的"文件还在就跳过"才认得出来。
        rename_artifact(outdir, manifest, old[0], name)
        old = (name, manifest["cases"].get(name, old[1]))
    try:
        result = render_overview(
            target,
            title=f"遥测问题 {card.short_id}",
            subtitle=f"{card.count or 0} 次 · {card.status or '?'}",
            fields=fields,
            images=images,
            notes=notes,
            mermaid=mermaid,
            trace_extra={"code": code_stages()},
            recorded_stages=(
                (old[1].get("trace_stages") if old else None) or None
            ),
        )
    except ModuleNotFoundError as exc:
        # 缺 Pillow 这类环境问题：报出来但不中断整批（`failed_issues` 会保住这条 issue 的旧图）
        log.error("错误：%s: %s", card.short_id, exc)
        return None
    if result.skipped:
        log.info("跳过（%s）：%s", result.skip_reason, target)
        if kept is not None:
            kept.add(name)
        return None
    manifest["cases"][name] = {
        "fingerprint": fingerprint,
        # 清理旧产物靠这两个字段判断"这条记录属于哪个 issue、是批量还是单包画的"
        "issue": card.short_id,
        "origin": origin,
        # 增量判定：分步摘要与合成 sha（`flowlog/trace.py`）
        "trace": result.trace,
        "trace_stages": result.trace_stages,
        "path": result.path,
        "mermaid": result.mermaid,
        "width": result.width,
        "height": result.height,
        "steps": result.steps,
        "boxes": result.boxes,
        "rounds": result.rounds,
        "columns": result.columns,
        "overlay_groups": result.overlay_groups,
        "truncated_start": result.truncated_start,
        "derived_from": result.derived_from,
        "out_of_order": result.out_of_order,
        "notes": [n.text for n in result.notes],
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "stale": None,
    }
    if kept is not None:
        kept.add(name)
    print(f"概览 {card.short_id} → {result.path}  ({result.width}×{result.height})")
    return result
