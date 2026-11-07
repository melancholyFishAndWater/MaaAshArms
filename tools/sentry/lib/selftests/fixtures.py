"""自检用的固定输入：release 串、合成行集、工件时间。

这些常量只服务断言——改动它们等于改口径断言，需同时复核期望值。
"""

from __future__ import annotations

from typing import Any, Callable

Emit = Callable[[str], None]

# 两个渠道 release 内嵌同一个资源版本：用来验证「按版本聚合」不会因为
# MFA 版本不同而把同一版本的行拆开。
RELEASE_D38 = "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38"
RELEASE_D37_OLD_MFA = "MFA@v2.15.0+MaaAshArms@v1.0.0-beta.37"
RELEASE_D37_NEW_MFA = "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.37"
RELEASE_D36 = "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.36"
V38 = "v1.0.0-beta.38"
V37 = "v1.0.0-beta.37"
V36 = "v1.0.0-beta.36"

# 合成数据设计（口径断言全靠这几行）：
#   训练所: 38 → 6/50,  37 → 1/40,  36 → 0/30        （β37→β38 恶化 +7.5pp）
#   联络人: 38 → 0/40,  37 → 2/40,  36 → 0/30        （β37→β38 改善 -5.0pp）
#   消耗合金: 38 → 1/3, 37 → 0/3,   36 → 0/0         （样本不足，但 +33.3pp 仍要显示）
#   订单工厂: 只在 36 有数据                          （旧版本任务，最新版本暂无样本）
#   已下线任务: 只有 37 有 ok=1                       （刚过 min_latest_runs 门槛）
#
# β37 刻意拆成两个渠道 release（MFA 版本不同）：同一资源版本的行必须合并计数，
# 所以训练所在 β37 是 20+20=40 次、失败 1 次（1/40 = 2.5%）。
ROWS_TOTAL: list[dict[str, Any]] = [
    {"description": "训练所", "release": RELEASE_D38, "_count": 50},
    {"description": "训练所", "release": RELEASE_D37_OLD_MFA, "_count": 20},
    {"description": "训练所", "release": RELEASE_D37_NEW_MFA, "_count": 20},
    {"description": "训练所", "release": RELEASE_D36, "_count": 30},
    {"description": "联络人", "release": RELEASE_D38, "_count": 40},
    {"description": "联络人", "release": RELEASE_D37_OLD_MFA, "_count": 40},
    {"description": "联络人", "release": RELEASE_D36, "_count": 30},
    {"description": "消耗合金", "release": RELEASE_D38, "_count": 3},
    {"description": "消耗合金", "release": RELEASE_D37_OLD_MFA, "_count": 3},
    {"description": "订单工厂", "release": RELEASE_D36, "_count": 30},
    {"description": "已下线任务", "release": RELEASE_D37_OLD_MFA, "_count": 1},
    {"description": "启动游戏", "release": RELEASE_D36, "_count": 40},
]

