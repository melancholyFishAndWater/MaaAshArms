"""任务失败率的跨版本趋势与环比对比报告。

与 M9A 版本的三处差异（都是为了让本项目真能跑出结果）：

1. **默认包含预发布版本**，用 ``--stable-only`` 反向排除。本项目至今只发 beta，
   照抄 M9A「默认仅正式版」会让趋势表一个版本都选不出来。
2. **用户数门槛可调**（config.json 的 ``min_release_unique_users``，默认 3），
   而不是硬编码 10；找不到达标版本时不抛错，而是放宽并提示。
3. **失败率附 Wilson 95% 区间**，样本少于 ``min_samples`` 时直接标注，
   避免拿榜单末尾个位数样本的 +7pp 当回归。

版本号取自 release 字符串里**最后一个** ``<name>@vX.Y.Z`` 片段（见
`report_common.release_match`）——双 ``@`` 拼接格式下它才是资源版本。
"""

from __future__ import annotations

import io
import argparse
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
        DEFAULT_SPAN_PERIOD,
        MAX_RELIABLE_SPAN_PERIOD_DAYS,
        STABLE_RELEASE_RANK,
        Artifact,
        SpanQuery,
        authority_error_hint,
        explore,
        explore_all,
        format_delta,
        format_rate_with_ci,
        load_artifact,
        module_summary,
        provenance_line,
        release_version_key,
        resolve_sentry_command,
        show_progress,
        version_label,
        warn_on_truncated_period,
        write_console_table,
    )
except ImportError:
    from report_common import (
        DEFAULT_SENTRY_TIMEOUT_SECONDS,
        DEFAULT_SPAN_PERIOD,
        MAX_RELIABLE_SPAN_PERIOD_DAYS,
        STABLE_RELEASE_RANK,
        Artifact,
        SpanQuery,
        authority_error_hint,
        explore,
        explore_all,
        format_delta,
        format_rate_with_ci,
        load_artifact,
        module_summary,
        provenance_line,
        release_version_key,
        resolve_sentry_command,
        show_progress,
        version_label,
        warn_on_truncated_period,
        write_console_table,
    )

DEFAULT_TARGET = CONFIG.target
DEFAULT_VERSIONS_COUNT = 3
DEFAULT_MIN_LATEST_RUNS = 10
INTERNAL_ERROR = "internal_error"
OK = "ok"
DEFAULT_SYSTEM_LABELS = {
    "controller_initialization_failed": "控制器初始化失败",
    "connection_failed": "连接失败",
    "agent_start_failed": "Agent 启动失败",
    "resource_initialization_failed": "资源初始化失败",
    "controller_link_start_failed": "控制器连接失败",
}
# 默认表 + config.json 的 system_labels（真实数据里补的框架伪任务就在后者）
SYSTEM_LABELS = {**DEFAULT_SYSTEM_LABELS, **CONFIG.system_labels}


def _is_ignored(description: str) -> bool:
    """框架伪任务与配置声明忽略的 span 不进趋势表。

    真实数据里 MFA 会报出 `连接`/`开始前脚本`/`性能基准`/`结束后脚本`（64/64/62/31 次运行、
    失败率恒为 0）；不排掉它们会靠"运行数最多"占住表格头部，把真实任务的走势挤下去。
    """
    return (
        description in SYSTEM_LABELS
        or description in CONFIG.ignored_descriptions
        or any(description.startswith(prefix) for prefix in CONFIG.ignored_prefixes)
        or any(description.endswith(suffix) for suffix in CONFIG.ignored_suffixes)
    )


@dataclass(frozen=True)
class VersionTaskStat:
    """单个版本内某一任务的详细统计。"""

    version: str
    total: int
    failed: int
    failure_rate: float | None
    delta_pp: float | None = None


@dataclass(frozen=True)
class TaskTrendRow:
    """多版本对比表中某一任务的汇总行。"""

    task: str
    latest_total: int
    version_rates: dict[str, float | None]
    version_totals: dict[str, int]
    version_deltas: dict[str, float | None]


