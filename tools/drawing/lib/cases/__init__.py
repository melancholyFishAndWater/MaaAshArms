"""`flow_cases` 的实现：证据包编排（找证据、跑批量、记 manifest、保证幂等）。

入口仍是 `tools/drawing/lib/flow_cases.py`（命令行与口径见 `tools/drawing/README.md`）；
这里按职责拆成 pack / output / runner / evidence / overview / version_filter / cli，避免单文件过长。

对外沿用拆分前 `flow_cases` 模块顶层的全部名字（`tools/sentry/` 有从原路径
`from tools.drawing.lib.flow_cases import <名字>` 的调用，一个都不能少）。
"""

from __future__ import annotations

from .pack import (
    REPO_ROOT,
    LOG_NAMES,
    ROTATED_LOG_RE,
    PART_RE,
    RELEASE_VERSION_RE,
    EVIDENCE_ROOT,
    resource_version_from_release,
    _version_from_evidence,
    _WARNED_LOG_DIRS,
    _warn_extra_logs,
    _pick_log,
    Case,
    _LazyMember,
    _read_log_from_zips,
    load_case,
    discover_cases,
    log_digest,
    ISSUE_RE,
    TASK_ENTRY_RE,
    issue_name_of,
    task_name_of,
    log_stamp,
    _fmt_local,
    _telemetry_module,
    _pack_version,
)

from .output import (
    MANIFEST_NAME,
    _safe_part,
    base_filename,
    out_filename,
    stale_name,
    load_manifest,
    save_manifest,
    _stale_record,
)

from .runner import (
    run_case,
)

from .evidence import (
    _EVIDENCE_DIRS,
    _evidence_on_error,
    _usable_evidence_case,
    set_evidence_dirs,
    _candidates_in,
    _zips_in,
    _evidence_candidates,
    _evidence_case,
    _has_evidence,
    _evidence_task_id,
    _evidence_event_note,
    _terminal_dispatcher,
    _fetch_evidence,
)

from .overview import (
    overview_filename,
    run_overview,
)

from .cli import (
    _with_telemetry,
    create_argument_parser,
    main,
)

__all__ = [
    "Case",
    "EVIDENCE_ROOT",
    "ISSUE_RE",
    "LOG_NAMES",
    "MANIFEST_NAME",
    "PART_RE",
    "RELEASE_VERSION_RE",
    "REPO_ROOT",
    "ROTATED_LOG_RE",
    "TASK_ENTRY_RE",
    "_EVIDENCE_DIRS",
    "_LazyMember",
    "_WARNED_LOG_DIRS",
    "_candidates_in",
    "_evidence_candidates",
    "_evidence_case",
    "_evidence_event_note",
    "_evidence_on_error",
    "_evidence_task_id",
    "_fetch_evidence",
    "_fmt_local",
    "_has_evidence",
    "_pick_log",
    "_pack_version",
    "_read_log_from_zips",
    "_safe_part",
    "_stale_record",
    "_telemetry_module",
    "_terminal_dispatcher",
    "_usable_evidence_case",
    "_version_from_evidence",
    "_warn_extra_logs",
    "_with_telemetry",
    "_zips_in",
    "base_filename",
    "create_argument_parser",
    "discover_cases",
    "issue_name_of",
    "load_case",
    "load_manifest",
    "log_digest",
    "log_stamp",
    "main",
    "out_filename",
    "overview_filename",
    "resource_version_from_release",
    "run_case",
    "run_overview",
    "save_manifest",
    "set_evidence_dirs",
    "stale_name",
    "task_name_of",
]
