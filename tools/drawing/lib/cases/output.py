"""产物命名与 manifest：同名占用判定、指纹、原子替换、批量清理。

文件名 = 问题号/任务名 + 日志指纹；manifest 记录分步摘要与产物，命中即跳过（幂等）。
「要不要重画」由 `flowlog/trace.py` 的分步摘要决定，这里只管命名、记账与清理旧产物。
不读日志、不画图。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from tools.drawing.lib.flow_from_log import FlowOptions

from .pack import ISSUE_RE, Case, issue_name_of, log_digest, log_stamp, task_name_of

MANIFEST_NAME = "manifest.json"
# 批量出图前清掉的产物：`explore-csv` 会往同一目录写 `<csv 名>.log`（那是**输入**），不能删
ARTIFACT_SUFFIXES = (".png", ".mmd")
# 批量出图的文件名形状：`<问题号>.png` 或 `<问题号>_疑似过时.png`（`base_filename()` 的规则）。
# 问题号后面还有别的东西（`_task<id>`、`_px…`、`_<日志时间>`）就是"别的来路"，清理时更保守。
BULK_STEM_SUFFIXES = frozenset({"", "疑似过时"})

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


def clear_artifacts(outdir: str) -> int:
    """清掉输出目录里本工具的产物（`*.png` / `*.mmd` / `manifest.json`），返回删掉的文件数。

    只给批量出图用（`--telemetry-bulk`）：每轮都按**当前**遥测库重画一份，上一轮留下的文件
    （issue 被 resolve 后图还在、上次 `--outdir` 换过）会让目录看起来比库多。
    **不删**：`explore-csv` 落在同目录的 `<csv 名>.log`（那是输入）、子目录、其它扩展名。
    """
    removed = 0
    try:
        names = sorted(os.listdir(outdir))
    except OSError:
        return 0
    for name in names:
        path = os.path.join(outdir, name)
        if not os.path.isfile(path):
            continue
        if name != MANIFEST_NAME and not name.lower().endswith(ARTIFACT_SUFFIXES):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as exc:
            log.warning("提示：清不掉旧产物 %s（%s），本次仍会继续画", path, exc)
    return removed


def artifact_issue(name: str) -> tuple[str, str]:
    """产物名 → `(问题号, 问题号后面剩下的那段)`；认不出问题号时返回 `("", "")`。

    批量出的图一定以问题号开头（`base_filename()` 的规则），所以前缀就是归属。问题号不含
    `_`（`ISSUE_RE`），按第一个下划线切开就是"问题号 + 其余"；`PYTHON-33_x` 不会误判成
    `PYTHON-3`。
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    head, _sep, rest = stem.partition("_")
    return (head, rest) if ISSUE_RE.match(head) else ("", "")


def prune_artifacts(
    outdir: str,
    manifest: dict,
    kept: set[str],
    *,
    issues: set[str],
    failed_issues: set[str] | None = None,
) -> int:
    """批量出图结束后清掉不该留着的旧产物，返回删掉的个数。

    只动 manifest 里有记录的产物：磁盘上没有记录的 `png`（`explore-csv` 的 `.log`、子目录、
    用户自己放的文件）一律不碰——"清理"只清自己画的东西。

    三类处理：

    * 归属 issue **不在本次集合**里（版本筛掉 / 已 resolve / 库里消失）→ 删；
    * 归属 issue 在集合里，但这条是**批量**画的（`origin == "bulk"`，或名字是批量的
      `<问题号>.png` / `<问题号>_疑似过时.png`）且本次没保留 → 删（被本次重画取代）；
    * 其余（单包画的、本次画失败的 issue、认不出归属的）→ 留着。

    「本次画失败的 issue」必须保住旧图：重画失败时目录里那一张是唯一能看的东西。
    """
    failed = failed_issues or set()
    records: dict = manifest.get("cases") or {}
    removed = 0
    # 记录指向的文件已经不在（用户手删、上一次清理中断）：丢掉记录，否则同名会一直被
    # `out_filename()` 当成"被别的输入占着"，附加时间标签越加越长
    for name in [
        name
        for name in records
        if os.path.basename(name) == name
        and not os.path.isfile(os.path.join(outdir, name))
    ]:
        records.pop(name, None)
    for name in sorted(records):
        if name in kept or os.path.basename(name) != name:
            continue
        record = records[name]
        issue = str(record.get("issue") or "")
        rest = ""
        if not issue:
            issue, rest = artifact_issue(name)
        if not issue or issue in failed:
            continue
        if issue not in issues:
            reason = "不在本次 issue 集合内"
        elif record.get("origin") == "bulk" or rest in BULK_STEM_SUFFIXES:
            reason = "已被本次重画取代"
        else:
            continue
        path = os.path.join(outdir, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            log.warning("提示：删不掉旧产物 %s（%s），下次再试", path, exc)
            continue
        mermaid = record.get("mermaid")
        sidecar = mermaid if isinstance(mermaid, str) and mermaid else None
        if sidecar is None:
            sidecar = os.path.splitext(path)[0] + ".mmd"
        try:
            if os.path.isfile(sidecar):
                os.remove(sidecar)
        except OSError as exc:
            log.warning("提示：删不掉 %s（%s）", sidecar, exc)
        records.pop(name, None)
        removed += 1
        log.info("删掉旧产物（%s）：%s", reason, name)
    return removed


def load_manifest(outdir: str) -> dict:
    path = os.path.join(outdir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {"schema": 2, "cases": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("提示：%s 读不了（%s），本次当作空 manifest。", path, exc)
        return {"schema": 2, "cases": {}}
    if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
        return {"schema": 2, "cases": {}}
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