@dataclass(frozen=True)
class TrendReport:
    """跨版本任务表现报告。"""

    versions: list[str]
    period: str
    rows: list[TaskTrendRow]
    task_filter: str | None
    detailed_task_stats: list[VersionTaskStat] | None
    provenance: str
    min_samples: int


def _row_count(row: dict[str, Any], key: str = "_count") -> int | None:
    """宽松取计数列：接受中性名与 Sentry 原始名两种写法。"""
    for alias in (key, "count_unique(trace)"):
        value = row.get(alias)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def discover_versions(
    rows: Sequence[dict[str, Any]],
    *,
    count: int = DEFAULT_VERSIONS_COUNT,
    include_prerelease: bool = True,
    min_users: int = CONFIG.min_release_unique_users,
    quiet: bool = False,
) -> list[str]:
    """从 spans 样本里发现用户数达标的最近 N 个版本（降序）。

    多探索一个版本作为最老展示版本的环比基准。找不到达标版本时逐级放宽门槛，
    而不是直接失败——本项目装机量小，硬门槛会让命令在第一步就退出。
    """
    version_users: dict[tuple[int, int, int, int, int], int] = defaultdict(int)
    version_labels: dict[tuple[int, int, int, int, int], str] = {}
    filtered_users: dict[tuple[int, int, int, int, int], int] = defaultdict(int)
    filtered_labels: dict[tuple[int, int, int, int, int], str] = {}
    saw_user_count = False

    for row in rows:
        release = row.get("release")
        user_count = row.get("_count_user", row.get("count_unique(user)"))
        trace_count = row.get("_count", row.get("count_unique(trace)"))
        if not isinstance(release, str) or not isinstance(trace_count, int):
            continue
        key = release_version_key(release)
        label = version_label(release)
        if key is None or label is None:
            continue

        # 没有 user 口径的工件（例如只下了 task_total）用运行数代替用户数当门槛，
        # 否则任何版本都进不了候选。
        if isinstance(user_count, int):
            saw_user_count = True
            effective = user_count
        else:
            effective = trace_count

        version_users[key] += effective
        version_labels[key] = label
        # key[3] == STABLE_RELEASE_RANK 代表正式稳定版，其余为 alpha/beta/rc
        if not include_prerelease and key[3] != STABLE_RELEASE_RANK:
            continue
        filtered_users[key] += effective
        filtered_labels[key] = label

    def keys_to_labels(keys, labels: dict) -> list[str]:
        """选好的版本键按**版本号降序**输出（最新在前）。

        选版本按数据量排（真实数据里 β37 有 64 次运行、后续版本各 1–2 次，按版本号选会把
        有效样本压到 1），但 Δpp 的语义是"相对更早的版本"，所以输出与比较一律按时间序——
        否则按数据量排出来的顺序会把 Δpp 算到未来的版本上。
        """
        return [labels[key] for key in sorted(keys, reverse=True)]

    # 选"数据量最大的"版本，而不是"版本号最新的"。
    # min_users 门槛已保证选出来的是"有人真实跑过"的版本。
    qualified = [key for key in sorted(filtered_users, key=lambda k: (-filtered_users[k], -k[0], -k[2], -k[4])) if filtered_users[key] >= min_users]
    if qualified:
        return keys_to_labels(qualified[: count + 1], filtered_labels)

    # 放宽到「口径内出现过的全部版本」。口径外的版本只在口径内一个都没有时才兜底，
    # 否则 --stable-only 会被悄悄忽略。
    unit = "独立用户" if saw_user_count else "运行"
    relaxed = [key for key in sorted(filtered_users, key=lambda k: (-filtered_users[k], -k[0], -k[2], -k[4])) if filtered_users[key] > 0]
    if relaxed:
        note = f"没有达到 {min_users} 位{unit}的版本，已放宽到口径内出现过的全部版本"
        if not include_prerelease:
            note += "（--stable-only 下没有正式版）"
        show_progress(f"[提示] {note}。", quiet=quiet)
        return keys_to_labels(relaxed[: count + 1], filtered_labels)

    fallback = [key for key in sorted(version_users, key=lambda k: (-version_users[k], -k[0], -k[2], -k[4])) if version_users[key] > 0]
    if not fallback:
        return []
    if not include_prerelease:
        # --stable-only 是用户显式要求的口径，不能因为「没有正式版」就悄悄忽略：
        # 返回空列表，让调用方给出「该口径下一个版本都没有」的明确报错。
        return []
    note = f"没有达到 {min_users} 位{unit}的版本，已放宽到出现过的全部版本"
    show_progress(f"[提示] {note}。", quiet=quiet)
    return keys_to_labels(fallback[: count + 1], version_labels)


