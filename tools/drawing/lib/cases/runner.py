"""`run_case`：把 `Case` 渲染成 PNG（含「疑似过时」改名与 manifest 记账）。

幂等判定靠指纹（日志摘要 + 选项 + 证据来源 + 渲染器版本）；不负责发现证据包。
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime
from typing import Any

from tools.drawing.lib.flow_from_log import (
    RENDERER_VERSION,
    VERSION_SOURCE_TELEMETRY,
    FlowOptions,
    FlowResult,
    render_flow,
    stale_summary,
)

from .evidence import _evidence_event_note
from .output import _stale_record, out_filename, stale_name
from .pack import Case, _version_from_evidence, log_digest

log = logging.getLogger("tools.flow_cases")

def run_case(
    case: Case,
    options: FlowOptions,
    *,
    outdir: str,
    mermaid: bool,
    force: bool,
    manifest: dict,
    vision_override: Any = None,
    on_error_override: Any = None,
    issue_only: bool = False,
    stem_extra: str = "",
) -> FlowResult | None:
    """渲染一个 case；命中 manifest 且产物还在就跳过（幂等）。

    「疑似过时」只有在渲染完才知道（检查要拿到日志里实际跑过的节点名），所以文件名里的
    `_疑似过时` 是**画完之后**补的：先按普通名字渲染，命中就 `os.replace` 改名，再把
    manifest 记到新名字上。幂等检查因此要同时认普通名与带后缀的名——上一轮画出来的
    是哪一个，这一轮还不知道。
    """
    # 资源版本基线：命令行 --version 优先；否则从遥测证据包 evidence.json 的 release 取
    # （原生 maafw.log 没有「资源版本」那行，同装 MFA 界日志也不会被下载进来）。
    version, version_source = options.version, options.version_source
    if version is None:
        found = _version_from_evidence(case)
        if found:
            version, version_source = found, VERSION_SOURCE_TELEMETRY
    case_options = (
        replace(options, version=version, version_source=version_source)
        if (version != options.version or version_source != options.version_source)
        else options
    )
    # 遥测证据包：把用的是哪次运行（event.id / 运行时间）画到图上（ISSUES #1）
    ev_note = _evidence_event_note(case)
    if ev_note is not None:
        case_options = replace(
            case_options,
            extra_notes=list(case_options.extra_notes) + [ev_note],
        )

    name = out_filename(
        case,
        case_options,
        outdir,
        manifest,
        issue_only=issue_only,
        stem_extra=stem_extra,
    )
    vision = vision_override if vision_override is not None else case.vision()
    on_error = on_error_override if on_error_override is not None else case.on_error()
    fingerprint = {
        "log": log_digest(case),
        "options": case_options.summary(),
        "source": case.describe(),
        "has_vision": bool(vision),
        "has_on_error": bool(on_error),
        # 渲染器版本：解析口径/版面改了必须换指纹，否则同一个证据包会一直命中旧记录"跳过"
        "renderer": RENDERER_VERSION,
    }
    if not force:
        for candidate in (name, stale_name(name)):
            recorded = manifest["cases"].get(candidate)
            path = os.path.join(outdir, candidate)
            if (
                recorded
                and recorded.get("fingerprint") == fingerprint
                and os.path.isfile(path)
            ):
                log.info("跳过（已画过）：%s", path)
                return None

    target = os.path.join(outdir, name)
    result = render_flow(
        case.log_source(),
        vision=vision,
        on_error=on_error,
        out=target,
        options=case_options,
        mermaid=mermaid,
    )
    assert isinstance(result, FlowResult)

    # 疑似过时命中：把 _疑似过时 追加到文件名最末尾（扩展名之前），.mmd 跟着改
    stale_hit = result.stale is not None and bool(result.stale.nodes)
    if stale_hit:
        renamed = stale_name(name)
        renamed_path = os.path.join(outdir, renamed)
        os.replace(target, renamed_path)
        if result.mermaid and os.path.isfile(result.mermaid):
            new_mermaid = os.path.splitext(renamed_path)[0] + ".mmd"
            os.replace(result.mermaid, new_mermaid)
            result.mermaid = new_mermaid
        result.path = renamed_path
        name = renamed

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
        "stale": _stale_record(result.stale),
    }
    merged = (
        f"（{result.overlay_groups} 段重复已合并 → {result.columns} 列）"
        if result.overlay_groups
        else ""
    )
    print(
        f"步骤 {result.steps}（折叠后 {result.boxes} 个框）/ 轮次 {result.rounds}{merged}"
        f" → {result.path}  ({result.width}×{result.height})"
    )
    # 疑似过时命中：画完图后显式打一行（notes 里也有，但那是 stderr 的注记）
    summary = stale_summary(result.stale)
    if summary:
        log.info(summary)
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    if result.truncated_start:
        log.info(
            "提示：日志第一行晚于 --from，这份日志只含窗口后半段（遥测附件只带 tail）"
        )
    for note in result.notes:
        log.info("提示：%s", note.text)
    if case.bad_packages:
        log.warning("以下卷读不了，结果可能不完整：%s", case.bad_packages)
    return result

