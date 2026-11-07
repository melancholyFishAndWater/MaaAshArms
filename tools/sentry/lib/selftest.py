#!/usr/bin/env python
"""零网络的逻辑自检：用合成 fixtures 校验 release 解析与统计口径。

为什么要有这个文件：真正的报告要联网 + 已登录 Sentry 才能跑，而项目里最容易
静默出错的恰恰是**纯逻辑部分**——release 双 ``@`` 拼接解析、任务表与失败标记分流、
Δpp 方向、工件往返。这些不需要网络，用固定输入断言固定输出即可。

被 ``cli.py selftest`` 与 CI 的 ``tools/validation/lib/check_sentry_offline.py`` 共用，
后者额外渲染一次 console / markdown / json 报告并断言包含关键行——所以本文件除了
``run_selftest``，还要把 CI 用到的 fixtures（``MEASUREMENTS`` / ``RELEASE_D38`` /
``V36``~``V38``）re-export 出来。

实现分在 ``tools/sentry/selftests/`` 包（core / drawing / flow / telemetry /
evidence / reports，断言助手在 ``asserts.py``，固定输入在 ``fixtures.py``）；
本文件只做入口兼容（包内相对导入失败时退回顶层导入）与退出口。
"""

from __future__ import annotations


try:
    from .selftests import run_selftest
    from .selftests.fixtures import MEASUREMENTS, RELEASE_D38, V36, V37, V38
except ImportError:  # tools/sentry 在 sys.path 上时按顶层模块导入
    from lib.selftests import run_selftest  # type: ignore[no-redef]
    from lib.selftests.fixtures import (  # type: ignore[no-redef]
        MEASUREMENTS,
        RELEASE_D38,
        V36,
        V37,
        V38,
    )

__all__ = [
    "MEASUREMENTS",
    "RELEASE_D38",
    "V36",
    "V37",
    "V38",
    "run_selftest",
]
