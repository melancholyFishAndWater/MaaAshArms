"""一次抓取的编排与清单：`collect()` 把调查、下载、落盘、对账串起来，再排版输出。

这里决定"先看哪道闸门、失败怎么记、清单写什么"；单个动作的实现分别在 download/traces/layout。
"""

from __future__ import annotations

import json

from typing import Any
from ..config import CONFIG
from .constants import DEFAULT_MAX_EVENTS
from .constants import EVIDENCE_KIND
from .constants import EVIDENCE_SCHEMA
from .model import Evidence
from .model import Issue
from tools.drawing.lib.flow_cases import LOG_NAMES
from pathlib import Path
from collections.abc import Sequence
from typing import TextIO
from .download import collect_image_attachments
from .traces import dedupe_lines
from .download import download_attachment
from .layout import event_index
from .layout import existing_artifacts
from .layout import existing_run_ids
from .traces import fetch_log_lines
from .traces import marker_lines
from .layout import reconcile_written
from .. import report_common
from .download import sentry_token
from ..explore_csv import sort_lines
from .layout import write_event_images

def build_hints(
    outdir: Path,
    issue: Issue,
    *,
    log_rel: str | None,
    has_images: bool,
    runs: Sequence[str] = (),
) -> list[str]:
    """可复制的下一步命令（相对仓库根，命令都在仓库根跑）。

    `log_rel` / `has_images` 说的是**磁盘上有没有**（`existing_artifacts()` 的结论），不是本次下没下：
    清单里写着日志就一定能画，别再让"本次没下"把可用的命令藏掉。文件名也照磁盘上那个来
    （`LOG_NAMES` 里可能是 `maa.log`），别硬写 `maafw.log` 指到不存在的文件。

    `runs` 非空且不止一个时，多给一条"只画某一次失败"的写法：现场图按 run_id 分了目录，
    `--on-error <case>/on_error/<run_id>` 就能只贴那一次的现场图。
    """
    if not log_rel:
        return []
    root = report_common.repo_root()
    try:
        shown = outdir.relative_to(root).as_posix()
    except ValueError:
        shown = outdir.as_posix()
    case_dir = f"{shown}/debug"
    hints: list[str] = []
    task = issue.evidence.get("failure_task_id")
    task_arg = f" --task-id {task}" if task else ""
    hints.append(f"python -m tools.sentry.cli flow --case {case_dir}{task_arg}")
    if has_images:
        hints.append(
            f"python tools/drawing/cli.py flow {shown}/{log_rel} "
            f"--on-error {case_dir}/on_error{task_arg}"
        )
    else:
        hints.append(
            f"python tools/drawing/cli.py flow {shown}/{log_rel}{task_arg} --no-thumbs"
        )
    ordered = sorted(runs)
    if len(ordered) > 1:
        shown_runs = "、".join(ordered[:6]) + ("…" if len(ordered) > 6 else "")
        hints.append(
            f"只贴某一次失败的现场图：--on-error {case_dir}/on_error/<run_id>"
            f"（共 {len(ordered)} 次：{shown_runs}）"
        )
    return hints


