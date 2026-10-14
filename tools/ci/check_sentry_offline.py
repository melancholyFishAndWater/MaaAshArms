"""离线自检：校验 tools/sentry 的纯逻辑部分，零网络、零凭证。

    python tools/ci/check_sentry_offline.py

与 `python -m tools.sentry.cli selftest` 共用同一套 fixtures 与断言，额外渲染
一次完整报告到 stdout，方便人眼确认表格没坏。CI 里跑这个文件即可。

不需要 Sentry 登录，也不发起任何网络请求——报告里最容易静默出错的是
release 双 @ 拼接解析、任务表与失败标记分流、Δpp 方向这些纯逻辑。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.sentry import selftest, task_failure_report, task_trend_report  # noqa: E402


def _reconfigure_streams() -> None:
    """Windows runner 的控制台编码是 cp1252，中文输出会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def render_sample_reports() -> str:
    """把 fixtures 渲染成一份完整报告，确认渲染路径没有异常。"""
    failure_report = task_failure_report.build_report(
        selftest.MEASUREMENTS,
        release=selftest.RELEASE_D38,
        period="7d",
        provenance="离线自检 fixtures",
    )
    buffer = io.StringIO()
    task_failure_report.write_console(failure_report, buffer)

    trend_report = task_trend_report.build_report(
        selftest.MEASUREMENTS,
        versions=[selftest.V38, selftest.V37, selftest.V36],
        period="30d",
        provenance="离线自检 fixtures",
        min_latest_runs=1,
    )
    task_trend_report.write_console(trend_report, buffer)
    return buffer.getvalue()


def main() -> int:
    _reconfigure_streams()
    print("== tools/sentry 离线自检（不需要 Sentry 登录）==\n")
    code = selftest.run_selftest(print)
    if code != 0:
        return code
    print("\n== 样例报告（合成数据，非真实遥测）==\n")
    print(render_sample_reports())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
