#!/usr/bin/env python
"""tools/perf 的统一入口：真实日志成本统计（`rounds`）与合成下限基准（`bench`）。

    python tools/perf/cli.py rounds <日志或目录> [--module escort] …
    python tools/perf/cli.py bench [模式 …] [--x 2] …

两个子命令分别对应原来的 `rounds.py` / `bench.py`，参数与退出码原样转发（实现都在 `lib/`）。
不带子命令或 `-h` 时只打印这份总览。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 子命令 → (一句话说明, 实现模块:main)。`bench` 依赖 maafw / numpy，只在用到时 import。
COMMANDS: dict[str, tuple[str, str]] = {
    "rounds": (
        "从真实 maafw.log 统计派发成本（原 rounds.py）",
        "tools.perf.lib.rounds:main",
    ),
    "bench": (
        "合成基准：框架下限与加权完整时间（原 bench.py）",
        "tools.perf.lib.bench:main",
    ),
}


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "python tools/perf/cli.py",
        description="性能基准：真实日志统计与合成下限。",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (description, _target) in COMMANDS.items():
        subparsers.add_parser(name, help=description, description=description, add_help=False)
    return parser


def _load_main(target: str) -> Callable[..., int]:
    module_name, _, attribute = target.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _dispatch(target: str, rest: list[str], prog: str) -> int:
    """把剩余参数交给子命令自己的 `main`：它按 `sys.argv` 解析，这里临时换掉再还原。"""
    handler = _load_main(target)
    saved = sys.argv
    sys.argv = [prog, *rest]
    try:
        try:
            code = handler()
        except SystemExit as exc:
            code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    finally:
        sys.argv = saved


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = create_argument_parser()
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    command, rest = arguments[0], arguments[1:]
    entry = COMMANDS.get(command)
    if entry is None:
        parser.error(f"未知子命令：{command}")
    _description, target = entry
    return _dispatch(target, rest, f"{parser.prog} {command}")


if __name__ == "__main__":
    raise SystemExit(main())
