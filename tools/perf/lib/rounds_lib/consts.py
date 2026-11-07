"""rounds 的常量与正则：命令行默认值、桶权重、日志行格式。

只放"到处都要用、又不依赖别的模块"的东西；改这里等于改口径，记得同步 README。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

WEIGHTS = {0: 0.20, 1: 0.30, 2: 0.50}
DEFAULT_X = 2
DEFAULT_MAX_MS = 10_000
DEFAULT_MIN_SAMPLES = 3
MAX_BUCKET = 4
MAX_GAP_S = 10.0
FRAMEWORK_PREFIXES = ("@",)
# MFA 换名后的日志是 `maafw.bak.<换名时刻>.log`；传目录就按这个通配一次全收。
LOG_GLOB = "maafw*.log"
DEFAULT_LOG_DIR = REPO_ROOT / "debug"

LINE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]"
    r"\[([A-Z]+)\]\[Px(\d+)\]\[Tx(\d+)\]\[([^\]]+)\](?:\[L\d+\])?\[([^\]]+)\](.*)$"
)
EVENT_MSG = re.compile(r"\[msg=([A-Za-z.]+)\]")
DETAILS = re.compile(r"\[details=(\{.*\})\]\s*$")