def build_trend_rows(
    totals: Iterable[dict[str, Any]],
    statuses: Iterable[dict[str, Any]],
    versions: Sequence[str],
    *,
    display_versions: Sequence[str] | None = None,
    task_filter: str | None = None,
    min_latest_runs: int = DEFAULT_MIN_LATEST_RUNS,
    sort: str = "runs",
    reverse: bool = False,
    limit: int | None = None,
) -> tuple[list[TaskTrendRow], list[VersionTaskStat] | None]:
    """按版本聚合各任务的失败率与环比 Δpp。

    环比方向：``versions`` 是降序（最新在前），所以每个版本的 Δpp 是
    「本版本 − 下一个更早版本」。最早展示版本的 Δpp 为 None。
    """
    version_set = set(versions)
    task_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    task_failures: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    task_oks: dict[str, int] = defaultdict(int)

    for row in totals:
        release = row.get("release")
        label = version_label(str(release)) if isinstance(release, str) else None
        description = row.get("description")
        count = _row_count(row)
        if label in version_set and isinstance(description, str) and count:
            task_totals[label][description] += count

    for row in statuses:
        release = row.get("release")
        label = version_label(str(release)) if isinstance(release, str) else None
        description = row.get("description")
        status = row.get("status")
        count = _row_count(row)
        if label in version_set and isinstance(description, str) and count:
            if status == INTERNAL_ERROR:
                task_failures[label][description] += count
            elif status == OK:
                task_oks[description] += count

    target_versions = list(display_versions) if display_versions else list(versions)
    all_tasks: set[str] = set()
    for version in target_versions:
        all_tasks.update(task_totals[version].keys())
        all_tasks.update(task_failures[version].keys())

    detailed_stats: list[VersionTaskStat] | None = None
    resolved_task_filter = task_filter
    if task_filter:
        exact_match = next((task for task in sorted(all_tasks) if task.lower() == task_filter.lower()), None)
        if exact_match is not None:
            target_task = exact_match
        else:
            fuzzy_matches = [task for task in sorted(all_tasks) if task_filter.lower() in task.lower()]
            if len(fuzzy_matches) == 1:
                target_task = fuzzy_matches[0]
            elif len(fuzzy_matches) > 1:
                candidates = "、".join(f"'{match}'" for match in fuzzy_matches)
                raise ValueError(f"任务筛选 '{task_filter}' 匹配到多个候选：{candidates}。请指定更精确的名称。")
            else:
                target_task = task_filter
        resolved_task_filter = target_task
        detailed_stats = []
        for index, version in enumerate(versions):
            total = task_totals[version].get(target_task, 0)
            failed = task_failures[version].get(target_task, 0)
            rate = (failed / total) if total > 0 else None
            detailed_stats.append(
                VersionTaskStat(
                    version=version,
                    total=total,
                    failed=failed,
                    failure_rate=rate,
                    delta_pp=_delta_pp(index, versions, total, rate, task_totals, task_failures, target_task),
                )
            )

    rows: list[TaskTrendRow] = []
    for task in sorted(all_tasks):
        if _is_ignored(task):
            continue
        # 未显式筛选任务时，排除没有成功样本的纯失败标记（如 ReturnMain、HomeFlag）。
        if not task_filter and task_oks.get(task, 0) == 0:
            continue

        latest_version = target_versions[0] if target_versions else ""
        latest_total = task_totals[latest_version].get(task, 0)
        any_total = sum(task_totals[version].get(task, 0) for version in versions)
        if latest_total < min_latest_runs and any_total < min_latest_runs * 2:
            continue

        rates: dict[str, float | None] = {}
        totals_by_version: dict[str, int] = {}
        deltas: dict[str, float | None] = {}
        for index, version in enumerate(versions):
            total = task_totals[version].get(task, 0)
            failed = task_failures[version].get(task, 0)
            rate = (failed / total) if total > 0 else None
            rates[version] = rate
            totals_by_version[version] = total
            deltas[version] = _delta_pp(index, versions, total, rate, task_totals, task_failures, task)

        rows.append(
            TaskTrendRow(
                task=task,
                latest_total=latest_total,
                version_rates=rates,
                version_totals=totals_by_version,
                version_deltas=deltas,
            )
        )

    latest_label = target_versions[0] if target_versions else ""
    if sort == "rate":
        rows.sort(
            key=lambda row: (
                row.version_rates.get(latest_label) is not None,
                row.version_rates.get(latest_label) or 0.0,
                row.latest_total,
                row.task,
            ),
            reverse=not reverse,
        )
    elif sort == "delta":
        rows.sort(
            key=lambda row: (
                row.version_deltas.get(latest_label) is not None,
                row.version_deltas.get(latest_label) or 0.0,
                row.latest_total,
                row.task,
            ),
            reverse=not reverse,
        )
    elif sort == "name":
        rows.sort(key=lambda row: row.task, reverse=reverse)
    else:  # "runs"（默认）
        rows.sort(key=lambda row: (row.latest_total, row.task), reverse=not reverse)

    if limit is not None and limit > 0:
        rows = rows[:limit]

    return rows, detailed_stats


