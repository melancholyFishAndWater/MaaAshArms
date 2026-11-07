"""证据包用例：附件与磁盘一致性、报告锁（并发写同一份报告时互斥）。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import io
import shutil
import sys

from typing import Any

from .asserts import _eq, _contains
from .deps import DEFAULT_ARTIFACT_DIR
def check_evidence() -> list[str]:
    """失败证据的纯逻辑：解包白名单、附件名取 task_id、去重、扁平 context 键、可复制命令。

    只测不联网、不写文件的部分（取附件与取日志要 Sentry 登录，属于 `evidence` 的手工验收）。
    """
    import io as _io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib import evidence as ev

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

    # 2b) 附件名 → run_id（现场图按 run 分目录的键），以及"已下过哪些 run / 事件索引"
    _eq(
        ev.parse_run_id(
            "mfa-task-failure-3204f818b4e04a3494b29d911ec47724-200000062-screenshots.zip"
        ),
        "3204f818b4e04a3494b29d911ec47724",
        "附件名里的 run_id",
    )
    _eq(ev.parse_run_id("log_20261014-part01.zip"), None, "非附件名没有 run_id")
    _eq(
        ev.existing_run_ids(
            [
                "debug/on_error/aaa/1_x.png",
                "debug/on_error/aaa/2_y.png",
                "debug/on_error/bbb/3_z.png",
                "debug/on_error/legacy.png",
            ]
        ),
        {"aaa", "bbb"},
        "已下过现场图的 run_id（平铺老图不算）",
    )
    indexed = ev.event_index(
        ["debug/on_error/aaa/1_x.png", "debug/on_error/bbb/2_y.png"],
        [{"run_id": "aaa", "event_id": "e1", "task_id": 7}],
    )
    _eq(
        [
            (i["run_id"], i["event_id"], i["task_id"], len(i["images"]))
            for i in indexed
        ],
        [("aaa", "e1", 7, 1), ("bbb", None, None, 1)],
        "事件索引按 run 归拢、没有元数据的留空",
    )

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

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib import evidence as ev

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
        # 新格式：现场图按失败事件分目录（`debug/on_error/<run_id>/`），递归扫描要认出来
        nested = (
            directory
            / "debug"
            / "on_error"
            / "9fecde7baf654fe1943bafda3313d583"
            / "2026.11.02-21.14.00.000_MaintenanceBranchAble.png"
        )
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_bytes(blank)
        (directory / "debug" / "on_error" / "readme.txt").write_text(
            "不是现场图", encoding="utf-8"
        )

        log_rel, images = ev.existing_artifacts(directory)
        _eq(log_rel, "debug/maafw.log", "扫到磁盘上的日志")
        _eq(
            images,
            [
                "debug/on_error/2026.10.14-19.20.37.889_EntrustEnterMainLine.png",
                "debug/on_error/9fecde7baf654fe1943bafda3313d583/"
                "2026.11.02-21.14.00.000_MaintenanceBranchAble.png",
            ],
            "递归扫到平铺图与按 run 分目录的现场图",
        )
        _eq(
            ev.existing_run_ids(images),
            {"9fecde7baf654fe1943bafda3313d583"},
            "分目录的那份被认成已下过的 run",
        )

        # 附件 zip → `debug/on_error/<run_id>/`：去掉 zip 里的 `debug/on_error/` 前缀再落盘
        import io as _io2
        import zipfile as _zipfile

        write_dir = scratch / "write-test"
        buffer = _io2.BytesIO()
        with _zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "debug/on_error/2026.11.02-21.14.00.000_MaintenanceBranchAble.png",
                blank,
            )
        written_images = ev.write_event_images(buffer.getvalue(), write_dir, "9fecde7b")
        _eq(
            written_images,
            [
                "debug/on_error/9fecde7b/"
                "2026.11.02-21.14.00.000_MaintenanceBranchAble.png"
            ],
            "现场图按 run 分目录落盘",
        )
        _eq(
            (write_dir / written_images[0]).is_file(),
            True,
            "落盘路径真的写出了文件",
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
        _eq(len(written["images"]), 2, "磁盘上的现场图（平铺 + 分目录）都进了 written")
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
            "images": list(images),
            "zip_bytes": 1,
        }
        more: list[str] = []
        ev.reconcile_written(directory, again, log_rel, images, more)
        _eq(
            (again["log_bytes"], len(again["images"]), more),
            (999, 2, []),
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

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib import report_common as rc

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
