"""零网络的逻辑自检：用合成 fixtures 校验 release 解析与统计口径。

为什么要有这个文件：真正的报告要联网 + 已登录 Sentry 才能跑，而项目里最容易
静默出错的恰恰是**纯逻辑部分**——release 双 ``@`` 拼接解析、任务表与失败标记分流、
Δpp 方向、工件往返。这些不需要网络，用固定输入断言固定输出即可。

被 ``cli.py selftest`` 与 CI 的 ``tools/validation/check_sentry_offline.py`` 共用，
后者额外渲染一次 console / markdown / json 报告并断言包含关键行。
"""

from __future__ import annotations

import io
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from .config import CONFIG
except ImportError:
    from config import CONFIG

try:
    from . import task_failure_report, task_trend_report
except ImportError:
    import task_failure_report  # type: ignore[no-redef]
    import task_trend_report  # type: ignore[no-redef]

try:
    from .report_common import (
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
    from report_common import (
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


def build_probe_artifact() -> Artifact:
    """合成一份覆盖全部 measurement 的工件，供往返与渲染断言使用。"""
    return Artifact(
        target=CONFIG.target,
        period="7d",
        measurements={
            name: Measurement(
                name=name,
                rows=rows,
                fields=(
                    "span.description",
                    "span.status",
                    "release",
                    "count_unique(trace)",
                ),
            )
            for name, rows in MEASUREMENTS.items()
        },
        generated_at=ARTIFACT_FIXTURE_TIME,
    )


def save_probe_artifact(path: Path) -> Artifact:
    """把合成工件写盘并读回，返回读回后的对象。"""
    save_artifact(build_probe_artifact(), path)
    return load_artifact(path)


class CheckFailure(Exception):
    """一条断言不成立。"""


def _eq(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise CheckFailure(f"{what}: 期望 {expected!r}，实际 {actual!r}")


def _contains(haystack: str, needle: str, what: str) -> None:
    if needle not in haystack:
        raise CheckFailure(f"{what}: 输出里找不到 {needle!r}")


def _raises(action: Callable[[], Any], error: type[Exception], what: str) -> None:
    """断言动作抛出指定异常类型（入参错误这类路径要能测到）。

    这里必须捕任意 `Exception` 再报成断言失败：目的是"把意外的异常类型也讲清楚"，
    而不是让自检自己崩掉。抑制与 `except` 写在同一行——多行 `except (` 会被 ruff
    判成"抑制未使用"并连带报 S110/BLE001（见 tools/validation/README 的假红那节）。
    """
    try:
        action()
    except error:
        return
    except Exception as unexpected:  # noqa: BLE001
        raise CheckFailure(
            f"{what}: 期望 {error.__name__}，实际 {type(unexpected).__name__}"
        ) from None
    raise CheckFailure(f"{what}: 期望抛 {error.__name__}，但没有抛")


def check_release_parsing() -> list[str]:
    """release 解析：双 @ 拼接取资源版本，不能命中前面的 MFA 版本。"""
    lines: list[str] = []
    cases: list[tuple[str, tuple[int, int, int, int, int] | None, str | None]] = [
        (RELEASE_D38, (1, 0, 0, 0, 38), V38),
        # 关键回归点：M9A 的 search() 在这里会返回 (2,16,1)。
        ("MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38", (1, 0, 0, 0, 38), V38),
        (RELEASE_D37_NEW_MFA, (1, 0, 0, 0, 37), V37),
        ("MaaAshArms@v1.0.0", (1, 0, 0, 2, 0), "v1.0.0"),
        ("MFA@v2.16.1+MaaAshArms@v1.0.0-rc.4", (1, 0, 0, 1, 4), "v1.0.0-rc.4"),
        # 不含本项目前缀的 release 必须解析失败，而不是退回 MFA 版本。
        ("MFA@v2.16.1", None, None),
        ("MFA@v2.16.1+MaaAshArms@v1.0.0-alpha.1", (1, 0, 0, -1, 1), "v1.0.0-alpha.1"),
    ]
    for release, expected_key, expected_label in cases:
        _eq(
            release_version_key(release),
            expected_key,
            f"release_version_key({release!r})",
        )
        _eq(version_label(release), expected_label, f"version_label({release!r})")
        lines.append(f"  {release} → {expected_label}")
    return lines


def check_ordering() -> list[str]:
    """版本排序键：预发布版本排在对应正式版之前，beta.38 高于 beta.37。"""
    stable = release_version_key("MaaAshArms@v1.0.0")
    beta38 = release_version_key(RELEASE_D38)
    beta37 = release_version_key(RELEASE_D37_NEW_MFA)
    assert stable is not None and beta38 is not None and beta37 is not None
    if not (beta37 < beta38 < stable):
        raise CheckFailure(
            f"版本排序不对（应满足 beta37 < beta38 < stable）：beta37={beta37} beta38={beta38} stable={stable}"
        )
    return ["  beta37 < beta38 < stable 成立（预发布排在对应正式版之前）"]


def check_wilson() -> list[str]:
    """Wilson 区间的手算对照值。"""
    lines: list[str] = []
    for failures, total, expected_low, expected_high in (
        (6, 50, 0.056, 0.240),
        (0, 30, 0.000, 0.114),
    ):
        interval = wilson_interval(failures, total)
        assert interval is not None
        low, high = interval
        if abs(low - expected_low) > 0.002 or abs(high - expected_high) > 0.002:
            raise CheckFailure(
                f"wilson_interval({failures}, {total}) 期望约 ({expected_low}, {expected_high})，实际 ({low:.4f}, {high:.4f})"
            )
        lines.append(f"  wilson({failures}/{total}) = {low:.3f}–{high:.3f}")
    _eq(wilson_interval(0, 0), None, "wilson_interval(0, 0)")
    return lines


def check_task_rows() -> list[str]:
    """任务表 / 失败标记分流、取消列、排序、限制行数。"""
    lines: list[str] = []
    version_key = release_version_key(RELEASE_D38)

    tasks, markers = task_failure_report.build_task_rows(
        ROWS_TOTAL, ROWS_STATUS, version_key
    )
    names = [row.task for row in tasks]
    # 同失败数时按总次数降序（消耗合金 3 次 > 联络人 0 次），最后按任务名，保证输出稳定。
    _eq(names, ["训练所", "消耗合金", "联络人"], "beta.38 任务表（按失败数降序）")
    trained = tasks[0]
    _eq(
        (trained.total, trained.failed, trained.cancelled),
        (50, 5, 1),
        "训练所 beta.38 计数",
    )
    if abs((trained.failure_rate or 0) - 0.1) > 1e-9:
        raise CheckFailure(
            f"训练所 beta.38 失败率应为 0.1，实际 {trained.failure_rate}"
        )

    marker_names = [row.task for row in markers]
    _eq(marker_names, ["HomeFlag"], "beta.38 失败标记表（只在失败时上报）")
    if any(row.failure_rate is not None for row in markers):
        raise CheckFailure("失败标记不该有失败率")

    by_rate, _ = task_failure_report.build_task_rows(
        ROWS_TOTAL, ROWS_STATUS, version_key, sort="rate"
    )
    _eq([row.task for row in by_rate], ["消耗合金", "训练所", "联络人"], "按失败率降序")

    limited, _ = task_failure_report.build_task_rows(
        ROWS_TOTAL, ROWS_STATUS, version_key, limit=2
    )
    _eq(len(limited), 2, "--limit 2 生效")

    # 不指定版本时应覆盖所有 release。
    all_tasks, all_markers = task_failure_report.build_task_rows(
        ROWS_TOTAL, ROWS_STATUS, None
    )
    _eq(
        sorted(row.task for row in all_tasks),
        sorted(["训练所", "联络人", "消耗合金", "订单工厂", "已下线任务", "启动游戏"]),
        "不限版本时的任务全集",
    )
    _eq(
        sorted(row.task for row in all_markers),
        ["HomeFlag", "BaseReturnHome"],
        "不限版本时的标记全集",
    )

    # 同一版本跨两个渠道 release 合并：beta.37 的总数必须是 20+20。
    beta37_tasks, _ = task_failure_report.build_task_rows(
        ROWS_TOTAL, ROWS_STATUS, release_version_key(RELEASE_D37_NEW_MFA)
    )
    beta37_trained = next(row for row in beta37_tasks if row.task == "训练所")
    _eq(beta37_trained.total, EXPECTED_BETA37_TOTAL, "beta.37 跨渠道合并后的训练所总数")
    _eq(
        beta37_trained.failed,
        EXPECTED_BETA37_FAILED,
        "beta.37 跨渠道合并后的训练所失败数",
    )

    lines.append(f"  beta.38 任务表: {', '.join(names)}")
    lines.append(f"  beta.38 失败标记: {', '.join(marker_names)}")
    lines.append(
        f"  beta.37 跨渠道合并: 训练所 {beta37_trained.total} 次 / {beta37_trained.failed} 失败"
    )
    return lines


def check_release_rows() -> list[str]:
    """运行失败率按 release 原文聚合，并只保留目标版本。"""
    lines: list[str] = []
    runs = task_failure_report.build_release_rows(
        ROWS_RUN_TOTAL, ROWS_RUN_FAILED, release_version_key(RELEASE_D38)
    )
    _eq(
        [row.release for row in runs],
        [RELEASE_D38],
        "beta.38 的运行失败率只含本版本渠道",
    )
    _eq((runs[0].total, runs[0].failed), (20, 4), "beta.38 运行计数")
    if abs(runs[0].failure_rate - 0.2) > 1e-9:
        raise CheckFailure(f"beta.38 运行失败率应为 0.2，实际 {runs[0].failure_rate}")

    runs37 = task_failure_report.build_release_rows(
        ROWS_RUN_TOTAL, ROWS_RUN_FAILED, release_version_key(RELEASE_D37_NEW_MFA)
    )
    _eq(len(runs37), 2, "beta.37 的两条渠道都要出现")
    lines.append(f"  beta.37 拆出 {len(runs37)} 条渠道 release（合计 20 次运行）")
    return lines


def check_context_rows() -> list[str]:
    """运行环境分布。"""
    rows = task_failure_report.build_context_rows(
        (
            ("操作系统", ROWS_RUNTIME_OS, "os_name"),
            ("运行环境", ROWS_RUNTIME_ENV, "environment"),
        ),
        release_version_key(RELEASE_D38),
    )
    values = [row.value for row in rows if row.value]
    _eq(len(values), 3, "环境分布取值数（2 个 OS + 1 个 environment）")
    if not any(value.startswith("Windows 11") for value in values):
        raise CheckFailure(f"环境分布里找不到 Windows 11：{values}")
    return [f"  {' / '.join(values)}"]


def check_trend() -> list[str]:
    """跨版本趋势：版本发现、Δpp 方向、小样本仍进表。"""
    lines: list[str] = []
    versions = task_trend_report.discover_versions(
        ROWS_RELEASE_USERS, count=3, min_users=CONFIG.min_release_unique_users
    )
    # 选版本按**数据量**排（β37 的用户分散在两条渠道 release 里共 8 个，比 β38 的 6 多），
    # 但输出一律按**版本号降序**——Δpp 的语义是"相对更早的版本"，顺序错会把 Δpp 算到未来版本上。
    # β35 用户数为 0，被门槛滤掉。
    _eq(versions, [V38, V37, V36], "版本发现（按数据量选、按版本号输出）")

    stable_only = task_trend_report.discover_versions(
        ROWS_RELEASE_USERS, count=3, include_prerelease=False, min_users=1, quiet=True
    )
    _eq(stable_only, [], "--stable-only 下本项目没有正式版")

    rows, _ = task_trend_report.build_trend_rows(
        ROWS_TOTAL, ROWS_STATUS, versions, min_latest_runs=1, sort="delta"
    )
    by_task = {row.task: row for row in rows}
    if "训练所" not in by_task or "联络人" not in by_task:
        raise CheckFailure(f"趋势表里缺少关键任务：{sorted(by_task)}")

    # 样本门槛单列一组断言，别和上面的 Δpp 断言混在一个口径里。
    # 门槛判据是「最新版本 ≥ N **或** 累计 ≥ 2N」，所以 --min-latest-runs 1 仍会剔掉
    # 累计只有 1 次的任务——门槛不是"≥N 就看"，而是"要么最新版本够、要么总量够两倍"。
    margin, _ = task_trend_report.build_trend_rows(
        ROWS_TOTAL, ROWS_STATUS, versions, min_latest_runs=1, sort="delta"
    )
    margin_names = {row.task for row in margin}
    if "已下线任务" in margin_names:
        raise CheckFailure(
            "累计只有 1 次的任务不该进趋势表（不满足 2×min_latest_runs）"
        )
    if "订单工厂" not in margin_names:
        raise CheckFailure(
            "订单工厂累计 30 次 ≥ 2×1，应进趋势表（只是最新版本暂无样本）"
        )

    trained_rate = by_task["训练所"].version_rates[V38]
    _eq(
        trained_rate is not None and round(trained_rate, 4),
        0.1,
        "训练所 beta.38 失败率",
    )
    trained_delta = by_task["训练所"].version_deltas[V38]
    # β37 跨两个渠道合并后是 1/40 = 2.5%，β38 是 6/50 = 12.5%？否——5/50 = 10.0%，
    # 所以 Δpp = 10.0 - 2.5 = +7.5。若解析取到 MFA 版本或渠道没合并，这个数就不是 7.5。
    _eq(
        trained_delta is not None and round(trained_delta, 1),
        7.5,
        "训练所 Δpp 应为 +7.5pp（相对合并后的 β37）",
    )
    contact_delta = by_task["联络人"].version_deltas[V38]
    _eq(
        contact_delta is not None and round(contact_delta, 1),
        -5.0,
        "联络人 Δpp 应为 -5.0pp",
    )

    # 最早展示版本没有更早的基准，Δpp 必须是 None 而不是 0。
    _eq(by_task["训练所"].version_deltas[V36], None, "最老版本的 Δpp 应为 None")

    by_delta, _ = task_trend_report.build_trend_rows(
        ROWS_TOTAL, ROWS_STATUS, versions, min_latest_runs=1, sort="delta"
    )
    # 消耗合金只有 3 次样本仍在表里（这是刻意的：小样本要能被看见并标出来），
    # 它的 +33.3pp 会排第一 —— 正好演示「样本不足的榜首不是回归」。
    _eq(by_delta[0].task, "消耗合金", "按 Δpp 降序时涨幅最大者排第一")

    # 单任务穿透。
    _, detailed = task_trend_report.build_trend_rows(
        ROWS_TOTAL, ROWS_STATUS, versions, min_latest_runs=1, task_filter="训练所"
    )
    assert detailed is not None
    _eq([stat.version for stat in detailed], [V38, V37, V36], "单任务明细的版本顺序")
    _eq([stat.total for stat in detailed], [50, 40, 30], "单任务明细的各版本运行数")

    lines.append(f"  版本序列: {', '.join(versions)}")
    lines.append(
        f"  训练所 Δpp={trained_delta:+.1f}pp，联络人 Δpp={contact_delta:+.1f}pp"
    )
    return lines


def check_artifact_roundtrip() -> list[str]:
    """工件写盘 → 读回 → 报告内容必须与直接喂行完全一致。"""
    lines: list[str] = []
    # 刻意不用 TemporaryDirectory：DSH 沙箱下系统临时目录的删除会被拒，
    # 于是「测试通过但清理失败」变成 PermissionError。写进工作区里 gitignore 的 temp/。
    scratch = repo_root() / DEFAULT_ARTIFACT_DIR / "_selftest"
    try:
        path = scratch / "probe.json"
        loaded = save_probe_artifact(path)
        _eq(loaded.target, CONFIG.target, "读回后的 target")
        _eq(loaded.generated_at, ARTIFACT_FIXTURE_TIME, "读回后的 generated_at")
        for name, rows in MEASUREMENTS.items():
            _eq(loaded.get(name), rows, f"读回后的 measurement {name}")

        # 走一遍真正的离线路径（collect_report 吃工件），而不是只比 build_report 的输出——
        # 报告命令的 `--input` 分支就在那条路上，从没被执行到的话它坏了也发现不了。
        offline = task_failure_report.collect_report(
            sentry_command=None,
            release=RELEASE_D38,
            target=CONFIG.target,
            period="7d",
            artifact=loaded,
            sort="failures",
            reverse=False,
            limit=None,
            fresh=True,
            timeout_seconds=1.0,
            verbose=False,
            quiet=True,
        )
        direct = task_failure_report.build_report(
            MEASUREMENTS, release=RELEASE_D38, period="7d", provenance="direct"
        )
        _eq(offline.tasks, direct.tasks, "工件路径与直连路径的任务表")
        _eq(offline.runs, direct.runs, "工件路径与直连路径的运行失败率")
        _eq(offline.context, direct.context, "工件路径与直连路径的环境分布")
        if loaded.path is None or loaded.path.name not in offline.provenance:
            raise CheckFailure(
                f"离线路径的 provenance 没带上工件路径：{offline.provenance}"
            )
        lines.append(
            f"  往返一致：{len(direct.tasks)} 个任务 / {len(direct.runs)} 条 release；collect_report 离线路径可用"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # 缺 measurement 时要点名缺哪个。
    thin = Artifact(
        target=CONFIG.target,
        period="7d",
        measurements={},
        generated_at="2026-01-01T00:00:00Z",
    )
    try:
        thin.get("task_total", "task-failure")
    except ValueError as error:
        _contains(str(error), "task_total", "缺 measurement 的报错")
    else:
        raise CheckFailure("缺 measurement 时没有报错")
    return lines


def check_version_filter_messages() -> list[str]:
    """版本被过滤光时报错必须指向**真正的原因**，不能统一说成"没有可解析版本号的 release"。

    本项目至今只发 beta：`--stable-only` 会把版本全滤掉。这时若报"没有能解析出版本号的
    release"，会把人引去查 release_pattern（错的），所以两条路径的文案必须分开。
    """
    lines: list[str] = []
    stable_only_empty = task_trend_report.discover_versions(
        ROWS_RELEASE_USERS, count=3, include_prerelease=False, min_users=1, quiet=True
    )
    _eq(stable_only_empty, [], "--stable-only 无候选")

    # 真版本能被选出（回归前置）
    got = task_trend_report.discover_versions(
        ROWS_RELEASE_USERS, count=3, min_users=1, quiet=True
    )
    _eq(got[:3], [V38, V37, V36], "默认口径选出三个 beta")

    # 走到 collect_report 的报错分支：工件 + --stable-only
    scratch = repo_root() / DEFAULT_ARTIFACT_DIR / "_selftest_filter"
    try:
        path = scratch / "probe.json"
        loaded = save_probe_artifact(path)
        try:
            task_trend_report.collect_report(
                sentry_command=None,
                target=CONFIG.target,
                period="7d",
                artifact=loaded,
                versions_count=3,
                include_prerelease=False,
                task_filter=None,
                min_latest_runs=1,
                sort="runs",
                reverse=False,
                limit=None,
                fresh=True,
                timeout_seconds=1.0,
                verbose=False,
                quiet=True,
                min_users=1,
            )
        except ValueError as error:
            message = str(error)
            _contains(
                message, "stable-only", "--stable-only 的报错要说明是这个开关造成的"
            )
            _contains(message, "正式版", "--stable-only 的报错要说清缺的是正式版")
            if "release_pattern" in message:
                raise CheckFailure(
                    f"报错把原因指向了 release_pattern（错的）：{message}"
                )
        else:
            raise CheckFailure("--stable-only 无候选时没有报错")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    lines.append("  过滤光版本的报错指向 --stable-only，不再误导到 release_pattern")
    return lines


def check_renderers() -> list[str]:
    """console / markdown / json 三种渲染都要包含关键内容。"""
    import io
    import json as json_module

    lines: list[str] = []
    failure_report = task_failure_report.build_report(
        MEASUREMENTS, release=RELEASE_D38, period="7d", provenance="selftest"
    )

    console = io.StringIO()
    task_failure_report.write_console(failure_report, console)
    console_text = console.getvalue()
    _contains(console_text, "训练所", "console 报告")
    _contains(console_text, "样本不足", "console 报告应标出小样本")
    _contains(console_text, "HomeFlag", "console 报告应含失败标记")

    markdown = io.StringIO()
    task_failure_report.write_markdown(failure_report, markdown)
    _contains(markdown.getvalue(), "| 训练所 |", "markdown 报告")

    payload = io.StringIO()
    task_failure_report.write_json(failure_report, payload)
    decoded = json_module.loads(payload.getvalue())
    _eq(decoded["version"], V38, "json 报告的版本")

    versions = [V38, V37, V36]
    trend_report = task_trend_report.build_report(
        MEASUREMENTS,
        versions=versions,
        period="30d",
        provenance="selftest",
        min_latest_runs=1,
    )
    trend_console = io.StringIO()
    task_trend_report.write_console(trend_report, trend_console)
    trend_text = trend_console.getvalue()
    _contains(trend_text, "+33.3pp", "趋势报告应出现消耗合金的 Δpp")
    _contains(trend_text, "+7.5pp", "趋势报告应出现训练所的 Δpp")
    _contains(trend_text, "样本不足", "趋势报告应标出小样本")
    trend_json = io.StringIO()
    task_trend_report.write_json(trend_report, trend_json)
    _eq(
        json_module.loads(trend_json.getvalue())["versions"],
        versions,
        "趋势 json 的版本序列",
    )

    lines.append("  console / markdown / json 三种渲染均可读")
    return lines


def check_flow_parser() -> list[str]:
    """流程图解析器：同一份日志，给文件路径与给文本必须解析出**完全相同**的结果。

    这是遥测侧唯一用得上流程图的地方——远程没有本地 `maafw.log` 文件，只有
    Sentry 取回的日志正文。不依赖 Pillow（只解析、不画图），所以 CI 也能跑。
    """
    import io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.flow_from_log import _first_log_ts, _open_log_lines, parse_log

    sample = "\n".join(
        [
            "[2026-10-14 00:30:00.050][INF][Px1][Tx1][Utils/EventDispatcher.hpp][L65][A::notify] "
            "!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.Starting] "
            '[details={"focus":null,"list":[{"anchor":false,"jump_back":false,"name":"BattleProbeNow"},'
            '{"anchor":false,"jump_back":true,"name":"BattleEnter"}],"name":"BattleLoop","task_id":200000118}] ',
            "[2026-10-14 00:30:00.100][INF][Px1][Tx1][Recognizer.cpp][L1][A::b] "
            "recognize_list [cur_node_=BattleProbeNow] [list=[{name=BattleProbeNow}]]",
            "[2026-10-14 00:30:00.200][INF][Px1][Tx1][Recognizer.cpp][L2][A::c] "
            'reco [result={"name":"BattleProbeNow","algorithm":"OCR","box":[1,2,3,4],"reco_id":11,"detail":{"text":"接管作战"}}]',
            "[2026-10-14 00:30:00.300][INF][Px1][Tx1][TaskBase.cpp][L3][A::d] "
            'action [i=0] [data.repeat=1] [result={"action":"Click","success":true,"action_id":21}]',
            "[2026-10-14 00:30:00.400][INF][Px1][Tx1][PipelineTask.cpp][L4][A::e] "
            'PipelineTask node done [result={"name":"BattleProbeNow","node_id":5,"reco_id":11,"action_id":21,'
            '"completed":true}] [task_id_=200000118]',
            # 不是日志行，必须被跳过且不影响结果
            "Total 0 files, 0 bytes",
        ]
    )

    from_text = parse_log(sample, None, None, None, False)
    from_stream = parse_log(io.StringIO(sample), None, None, None, False)
    _eq(len(from_text.steps), 1, "文本输入的步数")
    _eq([s.name for s in from_text.steps], ["BattleProbeNow"], "文本输入解析出的节点")
    _eq([s.name for s in from_stream.steps], ["BattleProbeNow"], "文本流输入的节点")
    _eq(from_text.steps[0].texts, ["接管作战"], "文本输入抓到的 OCR 文本")
    _eq(from_text.steps[0].action, "Click", "文本输入抓到的动作")
    # 同一份日志里的 NextList 事件要认出来（小写 + 行尾那个 `]` 都实测踩过）
    _eq(from_text.stats.nextlist_seen, 1, "NextList 事件计数")
    _eq(from_text.steps[0].parent, "BattleLoop", "父节点从 NextList 的 name 来")
    _eq(
        from_text.steps[0].candidates,
        ["BattleProbeNow", "BattleEnter"],
        "候选表按 list 顺序",
    )
    _eq(from_text.steps[0].jumps, ["BattleEnter"], "带 [JumpBack] 的候选")
    _eq(from_text.steps[0].parent_source, "nextlist", "父节点来源")
    # `_first_log_ts` 可能返回 None（不是 maafw.log 的文本），直接 .strftime 会抛
    # AttributeError 而不是可读的断言失败，所以先判 None。
    first_ts = _first_log_ts(sample)
    if first_ts is None:
        raise CheckFailure("_first_log_ts 没解析出首行时间戳")
    _eq(
        first_ts.strftime("%H:%M:%S.%f")[:-3],
        "00:30:00.050",
        "_first_log_ts 取首行时间戳",
    )

    # 路径输入路径仍要可用（本地工具的主用法）
    lines = list(_open_log_lines(sample))
    _eq(len(lines), 6, "文本按行切分")
    return [
        f"  文本 / 文本流 / 路径 三种入参一致（{len(from_text.steps)} 步，"
        f"父节点={from_text.steps[0].parent}）"
    ]


def check_flow_diagnostics() -> list[str]:
    """流程图解析的诊断行为：每种"画不出来/画得不全"的输入都要有自己的说法。

    这几条都是真实踩过的：Explore 导出的 CSV 时间戳会回跳（原来的提前 break 会把窗口内
    的记录整段丢掉）、Sentry 导出的日志只有识别事件没有 `PipelineTask node done`、
    导出按字节裁剪会把行尾切掉一半、路径写错与"窗口内没有记录"原来报同一句话。
    只解析不画图，不依赖 Pillow。
    """
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.flow_from_log import (
        FlowOptions,
        build_notes,
        describe_empty_window,
        parse_log,
        round_steps,
    )

    def raw(ts: str, msg: str, px: str = "Px1") -> str:
        return (
            f"[2026-10-14 {ts}][INF][{px}][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"
        )

    def done(ts: str, name: str, tid: int = 100, px: str = "Px1") -> str:
        return raw(
            ts,
            f'PipelineTask node done [result={{"name":"{name}","node_id":1,'
            f'"reco_id":1,"completed":true}}] [task_id_={tid}]',
            px=px,
        )

    def reco(ts: str, name: str, box: str = "[1,2,3,4]") -> str:
        return raw(
            ts,
            f'reco [result={{"name":"{name}","algorithm":"OCR","box":{box},'
            f'"reco_id":1,"detail":{{}}}}]',
        )

    out: list[str] = []

    # 1) 时间戳回跳：行要按时间重排，两条都要在
    shuffled = "\n".join(
        [done("00:30:02.000", "Second"), done("00:30:01.000", "First")]
    )
    parsed = parse_log(shuffled, None, None, None, False)
    _eq(parsed.stats.out_of_order, 1, "回跳行数")
    _eq([s.name for s in parsed.steps], ["First", "Second"], "回跳后按时间重排的顺序")
    _contains("".join(build_notes(parsed, FlowOptions())), "已按时间重排", "回跳注记")

    # 2) 只有识别事件：用识别记录合成步骤，不再抛"没有解析到任何节点执行记录"
    only_events = "\n".join(
        [reco("00:31:01.000", "EntrustChooseLevel", box="null")] * 4
    )
    parsed = parse_log(only_events, None, None, None, False)
    _eq(parsed.derived_from, "attempts", "仅事件模式的来源")
    _eq(len(parsed.steps), 4, "合成出的步骤数")
    _eq(len(round_steps(parsed, FlowOptions(collapse=3))), 1, "--collapse 3 折成一格")
    _eq(len(round_steps(parsed, FlowOptions(collapse=1))), 4, "--collapse 1 每次一格")
    _contains(
        "".join(build_notes(parsed, FlowOptions())), "由识别记录还原", "仅事件模式注记"
    )

    # 3) 行尾被截断的 Task timeout：仍要出现，并标明数字不全
    truncated = raw(
        "00:32:00.000",
        "Task timeout [pretask.name=EntrustMoveToFind] " "[duration_since(start_",
    )
    parsed = parse_log(truncated, None, None, None, False)
    _eq(
        [e.name for e in parsed.events],
        ["Task timeout EntrustMoveToFind"],
        "截断超时事件",
    )
    _contains(parsed.events[0].action, "行尾被截断", "截断超时的说明")
    _eq(parsed.stats.truncated_kept, 1, "截断但保留的条数")

    # 4) 行尾被截断的 reco：读不出就计数，不能抛
    parsed = parse_log(
        raw("00:33:00.000", 'reco [result={"name":"A","box":null'),
        None,
        None,
        None,
        False,
    )
    _eq(parsed.stats.truncated_skipped, 1, "截断被跳过的条数")

    # 5) 路径写错：必须是"没有这个文件"，不能混进"窗口内没有记录"
    opts = FlowOptions()
    missing = "no/such/dir/vision"
    message = describe_empty_window(
        parse_log(missing, None, None, None, False), opts, missing
    )
    _contains(message, "磁盘上没有这个文件", "路径写错的说法")
    _contains(message, "日志正文", "路径写错的说法")

    # 6) 窗口落在日志时间范围外：报出日志自己的时间范围
    elsewhere = done("00:34:00.000", "Only")
    from datetime import datetime as _dt

    opts = FlowOptions(t_from=_dt(2030, 1, 1), t_to=_dt(2030, 1, 2))
    message = describe_empty_window(
        parse_log(elsewhere, opts.t_from, opts.t_to, None, False), opts, elsewhere
    )
    _contains(message, "2026-10-14 00:34:00.000", "空窗口时的日志时间范围")

    # 7) --task-id 严格过滤：别的任务的失败收尾不能进图，框架级事件要留
    mixed = "\n".join(
        [
            done("00:35:00.000", "Mine", tid=100),
            raw(
                "00:35:01.000",
                'task end: [cb_detail={"entry":"Other","task_id":999}] [ret=false]',
            ),
            raw("00:35:02.000", "invalid node id, handle error [node.name=Mine]"),
        ]
    )
    parsed = parse_log(mixed, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events], ["invalid node id"], "别的任务的失败收尾被排掉"
    )
    _eq(parsed.stats.events_dropped, 1, "被排掉的失败事件数")
    _contains(
        "".join(build_notes(parsed, FlowOptions(task_id=100))),
        "已排除其他 task",
        "排掉事件的注记",
    )

    # 8) 窗口内全是别的 task：说法要指向 task_id，而不是"窗口内没有记录"
    other = done("00:36:00.000", "Theirs", tid=999)
    opts = FlowOptions(task_id=100)
    message = describe_empty_window(
        parse_log(other, None, None, 100, False), opts, other
    )
    _contains(message, "其他 task_id", "全是别人的 task 时的说法")

    # 9) 任务判定窗口：框架级事件没有 task_id，"下一个别的任务的第一步"之外的那些要排掉
    #    （实测：一份装了多次运行的导出把 23:59 那趟训练的 node not completed 画进了 13:11 的图上）
    span_log = "\n".join(
        [
            done("00:40:00.000", "Mine", tid=100),
            done("00:40:05.000", "Mine", tid=100),
            raw(
                "00:40:06.000", "invalid node id, handle error [node.name=Mine]"
            ),  # 自己的失败标记：留
            done("01:10:00.000", "Theirs", tid=200),  # 别的任务开始了：右界
            raw(
                "01:10:05.000", "node not completed, handle error [node.name=Theirs]"
            ),  # 别的运行的：排掉
        ]
    )
    parsed = parse_log(span_log, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events],
        ["invalid node id"],
        "自己的失败标记留、别的运行的排掉",
    )
    _eq(parsed.stats.events_dropped, 1, "窗口外被排掉的事件数")
    _eq(parsed.span is not None, True, "选中任务的判定窗口")
    _eq(len(parsed.steps), 2, "窗口只影响事件与未命中聚合，不动步骤")

    # 10) 有 task end 时右端就是它自己：别的任务连步骤都没有也不能漏进来
    #     （实测 200000054 的图里混进过同一运行里 200000062 的 Task timeout 与 invalid node id）
    ended_log = "\n".join(
        [
            done("00:50:00.000", "Mine", tid=100),
            raw(
                "00:50:09.000",
                'task end: [cb_detail={"entry":"TaskMine","task_id":100}] [ret=false]',
            ),
            raw("00:50:37.000", "invalid node id, handle error [node.name=Later]"),
        ]
    )
    parsed = parse_log(ended_log, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events],
        ["任务结束 TaskMine 失败"],
        "自己的 task end 之外的框架事件被排掉",
    )
    _eq(parsed.stats.events_dropped, 1, "task end 之外被排掉的事件数")

    out.append(
        "  回跳重排 / 仅事件模式（4 条识别合成步骤）/ 截断行 / 三类空窗口 / 严格 task 过滤 / "
        "任务区间 各自有说法"
    )
    return out


def check_speed_report() -> list[str]:
    """速度报告：版本排序、截断、样本量门槛、重构对比判定、系统伪任务排除。"""
    import io as _io

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.speed_report import (
        DEFAULT_COMPARE_MIN_COUNT,
        DEFAULT_LAST,
        build_speed_report,
        format_ms,
        write_console,
        write_json,
        write_markdown,
    )

    def row(task: str, release: str, p50: float, count: int) -> dict:
        return {
            "description": task,
            "release": release,
            "_count": count,
            "p50(span_duration)": p50,
            "p95(span_duration)": p50 * 2,
            "avg(span_duration)": p50,
        }

    def rel(tag: str) -> str:
        return f"MaaAshArms@v1.0.0-{tag}"

    rows = [
        # 同一任务跨 5 个版本；beta.2 只有 n=1（样本不足）
        row("Training", rel("beta.1"), 100.0, 6),
        row("Training", rel("beta.2"), 5.0, 1),
        row("Training", rel("beta.10"), 200.0, 7),
        row("Training", rel("beta.9"), 120.0, 8),
        # 框架伪任务：应被排除
        row("连接", rel("beta.1"), 4.8, 50),
        row("开始前脚本", rel("beta.1"), 9.7, 50),
        # mfa. 前缀同样是系统伪任务
        row("mfa.task_run", rel("beta.1"), 44385.0, 50),
    ]

    report = build_speed_report(rows, last=0, min_count=2)

    # 1) 版本按语义排序：beta.9 必须在 beta.10 之前（字典序会排错，这是实测踩过的坑）
    _eq(
        [v for v in report.versions],
        ["v1.0.0-beta.1", "v1.0.0-beta.2", "v1.0.0-beta.9", "v1.0.0-beta.10"],
        "版本按版本号排序而非字典序（RELEASE_PATTERN 带包名前缀，喂短标签会全失配）",
    )

    # 2) 系统伪任务被排除
    _eq(
        report.tasks,
        ("Training",),
        "mfa.* 与 config.system_labels 里的伪任务不进任务表",
    )

    # 3) 样本量门槛：n=1 的格子不算够样本
    #    cell() 对没有样本的版本返回 None，那种格子一律算"不够样本"（enough() 入参是 SpeedCell，
    #    None 要在这里先滤掉）；有格子的版本照样参与。
    enough: dict[str, bool] = {}
    for v in report.versions:
        cell = report.cell("Training", v)
        enough[v] = report.enough(cell) if cell is not None else False
    _eq(
        enough,
        {
            "v1.0.0-beta.1": True,
            "v1.0.0-beta.2": False,
            "v1.0.0-beta.9": True,
            "v1.0.0-beta.10": True,
        },
        "n=1 的版本不达门槛",
    )

    # 4) 截断：只留最近 N 个版本
    short = build_speed_report(rows, last=2, min_count=2)
    _eq(
        short.versions,
        ("v1.0.0-beta.9", "v1.0.0-beta.10"),
        f"--last 2 只留最近两个版本（默认 {DEFAULT_LAST}）",
    )

    # 5) 重构对比：beta.1(100ms) → beta.10(200ms) 是变慢
    slow = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.10"
    )
    _eq(len(slow.compares), 1, "只有两版都有数据的任务才进对比")
    _eq(slow.compares[0].verdict, "regressed", "p50 翻倍判为变慢")
    _eq(round(slow.compares[0].ratio, 3), 2.0, "比值 = 新版/基线")

    # 6) 噪声内：需要一对比值落在 [1/1.15, 1.15] ≈ [0.87, 1.15] 的组合。
    #    beta.1=100 → beta.9=120 是 1.20（超出，判变慢）；beta.10=200 → beta.9=120 是 0.60（超出，判改善）。
    #    所以专门加一对接近的：beta.3=100 → beta.4=105，比值 1.05。
    noise_rows = rows + [
        row("Training", rel("beta.3"), 100.0, 6),
        row("Training", rel("beta.4"), 105.0, 6),
    ]
    near = build_speed_report(
        noise_rows, last=0, min_count=2, compare="v1.0.0-beta.3..v1.0.0-beta.4"
    )
    _eq(near.compares[0].verdict, "noise", "比值 1.05 落在噪声带内，不硬给方向")
    over = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.9"
    )
    _eq(over.compares[0].verdict, "regressed", "比值 1.20 超出阈值 1.15 判变慢")

    # 6b) 改善：beta.10(200ms) → beta.1(100ms) 是 0.50x，明显改善
    better = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.10..v1.0.0-beta.1"
    )
    _eq(better.compares[0].verdict, "improved", "比值 0.50 低于 1/1.15 判改善")

    # 7) 样本不足的两版 → 证据不足（不是"改善"）
    thin_rows = [
        row("Gift", rel("beta.1"), 128400.0, 2),  # n=2 < compare 门槛 3
        row("Gift", rel("beta.2"), 801.1, 4),
    ]
    thin = build_speed_report(
        thin_rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.2"
    )
    _eq(thin.compares[0].verdict, "insufficient", "一侧样本不足时给证据不足而不是方向")
    _eq(
        (thin.compares[0].base_count, thin.compares[0].new_count),
        (2, 4),
        "判定里带两侧样本数",
    )
    _eq(DEFAULT_COMPARE_MIN_COUNT, 3, "对比门槛比展示门槛更严")

    # 8) --compare 的版本必须在窗口内
    _raises(
        lambda: build_speed_report(
            rows, last=1, compare="v1.0.0-beta.1..v1.0.0-beta.10"
        ),
        ValueError,
        "--compare 的版本不在窗口内",
    )
    _raises(
        lambda: build_speed_report(rows, last=0, compare="beta1"),
        ValueError,
        "--compare 缺 A..B 形式",
    )

    # 9) 输出：console 表要有样本密度提示，markdown/json 也要能出
    buffer = _io.StringIO()
    write_console(report, buffer)
    text = buffer.getvalue()
    _contains(text, "样本密度", "表格底部报样本密度")
    _contains(text, "n=1", "样本不足的格子标 n=N")

    buffer = _io.StringIO()
    write_markdown(report, buffer)
    _contains(buffer.getvalue(), "| 任务 |", "markdown 表头")

    buffer = _io.StringIO()
    write_json(report, buffer)
    _contains(buffer.getvalue(), '"min_count"', "json 报告带门槛参数")

    # 10) 毫秒/秒的人读格式
    _eq(format_ms(801.1), "801.1ms", "亚秒级用 ms")
    _eq(format_ms(246134.0), "246.1s", "超秒级换算成 s")

    return [
        "版本排序 / 伪任务排除 / 样本门槛 / 截断 / 对比判定 / 证据不足 / 三种输出 / 毫秒格式 各自有断言"
    ]


