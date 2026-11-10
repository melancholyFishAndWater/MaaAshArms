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
from .release_mark import RELEASE_MARK, mark_suffix, unmarked_name

MANIFEST_NAME = "manifest.json"
# 现场图的子目录名（`cases/scene.py` 往这里复制遥测/日志包里的失败现场图）。
# 常量放这里是因为 `clear_artifacts()` 也要清它，而 `scene.py` 反过来要 import 本模块的命名工具。
SCENE_DIR = "images"
# 批量出图前清掉的产物：`explore-csv` 会往同一目录写 `<csv 名>.log`（那是**输入**），不能删
ARTIFACT_SUFFIXES = (".png", ".mmd")
# 「疑似过时」后缀（`stale_name()` 补在最末尾）：单独写出来，清理判定要用同一串
STALE_SUFFIX = "疑似过时"
# 批量出图的文件名形状：`<问题号>.png` / `<问题号>_疑似过时.png` /
# `<问题号>_已修复待发版.png`（两者同时命中时是 `<问题号>_已修复待发版_疑似过时.png`）。
# 问题号后面还有别的东西（`_task<id>`、`_px…`、`_<日志时间>`）就是"别的来路"，清理时更保守。
BULK_STEM_SUFFIXES = frozenset(
    {
        "",
        STALE_SUFFIX,
        RELEASE_MARK,
        f"{RELEASE_MARK}_{STALE_SUFFIX}",
    }
)

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
    issue_label: str = "",
) -> str:
    """不含去重后缀的文件名主干：优先遥测问题名，其次任务名，最后证据包目录名。

    带 `--task-id` 时追加 `_task<id>`：一个 issue 里往往有多个任务失败（实测
    `PYTHON-1Z` 有 6 个），不追加会让它们互相覆盖到同一个文件名上.

    指定了 `--task-id` 时**不把任务名放进主干**：那个 task 的入口行不一定在导出的那一段里
    （实测 200000007 只留下 `BaseReturnHome`，它不是任务入口），而 `task<id>` 本身就唯一、
    也一定对——宁可少一个名字，不放一个可能错的名字。

    `issue_label` 是遥测卡上的**问题名**（`Maa task failed: Reward` → `Reward`）：主干就是
    问题号时追加一段，产物叫 `PYTHON-3B_Reward.png`，一眼看出是哪个任务。已经带了任务名
    （`--case` 里从日志认出来的）时不重复加。

    `issue_only=True`（批量出图按 issue 出一个）时不追加任务名：命名规则里 issue 就是身份，
    任务名会拼成 `<问题号>_<任务名>` 这种不符合规则的长名字。

    这个 issue 的修复提交已经进了「最新 tag 之后」但还没发版时，末尾再追加
    `_已修复待发版`（`cases/release_mark.py`）：图仍是失败现场，但读图的人立刻知道
    "已经修了、等发版"。标记只看 git，不联网；`--no-release-mark` 可关掉。
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
    elif issue and issue_label:
        # 遥测问题：问题号后面带一段问题名（`PYTHON-3B_Reward`），读图的人不用回查标题
        parts.append(_safe_part(issue_label))
    if stem_extra:
        # 例如 BaseTaskStopError 那类 issue：按 BaseErrorNext 的派发者分桶，带进文件名
        parts.append(_safe_part(stem_extra))
    if options.px:
        # 多进程日志里同号任务会出现多次，进程号必须进名字，否则两次运行互相覆盖
        parts.append(_safe_part(options.px).lower())
    mark = mark_suffix(issue or "")
    if mark:
        parts.append(mark.lstrip("_"))
    return "_".join(p for p in parts if p)


def out_filename(
    case: Case,
    options: FlowOptions,
    outdir: str,
    manifest: dict | None = None,
    *,
    issue_only: bool = False,
    stem_extra: str = "",
    issue_label: str = "",
) -> str:
    """产物文件名：`<问题名或任务名>[_<问题名>][_task<id>][_<日志时间>][_<短指纹>].png`.

    相比旧版去掉了 `flow_` 前缀（目录已经是 `temp/flow/`，前缀是重复的）。

    同名判定看两处：磁盘上已有同名文件，或 manifest 里已登记过**另一份**输入（日志指纹
    不同或参数不同）。任一冲突就追加日志时间标签；加了标签仍冲突时再追加日志短指纹，
    保证结果确定且不覆盖别人。

    「疑似过时」的后缀不在这里加：那项检查要等渲染完才知道有没有命中，
    所以后缀由 `stale_name()` 在渲染后补上。
    """
    base = base_filename(
        case,
        options,
        issue_only=issue_only,
        stem_extra=stem_extra,
        issue_label=issue_label,
    )
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
    return f"{stem}_{STALE_SUFFIX}{ext}"


def clear_artifacts(outdir: str) -> int:
    """清掉输出目录里本工具的产物（`*.png` / `*.mmd` / `manifest.json`），返回删掉的文件数。

    只给批量出图用（`--telemetry-bulk`）：每轮都按**当前**遥测库重画一份，上一轮留下的文件
    （issue 被 resolve 后图还在、上次 `--outdir` 换过）会让目录看起来比库多。
    同目录的 `images/`（现场图）也一起清：manifest 都清了，记录一没就再没人认得出那些图。
    **不删**：`explore-csv` 落在同目录的 `<csv 名>.log`（那是输入）、其它子目录、其它扩展名。
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
    scene = os.path.join(outdir, SCENE_DIR)
    try:
        scene_names = sorted(os.listdir(scene))
    except OSError:
        return removed
    for name in scene_names:
        path = os.path.join(scene, name)
        if not os.path.isfile(path) or not name.lower().endswith(".png"):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as exc:
            log.warning("提示：清不掉旧现场图 %s（%s），本次仍会继续画", path, exc)
    return removed


