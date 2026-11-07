"""把某个失败的证据拉到本地：现场图（附件）+ **那次运行**的日志，并打印能直接跑的绘图命令。

    python -m tools.sentry.cli evidence --list                # 只报告有没有证据
    python -m tools.sentry.cli evidence --issue PYTHON-14     # 拉一个 issue

实现细节在本包的 `evidence_lib/`；命令行入口是 `tools/sentry/cli.py`（`evidence` 子命令）。

产出（`temp/`，不入库）刻意照真机 `debug/` 那棵树，`flow --case` 与 `flow_from_log` 两条路都认：

    temp/evidence/<SHORT-ID>/
      debug/maafw.log                          ← `sentry trace logs` 拼出来的那次运行日志
      debug/on_error/<run_id>/<时间戳>_<节点名>.png  ← **按失败事件分目录**：一个 issue 常有多次失败，
                                                  每次一份 `mfa-task-failure-<run_id>-<task_id>-screenshots.zip`
      evidence.json                            ← 账目：两道闸门的状态、失败字段、重复行数、写了什么、每个事件的图

## 现场图按失败事件逐个取（不是只取一个事件）

`sentry issue view` 只回**一个**事件，只查它就会永远只有那一份现场图（实测同一个 issue 有 3 份，
其中同一任务 `200000003` 在 10-31 与 11-02 两次运行里都失败）。这里枚举最近 `--max-events` 个事件，
逐个查附件列表，有图的下到 `debug/on_error/<run_id>/`；已下过的 run 跳过（增量，不重下、不覆盖）。
日志仍只取 `issue view` 那个事件（`issue events` 精简视图拿不到 trace id）。只画某一次失败就
`--on-error temp/evidence/<issue>/debug/on_error/<run_id>`。

## 两道 Sentry 侧的闸门（先看再下，不然白等）

1. `attachment.status`：`attached` 才有现场图。实测 2026-10-13~15 的 12 个 issue 里只有 2 个是
   `attached`，其余 `no_evidence`（附件根本没有）。这个字段在 `sentry issue view <ID> --json` 的
   `event.context` 里，键名是**扁平的** `attachment.status`，不是嵌套对象。
2. `logs.status`：`captured` 就有这次运行的日志。**没有附件不等于没得画**——实测两次 `BaseTaskStopError`
   的 issue 都是 `no_evidence`，但日志拿得到（同一条 trace 11787 行、34 条 `PipelineTask node done`），
   照样能画"节点循环"，只是没有画面格。

## 三个把路子堵住的 CLI 事实（都实测过）

- `sentry api` **打不出二进制**（下载端点加 `--silent` 也是空 stdout）→ 必须自己带 Bearer
  `GET https://sentry.io/api/0/projects/<org>/<project>/events/<event_id>/attachments/<id>/?download=1`。
  token 用 `sentry auth token --json` 拿（**裸 JSON 字符串**），不必去读 `cli.db` 的 `auth` 表。
- `sentry trace logs` 的 `--limit` **上限 1000 且没有 `--cursor`**：长运行一次拿不全。失败发生在运行
  末尾，所以默认 `--sort newest`（只认 `newest` / `oldest`；`-timestamp` 在 Windows 上会被当成参数别名报错）。
  拿到的条数、是否还有更多（`hasMore`）都写进清单。
- `sentry trace logs` 返回的批次之间有**逐字节相同的行**（实测 5951 行里 1366 行重复，其中 737 行是
  maafw 原生行：`node done` 24 条里有 6 条是重复的）。来源是 MFA 续写日志时带的
  `LogPreludeBytes = 64 KB` 前缀重叠（见 `.agents/evidence/project.md`）。不去重的话图上
  `--miss-min` 聚合框的次数会被灌水——正是"按行计数翻倍"那个老坑。所以默认去重并记账，`--keep-duplicates` 可关。

**为什么还要写一份 `maafw.log`**：附件 zip 里只有 `debug/on_error/*.png`，而 `flow_cases.load_case()`
对目录要求根下有 `maafw.log`/`maa.log`——直接把解出来的目录喂 `--case` 会报"找不到 maafw.log"。
工具把 `trace logs` 写进 `debug/maafw.log`，这条目录才成为完整的证据包。

**清单说的是目录里有什么，不是本次下了什么**（`written` / `events` / `hints` 都按这个口径）：工具不删旧文件，
所以 `--force` 配 `--no-log` / `--no-images` 只重下一半时，`collect()` 会先扫目录
（`existing_artifacts()`）再把磁盘上还在的文件补进清单（`reconcile_written()`）——否则清单写着
"没有日志"，而 `debug/maafw.log` 就在旁边放着，照清单走的人会以为画不了图。

**复用旧包时回显它属于哪个事件**：以前只打一句"已存在"，照旧包出图却不知道画的是哪一次；
现在打 `event.id` / `received` / `release`。事件对不上时不复用日志，会重新取。

**单份附件失败不废整包**：SSL 中断之类只跳过那一份、记进 `written.attachment_failed` 并提示，
日志和其它事件的现场图照常处理（以前一个附件失败会把已经能拿到的日志一起带没）。

**不做**：不翻页取全历史事件（`issue events` 没有游标，只取最近 `--max-events` 个）、不把
newest+oldest 拼起来（中间有洞比截断更误导）、不下载 vision 帧（附件里没有）、不自动画图
（只打印命令）、不打印也不落盘 token。

实现分在 `tools/sentry/evidence_lib/`（constants / model / download / traces / layout / collect / cli 七个模块）；本文件只做仓库根引导与 re-export，命令行与产出约定见 `tools/sentry/README.md`。"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:  # 作为 `tools.sentry.lib.evidence` 导入时走相对路径（pyright 也能解析）
    from .evidence_lib import (
        ATTACHMENT_NAME_RE,
        DEFAULT_LIMIT,
        DEFAULT_LOG_LIMIT,
        DEFAULT_MAX_EVENTS,
        DEFAULT_OUTDIR,
        DEFAULT_PERIOD,
        EVIDENCE_KIND,
        EVIDENCE_SCHEMA,
        Evidence,
        HTTP_TIMEOUT_SECONDS,
        Issue,
        LOG_SORTS,
        MARKER_RES,
        MAX_LOG_LIMIT,
        SENTRY_HOST,
        build_hints,
        collect,
        collect_image_attachments,
        create_argument_parser,
        dedupe_lines,
        download_attachment,
        event_index,
        event_issue,
        existing_artifacts,
        existing_run_ids,
        fetch_log_lines,
        format_result,
        format_survey,
        issue_context,
        list_attachments,
        list_events,
        main,
        marker_lines,
        parse_run_id,
        parse_task_id,
        reconcile_written,
        release_version,
        safe_members,
        sentry_token,
        survey,
        survey_issue,
        wants_images,
        wants_log,
        write_event_images,
        write_members,
    )
except ImportError:  # 直跑脚本 / 被当作顶层模块导入
    from tools.sentry.lib.evidence_lib import (
        ATTACHMENT_NAME_RE,
        DEFAULT_LIMIT,
        DEFAULT_LOG_LIMIT,
        DEFAULT_MAX_EVENTS,
        DEFAULT_OUTDIR,
        DEFAULT_PERIOD,
        EVIDENCE_KIND,
        EVIDENCE_SCHEMA,
        Evidence,
        HTTP_TIMEOUT_SECONDS,
        Issue,
        LOG_SORTS,
        MARKER_RES,
        MAX_LOG_LIMIT,
        SENTRY_HOST,
        build_hints,
        collect,
        collect_image_attachments,
        create_argument_parser,
        dedupe_lines,
        download_attachment,
        event_index,
        event_issue,
        existing_artifacts,
        existing_run_ids,
        fetch_log_lines,
        format_result,
        format_survey,
        issue_context,
        list_attachments,
        list_events,
        main,
        marker_lines,
        parse_run_id,
        parse_task_id,
        reconcile_written,
        release_version,
        safe_members,
        sentry_token,
        survey,
        survey_issue,
        wants_images,
        wants_log,
        write_event_images,
        write_members,
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
