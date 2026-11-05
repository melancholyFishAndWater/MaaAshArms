#!/usr/bin/env python3
"""从真实 `maafw.log` 统计派发成本：按「命中前失败候选数」分桶，算加权完整时间。

    python tools/perf/rounds.py debug/maafw.log
    python tools/perf/rounds.py debug/            # 目录里所有 maafw*.log（含 .bak）
    python tools/perf/rounds.py debug/ --module escort
    python tools/perf/rounds.py debug/maafw.log debug/maafw.bak.*.log --x 2
    python tools/perf/rounds.py debug/maafw.log --mfa-log D:/MaaAshArms/logs
    python tools/perf/rounds.py temp/evidence/PYTHON-2H/debug/maafw.log --json temp/perf/rounds.json
    python tools/perf/rounds.py debug/maafw.log --module escort   # 只看一个模块（escort.json）
    python tools/perf/rounds.py --selftest

数据源是 `Node.NextList.*` / `Node.Recognition.*` 事件流，不猜、不读别的行：

    一次派发 = NextList.Starting → Succeeded / Failed
    命中前失败候选数 = 该次命中里有几个「顶层」Node.Recognition.Failed
    空转轮 = 未命中的 Node.NextList.Failed；整轮空转后还能命中的窗口很少见，单独统计

这比 `bench.py` 的合成基准更接近真实：识别是仓库里的模板/OCR/自定义识别，等待是
真实的 `rate_limit`，截图来自真机。代价是要有一份跑过的日志。

完整时间 = t0×20% + t1×30% + tx×50%（t0/t1/tx 分别是命中前失败 0 / 1 / x 个候选的中位数）。
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import logging
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._logging import configure_tool_logging
from tools._pipeline_state import (
    TS_RE,
    definition_changed,
    git,
    node_definitions,
    revision_before,
    strip_jsonc,
    version_in_effect,
)

log = logging.getLogger("tools.perf.rounds")

# 与 bench.py 的 WEIGHTS 同一口径：0/1/x 三个桶的权重
WEIGHTS = {0: 0.20, 1: 0.30, 2: 0.50}
DEFAULT_X = 2
DEFAULT_MAX_MS = 10_000
DEFAULT_MIN_SAMPLES = 3
MAX_BUCKET = 4
MAX_GAP_S = 10.0
FRAMEWORK_PREFIXES = ("@",)
# MFA 换名后的日志是 `maafw.bak.<换名时刻>.log`；传目录就按这个通配一次全收。
LOG_GLOB = "maafw*.log"
DEFAULT_LOG_DIR = REPO_ROOT / "debug"

LINE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]"
    r"\[([A-Z]+)\]\[Px(\d+)\]\[Tx(\d+)\]\[([^\]]+)\](?:\[L\d+\])?\[([^\]]+)\](.*)$"
)
EVENT_MSG = re.compile(r"\[msg=([A-Za-z.]+)\]")
DETAILS = re.compile(r"\[details=(\{.*\})\]\s*$")


@dataclass
class Dispatch:
    parent: str
    names: list[str]
    t0: datetime
    task_id: str
    ms: int = 0
    ok: bool = False
    closed: bool = False
    fails: int = 0


@dataclass
class Window:
    parent: str
    fails: int
    empty_rounds: int
    ms: float
    raw_ms: float


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def load(path: Path) -> tuple[list[Dispatch], int]:
    """解析一份日志，返回 (派发列表, 跳过的 OnEventNotify 行数)。"""
    dispatches: list[Dispatch] = []
    open_d: dict[tuple[str, str], Dispatch] = {}
    stacks: dict[tuple[str, str], list[tuple[str, bool]]] = defaultdict(list)
    skipped = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if "OnEventNotify" not in raw or "EventDispatcher" not in raw:
                continue
            match = LINE.match(raw.rstrip("\r\n"))
            if not match:
                skipped += 1
                continue
            ts_text, _level, px, tx, _file, _func, body = match.groups()
            msg_match = EVENT_MSG.search(body)
            detail_match = DETAILS.search(body)
            if not msg_match or not detail_match:
                skipped += 1
                continue
            try:
                detail = json.loads(detail_match.group(1))
            except json.JSONDecodeError:
                skipped += 1
                continue

            msg = msg_match.group(1)
            key = (px, tx)
            ts = parse_ts(ts_text)
            task_id = str(detail.get("task_id"))

            if msg == "Node.NextList.Starting":
                dispatch = Dispatch(
                    parent=detail.get("name") or "?",
                    names=[entry.get("name") for entry in detail.get("list") or []],
                    t0=ts,
                    task_id=task_id,
                )
                open_d[key] = dispatch
                dispatches.append(dispatch)
            elif msg in ("Node.NextList.Succeeded", "Node.NextList.Failed"):
                dispatch = open_d.pop(key, None)
                if dispatch and dispatch.task_id == task_id:
                    dispatch.ms = max(0, int((ts - dispatch.t0).total_seconds() * 1000))
                    dispatch.ok = msg.endswith("Succeeded")
                    dispatch.closed = True
            elif msg == "Node.Recognition.Starting":
                name = detail.get("name") or "?"
                stacks[key].append((name, not stacks[key]))
            elif msg in ("Node.Recognition.Succeeded", "Node.Recognition.Failed"):
                name = detail.get("name") or "?"
                stack = stacks[key]
                position = next(
                    (i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name),
                    None,
                )
                if position is None:
                    continue
                top_level = stack[position][1]
                del stack[position:]
                if top_level and msg.endswith("Failed"):
                    dispatch = open_d.get(key)
                    if dispatch and dispatch.task_id == task_id:
                        dispatch.fails += 1
    return dispatches, skipped


def chain_windows(dispatches: list[Dispatch], max_gap_s: float = MAX_GAP_S) -> list[Window]:
    """把「连续未命中轮 + 最后一次命中」并成一个窗口。"""
    ordered = sorted(dispatches, key=lambda d: d.t0)
    windows: list[Window] = []
    for index, dispatch in enumerate(ordered):
        if not dispatch.ok or not dispatch.closed:
            continue
        empty_rounds = 0
        first = dispatch
        cursor = index - 1
        while cursor >= 0:
            previous = ordered[cursor]
            if (
                previous.ok
                or not previous.closed
                or previous.task_id != dispatch.task_id
                or previous.parent != dispatch.parent
            ):
                break
            gap = (first.t0 - (previous.t0 + timedelta(milliseconds=previous.ms))).total_seconds()
            if gap < -1.0 or gap > max_gap_s:
                break
            empty_rounds += 1
            first = previous
            cursor -= 1
        end = dispatch.t0 + timedelta(milliseconds=dispatch.ms)
        total = (end - first.t0).total_seconds() * 1000
        windows.append(
            Window(
                parent=dispatch.parent,
                fails=dispatch.fails,
                empty_rounds=empty_rounds,
                ms=total,
                raw_ms=total,
            )
        )
    return windows


def bucket_of(fails: int) -> int:
    return min(fails, MAX_BUCKET)


def bucket_label(bucket: int) -> str:
    return f"{bucket}+" if bucket >= MAX_BUCKET else str(bucket)


def weighted(t0: float, t1: float, tx: float) -> float:
    return t0 * WEIGHTS[0] + t1 * WEIGHTS[1] + tx * WEIGHTS[2]


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * ratio))]


def pipeline_nodes(root: Path) -> dict[str, str]:
    """当前 pipeline 的顶层节点名 → 文件名；读不到就返回空 dict。"""
    owner: dict[str, str] = {}
    if not root.is_dir():
        return owner
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(
                strip_jsonc(path.read_text(encoding="utf-8", errors="replace"))
            )
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for name in data:
                owner.setdefault(name, path.name)
    return owner


def is_framework(name: str) -> bool:
    """框架合成的伪节点（例如 `@mse/action`），不在 pipeline JSON 里，不算「已删除」。"""
    return name.startswith(FRAMEWORK_PREFIXES)


def normalize_module(value: str) -> str:
    """`escort`、`escort.json`、`pipeline/escort.json` 都归成 `escort`。"""
    name = Path(value.replace("\\", "/")).name.strip()
    if name.lower().endswith(".json"):
        name = name[: -len(".json")]
    return name


def module_scope(
    pipeline: dict[str, str] | None, modules: list[str]
) -> tuple[dict[str, str], list[str]]:
    """按模块名（pipeline 文件名去后缀）收窄节点表，返回 (模块节点表, 找不到的模块)。"""
    scoped: dict[str, str] = {}
    if pipeline is None:
        return scoped, []
    wanted = {normalize_module(item) for item in modules if normalize_module(item)}
    known = {Path(file).stem for file in pipeline.values()}
    unknown = sorted(wanted - known)
    scoped = {name: file for name, file in pipeline.items() if Path(file).stem in wanted}
    return scoped, unknown


def filter_windows(windows: list[Window], nodes: set[str]) -> tuple[list[Window], int]:
    """只留派发者属于这个模块的窗口，返回 (留下的窗口, 被丢掉的数量)。"""
    kept = [window for window in windows if window.parent in nodes]
    return kept, len(windows) - len(kept)


@dataclass
class Baseline:
    """这份日志跑的时候 pipeline 长什么样，以及哪些派发者之后被改过。"""

    version: str = ""
    source: str = ""
    updates: int = 0
    rev: str = ""
    stale: list[str] = field(default_factory=list)
    changed_modules: list[str] = field(default_factory=list)
    reason: str = ""


def discover(specs: list[str]) -> tuple[list[Path], list[str]]:
    """文件 / 目录 / 通配 → 日志清单（未排序、未去重）；第二个值是展开时的提示。"""
    items: list[Path] = []
    notes: list[str] = []
    for spec in specs or [str(DEFAULT_LOG_DIR)]:
        path = Path(spec)
        if path.is_dir():
            found = sorted(path.glob(LOG_GLOB))
            if not found:
                notes.append(f"{path} 下没有 {LOG_GLOB}")
            items.extend(found)
        elif any(ch in spec for ch in "*?["):
            items.extend(sorted(Path(item) for item in glob.glob(spec)))
        else:
            items.append(path)
    return items, notes


def _first_ts(path: Path) -> datetime:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(80):
            line = fh.readline()
            if not line:
                break
            match = TS_RE.match(line)
            if match:
                return parse_ts(match.group(1))
    raise ValueError(f"{path} 里没有行首时间戳")


def _last_ts(path: Path, fallback: datetime) -> datetime:
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - 200_000))
        tail = fh.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        match = TS_RE.match(line)
        if match:
            return parse_ts(match.group(1))
    return fallback


def log_span(path: Path) -> tuple[datetime, datetime]:
    """日志内容的起止时间。文件名里的时刻是**换名**那一刻，不能拿来排序。"""
    first = _first_ts(path)
    return first, _last_ts(path, first)


def dedupe_logs(paths: list[Path]) -> tuple[list[Path], int]:
    """按「内容起点 + 文件大小」去重，返回 (保留的清单, 丢掉的份数)。"""
    seen: set[tuple[datetime, int]] = set()
    kept: list[Path] = []
    for path in paths:
        try:
            key = (_first_ts(path), path.stat().st_size)
        except (OSError, ValueError):
            kept.append(path)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(path)
    return kept, len(paths) - len(kept)


def stale_in(
    names: set[str],
    then: dict[str, dict[str, Any]],
    now: dict[str, dict[str, Any]],
) -> list[str]:
    """`names` 里哪些节点在 `then` 和现在的工作区之间被改过（只比定义）。"""
    owner = {name: module for module, defs in now.items() for name in defs}
    changed: list[str] = []
    for name in sorted(names):
        module = owner.get(name)
        if module is None or module not in then:
            continue
        before = then[module].get(name)
        after = now[module].get(name)
        if before is None or after is None:
            continue
        if definition_changed(before, after):
            changed.append(name)
    return changed


def baseline_for(
    path: Path,
    names: set[str],
    repo: Path,
    cache: dict[str, dict[str, dict[str, Any]]],
    span: tuple[datetime, datetime] | None,
    mfa_log_dir: Path | None = None,
) -> Baseline:
    """这份日志的基线修订 + 改过的节点。

    基线优先取**会话起点**的资源版本（旁边 `logs/log-*.log` 里那行 `资源版本：vX`）；
    版本查不到才退到「会话起点之时 git 里 pipeline 的修订」。
    """
    result = Baseline()
    if span is None:
        result.reason = "读不出日志的时间跨度"
        return result
    start, end = span
    extra = [mfa_log_dir] if mfa_log_dir else []
    version, source, updates = version_in_effect(path, start, end, extra)
    result.version, result.source, result.updates = version, source, updates
    rev = ""
    if version:
        code, tags = git(repo, "tag", "--list", version)
        if code == 0 and version in tags.split():
            rev = version
        else:
            result.reason = f"本地没有 {version} 这个 tag（先 git fetch --tags），改用时间点"
    if rev:
        result.rev = rev
    else:
        rev = revision_before(repo, start)
        if not rev:
            result.reason = result.reason or "不是 git 仓库或日志早于历史，查不了节点改没改过"
            return result
        result.rev = rev[:8]
        if not version:
            result.source = "时间点"
            result.version = rev[:8]
    then = cache.get(rev)
    if then is None:
        then = node_definitions(repo, rev)
        cache[rev] = then
    now = cache.get("")
    if now is None:
        now = node_definitions(repo, None)
        cache[""] = now
    result.stale = stale_in(names, then, now)
    owner = {name: module for module, defs in now.items() for name in defs}
    result.changed_modules = sorted({owner[name] for name in result.stale})
    return result


def node_flags(
    present: set[str], pipeline: dict[str, str] | None
) -> tuple[list[str], list[str]]:
    """日志里的派发者与当前 pipeline 对不上的两类：日志独有 / 当前独有。"""
    if pipeline is None:
        return [], []
    missing = sorted(
        name for name in present if name not in pipeline and not is_framework(name)
    )
    unseen = sorted(name for name in pipeline if name not in present)
    return missing, unseen


def impute_missing(
    windows: list[Window], pipeline: dict[str, str] | None
) -> tuple[float | None, int]:
    """把「当前 pipeline 已没有」的窗口时长换成现有窗口的平均值。

    平均只由现有节点（当前 pipeline 里的 + 框架伪节点）算；被替换的旧时长不参与。
    返回 (平均值, 被替换的窗口数)；没有可替换的或读不到 pipeline 时返回 (None, 0)。
    """
    if pipeline is None:
        return None, 0
    missing = [
        window
        for window in windows
        if window.parent not in pipeline and not is_framework(window.parent)
    ]
    existing = [
        window
        for window in windows
        if window.parent in pipeline or is_framework(window.parent)
    ]
    if not missing or not existing:
        return None, 0
    average = statistics.mean(window.ms for window in existing)
    for window in missing:
        window.ms = average
    return average, len(missing)


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


def run_selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, condition))

    check("桶 0", bucket_of(0) == 0)
    check("桶 1", bucket_of(1) == 1)
    check("桶 4+", bucket_of(9) == MAX_BUCKET)
    check("桶标签", bucket_label(MAX_BUCKET) == "4+")
    check("加权", abs(weighted(100.0, 200.0, 400.0) - 280.0) < 1e-9)
    check("p90", percentile([1.0, 2.0, 3.0, 4.0], 0.9) == 4.0)
    check(
        "JSONC 去注释",
        json.loads(strip_jsonc('{\n  "a": 1, // x\n  "b": [2,], /* y */\n}'))
        == {"a": 1, "b": [2]},
    )
    check(
        "日志独有/当前独有",
        node_flags({"A", "B", "@mse/action"}, {"B": "b.json", "C": "c.json"}) == (["A"], ["C"]),
    )
    check("框架伪节点", is_framework("@mse/action") and not is_framework("TaskEntrust"))
    check("不读 pipeline 不核对", node_flags({"A"}, None) == ([], []))
    check(
        "模块名去后缀",
        normalize_module("escort.json") == normalize_module("escort") == "escort",
    )
    check("模块名去路径", normalize_module("pipeline/escort.json") == "escort")
    scoped, unknown = module_scope({"A": "a.json", "B": "b.json"}, ["a.json"])
    check("模块收窄节点表", scoped == {"A": "a.json"} and unknown == [])
    check("模块不存在", module_scope({"A": "a.json"}, ["zzz"]) == ({}, ["zzz"]))
    kept, dropped = filter_windows(
        [Window("A", 0, 0, 1.0, 1.0), Window("B", 0, 0, 1.0, 1.0)], {"A"}
    )
    check("窗口按模块过滤", len(kept) == 1 and kept[0].parent == "A" and dropped == 1)
    then_defs = {"a.json": {"A": {"box": [0, 0, 1, 1]}, "B": {"box": [0, 0, 1, 1]}}}
    now_defs = {"a.json": {"A": {"box": [0, 0, 2, 2]}, "B": {"box": [0, 0, 1, 1]}}}
    check(
        "改过的派发者",
        stale_in({"A", "B", "C"}, then_defs, now_defs) == ["A"],
    )

    existing_window = Window("A", 0, 0, 100.0, 100.0)
    missing_window = Window("Old", 0, 0, 900.0, 900.0)
    average, count = impute_missing([existing_window, missing_window], {"A": "a.json"})
    check("缺失节点按平均", average == 100.0 and count == 1 and missing_window.ms == 100.0)
    check("缺失节点保留原值", missing_window.raw_ms == 900.0)
    framework_window = Window("@mse/action", 0, 0, 50.0, 50.0)
    check(
        "框架节点不算缺失",
        impute_missing([framework_window, existing_window], {"A": "a.json"}) == (None, 0),
    )
    check("不读 pipeline 不补", impute_missing([missing_window], None) == (None, 0))

    base = datetime(2026, 1, 1, 0, 0, 0)
    dispatches = [
        Dispatch("A", ["B"], base, "1", ms=100, ok=False, closed=True),
        Dispatch("A", ["B"], base + timedelta(milliseconds=200), "1", ms=100, ok=False, closed=True),
        Dispatch("A", ["B"], base + timedelta(milliseconds=400), "1", ms=150, ok=True, closed=True, fails=2),
    ]
    windows = chain_windows(dispatches)
    check("窗口数量", len(windows) == 1)
    check("空转轮数", bool(windows) and windows[0].empty_rounds == 2)
    check("窗口耗时", bool(windows) and abs(windows[0].ms - 550.0) < 1e-6)
    check("失败候选数", bool(windows) and windows[0].fails == 2)
    check("窗口带派发者", bool(windows) and windows[0].parent == "A")

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="从真实 maafw.log 统计派发成本（按命中前失败候选数分桶）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "logs",
        nargs="*",
        help=f"日志文件、目录或通配（目录只收 {LOG_GLOB}；不给则扫 debug/）",
    )
    parser.add_argument(
        "--mfa-log",
        default=None,
        metavar="DIR",
        help="MFA 界面日志目录（`logs/log-*.log`）；单独拷出来的 maafw.log 用这个指过去",
    )
    parser.add_argument("--x", type=int, default=DEFAULT_X, help="第三桶的失败候选数（默认 2）")
    parser.add_argument(
        "--max-ms",
        type=int,
        default=DEFAULT_MAX_MS,
        help=f"超过这个耗时的窗口当挂起剔除（默认 {DEFAULT_MAX_MS}）",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"三个桶各自的最低样本数，不够不算完整时间（默认 {DEFAULT_MIN_SAMPLES}）",
    )
    parser.add_argument(
        "--pipeline-dir",
        default=str(REPO_ROOT / "assets" / "resource" / "pipeline"),
        help="当前 pipeline 目录，用于核对派发者是否还存在（默认 assets/resource/pipeline）",
    )
    parser.add_argument(
        "--module",
        action="append",
        metavar="NAME",
        help="只看某个模块：pipeline 文件名去 .json（escort / escort.json 都可以；可重复）",
    )
    parser.add_argument(
        "--no-pipeline",
        action="store_true",
        help="不读当前 pipeline，跳过「节点是否还存在」的核对与平均值补齐",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="派发者表最多显示几个（默认 10；0 = 全部）",
    )
    parser.add_argument("--json", default=None, help="把结果写成 JSON 到这个路径")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="工具日志级别（默认 INFO）",
    )
    parser.add_argument("--log-file", default=None, help="工具日志文件；传 none 只走控制台")
    parser.add_argument("--selftest", action="store_true", help="只跑纯逻辑自检")
    return parser


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.x < 0 or args.max_ms <= 0 or args.min_samples < 1 or args.top < 0:
        log.error(
            "--x 必须 >= 0，--max-ms 必须 > 0，--min-samples 必须 >= 1，--top 必须 >= 0"
        )
        return 2

    _, log_path = configure_tool_logging(
        "perf_rounds", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)

    found, notes = discover(args.logs)
    for note in notes:
        log.warning(note)
    for path in found:
        if not path.is_file():
            log.error("找不到日志：%s", path)
            return 2
    if not found:
        log.error("没有找到日志：%s", "、".join(args.logs) or str(DEFAULT_LOG_DIR))
        return 2
    paths, duplicates = dedupe_logs(found)
    if duplicates:
        log.info("跳过重复日志 %d 份", duplicates)
    try:
        paths.sort(key=lambda item: log_span(item)[0])
    except (OSError, ValueError) as exc:
        log.error("读日志时间失败：%s", exc)
        return 1

    modules = [normalize_module(item) for item in args.module or []]
    if modules and args.no_pipeline:
        log.error("--module 要读当前 pipeline 才能定位模块，不能和 --no-pipeline 一起用")
        return 2
    pipeline = None if args.no_pipeline else pipeline_nodes(Path(args.pipeline_dir))
    if pipeline is not None and not pipeline:
        log.warning("当前 pipeline 目录里没读到节点：%s", args.pipeline_dir)
    scoped, unknown = module_scope(pipeline, modules)
    if unknown:
        log.error(
            "当前 pipeline 里没有这些模块（传文件名去 .json）：%s；目录 %s",
            "、".join(unknown),
            args.pipeline_dir,
        )
        return 2
    pipeline = scoped if modules else pipeline

    try:
        report = build_report(
            paths,
            max_ms=args.max_ms,
            x=args.x,
            min_samples=args.min_samples,
            pipeline=pipeline,
            modules=modules,
            top=args.top,
            repo=REPO_ROOT,
            mfa_log_dir=Path(args.mfa_log) if args.mfa_log else None,
        )
    except OSError as exc:
        log.error("读日志失败：%s", exc)
        return 1

    payload = print_report(report)

    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "tool": "tools/perf/rounds.py",
                    "files": [
                        {
                            "path": str(item.path),
                            "dispatches": item.dispatches,
                            "hits": item.hits,
                            "misses": item.misses,
                            "hangs": item.hangs,
                            "skipped": item.skipped,
                            "span": (
                                [
                                    item.span[0].isoformat(sep=" "),
                                    item.span[1].isoformat(sep=" "),
                                ]
                                if item.span is not None
                                else None
                            ),
                            "baseline": (
                                {
                                    "version": item.baseline.version,
                                    "source": item.baseline.source,
                                    "updates": item.baseline.updates,
                                    "rev": item.baseline.rev,
                                    "stale": item.baseline.stale,
                                    "changed_modules": item.baseline.changed_modules,
                                    "reason": item.baseline.reason,
                                }
                                if item.baseline is not None
                                else None
                            ),
                        }
                        for item in report.files
                    ],
                    "x": args.x,
                    "max_ms": args.max_ms,
                    **payload,
                },
                ensure_ascii=False,
                indent=4,
            ),
            encoding="utf-8",
        )
        print(f"JSON：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
