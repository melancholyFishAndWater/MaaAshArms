"""遥测报表用例：release 解析与排序、`--telemetry-version` 的筛选、Wilson 区间、任务/版本/环境表、趋势与工件往返、渲染器输出、遥测索引。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import shutil


from .asserts import save_probe_artifact, CheckFailure, _eq, _contains
from .deps import CONFIG, task_alias_map, task_failure_report, task_trend_report, to_neutral_rows, DEFAULT_ARTIFACT_DIR, Artifact, release_version_key, repo_root, version_label, wilson_interval
from .fixtures import RELEASE_D38, RELEASE_D37_NEW_MFA, V38, V37, V36, ROWS_TOTAL, ROWS_STATUS, ROWS_RUN_TOTAL, ROWS_RUN_FAILED, ROWS_RELEASE_USERS, ROWS_RUNTIME_OS, ROWS_RUNTIME_ENV, MEASUREMENTS, EXPECTED_BETA37_TOTAL, EXPECTED_BETA37_FAILED, ARTIFACT_FIXTURE_TIME
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


def check_version_filter() -> list[str]:
    """`cases --telemetry-version` 的条件解析与版本比较（实现画图侧，输入是遥测卡片）。

    比较口径必须与报表一致：沿用 `release_version_key` 的排序键，双 `@` release 里取**资源版本**
    （不能取到 `MFA@v2.16.1`），预发布按 alpha < beta < rc < 正式版排。
    """
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.cases.version_filter import VersionFilter, parse_version
    from tools.sentry.lib.telemetry_index import IssueCard

    lines: list[str] = []
    # 1) 版本解析：裸版本号与整串 release 归一到同一套键与标签
    _eq(parse_version("v1.0.0-beta.61"), ((1, 0, 0, 0, 61), "v1.0.0-beta.61"), "裸版本号的键与标签")
    _eq(parse_version("1.0.0"), ((1, 0, 0, 2, 0), "v1.0.0"), "正式版的键与标签")
    _eq(
        parse_version("MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61"),
        ((1, 0, 0, 0, 61), "v1.0.0-beta.61"),
        "整串 release 取资源版本，不能取到 MFA 版本",
    )
    _eq(parse_version("MFA@v2.16.1"), None, "只有客户端版本时解析失败")
    _eq(parse_version("beta.61"), None, "缺数字段的预发布不是版本号")

    # 2) 条件解析：运算符缺省 ==，`describe()` 归一成 vX.Y.Z[-pre.N]
    _eq(VersionFilter.parse(["v1.0.0-beta.61"]).describe(), "==v1.0.0-beta.61", "缺省运算符")
    _eq(VersionFilter.parse([">= 1.0.0"]).describe(), ">=v1.0.0", "运算符与版本之间的空格")
    _eq(
        VersionFilter.parse([">=v1.0.0-beta.55", "<=v1.0.0-beta.60"]).describe(),
        ">=v1.0.0-beta.55 且 <=v1.0.0-beta.60",
        "多个条件按且叠加",
    )

    # 3) 比较：beta < rc < 正式版；版本未知不算命中
    at_least = VersionFilter.parse([">=v1.0.0-beta.61"])
    hit = [
        version
        for version in ("v1.0.0-beta.61", "v1.0.0-beta.62", "v1.0.0-rc.1", "v1.0.0")
        if at_least.matches(version)
    ]
    _eq(hit, ["v1.0.0-beta.61", "v1.0.0-beta.62", "v1.0.0-rc.1", "v1.0.0"], ">= 含 rc 与正式版")
    _eq(at_least.matches("v1.0.0-beta.60"), False, ">= 不含更早的 beta")
    _eq(VersionFilter.parse(["<=v1.0.0-beta.55"]).matches("v1.0.0-beta.54"), True, "<= 含更早的 beta")
    _eq(at_least.matches(""), False, "没有 release 不算命中")
    _eq(at_least.matches("MFA@v2.16.1"), False, "解析不出资源版本不算命中")

    # 4) 分堆：命中 / release 缺失 / 版本不在范围内，三种处置不同
    cards = [
        IssueCard("PYTHON-A", release="MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55"),
        IssueCard("PYTHON-B", release="MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61"),
        IssueCard("PYTHON-C", release=""),
    ]
    kept, unknown, outside = at_least.partition(cards)
    _eq([card.short_id for card in kept], ["PYTHON-B"], "命中的卡片")
    _eq(unknown, ["PYTHON-C"], "release 缺失的卡片")
    _eq(outside, ["PYTHON-A"], "版本不在范围内的卡片")

    # 5) 看不懂的条件要报错并给写法，不能静默当成空筛选
    try:
        VersionFilter.parse([">=v1"])
    except ValueError as error:
        _contains(str(error), "看不懂", "版本条件看不懂时的报错")
        _contains(str(error), ">=v1.0.0-beta.61", "报错里带写法示例")
    else:
        raise CheckFailure("看不懂的版本条件没有报错")

    lines.append("  >=/<=/== 比较、双 @ release 取资源版本、版本未知不算命中、条件报错 各有断言")
    return lines


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
        # 期望值也要排序：左边是 sorted(...)，写成未排序的字面量时，
        # 只要名字的字典序变了（`ReturnMain` → `BaseReturnHome`）断言就会挂。
        sorted(["HomeFlag", "BaseReturnHome"]),
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

def check_telemetry_index() -> list[str]:
    """本地遥测库：纯逻辑（候选抽取 / release 归一 / 分级匹配 / 卡片注记）。

    网络那部分（`refresh`）不在这里测——它要登录，自检必须零网络。
    """
    import sys
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib import telemetry_index as ti

    # 1) 候选抽取：两种 `PipelineNode.Failed` 写法 + 其它失败标记
    lines = [
        "[2026-11-03 08:52:54.330][INF][Px1][Tx1][Node.cpp][L1][F] "
        '[msg=Node.PipelineNode.Failed] [details={"focus":null,"name":"BaseTaskStopError",'
        '"node_id":300000054,"task_id":200000003}]',
        "[2026-11-03 08:52:54.331][INF][Px1][Tx1][Node.cpp][L1][F] "
        "[msg=Node.PipelineNode.Failed] [name=OnlyName]",
        "[2026-11-03 08:52:54.332][WRN][Px1][Tx1][Task.cpp][L1][F] "
        "Task timeout [pretask.name=BaseProbeConfirm] [duration_since(start_clock)=1ms]",
    ]
    cands = ti.extract_candidates("\n".join(lines))
    _eq(len(cands), 3, "三种失败标记都抽到")
    _eq(cands[0].name, "BaseTaskStopError", "details 写法的节点名")
    _eq((cands[0].node_id, cands[0].task_id), (300000054, 200000003), "details 的 id")
    _eq((cands[1].node_id, cands[1].task_id), (None, None), "只有 name 的写法没 id")
    _eq(cands[2].name, "BaseProbeConfirm", "Task timeout 的节点名")

    # 2) release 是**对象** → 取 version（上一版 str() 写成 Python repr，这是回归）
    card = ti.parse_issue_view(
        {
            "shortId": "PYTHON-X",
            "title": "Maa task failed: Entrust (X/recognition)",
            "count": 3,
            "lastSeen": "2026-11-03T08:52:54Z",
            "event": {
                "id": "e1",
                "release": {"version": "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55"},
                "context": {
                    "failure.task_id": 200000003,
                    "failure.node_id": 300000054,
                },
            },
        },
        "PYTHON-X",
    )
    _eq(card.release, "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55", "release 对象取 version")
    _eq((card.failure_task_id, card.failure_node_id), (200000003, 300000054), "失败字段")
    _eq((card.label, card.node_name, card.stage), ("Entrust", "X", "recognition"), "title 拆开")

    # 3) 分级：高（task+node 全等）> 中（同节点 + 时间近）> 低（只有节点名）
    local = _dt(2026, 11, 3, 8, 52, 54)
    stamp = (local - ti._utc_offset()).strftime("%Y-%m-%dT%H:%M:%SZ")
    cards = [
        ti.IssueCard(
            "PYTHON-H",
            "Maa task failed: Train (BaseTaskStopError/action)",
            failure_task_id=200000003,
            failure_node_id=300000054,
            received=stamp,
            release="MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55",
        ),
        ti.IssueCard(
            "PYTHON-M",
            "Maa task failed: Train (BaseTaskStopError/action)",
            failure_task_id=999,
            failure_node_id=999,
            received=stamp,
        ),
        ti.IssueCard(
            "PYTHON-L",
            "Maa task failed: Train (BaseTaskStopError/action)",
            failure_task_id=999,
            failure_node_id=999,
            received="",
        ),
    ]
    matches = ti.match(cands[:1], cards, gap_days=3)
    _eq([m.card.short_id for m in matches], ["PYTHON-H", "PYTHON-M", "PYTHON-L"], "按级别排")
    _eq([m.tier for m in matches], ["high", "mid", "low"], "高/中/低")
    _eq(matches[0].detail, "task_id + node_id 全等", "高级的判据说明")

    # 3b) 只有「高」置信才拿来定 release 基线 / 命名产物 / 挂「遥测失败」注记
    #     （实测 `PYTHON-2Y` 的低置信匹配被当成基线，把当次 beta.73 的节点标成「疑似过时」）
    _eq(ti.usable_match(matches[0]), True, "高置信可用")
    _eq(ti.usable_match(matches[1]), False, "中置信不拿来定 release 基线")
    _eq(ti.usable_match(matches[2]), False, "低置信不拿来定 release 基线")
    _eq(ti.usable_match(matches[2], "PYTHON-L"), True, "显式 --telemetry-issue 例外")

    # 4) 注记
    notes = ti.card_notes(cards[0], matches, chosen="PYTHON-H")
    _eq(len(notes), 3, "卡片两行 + 候选一行")
    _contains(notes[0][0], "遥测问题 PYTHON-H", "问题号")
    _contains(notes[0][0], "beta.55", "release 版本")
    _contains(notes[1][0], "BaseTaskStopError", "失败标题")
    _eq(notes[0][1], "version", "版本注记的颜色")

    return ["候选抽取（两种写法）/ release 归一 / 分级匹配 / 注记 各自有断言"]


def check_task_aliases() -> list[str]:
    """任务别名：`interface.json` 的 label / entry → 遥测里 `span.description` 的 `name`。

    2026-11-12 用户报 `task-trend --task 武装押运 --versions 15` 整列「暂无样本」：面板写的是
    `label`（`武装押运`），遥测记的是 `name`（`Train`），查询过滤直接用原文就查不到行。
    """
    aliases = task_alias_map()
    _eq(aliases.get("武装押运"), "Train", "中文标签翻成任务 name")
    _eq(aliases.get("tasktrain"), "Train", "入口名也翻成 name")

    # 实时查询回来的行是 `span.*` 原始键，分析函数只认中性名；不转换任务表会整片空
    rows = to_neutral_rows(
        [{"span.description": "Train", "span.status": "ok", "count_unique(trace)": 3}],
        ("span.description", "span.status", "count_unique(trace)"),
    )
    _eq(sorted(rows[0]), ["_count", "description", "status"], "实时行换成中性字段名")

    # 筛选解析：别名优先、子串仍可用、都不命中时原样返回（让调用方拿到空表）
    saved = task_trend_report.task_alias_map
    task_trend_report.task_alias_map = lambda: {"测试任务": "XTestTask"}
    try:
        _eq(
            task_trend_report.resolve_task_name("测试任务", ["XTestTask", "Other"]),
            "XTestTask",
            "标签命中遥测里的 name",
        )
        _eq(
            task_trend_report.resolve_task_name("XTest", ["XTestTask"]),
            "XTestTask",
            "没给别名时按子串匹配",
        )
        _eq(
            task_trend_report.resolve_task_name("没这个", ["XTestTask"]),
            "没这个",
            "都不命中就原样返回",
        )
    finally:
        task_trend_report.task_alias_map = saved
    return ["别名映射 / 实时行中性化 / 筛选解析 各自有断言"]