def check_explore_csv() -> list[str]:
    """Explore CSV → 日志正文 + 清单：块边界接续、五分类、任务可画性、截断计数、入口报错。

    合成一份最小 CSV（用 `csv.writer` 写，保证引号与内嵌换行是真的 CSV），覆盖实测形状：
    每格首行是半截行、有一处能接回来的跨格断行、一条被裁断的 reco、一条 MFA 面板行。
    全程不写文件（`read_cells` 接受正文），所以受限沙箱下也能跑。
    """
    import csv as _csv
    import io as _io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry import explore_csv

    def maafw(ts: str, msg: str, px: str = "Px1") -> str:
        return (
            f"[2026-10-14 {ts}][INF][{px}][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"
        )

    node_a = maafw(
        "00:01:00.000",
        'PipelineTask node done [result={"name":"TaskA","node_id":1,'
        '"reco_id":11,"completed":true}] [task_id_=111]',
    )
    node_b = maafw(
        "00:05:00.000",
        'PipelineTask node done [result={"name":"TaskB","node_id":2,'
        '"reco_id":21,"completed":true}] [task_id_=222]',
    )
    head = "[2026-10-14 00:04:00.000][INF][Px1][Tx1"  # 第一格末行：断头（下一格接着）
    tail = "][Utils/EventDispatcher.hpp][L65][MaaNS::EventDispatcher::notify] !!!OnEventNotify!!!"
    dangling = "[2026-10-14 00:06:00.000][INF][Px1][Tx1"  # 第二格末行：没人接的断头
    cell_one = "\n".join(
        [
            'ls={"focus":null,"list":[]}',  # 碎片（没有时间戳）
            node_a,
            maafw(
                "00:03:00.000", 'reco [result={"name":"A","algorithm":"OCR","box":null'
            ),  # 被裁断
            head,
        ]
    )
    cell_two = "\n".join(
        [
            tail,  # 接上第一格的断头
            node_b,
            "[2026-10-14 00:00:50.306][WRN] [cfg=Default] 实时画面为空：截图方式=EmulatorExtras",
            dangling,
        ]
    )
    buffer = _io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["timestamp", "message"])
    writer.writerow(["2026-10-14T00:06:07+00:00", cell_one])
    writer.writerow(["2026-10-14T00:06:07+00:00", cell_two])
    text = buffer.getvalue()

    result = explore_csv.extract(text)
    recovered, parse = result.inventory["recovered"], result.inventory["parse"]
    _eq(result.inventory["source"]["csv_rows"], 2, "数据行数")
    _eq(recovered["boundaries_joined"], 1, "接回的跨格断行数")
    _eq(
        recovered["lines_complete"],
        4,
        "结构完整的行（两条 node done + 接回的那条 + 被裁断的 reco）",
    )
    _eq(recovered["lines_panel"], 1, "面板行数")
    _eq(recovered["lines_head_cut"], 1, "断头行数（没人接的那条）")
    _eq(recovered["lines_fragment"], 1, "碎片行数")
    _contains(result.log_text, tail, "接回来的那行进了正文")
    _eq(parse["steps"], 2, "两条 node done")
    _eq(parse["truncated_skipped"], 1, "被裁断的记录数")
    _eq(parse["truncated_by_head"], {"reco [result=": 1}, "截断记录按类型拆")
    _eq([task["task_id"] for task in result.tasks], [111, 222], "清单里的任务")
    _eq([task["drawable"] for task in result.tasks], [True, True], "两个任务都可画")
    _eq(result.inventory["panel_lines"]["count"], 1, "面板行计数")
    _contains(
        result.inventory["panel_lines"]["samples"][0]["text"],
        "实时画面为空",
        "面板行样例",
    )
    _eq(result.inventory["images"], False, "导出不含图片")

    # 卡在派发层的任务：只有 1 步 node done，但窗口里有 6 次连续未命中 —— 清单要能点名
    # （`reco` 行没有 task_id，窗口靠该 task 的 Node.NextList 事件撑开，所以这里必须带一条）
    def nextlist(ts: str, name: str, names: list[str], tid: int) -> str:
        entries = ",".join(
            '{"anchor":false,"jump_back":false,"name":"%s"}' % n for n in names
        )
        return maafw(
            ts,
            "!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.Failed] "
            f'[details={{"focus":null,"list":[{entries}],"name":"{name}","task_id":{tid}}}] ',
        )

    stall_lines = [
        nextlist("00:07:00.000", "TaskC", ["CX", "CY"], 333),
        *[
            maafw(
                f"00:07:0{i}.000",
                'reco [result={"name":"CX","algorithm":"OCR","box":null,'
                f'"reco_id":3{i}}}]',
            )
            for i in range(1, 7)
        ],
        maafw(
            "00:07:07.000",
            'PipelineTask node done [result={"name":"TaskC","node_id":9,'
            '"reco_id":39,"completed":true}] [task_id_=333]',
        ),
    ]
    stall_buffer = _io.StringIO()
    stall_writer = _csv.writer(stall_buffer, lineterminator="\r\n")
    stall_writer.writerow(["timestamp", "message"])
    stall_writer.writerow(["2026-10-14T00:08:00+00:00", "\n".join(stall_lines)])
    stall = explore_csv.extract(stall_buffer.getvalue())
    stall_task = next(task for task in stall.tasks if task["task_id"] == 333)
    _eq(stall_task["node_done"], 1, "空转任务只有 1 步")
    _eq(stall_task["misses"], 6, "窗口内的未命中次数")
    _eq(stall_task["stalled_misses"], 6, "最长的一段连续未命中")
    _eq(stall_task["stalled_at"], "2026-10-14T00:07:01.000", "空转起点（第一条未命中）")
    _eq(
        stall_task["window"][0],
        "2026-10-14T00:07:00.000",
        "窗口起点取 NextList 记录（早于第一步）",
    )
    stall_notes = " ".join(stall.inventory["notes"])
    _contains(stall_notes, "task 333", "清单点名空转任务")
    _contains(stall_notes, "连续 6 次识别未命中", "清单说明空转形态")

    # 列不对 / 路径不对要报清楚，不能混成"解析出 0 行"
    try:
        explore_csv.extract("trace,span.op\n1,db")
    except ValueError as error:
        _contains(str(error), "没有 message 列", "缺 message 列的报错")
    else:
        raise CheckFailure("缺 message 列时没有报错")
    try:
        explore_csv.extract("no/such/explore.csv")
    except FileNotFoundError as error:
        _contains(str(error), "没有这个文件", "路径写错的报错")
    else:
        raise CheckFailure("路径写错时没有报错")

    return [
        f"  2 格 CSV → {recovered['lines']} 行（完整 {recovered['lines_complete']} / 面板 "
        f"{recovered['lines_panel']}），接回 {recovered['boundaries_joined']} 处断行，"
        f"{len(result.tasks)} 个任务可画"
    ]


