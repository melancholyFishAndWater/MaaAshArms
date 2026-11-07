"""`rounds.py` 的实现：真实日志的派发成本统计。

入口仍是 `tools/perf/lib/rounds.py`（命令行与用法见 `tools/perf/README.md`）；
这里按职责拆成 consts / logs / scope / report / cli 五块，避免单文件过长。
"""

from __future__ import annotations

from .cli import main

__all__ = ["main"]
