"""`run_case`：把 `Case` 渲染成 PNG（含「疑似过时」改名与 manifest 记账）。

**要不要重画不在这里判**：分步摘要（`flowlog/trace.py`）与两级判定都在
`flowlog.render.render_flow()` 里——快路不必读日志，慢路按数据步比较。本模块只负责
找旧记录、把输入事实交过去、渲染成功（或按轨迹跳过）后记账。
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
from .output import (
    STALE_SUFFIX,
    _stale_record,
    find_existing,
    out_filename,
    rename_artifact,
    stale_name,
)
from .pack import Case, _version_from_evidence, issue_name_of, log_digest
from .signature import code_stages, repo_state

log = logging.getLogger("tools.flow_cases")


def _has_stale(name: str) -> bool:
    stem, _ext = os.path.splitext(name)
    return stem.endswith(f"_{STALE_SUFFIX}")


def _target_name(name: str, matched: str) -> str:
    """跳过时要落到哪个名字：沿用旧记录有没有「疑似过时」后缀，只更新标记那一段。"""
    return stale_name(name) if _has_stale(matched) else name


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
    origin: str = "case",
    kept: set[str] | None = None,
    issue_label: str = "",
) -> FlowResult | None:
    """渲染一个 case；按轨迹命中就跳过（幂等）。

    「疑似过时」只有在渲染完才知道（检查要拿到日志里实际跑过的节点名），所以文件名里的
    `_疑似过时` 是**画完之后**补的：先按普通名字渲染，命中就 `os.replace` 改名，再把
    manifest 记到新名字上。按轨迹跳过时认普通名与带后缀的名——上一轮画出来的是哪一个，
    这一轮还不知道。

    `origin` 记进 manifest（`bulk` / `case`），清理旧产物时用来区分"批量画的"与"单包画的"；
    `kept` 是本次运行保留的产物名集合，只有 `--telemetry-bulk` 会传。`issue_label` 是遥测卡
    上的问题名（`Reward` / `Factory`…），只影响产物名（`PYTHON-3B_Reward.png`），不改图。
    """
    # 资源版本基线：命令行 --version 优先；否则从遥测证据包 evidence.json 的 release 取
    # （原生 maafw.log 没有「资源版本」那行，同装 MFA 界面日志也不会被下载进来）。
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
        issue_label=issue_label,
    )
    vision = vision_override if vision_override is not None else case.vision()
    on_error = on_error_override if on_error_override is not None else case.on_error()
    digest = log_digest(case)
    # 命名与"同名是否被别人占着"仍看这份指纹（`output.out_filename` 的既有约定）；
    # 要不要重画改由分步摘要决定，所以这里不再比较指纹。
    fingerprint = {
        "log": digest,
        "options": case_options.summary(),
        "source": case.describe(),
        "has_vision": bool(vision),
        "has_on_error": bool(on_error),
        "renderer": RENDERER_VERSION,
    }
    old = None if force else find_existing(outdir, manifest, name, fingerprint)
    matched = old[0] if old else ""
    trace_extra = {
        "code": code_stages(),
        "input": {
            "log": digest,
            "source": case.describe(),
            "has_vision": bool(vision),
            "has_on_error": bool(on_error),
        },
        # 仓库状态单独一个阶段：它只影响「疑似过时」标记，提交后重新核对一遍，数据没变就不重画
        "repo": repo_state(),
    }
    result = render_flow(
        case.log_source(),
        vision=vision,
        on_error=on_error,
        out=os.path.join(outdir, name),
        options=case_options,
        mermaid=mermaid,
        trace_extra=trace_extra,
        recorded_stages=(old[1].get("trace_stages") if old else None) or None,
        existing=os.path.join(outdir, matched) if matched else None,
    )
    assert isinstance(result, FlowResult)
    if result.skipped:
        # 名字可能因为「已修复待发版」标记变化而与旧记录不同：只改名，不重画。
        # 图的内容没变（绘图逻辑与数据步都没动），所以这里既不重渲染也不动 RENDERER_VERSION。
        final = _target_name(name, matched or name)
        if matched and matched != final:
            rename_artifact(outdir, manifest, matched, final)
        final_path = os.path.join(outdir, final)
        log.info("跳过（%s）：%s", result.skip_reason, final_path)
        if kept is not None:
            kept.add(final)
        return None
    if result.redraw_reason:
        log.info("重画（%s）：%s", result.redraw_reason, name)
    elif old and not (old[1].get("trace_stages") or {}):
        # 升级前画的：记录里没有分步摘要，这一轮只能重画一遍把它补上
        log.info("重画（旧记录没有分步摘要）：%s", name)

    # 疑似过时命中：把 _疑似过时 追加到文件名最末尾（扩展名之前），.mmd 跟着改
    stale_hit = result.stale is not None and bool(result.stale.nodes)
    if stale_hit:
        renamed = stale_name(name)
        renamed_path = os.path.join(outdir, renamed)
        os.replace(result.path, renamed_path)
        if result.mermaid and os.path.isfile(result.mermaid):
            new_mermaid = os.path.splitext(renamed_path)[0] + ".mmd"
            os.replace(result.mermaid, new_mermaid)
            result.mermaid = new_mermaid
        result.path = renamed_path
        name = renamed

    issue = case_options.issue or issue_name_of(case) or ""
    manifest["cases"][name] = {
        "fingerprint": fingerprint,
        # 清理旧产物靠这两个字段判断"这条记录属于哪个 issue、是批量还是单包画的"
        "issue": issue,
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
        "stale": _stale_record(result.stale),
    }
    if kept is not None:
        kept.add(name)
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