def _delta_pp(
    index: int,
    versions: Sequence[str],
    total: int,
    rate: float | None,
    task_totals: dict[str, dict[str, int]],
    task_failures: dict[str, dict[str, int]],
    task: str,
) -> float | None:
    """计算相对**更早一个版本**的百分点变化。

    `versions` 必须是时间序（最新在前），delta 才是"相对上一版本"；
    没有更早版本、或更早版本没样本时为 None。
    """
    if rate is None:
        return None
    for previous_index in range(index + 1, len(versions)):
        previous = versions[previous_index]
        previous_total = task_totals[previous].get(task, 0)
        if previous_total <= 0:
            continue
        return (rate - task_failures[previous].get(task, 0) / previous_total) * 100
    return None


def build_report(
    measurements: dict[str, list[dict[str, Any]]],
    *,
    versions: Sequence[str],
    period: str,
    provenance: str,
    task_filter: str | None = None,
    min_latest_runs: int = DEFAULT_MIN_LATEST_RUNS,
    sort: str = "runs",
    reverse: bool = False,
    limit: int | None = None,
    min_samples: int | None = None,
) -> TrendReport:
    """纯函数：从 measurement 行集合构造趋势报告，不碰网络也不碰磁盘。"""
    totals = measurements.get("task_total", [])
    statuses = measurements.get("task_status", [])
    rows, detailed = build_trend_rows(
        totals,
        statuses,
        versions,
        display_versions=versions,
        task_filter=task_filter,
        min_latest_runs=min_latest_runs,
        sort=sort,
        reverse=reverse,
        limit=limit,
    )
    return TrendReport(
        versions=list(versions),
        period=period,
        rows=rows,
        task_filter=task_filter,
        detailed_task_stats=detailed,
        provenance=provenance,
        min_samples=CONFIG.min_samples if min_samples is None else min_samples,
    )