def check_evidence() -> list[str]:
    """失败证据的纯逻辑：解包白名单、附件名取 task_id、去重、扁平 context 键、可复制命令。

    只测不联网、不写文件的部分（取附件与取日志要 Sentry 登录，属于 `evidence` 的手工验收）。
    """
    import io as _io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry import evidence as ev

    # 1) 解包白名单：zip slip 与绝对路径都丢掉，`debug/…` 保留
    names = [
        "debug/on_error/2026.10.14-19.20.00.789_TrainingBattleWin.png",
        "../evil.txt",
        "/abs.png",
        "C:\\abs.png",
        "debug/",
        "",
    ]
    _eq(
        ev.safe_members(names),
        ["debug/on_error/2026.10.14-19.20.00.789_TrainingBattleWin.png"],
        "解包白名单",
    )

    # 2) 附件名 → task_id（`--task-id` 就是靠它拼出来的）
    _eq(
        ev.parse_task_id(
            "mfa-task-failure-3204f818b4e04a3494b29d911ec47724-200000062-screenshots.zip"
        ),
        200000062,
        "附件名里的 task_id",
    )
    _eq(ev.parse_task_id("log_20261014-part01.zip"), None, "非附件名不瞎猜")

    # 3) 逐字节相同的行要合并（MFA 续写的 64 KB 前缀重叠）
    _eq(ev.dedupe_lines(["a", "b", "a", "c", "b"]), (["a", "b", "c"], 2), "重复行合并")

    # 4) context 的键名点号是字符串的一部分，不是层级
    ctx = ev.issue_context(
        {
            "context": {
                "attachment.status": "attached",
                "logs.status": "captured",
                "failure.task_id": 200000062,
                "instance.task_count": 11,
            }
        }
    )
    _eq(ctx["attachment_status"], "attached", "attachment.status 取值")
    _eq(ctx["failure_task_id"], 200000062, "failure.task_id 取值")
    _eq(ev.wants_images(ctx), True, "attached 才下图")
    _eq(ev.wants_log(ctx), True, "captured 才下日志")

    # 5) 失败标记认得出节点名
    markers = ev.marker_lines(
        [
            "[2026-10-14 19:20:37.887][WRN][Px1][Tx1][PipelineTask.cpp][L155][MaaNS::T] "
            "Task timeout [pretask.name=EntrustEnterMainLine] [duration_since(start_clock)=20410ms] "
            "[pretask.reco_timeout=20000ms]",
            "[2026-10-14 19:20:37.888][WRN][Px1][Tx1][PipelineTask.cpp][L83][MaaNS::T] "
            "invalid node id, handle error [node.name=EntrustEnterMainLine]",
        ]
    )
    _contains(" ".join(markers), "EntrustEnterMainLine", "失败标记的节点名")

    # 6) 命令里必须带上 failure.task_id；没日志时不给 flow 的命令
    issue = ev.Issue(
        short_id="PYTHON-14",
        title="",
        count=2,
        last_seen="",
        event_id="e",
        trace_id="t",
        evidence=ctx,
    )
    hints = ev.build_hints(
        _Path("temp/evidence/PYTHON-14"),
        issue,
        log_rel="debug/maafw.log",
        has_images=True,
    )
    _eq(len(hints), 2, "有图有日志时给两条命令")
    for hint in hints:
        _contains(hint, "--task-id 200000062", "命令里的 task_id")
    _contains(hints[0], "--case temp/evidence/PYTHON-14/debug", "目录 case 命令")
    _eq(
        ev.build_hints(
            _Path("temp/evidence/PYTHON-14"), issue, log_rel=None, has_images=True
        ),
        [],
        "没有日志就没有可画的东西",
    )

    # 7) 状态表：一眼看出哪些有图、哪些只有日志
    buffer = _io.StringIO()
    ev.format_survey([issue], buffer)
    _contains(buffer.getvalue(), "图 + 日志", "状态表的标记")

    _eq(ev.MAX_LOG_LIMIT, 1000, "trace logs 的 limit 上限")
    return [
        f"解包白名单 / 附件名取 task_id / 去重 / 扁平 context / 失败标记 / 命令 各自有断言"
        f"（schema {ev.EVIDENCE_SCHEMA}）"
    ]