def collect(
    sentry: str,
    issue: Issue,
    *,
    outdir: Path,
    token: str | None,
    do_images: bool,
    do_log: bool,
    log_limit: int,
    log_sort: str,
    force: bool,
    keep_duplicates: bool,
    verbose: bool,
    timeout: float,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> Evidence:
    """把一个 issue 的证据落盘；已经下过就补缺、不重下（`--force` 重新下）。

    清单里 `written` 说的是**这个证据目录里有什么**，不是"本次下了什么"：本次没下但磁盘上还在的
    文件（`--force` 配 `--no-log` / `--no-images`）照样记进去，`written` 与 `hints` 才对得上磁盘。

    现场图按**失败事件**逐个取（见 `collect_image_attachments`）：一个 issue 常有多次失败、各一份
    on_error 包，只取"issue view 那一个事件"就永远只有那一份（实测 3 份不同运行的图）。这里枚举
    最近 `max_events` 个事件、逐个下到 `debug/on_error/<run_id>/`，已下过的 run 跳过（增量：
    不重下，也不覆盖别的事件）。
    """
    directory = outdir / issue.short_id
    manifest_path = directory / "evidence.json"
    recorded: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                recorded = loaded
        except (OSError, json.JSONDecodeError):
            recorded = {}
    reused = bool(recorded) and recorded.get("event", {}).get("id") == issue.event_id

    written: dict[str, Any] = {
        "log": None,
        "log_bytes": 0,
        "images": [],
        "zip_bytes": 0,
    }
    trace_meta: dict[str, Any] = {}
    notes: list[str] = []
    # 上一趟留下的文件（`--force` 只重下一半时它们仍在原地）：先记下来，免得清单与磁盘脱节
    existing_log, existing_images = existing_artifacts(directory)
    have_runs = existing_run_ids(existing_images)

    jobs: list[dict] = []
    downloaded_images = 0
    if do_images:
        jobs = collect_image_attachments(
            sentry, issue, verbose=verbose, timeout=timeout, max_events=max_events
        )
        wanted = jobs if force else [j for j in jobs if j["run_id"] not in have_runs]
        failed: list[str] = []
        for job in wanted:
            if token is None:
                token = sentry_token(sentry, verbose=verbose, timeout=timeout)
            # 单份附件失败不能把整包废掉：原来一个 SSL 中断就抛出去了，连这次已经能拿到的
            # 日志和别的失败的现场图一起没了（ISSUES 里记的那条）。这里跳过它、继续下一份。
            try:
                blob = download_attachment(
                    CONFIG.target, str(job["event_id"]), str(job["attachment_id"]), token
                )
            except (RuntimeError, OSError) as exc:
                failed.append(f"{job['run_id']}（{exc}）")
                continue
            if blob[:4] != b"PK\x03\x04":
                notes.append(
                    f"附件 {job['name']} 不是 zip（前 4 字节 {blob[:4]!r}），跳过"
                )
                continue
            written["zip_bytes"] += len(blob)
            written["images"].extend(
                write_event_images(blob, directory, str(job["run_id"]))
            )
            have_runs.add(str(job["run_id"]))
            downloaded_images += 1
        if failed:
            written["attachment_failed"] = failed
            notes.append(
                f"{len(failed)} 份现场图没下成，已跳过（日志和其它事件的现场图照常）："
                + "；".join(failed)
            )
        if not jobs:
            notes.append(
                f"没有现场图（最近 {max_events} 个事件里都没有 "
                "`mfa-task-failure-*.zip` 附件）"
            )
        elif not wanted:
            notes.append(
                f"现场图共 {len(jobs)} 份、都是上一趟下过的（增量只补新的失败事件）"
            )

    got_log = False
    # 日志取当前事件（`issue view` 的最新事件，或 `--event` 指定的那个）的 trace。
    # 已下过、且**事件没变**（reused）才不重下；事件变了必须重下——否则 `evidence.json`
    # 说最新事件、`debug/maafw.log` 还是旧事件的（ISSUES 第 1 条：拿两周前的日志画图）。
    if do_log and issue.logs and issue.trace_id and (
        force or not existing_log or not reused
    ):
        lines, has_more = fetch_log_lines(
            sentry,
            CONFIG.target,
            issue.trace_id,
            limit=log_limit,
            sort=log_sort,
            verbose=verbose,
            timeout=timeout,
        )
        total = len(lines)
        kept, dropped = dedupe_lines(lines) if not keep_duplicates else (list(lines), 0)
        ordered, out_of_order = sort_lines(kept)
        log_path = directory / "debug" / LOG_NAMES[0]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(ordered) + ("\n" if ordered else "")
        log_path.write_text(text, encoding="utf-8", newline="\n")
        written["log"] = f"debug/{LOG_NAMES[0]}"
        written["log_bytes"] = len(text.encode("utf-8"))
        trace_meta = {
            "event_id": issue.event_id,
            "id": issue.trace_id,
            "items_limit": log_limit,
            "sort": log_sort,
            "has_more": has_more,
            "lines": total,
            "duplicates_dropped": dropped,
            "out_of_order": out_of_order,
            "markers": marker_lines(ordered),
        }
        if has_more:
            notes.append(
                f"日志超过 {log_limit} 条上限（Sentry CLI 没有游标）：只含 {log_sort} 的那一段"
            )
        if dropped:
            notes.append(
                f"去掉 {dropped} 行完全重复的行（MFA 续写日志的 64 KB 前缀重叠）"
            )
        if not marker_lines(ordered):
            notes.append(
                "日志里没有看到 Task timeout / invalid node id / node not completed"
            )
        # 日志是"整个运行"的：一次运行常排多个任务，标记可能来自同一运行里的别的任务。
        # 同一条 trace 会同时挂多个 issue（实测 PYTHON-14 与 PYTHON-16 就是这样），
        # 所以上面那几条标记不能默认都算在这个 issue 头上——用 --task-id 定到 failure.task_id 才准。
        tasks_run = issue.evidence.get("instance_task_count")
        try:
            task_count = int(tasks_run) if tasks_run is not None else 0
        except (TypeError, ValueError):
            task_count = 0
        if task_count > 1:
            notes.append(
                f"这次运行共 {task_count} 个任务，日志是整段的：标记可能来自别的任务，"
                f"按 --task-id {issue.evidence.get('failure_task_id')} 才只看本次失败的那个"
            )
        got_log = True
    elif do_log and not existing_log:
        notes.append(
            f"没有日志（logs.status={issue.evidence.get('logs_status') or '未知'}）"
        )
    if not got_log and isinstance(recorded.get("trace"), dict):
        # 复用时保留上一趟的 trace 账（行数/去重/标记），别让清单里那几项变空
        trace_meta = dict(recorded["trace"])

    # 清单记的是"目录里有什么"：本次没下但磁盘上还在的（`--force` 配 `--no-log`/`--no-images`）也补进来
    reconcile_written(directory, written, existing_log, existing_images, notes)

    runs = sorted(existing_run_ids(written["images"]))
    has_log = written["log"] is not None
    hints = build_hints(
        directory,
        issue,
        log_rel=str(written["log"]) if has_log else None,
        has_images=bool(written["images"]),
        runs=runs,
    )
    if not hints:
        # hints 为空 = 没有日志（`build_hints` 第一条就要求 log_rel）；现场图再多也画不了流程图
        if written["images"]:
            notes.append(
                f"没有日志：画不出流程图（现场图有 {len(written['images'])} 张，"
                "但图上的步骤来自日志）"
            )
        else:
            notes.append("既没有日志也没有现场图：画不出流程图（画图必须有日志）")
    if reused and downloaded_images == 0 and not got_log:
        # 复用旧包时必须回显它属于哪个事件：否则照着旧包出图却不知道画的是哪一次（踩过的坑）
        old = recorded.get("event", {}) or {}
        bits = []
        if old.get("id"):
            bits.append(f"event.id={old['id']}")
        if old.get("received"):
            bits.append(f"received={old['received']}")
        if old.get("release"):
            bits.append(f"release={old['release']}")
        detail = f"（{'，'.join(bits)}）" if bits else ""
        notes.append(f"已存在（--force 重新下载）{detail}")

    inventory = {
        "schema": EVIDENCE_SCHEMA,
        "kind": EVIDENCE_KIND,
        "generated_at": report_common.utc_now_iso(),
        "issue": {
            "short_id": issue.short_id,
            "title": issue.title,
            "count": issue.count,
            "last_seen": issue.last_seen,
        },
        "event": {
            "id": issue.event_id,
            "received": issue.received,
            **issue.evidence,
            "release": issue.release,
        },
        # 每个失败事件一份现场图（`debug/on_error/<run_id>/`）：哪份图属哪次运行、哪个 task
        "events": event_index(written["images"], jobs),
        "trace": trace_meta,
        "written": written,
        "hints": hints,
        "notes": notes,
    }
    directory.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(inventory, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return Evidence(directory=directory, inventory=inventory, hints=hints, notes=notes)


def format_survey(issues: Sequence[Issue], output: TextIO) -> None:
    """survey 结果表：一眼看出哪些有图、哪些只有日志。"""
    rows = []
    for issue in issues:
        if not issue.has_anything:
            mark = "无证据"
        elif issue.images:
            mark = "图 + 日志" if issue.logs else "只有图"
        else:
            mark = "只有日志"
        rows.append(
            (
                issue.short_id,
                str(issue.count),
                str(issue.evidence.get("attachment_status") or "-"),
                str(issue.evidence.get("logs_status") or "-"),
                mark,
                issue.title[:38],
            )
        )
    report_common.write_console_table(
        ("issue", "次数", "附件", "日志", "可拉到的", "标题"),
        rows,
        output,
        right_aligned={1},
    )


def format_result(evidence: Evidence, output: TextIO, stderr: TextIO) -> None:
    inventory = evidence.inventory
    written = inventory.get("written", {})
    trace = inventory.get("trace", {})
    print(f"\n{evidence.directory.as_posix()}", file=output)
    print(
        f"  现场图: {len(written.get('images') or [])} 个"
        f"（附件 {written.get('zip_bytes', 0)} 字节）",
        file=output,
    )
    events = inventory.get("events") or []
    if events:
        # 一个失败事件一份现场图：列出「run_id（task）几张」，
        # 现场图目录是 `debug/on_error/<run_id>/`，要只画某一次就 --on-error 指过去
        rows = []
        for item in events:
            task = item.get("task_id")
            count = len(item.get("images") or [])
            rows.append(f"{item['run_id']}（task {task or '?'}，{count} 张）")
        print(f"  失败事件: {len(events)} 个 —— {'；'.join(rows)}", file=output)
    failed = written.get("attachment_failed") or []
    if failed:
        print(
            f"  现场图未下成: {len(failed)} 份（已跳过，不影响日志）—— {'；'.join(failed)}",
            file=output,
        )
    if written.get("log"):
        print(
            f"  日志:   {written['log']}（{written['log_bytes']} 字节"
            f"；拿到 {trace.get('lines')} 行，去掉重复 {trace.get('duplicates_dropped')} 行"
            f"{'，还有更多' if trace.get('has_more') else ''}）",
            file=output,
        )
    else:
        print("  日志:   无", file=output)
    rel = inventory.get("event", {}).get("release")
    if rel:
        label = report_common.version_label(rel)
        tag = f"（资源版本 {label}）" if label else ""
        print(f"  release: {rel}{tag}", file=output)
    for marker in trace.get("markers") or []:
        print(f"  失败标记: {marker}", file=output)
    for hint in evidence.hints:
        print(f"  下一步: {hint}", file=output)
    for note in evidence.notes:
        print(f"  提示: {note}", file=stderr)
