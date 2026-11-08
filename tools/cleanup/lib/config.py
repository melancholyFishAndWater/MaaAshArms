"""保留策略的默认值，以及可选的 `tools/cleanup/config.json` 覆盖。

默认忽略项挑的是"删了要重新下载/重登"的目录：

- `flow`：流程图产物（重画要跑一遍证据包）；
- `M9A`：参考仓库副本；
- `telemetry`：遥测工件缓存（`temp/telemetry/`，重新落盘要联网）；
- `sentry-cfg`：Sentry CLI 凭据副本（删了要重新 `sentry auth login` 并复制 `cli.db`）；
- `uvcache` / `uvtools`：`uvx` 的包缓存，重建慢。
"""

from __future__ import annotations

import json
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = TOOL_ROOT / "config.json"

DEFAULT_DAYS = 7
DEFAULT_IGNORE = ["flow", "M9A", "telemetry", "sentry-cfg", "uvcache", "uvtools"]


def load_config() -> tuple[int, list[str]]:
    """返回 `(days, ignore)`；没有配置文件就用默认值。坏文件按默认值处理（不中断清理）。"""
    if not CONFIG_PATH.is_file():
        return DEFAULT_DAYS, list(DEFAULT_IGNORE)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_DAYS, list(DEFAULT_IGNORE)
    days = data.get("days", DEFAULT_DAYS)
    ignore = data.get("ignore", DEFAULT_IGNORE)
    if not isinstance(days, int) or days < 1:
        days = DEFAULT_DAYS
    if not isinstance(ignore, list) or not all(isinstance(i, str) for i in ignore):
        ignore = DEFAULT_IGNORE
    return days, list(ignore)
