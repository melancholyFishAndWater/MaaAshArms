"""`evidence.py` 的实现：把 Sentry 失败事件的证据（现场图 + trace 日志）拉到 `temp/evidence/`。

入口是 `tools/sentry/cli.py` 的 `evidence` 子命令（实现在上层 `lib/evidence.py`）；
这里按职责拆成 constants / model / download / traces / layout / collect / cli，避免单文件过长。
"""

from __future__ import annotations

from .constants import (
    ATTACHMENT_NAME_RE,
    DEFAULT_LIMIT,
    DEFAULT_LOG_LIMIT,
    DEFAULT_MAX_EVENTS,
    DEFAULT_OUTDIR,
    DEFAULT_PERIOD,
    EVIDENCE_KIND,
    EVIDENCE_SCHEMA,
    HTTP_TIMEOUT_SECONDS,
    LOG_SORTS,
    MARKER_RES,
    MAX_LOG_LIMIT,
    SENTRY_HOST,
)

from .model import (
    Evidence,
    Issue,
    issue_context,
    release_version,
    wants_images,
    wants_log,
)

from .download import (
    collect_image_attachments,
    download_attachment,
    event_issue,
    list_attachments,
    list_events,
    sentry_token,
    survey,
    survey_issue,
)

from .traces import (
    dedupe_lines,
    fetch_log_lines,
    marker_lines,
    parse_run_id,
    parse_task_id,
    safe_members,
)

from .layout import (
    event_index,
    existing_artifacts,
    existing_run_ids,
    reconcile_written,
    write_event_images,
    write_members,
)

from .collect import (
    build_hints,
    collect,
    format_result,
    format_survey,
)

from .cli import (
    create_argument_parser,
    main,
)

__all__ = [
    "ATTACHMENT_NAME_RE",
    "DEFAULT_LIMIT",
    "DEFAULT_LOG_LIMIT",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_OUTDIR",
    "DEFAULT_PERIOD",
    "EVIDENCE_KIND",
    "EVIDENCE_SCHEMA",
    "Evidence",
    "HTTP_TIMEOUT_SECONDS",
    "Issue",
    "LOG_SORTS",
    "MARKER_RES",
    "MAX_LOG_LIMIT",
    "SENTRY_HOST",
    "build_hints",
    "collect",
    "collect_image_attachments",
    "create_argument_parser",
    "dedupe_lines",
    "download_attachment",
    "event_index",
    "event_issue",
    "existing_artifacts",
    "existing_run_ids",
    "fetch_log_lines",
    "format_result",
    "format_survey",
    "issue_context",
    "list_attachments",
    "list_events",
    "main",
    "marker_lines",
    "parse_run_id",
    "parse_task_id",
    "reconcile_written",
    "release_version",
    "safe_members",
    "sentry_token",
    "survey",
    "survey_issue",
    "wants_images",
    "wants_log",
    "write_event_images",
    "write_members",
]
