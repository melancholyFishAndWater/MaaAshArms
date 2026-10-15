"""统一入口：下载遥测数据、跑统计分析、出报告。

    python -m tools.sentry.cli <子命令> ...

子命令：

- ``fetch``         下载遥测聚合数据到 JSON 工件（可离线复用）
- ``task-failure``  单版本任务失败率深度体检
- ``task-trend``    跨版本失败率趋势与 Δpp
- ``context``       只看运行环境（os / environment）分布
- ``evidence``      把失败现场图与那次运行的日志拉到本地（不画图，只打印命令）
- ``explore-csv``   把浏览器导出的 Explore CSV 还原成日志正文 + 清单
- ``flow``          把本地日志证据包渲染成流程图（产出落到 temp/flow/）
- ``selftest``      零网络的纯逻辑自检（校验 release 解析与统计口径）

前置条件：装好并登录 Sentry CLI（``pnpm add -g sentry`` 后执行一次 ``sentry auth login``）。
``explore-csv`` 与 ``flow`` 不联网：前者读浏览器下载的 CSV，后者读本地证据；``evidence`` 要联网
（它反过来把遥测侧的图片与日志拉到本地，交给前两者）。三者的产出都在 ``temp/`` 下。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections.abc import Callable, Sequence

# 兄弟模块在本目录；`tools.drawing` 的绝对 import 需要仓库根（tools/ 上溯一级）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from . import evidence, explore_csv, fetch, report_common, task_failure_report, task_trend_report
except ImportError:
    import evidence  # type: ignore[no-redef]
    import explore_csv  # type: ignore[no-redef]
    import fetch  # type: ignore[no-redef]
    import report_common  # type: ignore[no-redef]
    import task_failure_report  # type: ignore[no-redef]
    import task_trend_report  # type: ignore[no-redef]

try:
    from tools.drawing import flow_cases
except ImportError:
    import flow_cases  # type: ignore[no-redef]

ReportMain = Callable[..., int]

REPORTS: dict[str, tuple[str, ReportMain]] = {
    "fetch": ("下载遥测聚合数据到 JSON 工件", fetch.main),
    "task-failure": ("单版本任务失败率深度体检", task_failure_report.main),
    "task-trend": ("跨版本失败率趋势与 Δpp 对比", task_trend_report.main),
    "context": ("只看运行环境分布（os / environment）", task_failure_report.main),
    "evidence": ("把失败现场图与那次运行的日志拉到本地", evidence.main),
    "explore-csv": ("把 Explore 导出的 CSV 还原成日志正文 + 清单", explore_csv.main),
    "flow": ("把本地证据包（日志 + 图片）渲染成流程图", flow_cases.main),
}

CONTEXT_DEFAULTS = ("--sections", "runtime")


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "python -m tools.sentry.cli",
        description="下载 Sentry 遥测数据并生成统计分析报告。",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (description, _main) in REPORTS.items():
        subparsers.add_parser(name, help=description, description=description)
    subparsers.add_parser("selftest", help="零网络的纯逻辑自检", description="零网络的纯逻辑自检")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = create_argument_parser()
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0

    command = arguments[0]
    if command == "selftest":
        try:
            from .selftest import run_selftest
        except ImportError:
            from selftest import run_selftest  # type: ignore[no-redef]
        return run_selftest()

    report = REPORTS.get(command)
    if report is None:
        parser.error(f"未知子命令：{command}")

    _description, report_main = report
    forwarded = list(arguments[1:])
    if command == "context":
        # context 复用 task-failure 的报告体，只换默认排序：按运行量排，且不限行数。
        forwarded = [*CONTEXT_DEFAULTS, *forwarded]
    try:
        with report_common.report_run_guard():
            return report_main(forwarded, f"{parser.prog} {command}")
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        message = report_common.authority_error_hint(error) if isinstance(error, RuntimeError) else str(error)
        print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