def check_evidence_disk_agreement() -> list[str]:
    """清单与磁盘必须一致：磁盘上还留着的日志/现场图必须记进 `written`，hints 也要跟着给。

    对应踩过的坑：`evidence --issue X --force --no-log` 之后清单写着"没有日志、画不出图"，
    而 `debug/maafw.log` 就在旁边（工具不删旧文件）。这里在 `temp/selftest-evidence/` 下造一棵
    最小的证据树直接断言，不联网、不碰真实的 `temp/evidence/<ID>/`。
    """
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry import evidence as ev

    scratch = root / DEFAULT_ARTIFACT_DIR / "selftest-evidence"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    directory = scratch / "PYTHON-14"
    try:
        # 只有半截断头，检验 record 记的是"文件在不在"而不是"内容完不完整"
        (directory / "debug" / "on_error").mkdir(parents=True, exist_ok=True)
        log_bytes = "tail(512KB)\n".encode()
        blank = bytes.fromhex("89504e470d0a1a0a")
        (directory / "debug" / "maafw.log").write_bytes(log_bytes)
        (
            directory
            / "debug"
            / "on_error"
            / "2026.10.14-19.20.37.889_EntrustEnterMainLine.png"
        ).write_bytes(blank)
        (directory / "debug" / "on_error" / "readme.txt").write_text(
            "不是现场图", encoding="utf-8"
        )

        log_rel, images = ev.existing_artifacts(directory)
        _eq(log_rel, "debug/maafw.log", "扫到磁盘上的日志")
        _eq(
            images,
            ["debug/on_error/2026.10.14-19.20.37.889_EntrustEnterMainLine.png"],
            "只算 on_error/*.png",
        )

        # 本次没下任何东西（--force --no-log）：磁盘上有的都要补进清单并给出提示
        written: dict[str, Any] = {
            "log": None,
            "log_bytes": 0,
            "images": [],
            "zip_bytes": 0,
        }
        notes: list[str] = []
        ev.reconcile_written(directory, written, log_rel, images, notes)
        _eq(written["log"], "debug/maafw.log", "磁盘上的日志进了 written")
        _eq(written["log_bytes"], len(log_bytes), "原有日志记了真实字节数")
        _eq(len(written["images"]), 1, "磁盘上的现场图进了 written")
        _contains(notes[0], "原有的", "记清楚了是原有的、不是本次下的")

        issue = ev.Issue(
            short_id="PYTHON-14",
            title="",
            count=1,
            last_seen="",
            event_id="e",
            trace_id="t",
        )
        hints = ev.build_hints(
            directory,
            issue,
            log_rel=str(written["log"]),
            has_images=bool(written["images"]),
        )
        _eq(len(hints), 2, "补进 written 后就有可复制的绘图命令")
        _contains(hints[0], "--case", "目录 case 那条命令")
        _contains(hints[1], "--on-error", "有现场图就带 --on-error")
        _eq(
            ev.build_hints(directory, issue, log_rel=None, has_images=False),
            [],
            "没有日志就没有可画的东西",
        )

        # 本次下了日志：保留本次的字节数，不拿磁盘上的旧值盖掉，也不重复记图片
        again: dict[str, Any] = {
            "log": "debug/maafw.log",
            "log_bytes": 999,
            "images": [images[0]],
            "zip_bytes": 1,
        }
        more: list[str] = []
        ev.reconcile_written(directory, again, log_rel, images, more)
        _eq(
            (again["log_bytes"], len(again["images"]), more),
            (999, 1, []),
            "本次下过就不动它",
        )

        # 空目录：不能凭空记出东西来
        empty = scratch / "empty"
        empty.mkdir(parents=True, exist_ok=True)
        blank_written: dict[str, Any] = {
            "log": None,
            "log_bytes": 0,
            "images": [],
            "zip_bytes": 0,
        }
        ev.reconcile_written(empty, blank_written, *ev.existing_artifacts(empty), [])
        _eq(
            (blank_written["log"], blank_written["images"]),
            (None, []),
            "空目录不补任何东西",
        )
        _eq(ev.existing_artifacts(empty), (None, []), "空目录扫出来是空的")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return ["磁盘上留着的日志/现场图会补进清单（`--force --no-log` 不再把清单写坏）"]


