"""仓库根与默认产出目录。

包内文件在 `tools/drawing/flowlog/`，所以仓库根是上溯四级；产出默认落到
`<仓库根>/temp/flow`，不写到日志旁边。改目录层级要同步改 `parents[N]`，
`check_python.py` 的 repo-root 检查会拦级数不符。"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
DEFAULT_OUTDIR = str(Path(REPO_ROOT) / "temp" / "flow")