def find_existing(
    outdir: str, manifest: dict, name: str, fingerprint: dict | None = None
) -> tuple[str, dict] | None:
    """找"同一张图的上一轮产物"，返回 `(记在哪个名字下, 记录)`；没画过返回 None。

    先按名字试四种写法：本次算出的名字、它的「疑似过时」变体、去掉「已修复待发版」标记的
    两个变体——标记刚出现/刚消失时旧记录就在另一侧。

    再退回**按指纹认**：`fingerprint` 相同（同一份日志、同一套参数、同一来源、同一渲染器）
    就是同一张图，名字怎么变都认得出。这一步兜住"名字里带了日志时间标签"的老产物
    （`PYTHON-M_20261109175132_疑似过时.png`），否则标记一变就会把它们当新图重画、还留下两张。

    命中之后调用方**只改名、不重画**：图的内容没变，变的只是名字。
    """
    bare = unmarked_name(name)
    seen: list[str] = []
    for base in (name, bare):
        for candidate in (base, stale_name(base)):
            if candidate in seen:
                continue
            seen.append(candidate)
            record = manifest["cases"].get(candidate)
            if record and os.path.isfile(os.path.join(outdir, candidate)):
                return candidate, record
    if not fingerprint:
        return None
    for candidate, record in sorted(manifest.get("cases", {}).items()):
        if os.path.basename(candidate) != candidate:
            continue
        if record.get("fingerprint") != fingerprint:
            continue
        if os.path.isfile(os.path.join(outdir, candidate)):
            return candidate, record
    return None


def rename_artifact(outdir: str, manifest: dict, old: str, new: str) -> bool:
    """把已画好的产物从 `old` 改名成 `new`（png + 同干 `.mmd` + manifest 记录），不重画。

    只给「已修复待发版」标记出现/消失时用：那次改动只涉及文件名，图本身没变
    （`cases/release_mark.py`），所以按轨迹跳过时顺手把上一轮的产物换个名字，省一次渲染。
    成功返回 True；`old` 的文件不在了就返回 False，让调用方照常重画。
    """
    if old == new:
        return False
    old_path = os.path.join(outdir, old)
    if not os.path.isfile(old_path):
        return False
    record = manifest["cases"].pop(old, None)
    try:
        os.replace(old_path, os.path.join(outdir, new))
    except OSError as exc:
        if record is not None:
            manifest["cases"][old] = record  # 改不动就把记录放回去，别把账弄丢
        log.warning("提示：%s 改名成 %s 失败（%s），本次按重画处理", old, new, exc)
        return False
    old_mermaid = os.path.splitext(old_path)[0] + ".mmd"
    new_mermaid = os.path.splitext(os.path.join(outdir, new))[0] + ".mmd"
    if os.path.isfile(old_mermaid):
        try:
            os.replace(old_mermaid, new_mermaid)
        except OSError as exc:
            log.warning("提示：%s 改名失败（%s）", old_mermaid, exc)
    if record is not None:
        record["path"] = os.path.join(outdir, new)
        if isinstance(record.get("mermaid"), str) and record["mermaid"]:
            record["mermaid"] = new_mermaid
        manifest["cases"][new] = record
    log.info("改名（已修复待发版标记变化）：%s → %s", old, new)
    return True