def write_console(report: TrendReport, output: TextIO) -> None:
    if report.detailed_task_stats:
        print(f"任务趋势明细: {report.task_filter or '指定任务'}", file=output)
        print(f"窗口: {report.period}　所有次数按唯一 trace 去重，变化量为相对更早一个版本的 Δpp。\n", file=output)
        write_console_table(
            ("版本", "运行数", "失败数", "失败率（95%CI）", "相对上一版本"),
            [
                (
                    stat.version,
                    str(stat.total),
                    str(stat.failed),
                    format_rate_with_ci(stat.failure_rate, stat.total, report.min_samples),
                    format_delta(stat.delta_pp) if stat.delta_pp is not None else "-",
                )
                for stat in report.detailed_task_stats
            ],
            output,
            right_aligned={1, 2, 4},
        )
        print(f"\n{report.provenance}", file=output)
        return

    print("任务失败率跨版本对比（含相对更早版本的 Δpp）", file=output)
    print(f"窗口: {report.period}　运行数按唯一 trace 去重。\n", file=output)
    latest = report.versions[0] if report.versions else "最新"
    headers = ["任务", f"运行数({latest})", *report.versions]
    table_rows = []
    for row in report.rows:
        cells = [row.task, str(row.latest_total)]
        for version in report.versions:
            cells.append(format_rate_with_ci(row.version_rates.get(version), row.version_totals.get(version, 0), report.min_samples))
        table_rows.append(cells)
    write_console_table(headers, table_rows, output, right_aligned=set(range(1, len(headers))))

    print("\n环比 Δpp", file=output)
    print("每个版本相对更早一个版本的变化；未展示的更早版本若有样本也会参与计算。\n", file=output)
    write_console_table(
        ["任务", *report.versions],
        [
            [
                row.task,
                *(format_delta(row.version_deltas.get(version)) or "-" for version in report.versions),
            ]
            for row in report.rows
        ],
        output,
        right_aligned=set(range(1, len(report.versions) + 1)),
    )
    print(f"\n{report.provenance}", file=output)


def write_markdown(report: TrendReport, output: TextIO) -> None:
    if report.detailed_task_stats:
        print(f"## 任务趋势明细: {report.task_filter or '指定任务'}\n", file=output)
        print(f"> 窗口: `{report.period}`；所有次数按唯一 trace 去重，变化量为 Δpp。\n", file=output)
        print("| 版本 | 运行数 | 失败数 | 失败率（95%CI） | 相对上一版本 |", file=output)
        print("|---|---:|---:|---|---:|", file=output)
        for stat in report.detailed_task_stats:
            delta = format_delta(stat.delta_pp) if stat.delta_pp is not None else "-"
            print(
                f"| {stat.version} | {stat.total} | {stat.failed} | "
                f"{format_rate_with_ci(stat.failure_rate, stat.total, report.min_samples)} | {delta} |",
                file=output,
            )
        print(f"\n> {report.provenance}", file=output)
        return

    print("## 任务失败率跨版本对比（含 Δpp）\n", file=output)
    print(f"> 窗口: `{report.period}`；运行数按唯一 trace 去重。\n", file=output)
    latest = report.versions[0] if report.versions else "最新"
    headers = ["任务", f"运行数({latest})", *report.versions]
    print("| " + " | ".join(headers) + " |", file=output)
    print("|---|" + "|".join("---" for _ in headers[1:]) + "|", file=output)
    for row in report.rows:
        cells = [row.task, str(row.latest_total)]
        for version in report.versions:
            cells.append(
                format_rate_with_ci(row.version_rates.get(version), row.version_totals.get(version, 0), report.min_samples)
            )
        print("| " + " | ".join(cells) + " |", file=output)

    print("\n### 环比 Δpp\n", file=output)
    print("| 任务 | " + " | ".join(report.versions) + " |", file=output)
    print("|---|" + "|".join("---:" for _ in report.versions) + "|", file=output)
    for row in report.rows:
        deltas = [format_delta(row.version_deltas.get(version)) or "-" for version in report.versions]
        print("| " + " | ".join([row.task, *deltas]) + " |", file=output)
    print(f"\n> {report.provenance}", file=output)


def write_json(report: TrendReport, output: TextIO) -> None:
    json.dump(asdict(report), output, ensure_ascii=False, indent=2)
    output.write("\n")


