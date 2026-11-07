"""把窗口汇总成报告：分桶中位数、加权完整时间、派发者表、节点对账说明。

只做聚合与排版；读日志在 logs.py，读 pipeline 与基线在 scope.py。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .logs import Window, bucket_label, bucket_of, chain_windows, load, percentile, weighted

from .scope import (
    Baseline,
    baseline_for,
    filter_windows,
    impute_missing,
    is_framework,
    log_span,
    node_flags,
)

@dataclass
class FileStats:
    path: Path
    dispatches: int = 0
    hits: int = 0
    misses: int = 0
    hangs: int = 0
    skipped: int = 0
    span: tuple[datetime, datetime] | None = None
    baseline: Baseline | None = None


@dataclass
class Report:
    files: list[FileStats]
    buckets: dict[int, list[float]]
    windows: list[Window]
    all_windows: list[Window]
    x: int
    min_samples: int
    max_ms: int
    pipeline: dict[str, str] | None
    modules: list[str]
    dropped_windows: int
    top: int
    imputed_avg_ms: float | None
    imputed_windows: int
    stale_names: list[str]


def collect(
    paths: list[Path], max_ms: int, repo: Path, mfa_log_dir: Path | None
) -> tuple[list[FileStats], list[Window]]:
    stats: list[FileStats] = []
    all_windows: list[Window] = []
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    for path in paths:
        dispatches, skipped = load(path)
        item = FileStats(path=path, skipped=skipped)
        try:
            item.span = log_span(path)
        except (OSError, ValueError):
            item.span = None
        names = {
            dispatch.parent
            for dispatch in dispatches
            if dispatch.closed and not is_framework(dispatch.parent)
        }
        item.baseline = baseline_for(path, names, repo, cache, item.span, mfa_log_dir)
        for dispatch in dispatches:
            if not dispatch.closed:
                continue
            item.dispatches += 1
            if dispatch.ms > max_ms:
                item.hangs += 1
            elif not dispatch.ok:
                item.misses += 1
        item_windows = [
            window
            for window in chain_windows(dispatches)
            if window.ms <= max_ms
        ]
        item.hits = len(item_windows)
        all_windows.extend(item_windows)
        stats.append(item)
    return stats, all_windows


def build_report(
    paths: list[Path],
    *,
    max_ms: int,
    x: int,
    min_samples: int,
    pipeline: dict[str, str] | None,
    modules: list[str],
    top: int,
    repo: Path,
    mfa_log_dir: Path | None,
) -> Report:
    stats, all_windows = collect(paths, max_ms, repo, mfa_log_dir)
    dropped = 0
    if modules and pipeline is not None:
        all_windows, dropped = filter_windows(all_windows, set(pipeline))
    imputed_avg, imputed_count = impute_missing(all_windows, pipeline)
    buckets: dict[int, list[float]] = defaultdict(list)
    for window in all_windows:
        buckets[bucket_of(window.fails)].append(window.ms)
    visible = {window.parent for window in all_windows}
    stale_names = sorted(
        {
            name
            for item in stats
            if item.baseline is not None
            for name in item.baseline.stale
            if name in visible
        }
    )
    return Report(
        files=stats,
        buckets=dict(buckets),
        windows=all_windows,
        all_windows=all_windows,
        x=x,
        min_samples=min_samples,
        max_ms=max_ms,
        pipeline=pipeline,
        modules=modules,
        dropped_windows=dropped,
        top=top,
        imputed_avg_ms=imputed_avg,
        imputed_windows=imputed_count,
        stale_names=stale_names,
    )


def format_ms(value: float) -> str:
    if value >= 60_000:
        return f"{value / 60_000:.1f} min"
    if value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{value:.0f} ms"


def print_report(report: Report) -> dict[str, object]:
    for item in report.files:
        print(
            f"文件：{item.path}  派发 {item.dispatches}（命中 {item.hits}，"
            f"未命中轮 {item.misses}，剔除 >{report.max_ms} ms {item.hangs}）"
        )
        if item.span is not None:
            start, end = item.span
            print(f"      时间 {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}")
        if item.baseline is not None:
            base = item.baseline
            if base.version:
                if base.source == "时间点":
                    print(
                        f"      基线 时间点 {base.version}"
                        f"（附近没有「资源版本」标记，按会话起点的 git 修订）"
                    )
                else:
                    source = f"，{base.source}" if base.source else ""
                    updates = f"｜会话内变过 {base.updates} 次" if base.updates else ""
                    print(f"      基线 {base.version}{source}{updates}")
                if base.reason:
                    print(f"      查不了：{base.reason}")
                if base.stale:
                    stale = [
                        name
                        for name in base.stale
                        if not report.modules or name in (report.pipeline or {})
                    ]
                    if stale:
                        shown = "、".join(stale[:4])
                        if len(stale) > 4:
                            shown += "…"
                        print(
                            f"      改过 {len(stale)} 个派发者：{shown}（只标注，时长仍计入）"
                        )
            elif base.reason:
                print(f"      基线：{base.reason}")
        if item.skipped:
            print(f"      跳过读不出的 OnEventNotify 行 {item.skipped}")
    print()

    if report.modules:
        print(
            f"模块：{'、'.join(report.modules)}"
            f"（只统计该 pipeline 文件里的派发者，节点 {len(report.pipeline or {})} 个）"
        )
        if report.dropped_windows:
            print(
                f"      另有 {report.dropped_windows} 个窗口的派发者不在该模块，已排除"
                f"（属于别的模块，或已从 pipeline 删除、归不到模块）"
            )
        if not report.windows:
            print(f"      这份日志里没有 {'、'.join(report.modules)} 的派发窗口")
        print()

    print(f"按「命中前失败候选数」分桶（窗口 {len(report.windows)} 个）：")
    medians: dict[int, float] = {}
    for bucket in sorted(report.buckets):
        values = report.buckets[bucket]
        if not values:
            continue
        median = statistics.median(values)
        medians[bucket] = median
        print(
            f"  {bucket_label(bucket):>2} 个：n={len(values):4d}  "
            f"中位 {median:8.0f} ms  p90 {percentile(values, 0.9):8.0f} ms"
        )

    x_bucket = bucket_of(report.x)
    sample_n = {bucket: len(report.buckets.get(bucket, [])) for bucket in (0, 1, x_bucket)}
    if all(count >= report.min_samples for count in sample_n.values()):
        total = weighted(medians[0], medians[1], medians[x_bucket])
        print(
            f"\n完整时间 = t0×20% + t1×30% + t{report.x}×50% = {total:.0f} ms"
            f"（n={sample_n[0]}/{sample_n[1]}/{sample_n[x_bucket]}）"
        )
    else:
        missing = "、".join(
            f"{bucket_label(bucket)}（n={sample_n[bucket]}）"
            for bucket, count in sample_n.items()
            if count < report.min_samples
        )
        print(f"\n完整时间：样本不足 {missing}，不算（门槛 {report.min_samples}）")

    empty = [window for window in report.windows if window.empty_rounds > 0]
    print(
        f"整轮空转：命中前空转的窗口 {len(empty)} 个"
        + (
            f"（最多 {max(window.empty_rounds for window in empty)} 轮，"
            f"中位耗时 {statistics.median([window.ms for window in empty]):.0f} ms）"
            if empty
            else ""
        )
        + f"；整轮未命中 {sum(item.misses for item in report.files)} 次（多数以任务结束/超时收尾）"
    )

    by_node: dict[str, list[Window]] = defaultdict(list)
    for window in report.windows:
        by_node[window.parent].append(window)
    present = {window.parent for window in report.all_windows}
    missing, unseen = node_flags(present, report.pipeline)
    ranked = sorted(
        by_node.items(), key=lambda item: sum(window.ms for window in item[1]), reverse=True
    )
    limit = len(ranked) if report.top <= 0 else min(report.top, len(ranked))

    print("\n派发者（按窗口总耗时排序）：")
    node_rows: list[dict[str, object]] = []
    for name, windows in ranked:
        values = [window.ms for window in windows]
        flags = [0, 0, 0]
        for window in windows:
            bucket = bucket_of(window.fails)
            flags[0 if bucket == 0 else 1 if bucket == 1 else 2] += 1
        if report.pipeline is None:
            state = "—"
        elif name in report.pipeline:
            state = "是"
        elif is_framework(name):
            state = "框架"
        else:
            state = "否"
        imputed = (
            report.pipeline is not None
            and name not in report.pipeline
            and not is_framework(name)
        )
        raw_median = statistics.median([window.raw_ms for window in windows])
        node_rows.append(
            {
                "name": name,
                "in_pipeline": None if report.pipeline is None else name in report.pipeline,
                "framework": is_framework(name),
                "imputed": imputed,
                "windows": len(windows),
                "median_ms": round(statistics.median(values), 3),
                "raw_median_ms": round(raw_median, 3) if imputed else None,
                "p90_ms": round(percentile(values, 0.9), 3),
                "fails_0": flags[0],
                "fails_1": flags[1],
                "fails_2plus": flags[2],
                "stale": name in report.stale_names,
            }
        )
        if len(node_rows) <= limit:
            median_text = f"{statistics.median(values):.0f} ms"
            if imputed:
                median_text += f"（原 {raw_median:.0f}）"
            marker = " 改" if name in report.stale_names else ""
            print(
                f"  {name} | 当前 {state}{marker} | n={len(values)} | "
                f"中位 {median_text} | p90 {percentile(values, 0.9):.0f} ms | "
                f"0/1/2+ = {flags[0]}/{flags[1]}/{flags[2]}"
            )
    if limit < len(ranked):
        print(f"  …… 还有 {len(ranked) - limit} 个（--top 0 看全部）")

    if report.pipeline is None:
        print("节点存在性：没读当前 pipeline（--no-pipeline），未核对。")
    else:
        if missing:
            shown = "、".join(missing[:5]) + ("…" if len(missing) > 5 else "")
            if report.imputed_avg_ms is not None:
                note = (
                    f"按现有节点的平均 {report.imputed_avg_ms:.0f} ms 计入"
                    f"（{report.imputed_windows} 个窗口；原时长不参与平均）"
                )
            else:
                note = "没有可用的平均时间，仍按原时长计入"
            print(
                f"节点对不上：日志里有 {len(missing)} 个派发者当前 pipeline 没有（{shown}），{note}。"
            )
        if unseen:
            shown = "、".join(unseen[:5]) + ("…" if len(unseen) > 5 else "")
            in_log = len(present & set(report.pipeline))
            print(
                f"当前 pipeline 共 {len(report.pipeline)} 个节点，本次日志派发了 {in_log} 个；"
                f"其余 {len(unseen)} 个没跑到或新增（{shown}；不参与统计）。"
            )
        if not missing and not unseen:
            print("节点存在性：日志里的派发者都能在当前 pipeline 找到。")

    if report.stale_names:
        shown = "、".join(report.stale_names[:5])
        if len(report.stale_names) > 5:
            shown += "…"
        print(
            f"\n过时：{len(report.stale_names)} 个派发者在日志之后改过（{shown}）"
            f"——只标注，时长仍计入；节点表里带「改」"
        )

    weighted_value = (
        round(weighted(medians[0], medians[1], medians[x_bucket]), 3)
        if all(count >= report.min_samples for count in sample_n.values())
        else None
    )
    return {
        "buckets": {
            bucket_label(bucket): {
                "n": len(values),
                "median_ms": round(statistics.median(values), 3),
                "p90_ms": round(percentile(values, 0.9), 3),
            }
            for bucket, values in sorted(report.buckets.items())
        },
        "weighted_ms": weighted_value,
        "empty_round_windows": len(empty),
        "modules": report.modules,
        "dropped_windows": report.dropped_windows,
        "stale_nodes": report.stale_names,
        "nodes": node_rows,
        "missing_in_pipeline": missing,
        "unseen_in_pipeline": unseen,
        "imputed_avg_ms": (
            round(report.imputed_avg_ms, 3)
            if report.imputed_avg_ms is not None
            else None
        ),
        "imputed_windows": report.imputed_windows,
    }
