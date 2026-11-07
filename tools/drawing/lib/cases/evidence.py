"""证据包搜索与遥测侧取数：`--evidence-dir` 各根下的包、失败 task 定位、证据事件注记。

只回答「这个 issue 的包在哪、对应哪次运行、哪个 task」，画图交给 runner/overview。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from tools.drawing.lib.flow_from_log import _is_zip, parse_log, scan_tasks

from .pack import (
    EVIDENCE_ROOT,
    ISSUE_RE,
    Case,
    _fmt_local,
    _pick_log,
    load_case,
)

log = logging.getLogger("tools.flow_cases")

def _evidence_on_error(short_id: str) -> list[str]:
    """这个 issue 的失败现场图：证据包 `on_error/` 下的 png（含按 run 分桶的子目录），最多 4 张。

    搜索 `--evidence-dir` 给的每个根；没有现场图就返回空（概览卡纯文字）。
    """
    for cand in _evidence_candidates(short_id):
        base = os.path.join(cand, "on_error")
        pngs: list[str] = []
        for dirpath, _dirs, files in os.walk(base):
            pngs.extend(
                os.path.join(dirpath, name)
                for name in files
                if name.lower().endswith(".png")
            )
        if pngs:
            return sorted(pngs)[:4]
    return []


def set_evidence_dirs(dirs: list[str] | None) -> None:
    """设置证据包搜索目录：用户给的在先，默认 `temp/evidence` 兜底，重复的去掉。"""
    global _EVIDENCE_DIRS
    ordered: list[str] = []
    for item in [*(dirs or []), EVIDENCE_ROOT]:
        path = os.path.abspath(os.path.expanduser(item))
        if path not in ordered:
            ordered.append(path)
    for path in (dirs or []):
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(full):
            log.warning("--evidence-dir 不存在，跳过：%s", path)
    _EVIDENCE_DIRS = ordered


def _candidates_in(root: str, short_id: str) -> list[str]:
    """这个根下属于该 issue 的候选包目录。

    三种布局都认：`<根>/<问题号>/debug`（`evidence` 命令的标准产出）、`<根>/<问题号>/`
    直接放日志、以及根目录名本身就是问题号（`--evidence-dir D:/下载/PYTHON-2T`）。
    """
    out: list[str] = []
    for cand in (
        os.path.join(root, short_id, "debug"),
        os.path.join(root, short_id),
    ):
        if os.path.isdir(cand) and cand not in out:
            out.append(cand)
    if os.path.isdir(root) and os.path.basename(os.path.normpath(root)) == short_id:
        if root not in out:
            out.append(root)
    return out


def _zips_in(root: str, short_id: str) -> list[str]:
    """这个根下文件名带问题号的归档；多卷按名字排序一起给（`load_case` 认多卷）。"""
    if not os.path.isdir(root):
        return []
    return [
        os.path.join(root, name)
        for name in sorted(os.listdir(root))
        if short_id in name
        and os.path.isfile(os.path.join(root, name))
        and _is_zip(os.path.join(root, name))
    ]


def _evidence_candidates(short_id: str) -> list[str]:
    """所有证据根下的候选包目录（搜索顺序：先给的根在前，默认根在最后）。"""
    return [cand for root in _EVIDENCE_DIRS for cand in _candidates_in(root, short_id)]


def _evidence_case(short_id: str) -> Case | None:
    """这个 issue 有本地证据包时，构造可画的 Case（按 `--evidence-dir` 的先后找）。

    每个根内先看目录（标准 / 扁平布局），再看文件名带问题号的归档；**同一个根**里
    目录优先。这是批量出图「未命中日志包」时的**完整**来源：证据包里就是那次运行的
    `maafw.log` 尾部 + `on_error/` 现场图，足够画真实流程图。
    """
    for root in _EVIDENCE_DIRS:
        for cand in _candidates_in(root, short_id):
            try:
                return load_case([cand])
            except (FileNotFoundError, ValueError):
                continue
        zips = _zips_in(root, short_id)
        if zips:
            try:
                return load_case(zips)
            except (FileNotFoundError, ValueError):
                continue
    return None


def _has_evidence(short_id: str) -> bool:
    """本地有没有这个 issue 的**日志**。

    只看目录在不在不够：证据包可能只有 `on_error/` 没有 `maafw.log`，那种情况也算"本地没有
    日志"，应该尝试拉取。
    """
    for root in _EVIDENCE_DIRS:
        for cand in _candidates_in(root, short_id):
            try:
                if _pick_log(cand):
                    return True
            except OSError:
                continue
        if _zips_in(root, short_id):
            return True
    return False


def _evidence_task_id(case: Case, card: Any) -> tuple[int | None, int]:
    """在证据日志里定位这个 issue 的失败 task，返回 `(task_id, 日志里的 task 数)`。

    证据包的日志与 Sentry 卡片不一定来自同一次运行（实测 PYTHON-2S 的日志是 11-02、
    卡片最新事件是 11-04，`failure_task_id` 在日志里根本不存在）。所以不能只看
    `card.failure_task_id`，依次尝试：

    1. `failure_task_id` 确实在日志的任务列表里 → 用它；
    2. 失败节点名（`card.node_name`）出现在哪个 task 的步骤里 → 用那个 task；
    3. 任务入口名（`card.label`，如 `Entrust`）对上某个 task 的 `entry`（`TaskEntrust`）。

    都定位不到返回 `(None, 任务数)`；调用方在多于一个 task 时据此退概览卡，避免把
    Train/Escort/Battle/Training 接成一条链。
    """
    log_source = case.log_source()
    try:
        tasks = scan_tasks(log_source, None, None)
    except (OSError, TypeError):
        tasks = []
    ids = {t.task_id for t in tasks}
    if card.failure_task_id is not None and card.failure_task_id in ids:
        return card.failure_task_id, len(tasks)
    if card.node_name:
        try:
            parsed = parse_log(log_source, None, None, None, False)
        except (OSError, TypeError, ValueError):
            parsed = None
        if parsed is not None:
            hits = [
                s.task_id
                for s in parsed.steps
                if s.name == card.node_name and s.task_id is not None
            ]
            if hits:
                return hits[-1], len(tasks)
    if card.label:
        want = card.label
        for task in tasks:
            entry = task.entry or ""
            norm = entry[4:] if entry.startswith("Task") else entry
            if norm == want:
                return task.task_id, len(tasks)
    return None, len(tasks)


def _evidence_event_note(case: Case) -> tuple[str, str] | None:
    """遥测证据包 `evidence.json` 里的 `event.id` / 运行时间，画到图上注明用的是哪次运行。"""
    if not case.root:
        return None
    parent = os.path.dirname(case.root)
    cands: list[str] = []
    if ISSUE_RE.match(os.path.basename(parent) or ""):
        cands.append(os.path.join(parent, "evidence.json"))
    elif ISSUE_RE.match(os.path.basename(case.root) or ""):
        cands.append(os.path.join(case.root, "evidence.json"))
    for path in cands:
        if not os.path.isfile(path):
            continue
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        event = data.get("event") or {}
        eid = str(event.get("id") or "")
        received = str(event.get("received") or "")
        if not eid and not received:
            return None
        when = _fmt_local(received) if received else "?"
        return (f"遥测证据事件：{eid or '?'}（{when}）", "version")
    return None


def _terminal_dispatcher(case: Case, card: Any, task_id: int | None) -> str:
    """终端失败是 `BaseTaskStopError` 时，取 `BaseErrorNext` 的派发者（本次失败的上游环节）。

    同一个 issue 的不同运行/任务里，`BaseErrorNext` 的派发者可能不同（ISSUES #3 的
    `EscortClickOk` / `EscortEnterHome`）；拿它当文件名后缀，把不同上游分桶、互不覆盖。
    """
    if card.node_name != "BaseTaskStopError":
        return ""
    try:
        parsed = parse_log(case.log_source(), None, None, None, False)
    except (OSError, TypeError, ValueError):
        return ""
    hits = [
        s
        for s in parsed.steps
        if s.name == "BaseErrorNext"
        and s.dispatcher
        and (task_id is None or s.task_id in (None, task_id))
    ]
    return hits[-1].dispatcher if hits else ""


def _fetch_evidence(short_id: str) -> None:
    """本地没有就调 `evidence` 拉取这个 issue 的证据包（联网）；失败只提示，不影响后面画图。"""
    log.info("本地没有证据包，拉取 %s（evidence）…", short_id)
    try:
        from tools.sentry.lib import evidence as evidence_mod

        code = evidence_mod.main(["--issue", short_id])
        if code != 0:
            log.warning("拉取 %s 失败（exit %s），退概览卡", short_id, code)
        else:
            log.info("证据包已拉取：%s", short_id)
    except (ImportError, RuntimeError, OSError) as exc:
        log.warning("拉取 %s 失败：%s，退概览卡", short_id, exc)