def check_report_lock() -> list[str]:
    """并发锁的判据：活着才提示、已退出就清掉；读不出 pid 时按旧行为提示（宁多勿漏）。"""
    import os as _os
    import subprocess as _subprocess
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry import report_common as rc

    scratch = root / DEFAULT_ARTIFACT_DIR / "selftest-lock"
    scratch.mkdir(parents=True, exist_ok=True)
    lock = scratch / rc.REPORT_LOCK_NAME
    try:
        _eq(rc.lock_owner_alive(lock), None, "锁文件不存在时不猜")
        lock.write_text("", encoding="utf-8")
        _eq(rc.lock_owner_alive(lock), None, "空锁文件不猜")

        # 已退出的进程：Windows 上 `os.kill(pid, 0)` 对它是安静的，所以这里顺带钉住判据是退出码
        child = _subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        dead = child.pid
        del child
        lock.write_text(str(dead), encoding="utf-8")
        _eq(rc.lock_owner_alive(lock), False, f"已退出的 pid（{dead}）判成不在了")
        buffer = io.StringIO()
        previous, sys.stderr = sys.stderr, buffer
        try:
            with rc.report_run_guard(lock):
                inside = (buffer.getvalue(), lock.read_text(encoding="utf-8"))
        finally:
            sys.stderr = previous
        warning, held = inside
        _eq(warning, "", "残留锁不再打「另一个报告正在运行」")
        _eq(held, str(_os.getpid()), "持锁期间锁文件写的是自己的 pid")
        _eq(lock.is_file(), False, "退出时清掉自己的锁")
        # 活着的进程：仍然要提示（提示是建议性的，不该因为这次改动消失）
        alive = _subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            lock.write_text(str(alive.pid), encoding="utf-8")
            _eq(rc.lock_owner_alive(lock), True, f"活着的 pid（{alive.pid}）判成还在")
            buffer = io.StringIO()
            previous, sys.stderr = sys.stderr, buffer
            try:
                with rc.report_run_guard(lock):
                    pass
            finally:
                sys.stderr = previous
            _contains(buffer.getvalue(), "另一个 Sentry 报告正在运行", "真并发仍然提示")
        finally:
            alive.kill()
            alive.wait()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return ["残留锁（持锁进程已退出）不提示且自动清掉；真并发仍提示"]


