"""单版本任务失败率深度体检报告。

数据来源两条路径，分析代码完全相同：

- Sentry 实时查询（默认）：自动选最新版本，或 --release 指定；
- 下载工件（--input）：用 `fetch --preset task-failure` 拉下来的 JSON 离线复现。

报告分四块，口径不同不能混着读：

- **任务失败率**：任务级 span（`mfa.task`）按 ok / internal_error / cancelled 分列，
  失败率 = 失败 trace 数 / 总 trace 数；附 Wilson 95% 区间，样本不足会标出来；
- **失败节点标记**：仅在失败时上报的 span 与系统级失败，没有成功样本，
  触发次数即触发该失败的运行数，不参与失败率计算；
- **运行失败率**：umbrella span（`mfa.task_run`）的失败占比，即一次完整运行失败的概率；
- **运行环境分布**：按 os / environment 聚合的运行数，用于判断失败是否集中在某类机器上。
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TextIO

try:
    from .config import CONFIG
except ImportError:
    from config import CONFIG

try:
    from .report_common import (
        DEFAULT_SENTRY_TIMEOUT_SECONDS,
        MAX_RELIABLE_SPAN_PERIOD_DAYS,
        Artifact,
        SpanQuery,
        authority_error_hint,
        explore_all,
        format_rate_with_ci,
        load_artifact,
        module_summary,
        provenance_line,
        release_version_key,
        resolve_latest_release,
        resolve_sentry_command,
        row_count,
        show_progress,
        version_label,
        warn_on_truncated_period,
        write_console_table,
    )
except ImportError:
    from report_common import (
        DEFAULT_SENTRY_TIMEOUT_SECONDS,
        MAX_RELIABLE_SPAN_PERIOD_DAYS,
        Artifact,
        SpanQuery,
        authority_error_hint,
        explore_all,
        format_rate_with_ci,
        load_artifact,
        module_summary,
        provenance_line,
        release_version_key,
        resolve_latest_release,
        resolve_sentry_command,
        row_count,
        show_progress,
        version_label,
        warn_on_truncated_period,
        write_console_table,
    )

DEFAULT_TARGET = CONFIG.target
DEFAULT_PERIOD = "7d"
INTERNAL_ERROR = "internal_error"
CANCELLED = "cancelled"
OK = "ok"
TASK_RUN_SPANS = CONFIG.task_run_spans
UMBRELLA_FILTER = f"span.description:[{','.join(TASK_RUN_SPANS)}]" if TASK_RUN_SPANS else ""

# 报告可单独裁剪的分块。`cli context` 只用 runtime 一块，避免「看环境」被任务表淹没。
ALL_SECTIONS: tuple[str, ...] = ("tasks", "markers", "runs", "runtime")

# span 描述 → 可读标签。默认表 + config.json 的 system_labels —— 真实数据里补的
# `连接`/`开始前脚本`/`性能基准`/`结束后脚本` 就是后者的内容。
DEFAULT_SYSTEM_LABELS = {
    "controller_initialization_failed": "控制器初始化失败",
    "connection_failed": "连接失败",
    "agent_start_failed": "Agent 启动失败",
    "resource_initialization_failed": "资源初始化失败",
    "controller_link_start_failed": "控制器连接失败",
}
SYSTEM_LABELS = {**DEFAULT_SYSTEM_LABELS, **CONFIG.system_labels}


@dataclass(frozen=True)
class TaskRow:
    task: str
    total: int
    failed: int
    cancelled: int
    failure_rate: float | None


@dataclass(frozen=True)
class ReleaseRow:
    release: str
    version: str | None
    total: int
    failed: int
    failure_rate: float


@dataclass(frozen=True)
class ContextRow:
    dimension: str
    value: str
    total: int


@dataclass(frozen=True)
class Report:
    release: str
    version: str | None
    period: str
    tasks: list[TaskRow]
    markers: list[TaskRow]
    runs: list[ReleaseRow]
    context: list[ContextRow]
    covered_releases: list[str]
    provenance: str
    sections: tuple[str, ...] = ALL_SECTIONS
    min_samples: int = CONFIG.min_samples


def describe(description: str) -> str:
    """把系统级 span 描述翻译为可读标签，任务名保持原样。"""
    return SYSTEM_LABELS.get(description, description)


def _in_version(row: dict[str, Any], version_key: tuple[int, int, int, int, int] | None) -> bool:
    """判断行是否属于目标版本；指定了目标版本时，缺少或无效 release 的行不算。"""
    if version_key is None:
        return True
    release = row.get("release")
    if not isinstance(release, str):
        return False
    return release_version_key(release) == version_key


def _is_umbrella_or_ignored(description: str) -> bool:
    """umbrella span、框架内部伪任务、以及配置声明忽略的 span 都不进这两张表。

    真实数据里 MFA 会报出一批界面不可选的框架 span（`连接`/`开始前脚本`/`性能基准`/
    `结束后脚本`，64/64/62/31 次运行、失败率恒为 0）——它们来自 `system_labels` 配置，
    不排掉会以"最多运行数 + 0% 失败率"占住排行榜头部，把真实任务的失败率挤下去。

    它们的运行口径由「运行失败率」分表单独呈现。
    """
    return (
        description in TASK_RUN_SPANS
        or description in CONFIG.system_labels
        or description in CONFIG.ignored_descriptions
        or any(description.startswith(prefix) for prefix in CONFIG.ignored_prefixes)
        or any(description.endswith(suffix) for suffix in CONFIG.ignored_suffixes)
    )


def build_task_rows(
    totals: Iterable[dict[str, Any]],
    statuses: Iterable[dict[str, Any]],
    version_key: tuple[int, int, int, int, int] | None,
    *,
    sort: str = "failures",
    reverse: bool = False,
    limit: int | None = None,
) -> tuple[list[TaskRow], list[TaskRow]]:
    """聚合各任务的成功率与失败率，并把纯失败标记分流出去。"""
    total_by_task: dict[str, int] = defaultdict(int)
    status_by_task: dict[str, dict[str, int]] = defaultdict(dict)

    for row in totals:
        if not _in_version(row, version_key):
            continue
        description = row.get("description")
        if not isinstance(description, str) or _is_umbrella_or_ignored(description):
            continue
        count = row_count(row)
        if count is not None:
            total_by_task[description] += count

    for row in statuses:
        if not _in_version(row, version_key):
            continue
        description = row.get("description")
        if not isinstance(description, str) or _is_umbrella_or_ignored(description):
            continue
        count = row_count(row)
        if count is None:
            continue
        status = str(row.get("status", "unknown"))
        status_by_task[description][status] = status_by_task[description].get(status, 0) + count

    tasks: list[TaskRow] = []
    markers: list[TaskRow] = []
    for description in sorted(total_by_task.keys() | status_by_task.keys()):
        status_counts = status_by_task.get(description, {})
        ok = status_counts.get(OK, 0)
        failed = status_counts.get(INTERNAL_ERROR, 0)
        cancelled = status_counts.get(CANCELLED, 0)
        # 没有成功样本也没有取消样本，却出现过失败 → 只在失败时上报的标记。
        if ok == 0 and cancelled == 0 and failed > 0:
            markers.append(TaskRow(task=description, total=failed, failed=failed, cancelled=0, failure_rate=None))
            continue
        total = total_by_task.get(description) or (ok + failed + cancelled)
        failed = min(failed, total)
        cancelled = min(cancelled, total - failed)
        tasks.append(
            TaskRow(
                task=description,
                total=total,
                failed=failed,
                cancelled=cancelled,
                failure_rate=failed / total if total else None,
            )
        )

    if sort == "rate":
        tasks.sort(
            key=lambda row: (row.failure_rate is not None, row.failure_rate or 0.0, row.total, row.task),
            reverse=not reverse,
        )
    elif sort == "total":
        tasks.sort(key=lambda row: (row.total, row.task), reverse=not reverse)
    elif sort == "name":
        tasks.sort(key=lambda row: row.task, reverse=reverse)
    else:  # "failures"（默认）
        tasks.sort(key=lambda row: (row.failed, row.total, row.task), reverse=not reverse)

    markers.sort(key=lambda row: (row.total, row.task), reverse=not reverse)
    if limit is not None and limit > 0:
        tasks = tasks[:limit]
        markers = markers[:limit]

    return tasks, markers


def build_release_rows(
    totals: Iterable[dict[str, Any]],
    failures: Iterable[dict[str, Any]],
    version_key: tuple[int, int, int, int, int] | None,
    *,
    target_release: str | None = None,
) -> list[ReleaseRow]:
    """按 release 原文聚合运行失败率。版本键为空时退化为按指定 release 精确过滤。"""
    total_by_release: dict[str, int] = {}
    failed_by_release: dict[str, int] = defaultdict(int)
    for row in totals:
        release = row.get("release")
        count = row_count(row)
        if isinstance(release, str) and count is not None:
            total_by_release[release] = total_by_release.get(release, 0) + count
    for row in failures:
        release = row.get("release")
        count = row_count(row)
        if isinstance(release, str) and count is not None:
            failed_by_release[release] += count

    report: list[ReleaseRow] = []
    for release, total in total_by_release.items():
        if version_key is not None:
            if release_version_key(release) != version_key:
                continue
        elif target_release is not None and release != target_release:
            continue
        failed = failed_by_release.get(release, 0)
        report.append(
            ReleaseRow(
                release=release,
                version=version_label(release),
                total=total,
                failed=failed,
                failure_rate=failed / total if total else 0.0,
            )
        )
    return sorted(report, key=lambda row: (-row.total, row.release))


def build_context_rows(
    measurements: Iterable[tuple[str, Iterable[dict[str, Any]], str]],
    version_key: tuple[int, int, int, int, int] | None,
) -> list[ContextRow]:
    """聚合运行环境分布。每项是 (维度名, 行, 行里的取值字段名)。"""
    rows: list[ContextRow] = []
    for dimension, items, value_field in measurements:
        total_by_value: dict[str, int] = defaultdict(int)
        for row in items:
            if not _in_version(row, version_key):
                continue
            count = row_count(row)
            if count is None:
                continue
            value = row.get(value_field)
            total_by_value[str(value) if value not in (None, "") else "(未知)"] += count
        if not total_by_value:
            continue
        rows.append(ContextRow(dimension=dimension, value="", total=0))  # 维度分隔行
        grand_total = sum(total_by_value.values())
        for value, count in sorted(total_by_value.items(), key=lambda item: (-item[1], item[0])):
            rows.append(
                ContextRow(
                    dimension=dimension,
                    value=f"{value}  ({count / grand_total:.0%})" if grand_total else value,
                    total=count,
                )
            )
    return rows


def build_report(
    measurements: dict[str, list[dict[str, Any]]],
    *,
    release: str,
    period: str,
    provenance: str,
    sort: str = "failures",
    reverse: bool = False,
    limit: int | None = None,
    sections: tuple[str, ...] = ALL_SECTIONS,
    min_samples: int | None = None,
) -> Report:
    """纯函数：从 measurement 行集合构造报告，不碰网络也不碰磁盘。

    ``min_samples`` 非空时覆盖 `config.json` 的默认值（`--min-samples`）。
    """
    samples_threshold = CONFIG.min_samples if min_samples is None else min_samples
    version_key = release_version_key(release)
    version = version_label(release)

    totals = measurements.get("task_total", [])
    statuses = measurements.get("task_status", [])
    tasks, markers = build_task_rows(totals, statuses, version_key, sort=sort, reverse=reverse, limit=limit)

    runs: list[ReleaseRow] = []
    if TASK_RUN_SPANS and ("run_total" in measurements or "run_failed" in measurements):
        runs = build_release_rows(
            measurements.get("run_total", []),
            measurements.get("run_failed", []),
            version_key,
            target_release=release,
        )

    context = build_context_rows(
        (
            ("操作系统", measurements.get("runtime_os", []), "os_name"),
            ("运行环境", measurements.get("runtime_env", []), "environment"),
        ),
        version_key,
    )

    covered = sorted(
        {
            str(row["release"])
            for row in (*totals, *statuses)
            if isinstance(row.get("release"), str)
            and (version_key is None or release_version_key(str(row["release"])) == version_key)
        }
    )

    return Report(
        release=release,
        version=version,
        period=period,
        tasks=tasks,
        markers=markers,
        runs=runs,
        context=context,
        covered_releases=covered,
        provenance=provenance,
        sections=tuple(sections),
        min_samples=samples_threshold,
    )


def _sections(report: Report) -> tuple[str, ...]:
    """报告要渲染的分块；空元组按「全部」处理。"""
    return report.sections or ALL_SECTIONS


def write_console(report: Report, output: TextIO) -> None:
    sections = _sections(report)
    header = f"版本: {report.version}" if report.version else f"Sentry release: {report.release}"
    print(header, file=output)
    print(f"窗口: {report.period}    报告内所有次数均按唯一 trace 去重。\n", file=output)

    if "tasks" in sections:
        print("任务失败率", file=output)
        print(
            f"口径: {INTERNAL_ERROR} 的 trace 数 / 总 trace 数；样本少于 {report.min_samples} 的运行不给区间。\n",
            file=output,
        )
        write_console_table(
            ("任务", "总次数", "失败", "取消", "失败率（95%CI）"),
            [
                (
                    row.task,
                    str(row.total),
                    str(row.failed),
                    str(row.cancelled),
                    format_rate_with_ci(row.failure_rate, row.total, report.min_samples),
                )
                for row in report.tasks
            ],
            output,
            right_aligned={1, 2, 3},
        )

    if "markers" in sections:
        print("\n失败节点标记", file=output)
        print("以下 span 仅在失败时上报；触发次数即触发该失败的运行数，不参与失败率计算。\n", file=output)
        write_console_table(
            ("节点/系统", "触发次数"),
            [(describe(row.task), str(row.total)) for row in report.markers],
            output,
            right_aligned={1},
        )

    if "runs" in sections and report.runs:
        spans_desc = " / ".join(TASK_RUN_SPANS)
        print("\n运行失败率", file=output)
        print(f"口径: umbrella span（{spans_desc}）的失败率，即一次完整运行失败的占比。\n", file=output)
        write_console_table(
            ("release", "运行数", "失败", "失败率"),
            [
                (
                    row.release,
                    str(row.total),
                    str(row.failed),
                    format_rate_with_ci(row.failure_rate, row.total, report.min_samples),
                )
                for row in report.runs
            ],
            output,
            right_aligned={1, 2},
        )

    if "runtime" in sections and report.context:
        print("\n运行环境分布", file=output)
        print("维度分布按 trace 计数，用于判断失败是否集中在某类机器上。\n", file=output)
        write_console_table(
            ("维度 / 取值", "运行数"),
            [
                (row.value if row.value else f"[{row.dimension}]", str(row.total) if row.total else "")
                for row in report.context
            ],
            output,
            right_aligned={1},
        )

    print(f"\n{report.provenance}", file=output)
    if "markers" in sections and report.markers:
        # 远程证据到不了"当时的画面"，这里只给一条可执行的下一步，不自动画图
        # （画图要本地证据，报告本身要能在 --input 工件上离线复现）。
        print(
            "\n想看某次失败的完整执行循环：在有 MFA 的那台机器上取 debug/ 目录（或让用户导出日志包），然后跑\n"
            "  python -m tools.sentry.cli flow --case <安装根>/debug --task-id <N> --from \"<起始>\" --to \"<结束>\"\n"
            "远程遥测不含 vision 识别帧，流程图上只能有节点循环与 on_error 失败现场图。",
            file=output,
        )
    if "tasks" in sections or "runs" in sections:
        if report.covered_releases:
            print("本次统计覆盖的 release：", file=output)
            for release in report.covered_releases:
                print(f"  - {release}", file=output)
        else:
            print("本次统计没有覆盖到任何 release（该窗口内没有上报数据）。", file=output)


def write_markdown(report: Report, output: TextIO) -> None:
    sections = _sections(report)
    if report.version:
        print(f"> 版本: `{report.version}`　窗口: `{report.period}`\n", file=output)
    else:
        print(f"> Sentry release: `{report.release}`　窗口: `{report.period}`\n", file=output)

    if "tasks" in sections:
        print("## 任务失败率\n", file=output)
        print("| 任务 | 总次数 | 失败 | 取消 | 失败率（95%CI） |", file=output)
        print("|---|---:|---:|---:|---|", file=output)
        for row in report.tasks:
            print(
                f"| {row.task} | {row.total} | {row.failed} | {row.cancelled} | "
                f"{format_rate_with_ci(row.failure_rate, row.total, report.min_samples)} |",
                file=output,
            )

    if "markers" in sections:
        print("\n## 失败节点标记\n", file=output)
        print("> 仅在失败时上报；触发次数即触发该失败的运行数，不参与失败率计算。\n", file=output)
        print("| 节点/系统 | 触发次数 |", file=output)
        print("|---|---:|", file=output)
        for row in report.markers:
            print(f"| {describe(row.task)} | {row.total} |", file=output)

    if "runs" in sections and report.runs:
        spans_desc = " / ".join(TASK_RUN_SPANS)
        print("\n## 运行失败率\n", file=output)
        print(f"> umbrella span（{spans_desc}）的失败占比。\n", file=output)
        print("| release | 运行数 | 失败 | 失败率 |", file=output)
        print("|---|---:|---:|---|", file=output)
        for row in report.runs:
            print(
                f"| `{row.release}` | {row.total} | {row.failed} | "
                f"{format_rate_with_ci(row.failure_rate, row.total, report.min_samples)} |",
                file=output,
            )

    if "runtime" in sections and report.context:
        print("\n## 运行环境分布\n", file=output)
        print("| 维度 / 取值 | 运行数 |", file=output)
        print("|---|---:|", file=output)
        for row in report.context:
            if row.value:
                print(f"| {row.value} | {row.total} |", file=output)
            else:
                print(f"| **{row.dimension}** |  |", file=output)

    print(f"\n> {report.provenance}\n", file=output)
    if ("tasks" in sections or "runs" in sections) and report.covered_releases:
        print("> 覆盖的 release：\n", file=output)
        for release in report.covered_releases:
            print(f"> - `{release}`", file=output)


def write_json(report: Report, output: TextIO) -> None:
    json.dump(asdict(report), output, ensure_ascii=False, indent=2)
    output.write("\n")


def collect_report(
    *,
    sentry_command: str | None,
    release: str | None,
    target: str,
    period: str,
    artifact: Artifact | None,
    sort: str,
    reverse: bool,
    limit: int | None,
    fresh: bool,
    timeout_seconds: float,
    verbose: bool,
    quiet: bool,
    sections: tuple[str, ...] = ALL_SECTIONS,
    min_samples: int | None = None,
    min_users: int | None = None,
) -> Report:
    """取得 measurement 行集合并构造报告；工件路径与实时查询共用 build_report。"""
    if artifact is not None:
        measurements = {
            "task_total": artifact.get("task_total", "task-failure"),
            "task_status": artifact.get("task_status", "task-failure"),
        }
        for optional in ("run_total", "run_failed", "runtime_os", "runtime_env"):
            if optional in artifact.measurements:
                measurements[optional] = artifact.get(optional)
        effective_release = release
        if effective_release is None:
            candidates = {
                str(row["release"])
                for row in (*measurements["task_total"], *measurements["task_status"])
                if isinstance(row.get("release"), str) and release_version_key(str(row["release"])) is not None
            }
            if not candidates:
                raise ValueError(
                    f"工件 {artifact.path} 里没有可解析出版本号的 release，无法确定分析目标；"
                    "请用 --release 显式指定。"
                )
            effective_release = max(candidates, key=lambda item: release_version_key(item) or (0, 0, 0, 0, 0))
            show_progress(f"工件推断版本来自 release: {effective_release}", quiet=quiet)
        return build_report(
            measurements,
            release=effective_release,
            period=artifact.period,
            provenance=provenance_line(artifact, artifact.period),
            sort=sort,
            reverse=reverse,
            limit=limit,
            sections=sections,
            min_samples=min_samples,
        )

    if sentry_command is None:
        raise ValueError("实时查询需要 sentry 命令。")

    if release is None:
        show_progress("[1/2] 自动选择最新版本", quiet=quiet)
        release = resolve_latest_release(
            sentry_command,
            target=target,
            min_users=CONFIG.min_release_unique_users if min_users is None else min_users,
            fresh=fresh,
            verbose=verbose,
            quiet=quiet,
            timeout_seconds=timeout_seconds,
        )

    version_key = release_version_key(release)
    version = version_label(release)
    if version is not None:
        show_progress(
            f"分析版本 {version}（来自 release: {release}；任务数据覆盖所有内嵌该版本的渠道）",
            quiet=quiet,
        )
    else:
        show_progress(f"分析单个 Sentry release: {release}", quiet=quiet)

    escaped_release = release.replace('"', '\\"')
    scope_filter = f'release:"{escaped_release}"'
    # 任务级分析按版本聚合：同一版本内嵌于多个渠道 release，
    # 查询不带 release 过滤，取回后按版本键本地过滤。
    # release 解析不出版本时（--release 传了非标准字符串）回退为精确过滤。
    task_query = "" if version_key is not None else scope_filter
    queries = [
        SpanQuery(
            fields=("span.description", "release", "count_unique(trace)"),
            query=task_query,
            sort="-count_unique(trace)",
        ),
        SpanQuery(
            fields=("span.description", "span.status", "release", "count_unique(trace)"),
            query=task_query,
            sort="-count_unique(trace)",
        ),
    ]
    if TASK_RUN_SPANS:
        umbrella_query = UMBRELLA_FILTER if version_key is not None else f"{scope_filter} {UMBRELLA_FILTER}".strip()
        queries.append(
            SpanQuery(fields=("release", "count_unique(trace)"), query=umbrella_query, sort="-count_unique(trace)")
        )
        queries.append(
            SpanQuery(
                fields=("release", "count_unique(trace)"),
                query=f"{umbrella_query} span.status:{INTERNAL_ERROR}".strip(),
                sort="-count_unique(trace)",
            )
        )
    queries.append(
        SpanQuery(fields=("os.name", "release", "count_unique(trace)"), query="", sort="-count_unique(trace)")
    )
    queries.append(
        SpanQuery(fields=("environment", "release", "count_unique(trace)"), query="", sort="-count_unique(trace)")
    )

    show_progress(f"[2/2] 并发查询 {len(queries)} 组任务与运行指标", quiet=quiet)
    results = explore_all(
        sentry_command,
        target=target,
        period=period,
        queries=queries,
        fresh=fresh,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )

    names = ["task_total", "task_status"]
    if TASK_RUN_SPANS:
        names.extend(("run_total", "run_failed"))
    names.extend(("runtime_os", "runtime_env"))
    measurements = {name: rows for name, rows in zip(names, results, strict=True)}

    return build_report(
        measurements,
        release=release,
        period=period,
        provenance=provenance_line(None, period),
        sort=sort,
        reverse=reverse,
        limit=limit,
        sections=sections,
        min_samples=min_samples,
    )


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=module_summary(__doc__))
    parser.add_argument("--release", help="指定 Sentry release（取其内嵌的项目版本）；未指定时自动选最新版本")
    parser.add_argument("--input", help="读下载好的遥测工件而不是实时查询（见 fetch --preset task-failure）")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="<org>/<project>")
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"查询范围，例如 24h、7d 或 2026-08-23..2026-08-24；"
        f"超过 {MAX_RELIABLE_SPAN_PERIOD_DAYS} 天 Sentry 只返回截断样本（默认 {DEFAULT_PERIOD}）",
    )
    parser.add_argument(
        "--no-fresh", action="store_true", help="复用 Sentry CLI 本地缓存而不拉取最新数据：同一查询约快 3 倍"
    )
    parser.add_argument(
        "--sort",
        choices=("failures", "rate", "total", "name"),
        default="failures",
        help="任务排序规则：failures（失败次数，默认）、rate（失败率）、total（总次数）、name（任务名）",
    )
    parser.add_argument("--reverse", action="store_true", help="反转排序顺序")
    parser.add_argument(
        "--min-users",
        type=int,
        default=None,
        help=f"自动选版本时的最低独立用户数（默认取 config.json 的 {CONFIG.min_release_unique_users}）",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=None,
        help=f"展示失败率与置信区间所需的最低样本数（默认取 config.json 的 {CONFIG.min_samples}）",
    )
    parser.add_argument("--limit", type=int, default=None, help="限制展示的任务数量（如 --limit 10；0 表示不限）")
    parser.add_argument(
        "--format", choices=("console", "markdown", "json"), default="console", help="输出格式"
    )
    parser.add_argument(
        "--sections",
        default=",".join(ALL_SECTIONS),
        help=f"要渲染的分块，逗号分隔（可选 {'/'.join(ALL_SECTIONS)}；默认全部）。cli context 只渲染 runtime",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_SENTRY_TIMEOUT_SECONDS, help="单次 Sentry 查询超时秒数（默认 120）"
    )
    parser.add_argument("--verbose", action="store_true", help="输出 sentry 查询命令")
    parser.add_argument("--quiet", action="store_true", help="不输出查询进度")
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = create_argument_parser(prog).parse_args(argv)
    warn_on_truncated_period(arguments.period)
    if not math.isfinite(arguments.timeout) or arguments.timeout <= 0:
        raise ValueError("--timeout 必须是大于 0 的有限数值。")
    if arguments.limit is not None and arguments.limit < 0:
        raise ValueError("--limit 不能是负数（0 表示不限）。")

    sections = tuple(part.strip() for part in arguments.sections.split(",") if part.strip())
    unknown = [part for part in sections if part not in ALL_SECTIONS]
    if unknown:
        raise ValueError(f"--sections 里有未知分块 {unknown}；可选 {'/'.join(ALL_SECTIONS)}。")
    for name, value in (("--min-users", arguments.min_users), ("--min-samples", arguments.min_samples)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} 必须是大于 0 的整数。")

    artifact = load_artifact(arguments.input) if arguments.input else None
    report = collect_report(
        sentry_command=None if artifact is not None else resolve_sentry_command(),
        release=arguments.release,
        target=arguments.target,
        period=arguments.period,
        artifact=artifact,
        sort=arguments.sort,
        reverse=arguments.reverse,
        limit=arguments.limit,
        fresh=not arguments.no_fresh,
        timeout_seconds=arguments.timeout,
        verbose=arguments.verbose,
        quiet=arguments.quiet,
        sections=sections,
        min_samples=arguments.min_samples,
        min_users=arguments.min_users,
    )
    writers = {"console": write_console, "markdown": write_markdown, "json": write_json}
    writers[arguments.format](report, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        message = authority_error_hint(error) if isinstance(error, RuntimeError) else str(error)
        print(f"错误：{message}", file=sys.stderr)
        raise SystemExit(1) from error
