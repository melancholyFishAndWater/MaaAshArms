"""`tools/sentry/lib/selftest.py` 的实现：零网络自检，按被测对象分模块。

入口仍是 `python -m tools.sentry.cli selftest`（以及 CI 的
`tools/validation/lib/check_sentry_offline.py`）；这里只 re-export `run_selftest`。
"""

from __future__ import annotations

from .cli import run_selftest

__all__ = ["run_selftest"]