ROWS_STATUS: list[dict[str, Any]] = [
    {"description": "训练所", "status": "ok", "release": RELEASE_D38, "_count": 44},
    {
        "description": "训练所",
        "status": "internal_error",
        "release": RELEASE_D38,
        "_count": 5,
    },
    {
        "description": "训练所",
        "status": "cancelled",
        "release": RELEASE_D38,
        "_count": 1,
    },
    {
        "description": "训练所",
        "status": "ok",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 20,
    },
    {
        "description": "训练所",
        "status": "internal_error",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 1,
    },
    {
        "description": "训练所",
        "status": "ok",
        "release": RELEASE_D37_NEW_MFA,
        "_count": 19,
    },
    {"description": "训练所", "status": "ok", "release": RELEASE_D36, "_count": 30},
    {"description": "联络人", "status": "ok", "release": RELEASE_D38, "_count": 40},
    {
        "description": "联络人",
        "status": "ok",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 38,
    },
    {
        "description": "联络人",
        "status": "internal_error",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 2,
    },
    {"description": "联络人", "status": "ok", "release": RELEASE_D36, "_count": 30},
    {"description": "消耗合金", "status": "ok", "release": RELEASE_D38, "_count": 2},
    {
        "description": "消耗合金",
        "status": "internal_error",
        "release": RELEASE_D38,
        "_count": 1,
    },
    {
        "description": "消耗合金",
        "status": "ok",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 3,
    },
    {"description": "订单工厂", "status": "ok", "release": RELEASE_D36, "_count": 30},
    {
        "description": "已下线任务",
        "status": "ok",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 1,
    },
    {"description": "启动游戏", "status": "ok", "release": RELEASE_D36, "_count": 40},
    # 只在失败时上报的内部 span：必须进失败标记表，不能进任务表。
    {
        "description": "HomeFlag",
        "status": "internal_error",
        "release": RELEASE_D38,
        "_count": 3,
    },
    {
        "description": "BaseReturnHome",
        "status": "internal_error",
        "release": RELEASE_D37_OLD_MFA,
        "_count": 2,
    },
]

ROWS_RUN_TOTAL: list[dict[str, Any]] = [
    {"release": RELEASE_D38, "_count": 20},
    {"release": RELEASE_D37_OLD_MFA, "_count": 10},
    {"release": RELEASE_D37_NEW_MFA, "_count": 10},
]

ROWS_RUN_FAILED: list[dict[str, Any]] = [
    {"release": RELEASE_D38, "_count": 4},
    {"release": RELEASE_D37_OLD_MFA, "_count": 1},
]

ROWS_RELEASE_USERS: list[dict[str, Any]] = [
    {"release": RELEASE_D38, "_count_user": 6, "_count": 90},
    {"release": RELEASE_D37_OLD_MFA, "_count_user": 4, "_count": 60},
    {"release": RELEASE_D37_NEW_MFA, "_count_user": 4, "_count": 60},
    {"release": RELEASE_D36, "_count_user": 3, "_count": 100},
]

ROWS_RUNTIME_OS: list[dict[str, Any]] = [
    {"os_name": "Windows 11", "release": RELEASE_D38, "_count": 18},
    {"os_name": "Windows 10", "release": RELEASE_D38, "_count": 2},
]

ROWS_RUNTIME_ENV: list[dict[str, Any]] = [
    {"environment": "production", "release": RELEASE_D38, "_count": 20},
]

MEASUREMENTS: dict[str, list[dict[str, Any]]] = {
    "task_total": ROWS_TOTAL,
    "task_status": ROWS_STATUS,
    "release_users": ROWS_RELEASE_USERS,
    "run_total": ROWS_RUN_TOTAL,
    "run_failed": ROWS_RUN_FAILED,
    "runtime_os": ROWS_RUNTIME_OS,
    "runtime_env": ROWS_RUNTIME_ENV,
}

# 用来验证「同一版本跨渠道合并」：beta.37 的 ok 分布散在两个 release 里。
EXPECTED_BETA37_TOTAL = 40
EXPECTED_BETA37_FAILED = 1
ARTIFACT_FIXTURE_TIME = "2026-01-01T00:00:00Z"


__all__ = [
    "Emit",
    "RELEASE_D38",
    "RELEASE_D37_OLD_MFA",
    "RELEASE_D37_NEW_MFA",
    "RELEASE_D36",
    "V38",
    "V37",
    "V36",
    "ROWS_TOTAL",
    "ROWS_STATUS",
    "ROWS_RUN_TOTAL",
    "ROWS_RUN_FAILED",
    "ROWS_RELEASE_USERS",
    "ROWS_RUNTIME_OS",
    "ROWS_RUNTIME_ENV",
    "MEASUREMENTS",
    "EXPECTED_BETA37_TOTAL",
    "EXPECTED_BETA37_FAILED",
    "ARTIFACT_FIXTURE_TIME",
]
