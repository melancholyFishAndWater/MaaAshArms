#!/usr/bin/env python3
"""从真实 `maafw.log` 统计派发成本：按「命中前失败候选数」分桶，算加权完整时间。

    python tools/perf/cli.py rounds debug/maafw.log
    python tools/perf/cli.py rounds debug/            # 目录里所有 maafw*.log（含 .bak）
    python tools/perf/cli.py rounds debug/ --module escort
    python tools/perf/cli.py rounds debug/maafw.log debug/maafw.bak.*.log --x 2
    python tools/perf/cli.py rounds debug/maafw.log --mfa-log D:/MaaAshArms/logs
    python tools/perf/cli.py rounds temp/evidence/PYTHON-2H/debug/maafw.log --json temp/perf/rounds.json
    python tools/perf/cli.py rounds debug/maafw.log --module escort   # 只看一个模块（escort.json）
    python tools/perf/cli.py rounds --selftest

数据源是 `Node.NextList.*` / `Node.Recognition.*` 事件流，不猜、不读别的行：

    一次派发 = NextList.Starting → Succeeded / Failed
    命中前失败候选数 = 该次命中里有几个「顶层」Node.Recognition.Failed
    空转轮 = 未命中的 Node.NextList.Failed；整轮空转后还能命中的窗口很少见，单独统计

这比 `bench.py` 的合成基准更接近真实：识别是仓库里的模板/OCR/自定义识别，等待是
真实的 `rate_limit`，截图来自真机。代价是要有一份跑过的日志。

完整时间 = t0×20% + t1×30% + tx×50%（t0/t1/tx 分别是命中前失败 0 / 1 / x 个候选的中位数）。

实现分在 `tools/perf/rounds_lib/` 五个模块（consts / logs / scope / report / cli）；
本文件只做仓库根引导并调用 `main()`，口径与参数见 `tools/perf/README.md`。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.perf.lib.rounds_lib.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
