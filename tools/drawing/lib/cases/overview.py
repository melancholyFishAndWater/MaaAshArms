"""概览卡：没有可画日志时的降级产物（问题号/标题/失败节点/次数/现场图/链接）。

只有 `run_overview` 与文件名分配；遥测卡字段来自 `tools.sentry.telemetry_index`。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from tools.drawing.lib.flow_from_log import (
    RENDERER_VERSION,
    FlowResult,
    Note,
    render_overview,
)

from .evidence import _evidence_on_error
from .pack import _fmt_local, _telemetry_module

log = logging.getLogger("tools.flow_cases")

def overview_filename(
    short_id: str, outdir: str, manifest: dict, fingerprint: dict
) -> str:
    """概览卡文件名：`<SHORT>.png`；被别的指纹占用时追加指纹短哈希。"""

    import hashlib
    import json

    base = f"{short_id}.png"

    def taken(name: str) -> bool:
        path = os.path.join(outdir, name)
        if os.path.isfile(path):
            return name not in manifest["cases"] or manifest["cases"][name].get(
                "fingerprint"
            ) != fingerprint
        return (
            name in manifest["cases"]
            and manifest["cases"][name].get("fingerprint") != fingerprint
        )

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
) -> FlowResult | None:
    """画一条遥测 issue 的概览卡（没有可画的完整日志时的降级产物）。

    `no_log_hint=True` 时在卡上补一句"怎么补完整输出"；证据包在但解析不出流程时传 False。
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
        "renderer": RENDERER_VERSION,
    }
    name = overview_filename(card.short_id, outdir, manifest, fingerprint)
    target = os.path.join(outdir, name)
    if not force:
        rec = manifest["cases"].get(name)
        if (
            rec
            and rec.get("fingerprint") == fingerprint
            and os.path.isfile(target)
        ):
            log.info("跳过（已画过）：%s", target)
            return None
    try:
        result = render_overview(
            target,
            title=f"遥测问题 {card.short_id}",
            subtitle=f"{card.count or 0} 次 · {card.status or '?'}",
            fields=fields,
            images=images,
            notes=notes,
            mermaid=mermaid,
        )
    except ModuleNotFoundError as exc:
        log.error("错误：%s: %s", card.short_id, exc)
        return None
    assert isinstance(result, FlowResult)
    manifest["cases"][name] = {
        "fingerprint": fingerprint,
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
    print(f"概览 {card.short_id} → {result.path}  ({result.width}×{result.height})")
    return result

