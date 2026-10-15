"""把 Sentry 遥测聚合结果下载成可离线复用的 JSON 工件。

用法分两类：

- ``--name X --field ...`` 手工任意查询（``--query`` / ``--sort`` 可选）；
- ``--preset {task-failure,task-trend,context,all}`` 拉齐标准报告所需的全部查询，
  之后 ``task-failure`` / ``task-trend`` / ``context`` 都能用 ``--input`` 离线出报告。

落盘结构见 `report_common.ARTIFACT_SCHEMA`；字段名会被换成中性名（``span.description`` →
``description``、``count_unique(trace)`` → ``_count``），读回时不依赖 Sentry 的字段拼写。
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    from .config import CONFIG
except ImportError:
    from config import CONFIG

try:
    from .report_common import (
        DEFAULT_SENTRY_TIMEOUT_SECONDS,
        Artifact,
        Measurement,
        SpanQuery,
        authority_error_hint,
        default_artifact_path,
        explore_all,
        module_summary,
        resolve_sentry_command,
        save_artifact,
        show_progress,
        to_neutral_rows,
        utc_now_iso,
        warn_on_truncated_period,
    )
except ImportError:
    from report_common import (
        DEFAULT_SENTRY_TIMEOUT_SECONDS,
        Artifact,
        Measurement,
        SpanQuery,
        authority_error_hint,
        default_artifact_path,
        explore_all,
        module_summary,
        resolve_sentry_command,
        save_artifact,
        show_progress,
        to_neutral_rows,
        utc_now_iso,
        warn_on_truncated_period,
    )

DEFAULT_TARGET = CONFIG.target
DEFAULT_PERIOD = "7d"
DEFAULT_PRESET = "all"

# umbrella span 过滤：只保留「一次完整运行」的根 span。
UMBRELLA_FILTER = f"span.description:[{','.join(CONFIG.task_run_spans)}]" if CONFIG.task_run_spans else ""


@dataclass(frozen=True)
class PresetQuery:
    """一个预设 measurement 的名称与查询。"""

    name: str
    query: SpanQuery


def preset_queries(preset: str) -> list[PresetQuery]:
    """返回预设包含的查询列表；任务表相关的查询不带 release 过滤，口径由本地按版本聚合。"""
    if preset not in {"task-failure", "task-trend", "context", "all"}:
        raise ValueError(f"未知预设 {preset!r}；可选 task-failure / task-trend / context / all。")

    queries: list[PresetQuery] = []
    if preset in {"task-failure", "task-trend", "all"}:
        queries.append(
            PresetQuery(
                "task_total",
                SpanQuery(
                    fields=("span.description", "release", "count_unique(trace)"),
                    query="",
                    sort="-count_unique(trace)",
                ),
            )
        )
        queries.append(
            PresetQuery(
                "task_status",
                SpanQuery(
                    fields=("span.description", "span.status", "release", "count_unique(trace)"),
                    query="",
                    sort="-count_unique(trace)",
                ),
            )
        )
    if preset in {"task-failure", "task-trend", "all"}:
        queries.append(
            PresetQuery(
                "release_users",
                SpanQuery(
                    fields=("release", "count_unique(user)", "count_unique(trace)"),
                    query="",
                    sort="-count_unique(user)",
                ),
            )
        )
    if preset in {"task-failure", "all"} and CONFIG.task_run_spans:
        queries.append(
            PresetQuery(
                "run_total",
                SpanQuery(fields=("release", "count_unique(trace)"), query=UMBRELLA_FILTER, sort="-count_unique(trace)"),
            )
        )
        queries.append(
            PresetQuery(
                "run_failed",
                SpanQuery(
                    fields=("release", "count_unique(trace)"),
                    query=f"{UMBRELLA_FILTER} span.status:internal_error".strip(),
                    sort="-count_unique(trace)",
                ),
            )
        )
    if preset in {"context", "task-failure", "all"}:
        queries.append(
            PresetQuery(
                "runtime_os",
                SpanQuery(
                    fields=("os.name", "release", "count_unique(trace)"),
                    query="",
                    sort="-count_unique(trace)",
                ),
            )
        )
        queries.append(
            PresetQuery(
                "runtime_env",
                SpanQuery(
                    fields=("environment", "release", "count_unique(trace)"),
                    query="",
                    sort="-count_unique(trace)",
                ),
            )
        )
    return queries


def fetch_measurements(
    *,
    sentry_command: str,
    target: str,
    period: str,
    queries: Sequence[PresetQuery],
    fresh: bool = True,
    verbose: bool = False,
    quiet: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> dict[str, Measurement]:
    """并发执行全部查询并转成中性字段名的 measurement。"""
    wanted = list(queries)
    if not wanted:
        return {}
    show_progress(f"查询 {len(wanted)} 组聚合数据（并发上限 4）", quiet=quiet)
    results = explore_all(
        sentry_command,
        target=target,
        period=period,
        queries=[item.query for item in wanted],
        fresh=fresh,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )
    measurements: dict[str, Measurement] = {}
    for item, rows in zip(wanted, results, strict=True):
        measurements[item.name] = Measurement(
            name=item.name,
            rows=to_neutral_rows(rows, item.query.fields),
            fields=item.query.fields,
            query=item.query.query,
            sort=item.query.sort,
        )
    return measurements


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=module_summary(__doc__))
    parser.add_argument("--name", help="手工查询的 measurement 名（与 --field 一起用）")
    parser.add_argument(
        "--field",
        action="append",
        default=None,
        metavar="FIELD",
        help="Sentry 查询字段或聚合，可重复，例如 --field span.description --field count_unique(trace)",
    )
    parser.add_argument("--query", default="", help="Sentry 搜索查询（可选）")
    parser.add_argument("--sort", default=None, help="排序字段，前缀 - 表示降序（可选）")
    parser.add_argument(
        "--preset",
        choices=("task-failure", "task-trend", "context", "all"),
        default=None,
        help=f"拉齐某个标准报告所需的全部查询（默认 {DEFAULT_PRESET}；指定 --name 时忽略本参数）",
    )
    parser.add_argument("--target", default=DEFAULT_TARGET, help="<org>/<project>")
    parser.add_argument("--period", default=DEFAULT_PERIOD, help=f"查询范围（默认 {DEFAULT_PERIOD}）")
    parser.add_argument("-o", "--output", help="落盘路径（默认 temp/telemetry/<project>-<period>-<时间戳>.json）")
    parser.add_argument(
        "--no-fresh",
        action="store_true",
        help="复用 Sentry CLI 本地缓存而不拉取最新数据：同一查询约快 3 倍",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_SENTRY_TIMEOUT_SECONDS,
        help=f"单次 Sentry 查询超时秒数（默认 {DEFAULT_SENTRY_TIMEOUT_SECONDS:g}）",
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

    if arguments.name:
        if not arguments.field:
            raise ValueError("手工查询必须给出至少一个 --field。")
        queries = [
            PresetQuery(
                arguments.name,
                SpanQuery(
                    fields=tuple(arguments.field),
                    query=arguments.query,
                    sort=arguments.sort,
                ),
            )
        ]
    else:
        queries = preset_queries(arguments.preset or DEFAULT_PRESET)

    measurements = fetch_measurements(
        sentry_command=resolve_sentry_command(),
        target=arguments.target,
        period=arguments.period,
        queries=queries,
        fresh=not arguments.no_fresh,
        verbose=arguments.verbose,
        quiet=arguments.quiet,
        timeout_seconds=arguments.timeout,
    )

    artifact = Artifact(
        target=arguments.target,
        period=arguments.period,
        measurements=measurements,
        generated_at=utc_now_iso(),
    )
    destination = Path(arguments.output) if arguments.output else default_artifact_path(
        arguments.target, arguments.period
    )
    written = save_artifact(artifact, destination)

    total_rows = sum(len(measurement.rows) for measurement in measurements.values())
    print(f"已下载 {len(measurements)} 组聚合、共 {total_rows} 行遥测数据")
    for name, measurement in measurements.items():
        print(f"  {name}: {len(measurement.rows)} 行")
    print(f"工件: {written}")
    if total_rows == 0:
        print(
            "提示: 全部 measurement 都是 0 行。可能是该时间窗内没有上报数据，"
            "或 target/period 写错；用 --verbose 看实际查询命令。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        message = authority_error_hint(error) if isinstance(error, RuntimeError) else str(error)
        print(f"错误：{message}", file=sys.stderr)
        raise SystemExit(1) from error
