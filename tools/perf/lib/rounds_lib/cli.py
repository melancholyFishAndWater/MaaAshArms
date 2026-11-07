"""命令行：参数解析、多日志发现与去重、自检、JSON 产出。

流程：discover → dedupe → 按内容起点排序 → build_report（logs + scope）→ print_report。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from datetime import timedelta
from datetime import datetime, timedelta
from pathlib import Path

from tools._logging import configure_tool_logging
from tools._pipeline_state import strip_jsonc

from .consts import (
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_MS,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_X,
    LOG_GLOB,
    MAX_BUCKET,
    REPO_ROOT,
)

from .logs import (
    Dispatch,
    Window,
    bucket_label,
    bucket_of,
    chain_windows,
    percentile,
    weighted,
)

from .report import build_report, print_report

from .scope import (
    dedupe_logs,
    discover,
    filter_windows,
    impute_missing,
    is_framework,
    log_span,
    module_scope,
    node_flags,
    normalize_module,
    pipeline_nodes,
    stale_in,
)

log = logging.getLogger("tools.perf.rounds")

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
                    "tool": "tools/perf/lib/rounds.py",
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
