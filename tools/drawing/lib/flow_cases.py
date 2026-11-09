#!/usr/bin/env python
"""把一个"证据包"渲染成流程图，产出统一落到 temp/flow/。

证据包指的是"排一次失败所要看的那些东西"：

  * 本机安装根：`<安装根>/debug/`（含 `maafw.log`、`on_error/`、`vision/`）；
  * MFA 手动导出包：`log_20261014_003000.zip`。超过 23.75 MB 会分成
    `…-part01.zip` / `…-part02.zip` 多卷，本工具把它们当**同一棵逻辑目录树**处理，
    所以多卷要一起传给 --case；
  * 遥测工件解出来的目录（Sentry 失败附件只含 `maafw.log` 的 tail 与 `on_error/`
    图片，**没有 `vision/`** —— 这是物理限制，见 tools/sentry/README.md）。

与 `tools/drawing/lib/flow_from_log.py` 的分工：那个文件是**渲染器**（解析 + 布局，唯一的实现处），
本文件只做**编排**（找证据、跑批量、记 manifest、保证幂等），不复制任何渲染逻辑。

用法示例：
  # 本机跑某个 task
  python tools/drawing/cli.py cases --case D:/MaaAshArms/debug --task-id 200000118

  # 别人给的多卷导出包
  python tools/drawing/cli.py cases --case log_20261014-part01.zip log_20261014-part02.zip \\
      --task-id 200000118 --from "2026-10-14 00:30:00" --to "2026-10-14 00:32:00"

  # 只列一遍有哪些证据包，不画
  python tools/drawing/cli.py cases --case D:/MaaAshArms/debug --list-cases

实现分在 `tools/drawing/cases/` 八个模块：
  cases/pack.py      日志挑选、`Case`、`load_case`/`discover_cases`、问题号与文件名要素
  cases/output.py    产物命名、manifest 读写、批量清理（`prune_artifacts`）
  cases/runner.py    `run_case`（渲染 + 「疑似过时」改名 + 记账）
  cases/evidence.py  `--evidence-dir` 各根下的证据包、失败 task 定位、证据事件注记
  cases/overview.py  概览卡（`run_overview`）与遥测卡接线
  cases/signature.py 代码摘要（读取实现 / 绘制实现）与仓库状态，增量判定的输入
  cases/version_filter.py  `--telemetry-version` 的条件解析、版本比较与卡片分堆
  cases/cli.py       argparse、`main`、批量出图循环
本文件只做 `sys.path` 引导、re-export 与 `main()`。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 兄弟模块（flow_from_log）与本包都在同一目录：先把它加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent))
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.drawing.lib.cases import (
    Case,
    EVIDENCE_ROOT,
    ISSUE_RE,
    LOG_NAMES,
    MANIFEST_NAME,
    PART_RE,
    RELEASE_VERSION_RE,
    REPO_ROOT,
    ROTATED_LOG_RE,
    TASK_ENTRY_RE,
    _EVIDENCE_DIRS,
    _LazyMember,
    _WARNED_LOG_DIRS,
    _candidates_in,
    _evidence_candidates,
    _evidence_case,
    _evidence_event_note,
    _evidence_on_error,
    _evidence_task_id,
    _fetch_evidence,
    _fmt_local,
    _has_evidence,
    _pack_version,
    _pick_log,
    _read_log_from_zips,
    _safe_part,
    _stale_record,
    _telemetry_module,
    _terminal_dispatcher,
    _usable_evidence_case,
    _version_from_evidence,
    _warn_extra_logs,
    _with_telemetry,
    _zips_in,
    BULK_STEM_SUFFIXES,
    artifact_issue,
    base_filename,
    code_stages,
    create_argument_parser,
    discover_cases,
    issue_name_of,
    load_case,
    load_manifest,
    clear_artifacts,
    log_digest,
    module_digest,
    log_stamp,
    main,
    out_filename,
    overview_filename,
    parse_signature,
    prune_artifacts,
    raster_signature,
    repo_state,
    resource_version_from_release,
    run_case,
    run_overview,
    save_manifest,
    set_evidence_dirs,
    stale_name,
    task_name_of,
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
    "_pack_version",
    "_pick_log",
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
    "BULK_STEM_SUFFIXES",
    "artifact_issue",
    "code_stages",
    "module_digest",
    "parse_signature",
    "prune_artifacts",
    "raster_signature",
    "repo_state",
    "create_argument_parser",
    "discover_cases",
    "issue_name_of",
    "load_case",
    "load_manifest",
    "clear_artifacts",
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

if __name__ == "__main__":
    raise SystemExit(main())
