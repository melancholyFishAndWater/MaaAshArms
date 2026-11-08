"""flowlog：把 maafw.log 还原成「实际执行流程图」的内部实现。

入口留给 `tools/drawing/lib/flow_from_log.py`（只做 sys.path 引导与 re-export），本包按
`logparse`（解析）→ `layout`（版面）→ `paint`/`draw`（像素）→ `render`（串联与落盘）
分层，`media` 管图片证据，`stale` 管「疑似过时」，`notes` 管诊断注记，`cli` 管参数。
行为与拆分前的单文件版本一致；对外只保证 `flow_from_log` 上那些名字可用。"""

from __future__ import annotations

from .cli import (
    main,
    parse_time,
)
from .constants import (
    LINE_RE,
    NODE_DONE_RE,
    RENDERER_VERSION,
    TRUNCATION_HEADS,
)
from .layout import (
    ColumnSpan,
    column_signature,
    overlay_repeats,
    parent_groups,
    round_steps,
)
from .logparse import (
    _open_log_lines,
    parse_log,
    scan_tasks,
)
from .media import (
    MediaEntry,
    MediaResolver,
    _is_zip,
    backfill_failure_thumbs,
    member_clock,
    window_failure_images,
)
from .model import (
    Note,
    Step,
    parse_nextlist,
    parse_ts,
    resolve_parent,
)
from .notes import (
    build_notes,
    describe_empty_window,
    stale_summary,
    stall_notes,
)
from .paths import (
    DEFAULT_OUTDIR,
)
from .render import (
    FlowOptions,
    FlowResult,
    _first_log_ts,
    render_flow,
    render_overview,
)
from .stale import (
    VERSION_SOURCE_CLI,
    VERSION_SOURCE_TELEMETRY,
    resource_version,
)

__all__ = [
    "DEFAULT_OUTDIR",
    "RENDERER_VERSION",
    "VERSION_SOURCE_CLI",
    "VERSION_SOURCE_TELEMETRY",
    "FlowOptions",
    "FlowResult",
    "MediaResolver",
    "Note",
    "_is_zip",
    "parse_log",
    "parse_time",
    "render_flow",
    "render_overview",
    "scan_tasks",
    "stale_summary",
    "resource_version",
    "_first_log_ts",
    "_open_log_lines",
    "build_notes",
    "describe_empty_window",
    "round_steps",
    "parent_groups",
    "parse_nextlist",
    "resolve_parent",
    "stall_notes",
    "Step",
    "ColumnSpan",
    "MediaEntry",
    "backfill_failure_thumbs",
    "column_signature",
    "member_clock",
    "overlay_repeats",
    "window_failure_images",
    "LINE_RE",
    "NODE_DONE_RE",
    "TRUNCATION_HEADS",
    "parse_ts",
    "main",
]
