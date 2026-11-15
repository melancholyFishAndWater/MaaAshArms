"""CLI 显式参数的冲突与依赖检查；在任何业务副作用前调用。"""
from __future__ import annotations

import argparse
from collections.abc import Sequence


def check_explicit_args(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    *,
    conflicts: Sequence[tuple[str, ...]] = (),
    requires: Sequence[tuple[str, str]] = (),
) -> None:
    """只检查实际传入的选项（含 --key=value）；不把默认值视为用户意图。"""
    supplied = {token.split("=", 1)[0] for token in argv if token.startswith("--")}
    for group in conflicts:
        present = [flag for flag in group if flag in supplied]
        if len(present) > 1:
            parser.error("参数互斥：" + "、".join(present))
    for flag, dependency in requires:
        if flag in supplied and dependency not in supplied:
            parser.error(f"{flag} 必须配合 {dependency} 使用")
