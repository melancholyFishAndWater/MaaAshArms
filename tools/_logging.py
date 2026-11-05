"""tools 下脚本共用的日志配置：控制台（stderr）+ `temp/logs/` 下的文件。

设计要点（用户要求：日志独立于其他调试，存入 temp）：

- 日志文件放在 `temp/logs/`，**不和** `temp/flow/`（图）、`temp/telemetry/`（遥测库）混在一起；
- 每个运行一个文件：`temp/logs/<tool>_<YYYYMMDD-HHMMSS>_<pid>.log`，互不干扰、便于按运行审计；
- 控制台输出只走 **stderr**，不碰 stdout——stdout 留给机器可读的结果（清单 / 匹配列表 / 报告）；
- 级别可过滤（`--log-level`）；路径可覆盖（`--log-file`），传 `none` / `""` 关闭文件只走控制台。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "temp" / "logs"

_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)


def configure_tool_logging(
    tool: str,
    *,
    log_file: str | None = None,
    level: str = "INFO",
) -> tuple[logging.Logger, str | None]:
    """给共用的父 logger `tools` 配日志：控制台(stderr) + `temp/logs/` 文件。

    各模块用 `logging.getLogger("tools.<模块名>")`（子 logger）打日志，记录会向上冒泡到这里，
    所以同一个进程里无论从哪个模块打，都进同一个文件、带各自的 `[tools.xxx]` 前缀。
    `tool` 只用来命名文件（`<tool>_<时间戳>_<pid>.log`），不改 logger 名。

    - `log_file=None`：按运行时间戳自动建 `temp/logs/<tool>_<时间戳>_<pid>.log`；
    - `log_file` 是别的路径：用那个路径（父目录会建出来）；
    - `log_file` 是 `""` / `"none"`：只走控制台，不写文件。

    返回 `(logger, 实际文件路径或 None)`。幂等：重复调用会先清掉旧 handler，
    所以同一进程里 main 调一次即可。
    """
    logger = logging.getLogger("tools")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handle in list(logger.handlers):
        logger.removeHandler(handle)
        with contextlib.suppress(Exception):
            handle.close()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_FORMAT)
    logger.addHandler(console)

    actual: str | None = None
    if log_file is None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            actual = str(LOG_DIR / f"{tool}_{datetime.now():%Y%m%d-%H%M%S}_{os.getpid()}.log")
            logger.addHandler(
                logging.FileHandler(actual, encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            logger.warning("日志文件建不成（%s），只走控制台：%s", LOG_DIR, exc)
            actual = None
    elif log_file and log_file.lower() not in ("", "none"):
        try:
            parent = os.path.dirname(log_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            actual = log_file
            logger.addHandler(
                logging.FileHandler(actual, encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            logger.warning("日志文件建不成（%s），只走控制台：%s", log_file, exc)
            actual = None
    return logger, actual
