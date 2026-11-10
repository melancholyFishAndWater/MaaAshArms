"""自检汇总：按固定顺序跑各用例模块，返回退出码（0 全过 / 1 有失败）。

用例按被测对象分在 core / drawing / flow / telemetry / evidence / reports；
这里只维护 `CHECKS` 表与失败汇总，新增用例记得同时登记。
"""

from __future__ import annotations

from collections.abc import Callable

from .asserts import CheckFailure
from .core import check_flow_parser
from .drawing import (
    check_evidence_pack_gate,
    check_flow_diagnostics,
    check_incremental_trace,
    check_local_release_assist,
    check_overview_render,
    check_scene_images,
)
from .evidence import (
    check_evidence,
    check_evidence_disk_agreement,
    check_report_lock,
)
from .fixtures import Emit
from .flow import check_flow_overlay, check_flow_parents
from .issue_resolve import check_issue_resolve
from .reports import check_explore_csv, check_speed_report
from .telemetry import (
    check_artifact_roundtrip,
    check_context_rows,
    check_ordering,
    check_release_parsing,
    check_release_rows,
    check_renderers,
    check_task_rows,
    check_telemetry_index,
    check_trend,
    check_version_filter,
    check_version_filter_messages,
    check_wilson,
)

CHECKS: tuple[tuple[str, Callable[[], list[str]]], ...] = (
    ("release 解析", check_release_parsing),
    ("版本排序", check_ordering),
    ("遥测版本筛选（--telemetry-version）", check_version_filter),
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
    ("issue 自动关闭（refs/提交/URL/hook）", check_issue_resolve),
    ("失败证据的纯逻辑", check_evidence),
    ("证据清单与磁盘一致", check_evidence_disk_agreement),
    ("并发锁的持有者判据", check_report_lock),
    ("流程图的父节点（派发者）", check_flow_parents),
    ("流程图重复轮次合并", check_flow_overlay),
    ("本地遥测库（候选/分级/注记）", check_telemetry_index),
    ("遥测概览卡（渲染/命名）", check_overview_render),
    ("增量绘制的分步摘要（代码摘要/数据步/理由）", check_incremental_trace),
    ("证据包版本门控与失败 task 定位", check_evidence_pack_gate),
    ("本地 release 辅助（按内容认运行）", check_local_release_assist),
    ("现场图导出（命名/挑图/增量/清理）", check_scene_images),
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
