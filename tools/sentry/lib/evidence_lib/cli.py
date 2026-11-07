"""命令行：参数解析与 `main()`（子命令 `evidence` 的入口）。

参数名、帮助文本、退出码都是对外契约，改动要同步 `tools/sentry/README.md`。
"""

from __future__ import annotations

import sys

from ..config import CONFIG
from .. import report_common
from .constants import DEFAULT_LIMIT
from .constants import DEFAULT_LOG_LIMIT
from .constants import DEFAULT_MAX_EVENTS
from .constants import DEFAULT_OUTDIR
from .constants import DEFAULT_PERIOD
from .model import Evidence
from .constants import LOG_SORTS
from .constants import MAX_LOG_LIMIT
from pathlib import Path
from collections.abc import Sequence
import argparse
from .collect import collect
from .collect import format_result
from .collect import format_survey
import io
# argparse 的 description 是 `evidence.py` 模块 docstring 的首行。拆分后不能再用本模块的
# `__doc__`（那会变成包内模块的说明），所以原样存一份；改这句等于改 `--help`，属于对外契约。
DESCRIPTION = (
    "把某个失败的证据拉到本地：现场图（附件）+ **那次运行**的日志，并打印能直接跑的绘图命令。"
)
from .download import sentry_token
from .download import survey

def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"扫哪段时间的 issue（默认 {DEFAULT_PERIOD}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"最多看几个 issue（默认 {DEFAULT_LIMIT}）",
    )
    parser.add_argument(
        "--issue", action="append", help="只处理这个 issue（如 PYTHON-14，可重复）"
    )
    parser.add_argument(
        "--event",
        action="append",
        help="只处理这个事件 id（可重复；issue 的历史事件用这条）",
    )
    parser.add_argument(
        "--outdir", default=DEFAULT_OUTDIR, help=f"产出目录（默认 {DEFAULT_OUTDIR}）"
    )
    parser.add_argument("--list", action="store_true", help="只报告证据状态，不下载")
    parser.add_argument(
        "--force",
        action="store_true",
        help="已经下过也重新下（只补本次允许下的那些；旧文件不删，仍会记进清单）",
    )
    parser.add_argument("--no-images", action="store_true", help="不下载现场图")
    parser.add_argument("--no-log", action="store_true", help="不取日志")
    parser.add_argument(
        "--log-limit",
        type=int,
        default=DEFAULT_LOG_LIMIT,
        help=f"取多少条日志（上限 {MAX_LOG_LIMIT}，失败在末尾所以默认取最新那段）",
    )
    parser.add_argument(
        "--sort",
        choices=LOG_SORTS,
        default=LOG_SORTS[0],
        help=f"日志取哪一段：{LOG_SORTS[0]}（默认，失败在末尾）/ {LOG_SORTS[1]}",
    )
    parser.add_argument(
        "--keep-duplicates", action="store_true", help="保留完全重复的行（默认去重）"
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=DEFAULT_MAX_EVENTS,
        help=f"现场图按失败事件逐个取，最多看最近几个事件（默认 {DEFAULT_MAX_EVENTS}）。"
        "一个 issue 常有多次失败、各一份 on_error 包，只取 issue view 那一个事件会只有最旧的那份",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="单次 Sentry 调用超时秒数（默认 180）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 sentry 查询命令")
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = create_argument_parser(prog).parse_args(argv)
    if arguments.no_images and arguments.no_log:
        raise ValueError("--no-images 与 --no-log 一起给就没有任何东西可拉。")

    sentry = report_common.resolve_sentry_command()
    try:
        issues = survey(
            sentry,
            CONFIG.target,
            period=arguments.period,
            limit=arguments.limit,
            issues=arguments.issue,
            events=arguments.event,
            verbose=arguments.verbose,
            timeout=arguments.timeout,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if not issues:
        print("没有匹配的 issue。", file=sys.stderr)
        return 0

    if arguments.list:
        format_survey(issues, sys.stdout)
        return 0

    outdir = Path(arguments.outdir)
    outdir = outdir if outdir.is_absolute() else report_common.repo_root() / outdir
    format_survey(issues, sys.stdout)

    token: str | None = None
    collected: list[Evidence] = []
    for issue in issues:
        if not issue.has_anything:
            print(
                f"\n跳过 {issue.short_id}：attachment.status="
                f"{issue.evidence.get('attachment_status') or '未知'}、logs.status="
                f"{issue.evidence.get('logs_status') or '未知'}",
                file=sys.stderr,
            )
            continue
        if issue.images and token is None and not arguments.no_images:
            # token 只取一次；不打印、不落盘、不进清单
            token = sentry_token(
                sentry, verbose=arguments.verbose, timeout=arguments.timeout
            )
        evidence = collect(
            sentry,
            issue,
            outdir=outdir,
            token=token,
            do_images=not arguments.no_images,
            do_log=not arguments.no_log,
            log_limit=arguments.log_limit,
            log_sort=arguments.sort,
            force=arguments.force,
            keep_duplicates=arguments.keep_duplicates,
            verbose=arguments.verbose,
            timeout=arguments.timeout,
            max_events=arguments.max_events,
        )
        collected.append(evidence)
        format_result(evidence, sys.stdout, sys.stderr)

    if collected:
        print(
            f"\n共 {len(collected)} 个证据目录落到 {outdir.as_posix()}", file=sys.stdout
        )
    return 0
