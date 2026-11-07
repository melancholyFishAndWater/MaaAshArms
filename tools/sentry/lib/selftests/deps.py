"""selftests 依赖的 sentry 模块：包内用相对导入，脚本模式退回顶层导入。

与拆分前 `selftest.py` 的 try/except 一致：`python -m tools.sentry.cli selftest`
与直接 `import selftest`（tools/sentry 在 sys.path 上）都要能用。
"""

from __future__ import annotations

try:
    from .. import task_failure_report, task_trend_report
except ImportError:
    from lib import task_failure_report  # type: ignore[no-redef]
    from lib import task_trend_report  # type: ignore[no-redef]

try:
    from ..config import CONFIG
except ImportError:
    from lib.config import CONFIG  # type: ignore[no-redef]

try:
    from ..report_common import (
        DEFAULT_ARTIFACT_DIR,
        Artifact,
        Measurement,
        load_artifact,
        release_version_key,
        repo_root,
        save_artifact,
        version_label,
        wilson_interval,
    )
except ImportError:
    from lib.report_common import (  # type: ignore[no-redef]
        DEFAULT_ARTIFACT_DIR,
        Artifact,
        Measurement,
        load_artifact,
        release_version_key,
        repo_root,
        save_artifact,
        version_label,
        wilson_interval,
    )

__all__ = [
    "CONFIG",
    "task_failure_report",
    "task_trend_report",
    "DEFAULT_ARTIFACT_DIR",
    "Artifact",
    "Measurement",
    "load_artifact",
    "release_version_key",
    "repo_root",
    "save_artifact",
    "version_label",
    "wilson_interval",
]
