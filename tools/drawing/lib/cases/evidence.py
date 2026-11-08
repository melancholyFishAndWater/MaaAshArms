"""证据包搜索与遥测侧取数：`--evidence-dir` 各根下的包、失败 task 定位、证据事件注记。

只回答「这个 issue 的包在哪、对应哪次运行、哪个 task」，画图交给 runner/overview。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tools.drawing.lib.flow_from_log import _is_zip, parse_log, scan_tasks

from .pack import (
    EVIDENCE_ROOT,
    Case,
    _evidence_manifest,
    _fmt_local,
    _pack_version,
    _pick_log,
    load_case,
)
from .version_filter import VersionFilter

log = logging.getLogger("tools.flow_cases")

# 证据包搜索根：`set_evidence_dirs()` 会把用户给的根排到前面、默认根兜底。
# 默认值写在这里（用过的地方读的是本模块的全局变量）：只在 CLI 里靠 `set_evidence_dirs()`
# 赋值的话，任何在它之前调用搜索的路径都会 `NameError`（实测直接调 `_evidence_case()`）。
_EVIDENCE_DIRS: list[str] = [EVIDENCE_ROOT]


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
    """设置证据包搜索目录：用户给的在先，默认 `temp/evidence` 兜底，重复的去掉。

    **就地改**这个列表、不重新赋值：`cases` 包把它导出给外部（`flow_cases._EVIDENCE_DIRS`），
    重新绑定会让导入方一直看到旧的那份。
    """
    ordered: list[str] = []
    for item in [*(dirs or []), EVIDENCE_ROOT]:
        path = os.path.abspath(os.path.expanduser(item))
        if path not in ordered:
            ordered.append(path)
    for path in (dirs or []):
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(full):
            log.warning("--evidence-dir 不存在，跳过：%s", path)
    _EVIDENCE_DIRS[:] = ordered


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

    1. 卡片、证据包清单里的 `failure_task_id` 确实在日志的任务列表里 → 用它；
    2. 失败节点名（`card.node_name`）出现在哪个 task 的步骤里 → 用那个 task；
    3. 任务入口名（`card.label`，如 `Entrust`）对上某个 task 的 `entry`（`TaskEntrust`）；
    4. 清单里的 `failure_task_id` 不在任务列表里（Sentry 只留日志尾部，那个 task 的
       `node done` 行被截掉了）→ 仍然用它。判据是 `scan_tasks()` 只认 `node done`，
       而 `--task-id` 还能靠失败事件的活动区间定位（实测 PYTHON-2P 的包只剩 5 个 task、
       失败那个不在里面，按清单里的 200000058 仍画出 45 步 9 轮）。卡片的 id 不做这一步：
       它属于卡片最新那次事件，不是这个包里的运行。

    都定位不到返回 `(None, 任务数)`；调用方在多于一个 task 时据此退概览卡，避免把
    Train/Escort/Battle/Training 接成一条链。
    """
    log_source = case.log_source()
    try:
        tasks = scan_tasks(log_source, None, None)
    except (OSError, TypeError):
        tasks = []
    ids = {t.task_id for t in tasks}
    event = _evidence_manifest(case).get("event") or {}
    pack_task = event.get("failure_task_id")
    if not isinstance(pack_task, int):
        pack_task = None
    for wanted in (card.failure_task_id, pack_task):
        if wanted is not None and wanted in ids:
            return wanted, len(tasks)
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
    if pack_task is not None:
        return pack_task, len(tasks)
    return None, len(tasks)


def _evidence_event_note(case: Case) -> tuple[str, str] | None:
    """遥测证据包 `evidence.json` 里的 `event.id` / 运行时间，画到图上注明用的是哪次运行。"""
    event = _evidence_manifest(case).get("event") or {}
    eid = str(event.get("id") or "")
    received = str(event.get("received") or "")
    if not eid and not received:
        return None
    when = _fmt_local(received) if received else "?"
    return (f"遥测证据事件：{eid or '?'}（{when}）", "version")


