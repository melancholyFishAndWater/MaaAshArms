"""产物命名与 manifest：同名占用判定、指纹、原子替换。

文件名 = 问题号/任务名 + 日志指纹；manifest 记录指纹与产物，命中即跳过（幂等）。
不读日志、不画图。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from tools.drawing.lib.flow_from_log import FlowOptions

from .pack import Case, issue_name_of, log_digest, log_stamp, task_name_of

MANIFEST_NAME = "manifest.json"

log = logging.getLogger("tools.flow_cases")
def _safe_part(text: str) -> str:
    """文件名里只留字母数字与 `-_`，其余换成 `-`（中文、空格、斜杠都换掉）。"""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)


def base_filename(
    case: Case,
    options: FlowOptions,
    *,
    issue_only: bool = False,
    stem_extra: str = "",
) -> str:
    """不含去重后缀的文件名主干：优先遥测问题名，其次任务名，最后证据包目录名。

    带 `--task-id` 时追加 `_task<id>`：一个 issue 里往往有多个任务失败（实测
    `PYTHON-1Z` 有 6 个），不追加会让它们互相覆盖到同一个文件名上.

    指定了 `--task-id` 时**不把任务名放进主干**：那个 task 的入口行不一定在导出的那一段里
    （实测 200000007 只留下 `BaseReturnHome`，它不是任务入口），而 `task<id>` 本身就唯一、
    也一定对——宁可少一个名字，不放一个可能错的名字。

    `issue_only=True`（批量出图按 issue 出一个）时不追加任务名：命名规则里 issue 就是身份，
    任务名会拼成 `<问题号>_<任务名>` 这种不符合规则的长名字。
    """
    # `options.issue` 优先：遥测反查 / `--telemetry-issue` 给的问题号比目录名可靠
    issue = options.issue or issue_name_of(case)
    task = task_name_of(case, options)
    explicit_task = options.task_id is not None
    stem = issue or (None if explicit_task else task) or task or case.label or "case"
    parts = [_safe_part(stem)]
    if explicit_task and not issue_only:
        parts.append(f"task{options.task_id}")
    elif issue and task and not issue_only:
        # 有 issue 名但没有 task_id：把任务名也带上，图才读得出画的是哪个任务
        parts.append(_safe_part(task))
    if stem_extra:
        # 例如 BaseTaskStopError 那类 issue：按 BaseErrorNext 的派发者分桶，带进文件名
        parts.append(_safe_part(stem_extra))
    if options.px:
        # 多进程日志里同号任务会出现多次，进程号必须进名字，否则两次运行互相覆盖
        parts.append(_safe_part(options.px).lower())
    return "_".join(p for p in parts if p)


def out_filename(
    case: Case,
    options: FlowOptions,
    outdir: str,
    manifest: dict | None = None,
    *,
    issue_only: bool = False,
    stem_extra: str = "",
) -> str:
    """产物文件名：`<问题名或任务名>[_task<id>][_<日志时间>][_<短指纹>].png`.

    相比旧版去掉了 `flow_` 前缀（目录已经是 `temp/flow/`，前缀是重复的）。

    同名判定看两处：磁盘上已有同名文件，或 manifest 里已登记过**另一份**输入（日志指纹
    不同或参数不同）。任一冲突就追加日志时间标签；加了标签仍冲突时再追加日志短指纹，
    保证结果确定且不覆盖别人。

    「疑似过时」的后缀不在这里加：那项检查要等渲染完才知道有没有命中，
    所以后缀由 `stale_name()` 在渲染后补上。
    """
    base = base_filename(case, options, issue_only=issue_only, stem_extra=stem_extra)
    digest = log_digest(case)
    recorded = (manifest or {}).get("cases", {})

    def taken(name: str) -> bool:
        path = os.path.join(outdir, name)
        if not os.path.isfile(path):
            # 文件不在，但 manifest 里记着另一份输入用过这个名字：也算被占（产物被清掉过）
            return (
                name in recorded
                and recorded[name].get("fingerprint", {}).get("log") != digest
            )
        if name not in recorded:
            return True  # 磁盘上有同名文件、但不是本工具画的
        return recorded[name].get("fingerprint", {}).get("log") != digest

    name = f"{base}.png"
    if not taken(name):
        return name
    stamp = log_stamp(case, options)
    if stamp:
        name = f"{base}_{stamp}.png"
        if not taken(name):
            return name
    return f"{base}_{digest}.png"


def stale_name(name: str) -> str:
    """给产物文件名加「疑似过时」后缀：`_疑似过时` 追加在最末尾（扩展名之前）。

    `X.png` -> `X_疑似过时.png`；`X_20261024-031849.png` -> `X_20261024-031849_疑似过时.png`。
    后缀用中文：文件名直接写清这是哪一类产物，不必再记 `_stale` 这个缩写。
    """
    stem, ext = os.path.splitext(name)
    return f"{stem}_疑似过时{ext}"


def load_manifest(outdir: str) -> dict:
    path = os.path.join(outdir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {"schema": 1, "cases": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("提示：%s 读不了（%s），本次当作空 manifest。", path, exc)
        return {"schema": 1, "cases": {}}
    if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
        return {"schema": 1, "cases": {}}
    return data


def save_manifest(outdir: str, manifest: dict) -> None:
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _stale_record(check) -> dict | None:
    """`StaleCheck` 的 JSON 形态，存进 manifest 用（dataclass 不能直接 dump）。"""
    return None if check is None else asdict(check)