def collect_report(
    *,
    sentry_command: str | None,
    target: str,
    period: str,
    artifact: Artifact | None,
    versions_count: int,
    include_prerelease: bool,
    task_filter: str | None,
    min_latest_runs: int,
    sort: str,
    reverse: bool,
    limit: int | None,
    fresh: bool,
    timeout_seconds: float,
    verbose: bool,
    quiet: bool,
    min_samples: int | None = None,
    min_users: int | None = None,
) -> TrendReport:
    """取得 measurement 行集合、发现版本序列并构造报告。"""
    if artifact is not None:
        totals = artifact.get("task_total", "task-trend")
        statuses = artifact.get("task_status", "task-trend")
        if "release_users" in artifact.measurements:
            discovery_rows: list[dict[str, Any]] = list(artifact.get("release_users"))
        else:
            # 工件里没有 release_users 时，把任务行里同一 release 的运行数相加当版本门槛。
            per_release: dict[str, int] = defaultdict(int)
            for row in totals:
                release = row.get("release")
                count = _row_count(row)
                if isinstance(release, str) and count:
                    per_release[release] += count
            discovery_rows = [{"release": release, "_count": count} for release, count in per_release.items()]
        versions = discover_versions(
            discovery_rows,
            count=versions_count,
            include_prerelease=include_prerelease,
            min_users=CONFIG.min_release_unique_users if min_users is None else min_users,
            quiet=quiet,
        )
        if not versions:
            raise ValueError(
                f"工件 {artifact.path} 里选不出可比对的版本："
                + ("本次用了 --stable-only，而该口径下一个正式版都没有；去掉它再试。"
                   if not include_prerelease
                   else "窗口内没有能解析出版本号的 release，检查 config.json 的 release_pattern。")
            )
        return build_report(
            {"task_total": totals, "task_status": statuses},
            versions=versions,
            period=artifact.period,
            provenance=provenance_line(artifact, artifact.period),
            task_filter=task_filter,
            min_latest_runs=min_latest_runs,
            sort=sort,
            reverse=reverse,
            limit=limit,
            min_samples=min_samples,
        )

    if sentry_command is None:
        raise ValueError("实时查询需要 sentry 命令。")

    show_progress(f"[1/2] 探索最近 {versions_count} 个版本", quiet=quiet)
    discovery_rows = explore(
        sentry_command,
        target=target,
        period=period,
        fields=("release", "count_unique(user)", "count_unique(trace)"),
        query="",
        sort="-count_unique(user)",
        fresh=fresh,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )
    versions = discover_versions(
        discovery_rows,
        count=versions_count,
        include_prerelease=include_prerelease,
        min_users=CONFIG.min_release_unique_users if min_users is None else min_users,
        quiet=quiet,
    )
    if not versions:
        raise RuntimeError(
            f"在 {period} 内选不出可比对的版本："
            + ("本次用了 --stable-only，而该口径下一个正式版都没有；去掉它再试。"
               if not include_prerelease
               else f"检查 target（{target}）与 config.json 的 release_pattern。")
        )
    display_versions = versions[:versions_count]
    show_progress(f"分析版本: {', '.join(display_versions)}（数据覆盖各版本的所有渠道）", quiet=quiet)

    query_filter = f'span.description:"{task_filter}"' if task_filter else ""
    show_progress("[2/2] 查询各版本任务执行总量与结果分布", quiet=quiet)
    totals, statuses = explore_all(
        sentry_command,
        target=target,
        period=period,
        fresh=fresh,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
        queries=(
            SpanQuery(
                fields=("span.description", "release", "count_unique(trace)"),
                query=query_filter,
                sort="-count_unique(trace)",
            ),
            SpanQuery(
                fields=("span.description", "span.status", "release", "count_unique(trace)"),
                query=query_filter,
                sort="-count_unique(trace)",
            ),
        ),
    )
    report = build_report(
        {"task_total": totals, "task_status": statuses},
        versions=versions,
        period=period,
        provenance=provenance_line(None, period),
        task_filter=task_filter,
        min_latest_runs=min_latest_runs,
        sort=sort,
        reverse=reverse,
        limit=limit,
        min_samples=min_samples,
    )
    # 只展示用户指定的 display_versions，但更早的版本仍参与 Δpp 基准计算。
    return TrendReport(
        versions=list(display_versions),
        period=report.period,
        rows=report.rows,
        task_filter=report.task_filter,
        detailed_task_stats=(
            [stat for stat in report.detailed_task_stats if stat.version in set(display_versions)]
            if report.detailed_task_stats
            else None
        ),
        provenance=report.provenance,
        min_samples=report.min_samples,
    )


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=module_summary(__doc__))
    parser.add_argument("--versions", type=int, default=DEFAULT_VERSIONS_COUNT, help=f"对比最近 N 个版本（默认 {DEFAULT_VERSIONS_COUNT}）")
    parser.add_argument("--task", help="只看某一个任务的详细版本走势（支持模糊匹配，如 --task 训练所）")
    parser.add_argument("--input", help="读下载好的遥测工件而不是实时查询（见 fetch --preset task-trend）")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="<org>/<project>")
    parser.add_argument(
        "--period",
        default=DEFAULT_SPAN_PERIOD,
        help=f"查询范围（默认 {DEFAULT_SPAN_PERIOD}）；超过 {MAX_RELIABLE_SPAN_PERIOD_DAYS} 天 Sentry 只返回截断样本",
    )
    parser.add_argument("--no-fresh", action="store_true", help="复用 Sentry CLI 本地缓存：同一查询约快 3 倍")
    parser.add_argument(
        "--stable-only",
        action="store_true",
        help="只对比正式稳定版（本项目至今只发 beta，默认会包含预发布版）",
    )
    parser.add_argument(
        "--min-latest-runs", type=int, default=DEFAULT_MIN_LATEST_RUNS, help=f"进大表所需的最低样本数（默认 {DEFAULT_MIN_LATEST_RUNS}）"
    )
    parser.add_argument(
        "--min-users",
        type=int,
        default=None,
        help=f"认定版本「有足够用户」所需的最低独立用户数（默认取 config.json 的 {CONFIG.min_release_unique_users}）",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=None,
        help=f"展示失败率与置信区间所需的最低样本数（默认取 config.json 的 {CONFIG.min_samples}）",
    )
    parser.add_argument(
        "--sort",
        choices=("runs", "rate", "delta", "name"),
        default="runs",
        help="排序规则：runs（运行数，默认）、rate（失败率）、delta（恶化幅度）、name（任务名）",
    )
    parser.add_argument("--reverse", action="store_true", help="反转排序顺序")
    parser.add_argument("--limit", type=int, default=None, help="限制展示的任务数量（0 表示不限）")
    parser.add_argument("--format", choices=("console", "markdown", "json"), default="console", help="输出格式")
    parser.add_argument("--timeout", type=float, default=DEFAULT_SENTRY_TIMEOUT_SECONDS, help="单次 Sentry 查询超时秒数（默认 120）")
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
    if arguments.versions <= 0:
        raise ValueError("--versions 必须是大于 0 的整数。")
    if arguments.min_latest_runs < 0:
        raise ValueError("--min-latest-runs 不能是负数。")
    if arguments.limit is not None and arguments.limit < 0:
        raise ValueError("--limit 不能是负数（0 表示不限）。")
    for name, value in (("--min-users", arguments.min_users), ("--min-samples", arguments.min_samples)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} 必须是大于 0 的整数。")

    artifact = load_artifact(arguments.input) if arguments.input else None
    report = collect_report(
        sentry_command=None if artifact is not None else resolve_sentry_command(),
        target=arguments.target,
        period=arguments.period,
        artifact=artifact,
        versions_count=arguments.versions,
        include_prerelease=not arguments.stable_only,
        task_filter=arguments.task,
        min_latest_runs=arguments.min_latest_runs,
        sort=arguments.sort,
        reverse=arguments.reverse,
        limit=arguments.limit,
        fresh=not arguments.no_fresh,
        timeout_seconds=arguments.timeout,
        verbose=arguments.verbose,
        quiet=arguments.quiet,
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