def check_flow_parents() -> list[str]:
    """父节点（派发者）的归属规则：候选命中、自循环、跨 task 隔离、几种退回。

    `Node.NextList.*` 是父子链的唯一来源（`[cur_node_=X]` 只知道派发者、不知道候选表）。
    全用合成日志，不联网、不写文件；真实日志上那条链另有验收（`.agents/project.md` 记了结论）。
    """
    import sys
    from datetime import datetime
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.flow_from_log import (
        FlowOptions,
        build_notes,
        parent_groups,
        parse_log,
        parse_nextlist,
        resolve_parent,
        stall_notes,
    )

    def line(ts: str, msg: str, px: str = "Px1") -> str:
        return f"[2026-10-14 {ts}][INF][{px}][Tx1][Unit.cpp][L1][MaaNS::T] {msg}"

    def nextlist(
        ts: str,
        name: str,
        names: list[str],
        tid: int,
        jumps: tuple[str, ...] = (),
        px: str = "Px1",
        kind: str = "Starting",
    ) -> str:
        entries = ",".join(
            '{"anchor":false,"jump_back":%s,"name":"%s"}'
            % ("true" if n in jumps else "false", n)
            for n in names
        )
        return line(
            ts,
            f"!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.{kind}] "
            f'[details={{"focus":null,"list":[{entries}],"name":"{name}","task_id":{tid}}}] ',
            px,
        )

    def done(ts: str, name: str, tid: int, px: str = "Px1") -> str:
        return line(
            ts,
            'PipelineTask node done [result={"name":"%s","node_id":1,"reco_id":1,'
            '"action_id":1,"completed":true}] [task_id_=%d]' % (name, tid),
            px,
        )

    # 1) 候选表里同时有本步与别的候选：父节点 = 派发者，候选表与 [JumpBack] 照抄
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000",
                "TrainingLoop",
                ["TrainingGetStatus", "TrainingEnterOne"],
                100,
            ),
            done("00:00:02.000", "TrainingEnterOne", 100),
        ]
    )
    # 局部变量叫 `st` 而不是 `step`：下面第 9 项会 `def step(...)`，同名会让类型检查把前面的
    # 赋值当成 `step` 的声明，后面每次 `step(...)` 调用都报"Step 不可调用"（Pylance 口径实测 5 条）。
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(st.parent, "TrainingLoop", "父节点 = 派发者")
    _eq(st.candidates, ["TrainingGetStatus", "TrainingEnterOne"], "候选表原样带上")
    _eq(
        st.parent_detail(),
        "候选：TrainingGetStatus ・ √TrainingEnterOne",
        "框里那一行（派发者名字在分组框角标里，这一行只写候选）",
    )

    # 2) 自循环 / 入口合成轮：list 里含自己 → 不画成父子，写成"自派发/自循环"
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000", "BattleWin", ["BaseClickRewardTap", "BattleWin"], 100
            ),
            done("00:00:02.000", "BattleWin", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(st.dispatched, True, "自己派发自己")
    _eq(st.parent_detail(), "父 BattleWin（自派发/自循环）", "自循环的说法")

    # 3) 候选多于 PARENT_CHOICE_LIMIT 项：框里折成"另 N 项"；--list 用 full=True 给全
    text = "\n".join(
        [
            nextlist("00:00:01.000", "Hub", ["A", "B", "C", "D", "E"], 100),
            done("00:00:02.000", "D", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _contains(st.parent_detail(), "另 3 项", "框里折起来的候选数")
    _eq(st.parent_detail().count("√"), 0, "被折掉的那个不在框里")
    _contains(st.parent_detail(full=True), "√D", "全量时标出本次跑的那个")
    _eq(st.parent_detail(full=True).count("・"), 4, "全量时五个候选都在")

    # 4) 跨 task 隔离：同名公共节点出现在别的任务的候选表里，不能认成它的父节点
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000", "EntrustLoop", ["EntrustTouchDown", "BaseMoveUp"], 200
            ),
            nextlist(
                "00:00:01.500", "BattleLoop", ["BattleProbeNow", "BaseMoveUp"], 100
            ),
            done("00:00:02.000", "BaseMoveUp", 100),
        ]
    )
    _eq(
        parse_log(text, None, None, 100, False).steps[0].parent,
        "BattleLoop",
        "只认同 task 的派发记录",
    )

    # 5) 退回顺序：没有候选记录时用 [cur_node_=X]；连它也没有时留空（图上写"未记录"）
    text = "\n".join(
        [
            line(
                "00:00:01.000",
                "recognize_list [cur_node_=BattleLoop] [list=[{name=BattleProbeNow}]]",
            ),
            done("00:00:02.000", "BattleProbeNow", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(
        (st.parent, st.parent_source, st.candidates),
        ("BattleLoop", "walk", []),
        "退回 cur_node_",
    )
    st = parse_log(
        done("00:00:02.000", "BattleProbeNow", 100), None, None, 100, False
    ).steps[0]
    _eq(
        (st.parent, st.parent_source, st.parent_detail()),
        ("", "none", "父 未记录"),
        "取不到就留空",
    )

    # 6) agent 进程那种形态（`details_json`）**不能**用来判父节点：它的载荷只有 focus + list，
    #    没有 name / task_id（实测本机日志 16 条全是这样）→ 必须返回 None，不能猜。
    agent_line = (
        "[2026-10-14 00:00:01.000][TRC][Px2][Tx1][AgentClient.cpp][L1]"
        "[MaaNS::A::ctx_event_sink] [handle=true] [message=Node.NextList.Starting] "
        '[details_json={"focus":null,"list":[{"anchor":false,"jump_back":false,"name":"X"}]}] '
    )
    _eq(
        parse_nextlist(agent_line, datetime(2026, 10, 14, 0, 0, 1), "Px2"),
        None,
        "没有 name 的 details_json 不参与父节点归属",
    )
    # 但行尾那个 `]` 与大小写要认：同一个正则也得吃下 details_json 里带 name 的写法
    named = agent_line.replace('"focus":null', '"focus":null,"name":"Hub","task_id":7')
    record = parse_nextlist(named, datetime(2026, 10, 14, 0, 0, 1), "Px2")
    if record is None:
        raise CheckFailure("带 name 的 details_json 形态没解析出来")
    _eq(
        (record.name, record.task_id, record.candidates),
        ("Hub", 7, ("X",)),
        "details_json 形态",
    )

    # 7) 时间上限：几十分钟前那条派发记录不再算这一步的父节点
    stale = parse_nextlist(
        nextlist("00:00:01.000", "Hub", ["A"], 100),
        datetime(2026, 10, 14, 0, 0, 1),
        "Px1",
    )
    if stale is None:
        raise CheckFailure("合成的 NextList 行没解析出来")
    _eq(
        resolve_parent([stale], 100, "A", datetime(2026, 10, 14, 1, 0, 1))[0],
        None,
        "超过 NEXTLIST_MAX_GAP_SECONDS 的派发记录不认",
    )

    # 8) 账按**进图的步骤**算，不按解析过的所有记录（`--task-id` 会滤掉别的任务的步骤）
    only_done = parse_log(
        done("00:00:02.000", "BattleProbeNow", 100), None, None, 100, False
    )
    _eq(
        (only_done.stats.parent_unknown, only_done.stats.parent_resolved),
        (1, 0),
        "未归属计数",
    )
    _contains(
        " ".join(build_notes(only_done, FlowOptions(task_id=100))),
        "父节点只能留空",
        "没有 NextList 事件时的注记",
    )

    # 9) 分组（图上套框 + 左上角标）：连续同父才算一组，父节点自己 / 未记录 / 任务起止都不入组
    def step(
        name: str, parent: str, kind: str = "hit", candidates: tuple[str, ...] = ("x",)
    ) -> Any:
        from tools.drawing.flow_from_log import Step as _Step

        return _Step(
            ts=datetime(2026, 10, 14, 0, 0, 1),
            name=name,
            completed=True,
            kind=kind,
            dispatcher=parent,
            candidates=list(candidates) if parent != name else [],
        )

    column = [
        step("TaskA", "TaskA", kind="task"),  # 任务起始：不入组
        step("A1", "Hub"),
        step("A2", "Hub"),  # 同一个父节点连着两步 → 一组
        step("Hub", "Hub"),  # 自派发（父就是自己）→ 不入组，自己隔开
        step("B1", "A2"),  # 父换了 → 新组
        step("none1", ""),  # 父未记录 → 不入组
        step("C1", "A2"),  # 与 B1 不连续 → 另起一组（同父不同组）
    ]
    _eq(
        parent_groups(column),
        [("Hub", 1, 2), ("A2", 4, 4), ("A2", 6, 6)],
        "连续同父才成组；自派发/未记录/任务起止不入组",
    )
    # 10) 候选表评估账 + "卡在派发层"的注记：连续 Failed 才是证据（单次 Failed 是循环推进的正常现象）
    text = "\n".join(
        [
            nextlist("00:00:01.000", "TaskLoop", ["Entry"], 100, kind="Succeeded"),
            nextlist(
                "00:00:02.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            nextlist(
                "00:00:03.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            nextlist(
                "00:00:04.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            done("00:00:05.000", "TaskLoop", 100),
        ]
    )
    parsed = parse_log(text, None, None, 100, False)
    stat = parsed.nextlist_stats[(100, "TaskLoop")]
    _eq(
        (stat.starting, stat.succeeded, stat.failed),
        (0, 1, 3),
        "候选表评估账按事件类型分记",
    )
    _eq(
        stat.real_candidates,
        ("ReplaysStart", "ReplaysEnd"),
        "真实候选表取全未命中那轮的（入口轮是 [自己]，不算）",
    )
    notes = stall_notes(parsed, 100, {"TaskLoop"})
    _eq(len(notes), 1, "只报一个派发者")
    _contains(notes[0], "3 轮全未命中", "注记给出未命中轮数")
    _contains(notes[0], "一个候选都没跑起来", "候选全没跑起来时的说法")
    _eq(
        stall_notes(parsed, 100, {"TaskLoop", "ReplaysStart"})[0].count("跑起来的候选"),
        1,
        "有候选跑起来时改说跑起来的是哪个",
    )
    _eq(stall_notes(parsed, 999, set()), [], "别的 task 不串台")
    return [
        "候选命中 / 跨 task 隔离 / 自循环 / 折行 / 三种退回 / 时间上限 / 计数口径 / 分组 / "
        "候选表评估账 各自有断言"
    ]


def check_flow_overlay() -> list[str]:
    """重复轮次合并（`overlay_repeats`）：只有连续、同形、整轮无图、无失败框才折。"""
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.flow_from_log import (
        ColumnSpan,
        Step,
        column_signature,
        overlay_repeats,
    )

    def step(
        name: str, kind: str = "hit", run_len: int = 1, completed: bool = True
    ) -> Step:
        return Step(
            ts=_dt(2026, 10, 21, 0, 0, 1),
            name=name,
            completed=completed,
            kind=kind,
            run_len=run_len,
        )

    def col(*names: str) -> list[Step]:
        return [step(n) for n in names]

    def no_thumb(_index: int) -> bool:
        """这几轮都没贴到图。"""
        return False

    def all_thumb(_index: int) -> bool:
        """每一轮都有图（用来验证"有图就不折"）。"""
        return True

    # 1) 三段同形 → 一列 ×3；用首轮的步骤列表（时间戳真实，不是复制出来的第 2/3 轮）
    columns = [col("A", "B"), col("A", "B"), col("A", "B")]
    merged = overlay_repeats(columns, columns, no_thumb, min_span=2)
    _eq(len(merged), 1, "三段同形应折成一列")
    _eq(merged[0][1], ColumnSpan(0, 2), "合并范围")
    _eq(merged[0][0], columns[0], "合并列用首轮的步骤（同一对象，不是副本）")

    # 2) 有图就不折：整组无图才合并，中间任何一轮有图都断开
    thumbs = {1}
    merged = overlay_repeats(columns, columns, lambda i: i in thumbs, min_span=2)
    _eq(
        [m[1] for m in merged],
        [ColumnSpan(0, 0), ColumnSpan(1, 1), ColumnSpan(2, 2)],
        "有图的那一轮把重复段切断",
    )

    # 3) 失败框不折：`classify()` 判 error 的框（未完成的 event）不能被 ×N 藏起来
    bad = col("A", "B") + [step("node not completed", kind="event", completed=False)]
    merged = overlay_repeats(
        [col("A", "B"), bad, col("A", "B")], [bad] * 3, no_thumb, min_span=2
    )
    _eq(len(merged), 3, "带失败框的轮不折")

    # 4) 未命中聚合框进签名：`A,B` 与 `A,B,未命中×6` 形状不同
    with_miss = col("A", "B") + [step("B", kind="miss", run_len=6)]
    _eq(
        column_signature(col("A", "B")) == column_signature(with_miss),
        False,
        "未命中聚合框要与命中步骤区分开",
    )
    merged = overlay_repeats(
        [col("A", "B"), with_miss], [col("A", "B"), with_miss], no_thumb, min_span=2
    )
    _eq(len(merged), 2, "多一个未命中框就不是同形")

    # 5) 折叠次数进签名：`×3` 的一格与三格不等价
    _eq(
        column_signature([step("A", run_len=3)])
        == column_signature(col("A", "A", "A")),
        False,
        "折叠后的 ×N 与逐次记录不等价",
    )

    # 6) 从列首对齐、不做子序列匹配：`A,B,C,D,E` 与 `B,C,D,E` 不合并
    long, short = col("A", "B", "C", "D", "E"), col("B", "C", "D", "E")
    merged = overlay_repeats(
        [long, short, short], [long, short, short], no_thumb, min_span=2
    )
    _eq(
        [m[1] for m in merged],
        [ColumnSpan(0, 0), ColumnSpan(1, 2)],
        "前缀差一步不算重复（后两轮才合并）",
    )

    # 7) 阈值：min_span=2 时两轮就折；min_span=3 时两轮不折
    two = [col("A"), col("A")]
    _eq(len(overlay_repeats(two, two, no_thumb, min_span=2)), 1, "两轮同形即折")
    _eq(len(overlay_repeats(two, two, no_thumb, min_span=3)), 2, "阈值 3 时两轮不折")
    _eq(len(overlay_repeats(two, two, all_thumb, min_span=2)), 2, "有图不折")

    # 8) 中间夹一轮不同形状 → 断成两段，各自按阈值判
    a, b = col("A"), col("B")
    mixed = [a, a, b, a, a]
    _eq(
        [m[1] for m in overlay_repeats(mixed, mixed, no_thumb, min_span=2)],
        [ColumnSpan(0, 1), ColumnSpan(2, 2), ColumnSpan(3, 4)],
        "不连续的重复各自成段",
    )

    return [
        "同形合并 / 有图断开 / 失败断开 / 未命中进签名 / 前缀不等价 / 阈值 / 不连续 各自有断言"
    ]


CHECKS: tuple[tuple[str, Callable[[], list[str]]], ...] = (
    ("release 解析", check_release_parsing),
    ("版本排序", check_ordering),
    ("Wilson 区间", check_wilson),
    ("任务表与失败标记分流", check_task_rows),
    ("运行失败率", check_release_rows),
    ("运行环境分布", check_context_rows),
    ("跨版本趋势与 Δpp", check_trend),
    ("工件往返", check_artifact_roundtrip),
    ("版本过滤报错文案", check_version_filter_messages),
    ("报告渲染", check_renderers),
    ("流程图解析（文本入参）", check_flow_parser),
    ("流程图诊断（回跳/仅事件/截断/空窗口）", check_flow_diagnostics),
    ("Explore CSV 提取", check_explore_csv),
    ("失败证据的纯逻辑", check_evidence),
    ("证据清单与磁盘一致", check_evidence_disk_agreement),
    ("并发锁的持有者判据", check_report_lock),
    ("流程图的父节点（派发者）", check_flow_parents),
    ("流程图重复轮次合并", check_flow_overlay),
    ("跨版本耗时报告", check_speed_report),
)


def run_selftest(emit: Emit = print, *, quiet: bool = False) -> int:
    """跑全部自检，返回 0 表示全部通过、1 表示有失败。"""
    failures = 0
    for title, check in CHECKS:
        try:
            details = check()
        except CheckFailure as error:
            failures += 1
            emit(f"[失败] {title}: {error}")
            continue
        # 自检要把任何异常都报成失败而不是崩掉；抑制注释与 `except` 写在同一行，
        # 因为多行 `except (\n Exception\n):  # 抑制` 在 ruff 0.17 起会被判成"抑制未使用"。
        except Exception as error:  # noqa: BLE001
            failures += 1
            emit(f"[失败] {title}: 未预期异常 {type(error).__name__}: {error}")
            continue
        if not quiet:
            emit(f"[通过] {title}")
            for line in details:
                emit(line)

    total = len(CHECKS)
    if failures:
        emit(f"\n自检未通过：{failures}/{total} 项失败。")
        return 1
    emit(f"\n自检通过：{total}/{total} 项。")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(_stream, io.TextIOWrapper):
            _stream.reconfigure(errors="replace")
    raise SystemExit(run_selftest())
