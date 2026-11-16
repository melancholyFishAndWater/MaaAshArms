#!/usr/bin/env python
"""本地日志工具入口：python tools/logs/cli.py inspect PATH [参数...]。"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("inspect", help="日志概览、任务索引和原文检索（JSON）", add_help=False)
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if arguments[0] != "inspect":
        parser.error(f"未知子命令：{arguments[0]}")
    from tools.logs.lib.inspect import main as inspect_main
    return inspect_main(arguments[1:])


if __name__ == "__main__":
    raise SystemExit(main())