def _in_default_evidence_root(case: Case) -> bool:
    """这个包是不是 `evidence` 命令自己落的（`temp/evidence` 下）。

    `--evidence-dir` 指过来的包重拉也改不到它（`evidence` 只往 `temp/evidence` 写），
    所以那种包版本不符时不要去拉，直接说清。
    """
    if not case.root:
        return False
    pack_dir = os.path.dirname(os.path.abspath(case.root))
    root = os.path.abspath(EVIDENCE_ROOT)
    return os.path.normcase(pack_dir).startswith(os.path.normcase(root) + os.sep)


def _usable_evidence_case(
    card: Any,
    case: Case,
    version_filter: VersionFilter,
    *,
    allow_fetch: bool,
) -> Case | None:
    """版本筛选下这个证据包还能不能用：包内 release 不在范围内时先按最新事件重拉。

    `evidence` 是增量的：清单里的 `event.id` 与 issue 最新事件不同才会重下日志
    （`collect()` 的 `reused` 判据），所以重拉能把包换到筛选对应的那次运行。
    重拉不了（离线 / `--no-fetch-evidence`）或重拉后仍不符 → 返回 None（调用方退概览卡），
    而不是拿旧版本的日志画一张"看起来是筛选结果"的图（ISSUES：PYTHON-V 的包是
    beta.52、筛的是 >=beta.68，图上的注记却写着 beta.52）。

    清单里没有 `event.release`、日志里也没有「资源版本」行时判不了版本，照用并提示一次。
    """
    version = _pack_version(case)
    if version is None:
        log.info(
            "提示：%s 的证据包里没有版本记录（清单 release / 日志「资源版本」都没有），"
            "版本判不了，照画",
            card.short_id,
        )
        return case
    if version_filter.matches(version):
        return case
    event = _evidence_manifest(case).get("event") or {}
    log.info(
        "版本筛选 %s：%s 的本地证据包是 %s（事件 %s），不在范围内",
        version_filter.describe(),
        card.short_id,
        version,
        event.get("id") or "?",
    )
    if not allow_fetch:
        log.info("离线或没开 --fetch-evidence，不重拉：这个包不用来画流程，退概览卡")
        return None
    if not _in_default_evidence_root(case):
        log.info(
            "包不在 %s 下（--evidence-dir 给的），重拉改不到它：退概览卡", EVIDENCE_ROOT
        )
        return None
    _fetch_evidence(card.short_id, why=f"本地证据包是 {version}，不在筛选范围，按最新事件重拉")
    refreshed = _evidence_case(card.short_id)
    new_version = _pack_version(refreshed) if refreshed is not None else None
    if refreshed is not None and (
        new_version is None or version_filter.matches(new_version)
    ):
        log.info("证据包已更新到 %s：%s", new_version or "未知版本", card.short_id)
        return refreshed
    log.warning(
        "重拉后证据包仍是 %s，不用它画流程（退概览卡）：%s",
        new_version or "未知版本",
        card.short_id,
    )
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


def _fetch_evidence(short_id: str, *, why: str = "本地没有证据包") -> None:
    """调 `evidence` 拉这个 issue 的证据包（联网）；失败只提示，不影响后面画图。

    `why` 说明为什么要拉（本地没有 / 本地包版本不符），原样进日志——两种情况都会走到这里。
    """
    log.info("%s，拉取 %s（evidence）…", why, short_id)
    try:
        from tools.sentry.lib import evidence as evidence_mod

        code = evidence_mod.main(["--issue", short_id])
        if code != 0:
            log.warning("拉取 %s 失败（exit %s），退概览卡", short_id, code)
        else:
            log.info("证据包已拉取：%s", short_id)
    except (ImportError, RuntimeError, OSError) as exc:
        log.warning("拉取 %s 失败：%s，退概览卡", short_id, exc)

