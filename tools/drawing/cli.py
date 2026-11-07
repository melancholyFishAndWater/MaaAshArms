#!/usr/bin/env python
"""tools/drawing 的统一入口：日志 → 流程图、证据包批量出图、白板图片。

    python tools/drawing/cli.py flow <日志> [--vision ...] …
    python tools/drawing/cli.py cases --case <证据包> …
    python tools/drawing/cli.py whiteboard …

三个子命令分别对应原来的 `flow_from_log.py` / `flow_cases.py` / `whiteboard_images.py`，
参数、帮助文本与退出码原样转发（实现都在 `lib/` 下）。不带子命令或 `-h` 时只打印这份总览。
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

# 子命令 → (一句话说明, 实现模块:main)。用字符串延迟导入：`bench` 这类依赖 maafw/numpy 的
# 模块只在真正用到时才 import，`--help` 不会因为它们缺库而失败。
COMMANDS: dict[str, tuple[str, str]] = {
    "flow": (
        "把 maafw.log 还原成执行流程图（原 flow_from_log.py）",
        "tools.drawing.lib.flow_from_log:main",
    ),
    "cases": (
        "把证据包渲染成流程图 / 遥测批量出图（原 flow_cases.py）",
        "tools.drawing.lib.flow_cases:main",
    ),
    "whiteboard": (
        "白板图片整理（原 whiteboard_images.py）",
        "tools.drawing.lib.whiteboard_images:main",
    ),
}


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "python tools/drawing/cli.py",
        description="日志 → 流程图、证据包批量出图、白板图片。",
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
