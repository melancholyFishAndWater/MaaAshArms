#!/usr/bin/env python
"""tools/validation 的统一入口：四个校验脚本。

    python tools/validation/cli.py check-python [--root tools] …
    python tools/validation/cli.py validate-refs [--self-test] …
    python tools/validation/cli.py validate-schema --schema-dir deps/tools …
    python tools/validation/cli.py check-sentry-offline

子命令分别对应原来的 `check_python.py` / `validate_refs.py` / `validate_schema.py` /
`check_sentry_offline.py`，参数、帮助文本与退出码原样转发（实现都在 `lib/`）。
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

# 子命令 → (一句话说明, 实现模块:main)。`validate-schema` 依赖 jsonschema，延迟到用到时再 import。
COMMANDS: dict[str, tuple[str, str]] = {
    "check-python": (
        "Python 源码规范检查（原 check_python.py）",
        "tools.validation.lib.check_python:main",
    ),
    "validate-refs": (
        "pipeline / interface / agent 引用一致性校验（原 validate_refs.py）",
        "tools.validation.lib.validate_refs:main",
    ),
    "validate-schema": (
        "按 deps/tools 的 JSON Schema 校验资源文件（原 validate_schema.py）",
        "tools.validation.lib.validate_schema:main",
    ),
    "check-sentry-offline": (
        "遥测工具的离线自检（原 check_sentry_offline.py）",
        "tools.validation.lib.check_sentry_offline:main",
    ),
}


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "python tools/validation/cli.py",
        description="仓库校验：Python 规范、引用一致性、JSON Schema、遥测离线自检。",
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