def artifact_issue(name: str) -> tuple[str, str]:
    """产物名 → `(问题号, 问题号后面剩下的那段)`；认不出问题号时返回 `("", "")`。

    批量出的图一定以问题号开头（`base_filename()` 的规则），所以前缀就是归属。问题号不含
    `_`（`ISSUE_RE`），按第一个下划线切开就是"问题号 + 其余"；`PYTHON-33_x` 不会误判成
    `PYTHON-3`。
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    head, _sep, rest = stem.partition("_")
    return (head, rest) if ISSUE_RE.match(head) else ("", "")


def _own_rest(rest: str, label: str = "") -> bool:
    """`<问题号>` 后面那一段是不是本工具会写出来的形状。

    会写出来的：`_疑似过时`、`_已修复待发版`（可叠加），以及问题名（`PYTHON-3B_Reward`，
    后面还可能有 `_<派发者>_task<id>`、日志时间、短指纹）。问题名只能按"已知 label"比，
    比不了就是别的来路（单包按任务名起的名、用户自己放的文件）。
    """
    if rest in BULK_STEM_SUFFIXES:
        return True
    safe = _safe_part(label) if label else ""
    if not safe:
        return False
    return rest == safe or rest.startswith(f"{safe}_")


def prune_artifacts(
    outdir: str,
    manifest: dict,
    kept: set[str],
    *,
    issues: set[str],
    failed_issues: set[str] | None = None,
    labels: dict[str, str] | None = None,
) -> int:
    """批量出图结束后清掉不该留着的旧产物，返回删掉的个数。

    只动 manifest 里有记录的产物：磁盘上没有记录的 `png`（`explore-csv` 的 `.log`、子目录、
    用户自己放的文件）一律不碰——"清理"只清自己画的东西。

    三类处理：

    * 归属 issue **不在本次集合**里（版本筛掉 / 已 resolve / 库里消失）→ 删；
    * 归属 issue 在集合里，但这条是**批量**画的（`origin == "bulk"`，或名字是批量的
      `<问题号>.png` / `<问题号>_疑似过时.png`）且本次没保留 → 删（被本次重画取代）；
    * 其余（单包画的、本次画失败的 issue、认不出归属的）→ 留着。

    记录丢了但文件还在的（改名 / 旧版本留下的）：名字正好是本工具的批量命名时也删——
    判据同上（集合外删、被本次保留的取代也删），别的无主文件仍然不碰。`labels` 是
    `问题号 → 问题名`（遥测卡上的 `Reward` / `Factory`…），用来认 `PYTHON-3B_Reward.png`
    这种带问题名的名字。

    「本次画失败的 issue」必须保住旧图：重画失败时目录里那一张是唯一能看的东西。
    """
    failed = failed_issues or set()
    label_of = labels or {}
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
        elif record.get("origin") == "bulk" or _own_rest(rest, label_of.get(issue, "")):
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
    # 没有 manifest 记录的旧产物：只有名字正好是本工具的批量命名（`<问题号>.png` /
    # `_疑似过时` / `_已修复待发版` 变体）时才动，别的无主文件一律不碰。
    # 这一条兜住"记录丢了但文件还在"（改名、旧版本、`--clean-flow` 之后又被旧图占位）：
    # 否则那张旧图会永远占着名字，新图只能写哈希名——实测 PYTHON-V 的旧卡（25 次、.65）
    # 一直躺在 temp/flow，新卡只能叫 PYTHON-V_2c6cb52faaaf.png，用户看到的是旧内容。
    kept_issues = {
        str(records.get(item, {}).get("issue") or "") or artifact_issue(item)[0]
        for item in kept
    }
    kept_issues.discard("")
    for name in sorted(os.listdir(outdir)):
        if name in records or name in kept or os.path.basename(name) != name:
            continue
        if not name.lower().endswith(".png"):
            # `.mmd` 不单独判：跟着同干的 png 一起删，免得把一份图的边文件算成两次清理
            continue
        issue, rest = artifact_issue(name)
        if not issue or not _own_rest(rest, label_of.get(issue, "")):
            continue
        if issue in failed:
            continue
        if issue in issues and issue not in kept_issues:
            # 本次该画它但没画出来（渲染失败 / 被别的名字取代前就退出了）：留着旧图
            continue
        path = os.path.join(outdir, name)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("提示：删不掉旧产物 %s（%s），下次再试", path, exc)
            continue
        sidecar = os.path.splitext(path)[0] + ".mmd"
        try:
            if os.path.isfile(sidecar):
                os.remove(sidecar)
        except OSError as exc:
            log.warning("提示：删不掉 %s（%s）", sidecar, exc)
        removed += 1
        log.info(
            "删掉旧产物（没有 manifest 记录、%s）：%s",
            "已被本次重画取代" if issue in kept_issues else "不在本次 issue 集合内",
            name,
        )
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

