"""现场图导出：把遥测证据包 / 补充日志包里的失败现场图复制到 `<outdir>/images/`。

**复制，不是搬走**：`on_error/` 的原图是画图用的证据（概览卡贴图、`--on-error`、输入摘要都在
读它），搬走就没法再画；而读图的人不想为了看一眼失败画面去翻
`temp/evidence/<ID>/debug/on_error/<run_id>/`，所以在 flow 产出目录里留一份。

文件名 = `<问题号>_<问题名>[_日志包][_<时刻>].png`（`scene_filename`）：

- 问题名取遥测卡标题里的任务名，与流程图同一条规则（`cases/output.py:base_filename`）；
- 图来自**补充日志包**（`--case` 的 `on_error/`，遥测包里没图时用它兜底）时加 `_日志包`，
  一眼能分出"遥测给的那张"与"日志包里对得上的那张"；
- 一个 issue 有多张时追加抓图时刻（`_20261109-173719`）：一次失败里几个节点同时抓图是常态，
  不加时刻会互相覆盖。

增量与清理：文件名相同且 sha256 相同就不重写（mtime 不动）；`prune_scene()` 只删
`manifest["images"]` 里记着的、且不属于本次 issue 集合（或已被本次取代）的那几张，
磁盘上无记录的文件一律不碰。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Any

from tools.drawing.lib.flow_from_log import MediaEntry, MediaResolver

from .output import SCENE_DIR, _safe_part, artifact_issue

log = logging.getLogger("tools.flow_cases")

# 来自补充日志包的图在名字里带这一段（遥测包自己带的图不带）
PACK_MARK = "日志包"
# 一次失败里几个节点在同一秒前后抓图：间隔在这个范围内的算同一批，一起导出；
# 超过它的属于**另一次**失败（同一个 issue 常有多次），不往这一份里塞
BURST_SECONDS = 60.0
# 一次最多导几张（与概览卡贴图的上限一致，看多了也没用）
LIMIT = 4
# 知道"这次画的运行失败在哪一刻"时，只认离它这么近的图（秒）。
# 一个证据包里躺着这个 issue 历次运行的图，而画的可能是历史那一次（实测 `PYTHON-3J`：图画的是
# 01:14 那次、包里的图是 13:51 那次，12 小时）——离得远的一律不采用，宁可不导。
MATCH_SECONDS = 900.0

# `2026.11.09-17.37.19.721_GiftChooseReverse.png`：时刻 + 节点名
_NAME_RE = re.compile(
    r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2})\.\d+_(?P<node>.+)\.png$"
)


def scene_dir(outdir: str) -> str:
    """现场图的落盘目录：`<outdir>/images/`。"""
    return os.path.join(outdir, SCENE_DIR)


def scene_entries(source: Any) -> list[tuple[datetime, str, MediaEntry]]:
    """枚举一份 `on_error/` 里的现场图，按抓图时刻排：`(时刻, 节点名, 条目)`。

    `source` 可以是目录、zip（含多卷）、`MediaResolver`，与画图侧同一套读法；
    文件名里没有时刻的（`legacy.png` 这种）不认——那是别的来路，导出时也无从排序。
    """
    entries: list[tuple[datetime, str, MediaEntry]] = []
    for entry in MediaResolver(source).iter_entries():
        match = _NAME_RE.match(os.path.basename(entry.member))
        if not match:
            continue
        try:
            ts = datetime.strptime(match.group("ts"), "%Y.%m.%d-%H.%M.%S")
        except ValueError:
            continue
        entries.append((ts, match.group("node"), entry))
    entries.sort(key=lambda item: (item[0], item[2].member))
    return entries


def scene_clock(entry: MediaEntry) -> str:
    """文件名里的抓图时刻 → 文件名用的一段：`2026.11.09-17.37.19.721_…` → `20261109-173719`。"""
    match = _NAME_RE.match(os.path.basename(entry.member))
    return match.group("ts").replace(".", "") if match else ""


def select_scene(
    entries: list[tuple[datetime, str, MediaEntry]],
    when: datetime | None = None,
    node_name: str = "",
    *,
    burst_seconds: float = BURST_SECONDS,
    limit: int = LIMIT,
    max_gap: float | None = None,
) -> list[MediaEntry]:
    """挑出"这次失败"的那几张：离 `when` 最近的一张，加上与它同批（±`burst_seconds`）的。

    `node_name`（遥测卡标题里的失败节点）命中时优先——同一次失败里框架可能给好几个节点各抓
    一张图，读图的人先要看的是"失败的那个节点"看到了什么（与 `flowlog/media.py:OnErrorIndex.find`
    同一取舍）。不知道失败时刻（卡片没有 `received`）时只取最新的一张，不猜。

    `max_gap` 给了就只认原始时刻离 `when` 不超过它的图；最近的一张都超出范围时返回空表
    （`MATCH_SECONDS`：别把另一次运行的现场图当成这次的）。
    """
    if not entries:
        return []
    if when is None:
        return [entries[-1][2]]

    def rank(item: tuple[datetime, str, MediaEntry]) -> tuple[float, str]:
        ts, name, entry = item
        distance = abs((ts - when).total_seconds())
        if node_name and name == node_name:
            distance *= 0.15
        return distance, entry.member

    ordered = sorted(entries, key=rank)
    if max_gap is not None:
        # 用**原始**时间差判，别用打了折的 rank 值
        nearest_gap = abs((ordered[0][0] - when).total_seconds())
        if nearest_gap > max_gap:
            return []
    best_ts = ordered[0][0]
    picked: list[MediaEntry] = []
    for ts, _name, entry in ordered:
        if abs((ts - best_ts).total_seconds()) > burst_seconds:
            continue
        picked.append(entry)
        if len(picked) >= limit:
            break
    return picked


def scene_filename(
    issue: str, label: str = "", *, from_pack: bool = False, clock: str = ""
) -> str:
    """`PYTHON-3B_Reward.png` / `PYTHON-3B_Reward_日志包.png` / `PYTHON-3B_Reward_20261109-173719.png`。"""
    parts = [_safe_part(issue)]
    safe_label = _safe_part(label) if label else ""
    if safe_label:
        parts.append(safe_label)
    if from_pack:
        parts.append(PACK_MARK)
    if clock:
        parts.append(clock)
    return "_".join(part for part in parts if part) + ".png"


def _bytes(entry: MediaEntry) -> bytes:
    """条目正文：目录来源读文件，zip 来源解压那一条。"""
    data = entry.data
    if data is None:
        if entry.path is None:
            raise ValueError(f"现场图 {entry.member} 既没有文件路径也没有字节内容")
        with open(entry.path, "rb") as handle:
            return handle.read()
    if isinstance(data, bytes):
        return data
    return bytes(data.read())


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pick_name(
    target: str,
    records: dict,
    issue: str,
    label: str,
    from_pack: bool,
    multi: bool,
    entry: MediaEntry,
    digest: str,
    used: set[str],
) -> str:
    """定下这张图叫哪个名字：同名记着别的 issue 就让路，本次已用过就加序号。"""
    clock = scene_clock(entry) if multi else ""
    base = scene_filename(issue, label, from_pack=from_pack, clock=clock)
    stem, ext = os.path.splitext(base)
    for index in range(1, 100):
        name = base if index == 1 else f"{stem}-{index}{ext}"
        if name in used:
            continue
        record = records.get(name)
        if record is not None and str(record.get("issue") or "") not in ("", issue):
            continue  # 这个名字记着别的 issue：不抢
        return name
    return f"{stem}-{digest[:8]}{ext}"


def export_scene(
    outdir: str,
    manifest: dict,
    *,
    issue: str,
    label: str = "",
    entries: list[tuple[MediaEntry, bool]],
    origin: str = "bulk",
) -> list[str]:
    """把 `entries`（`(条目, 是否来自日志包)`）复制进 `<outdir>/images/`，返回文件名。

    返回的是**这一轮确认过的名字**（包括内容没变、跳过了写入的那些）：清理时拿它当"本次保留"，
    内容一致就不再落盘，看图的工具不会因为 mtime 变了而重新解码。
    """
    if not issue or not entries:
        return []
    records = manifest.setdefault("images", {})
    if not isinstance(records, dict):  # 坏 manifest 防御
        records = {}
        manifest["images"] = records
    target = scene_dir(outdir)
    os.makedirs(target, exist_ok=True)
    multi = len(entries) > 1
    used: set[str] = set()
    written: list[str] = []
    for entry, from_pack in entries:
        blob = _bytes(entry)
        digest = hashlib.sha256(blob).hexdigest()
        name = _pick_name(target, records, issue, label, from_pack, multi, entry, digest, used)
        path = os.path.join(target, name)
        if not (os.path.isfile(path) and _sha256_file(path) == digest):
            temp = path + ".part"
            with open(temp, "wb") as handle:
                handle.write(blob)
            os.replace(temp, path)
        used.add(name)
        written.append(name)
        records[name] = {
            "issue": issue,
            "label": label,
            "source": "pack" if from_pack else "telemetry",
            "origin": origin,
            "sha256": digest,
            "size": len(blob),
            "from": entry.member,
            "copied_at": datetime.now().isoformat(timespec="seconds"),
        }
    return written


def prune_scene(
    outdir: str,
    manifest: dict,
    written: set[str],
    *,
    issues: set[str],
    keep_issues: set[str] | None = None,
) -> int:
    """清掉不该留着的现场图，返回删掉的张数。

    与 `cases/output.py:prune_artifacts` 同一套归属判据：

    * 归属 issue 不在本次集合（版本筛掉 / 已 resolve / 库里消失）→ 删；
    * 归属 issue 在集合里、这一轮也导出了图，但**这张**没被本次确认 → 删（被新的失败取代）；
    * 归属 issue 在集合里、这一轮一张图都没拿到（遥测包没图、日志包也没命中）→ 留着，
      道理同"重画失败的 issue 保住旧图"；
    * `keep_issues`（本次什么都没画出来的 issue）里的 → 不动。

    磁盘上**没有记录**、但名字正好是本工具命名（`<问题号>_<问题名>…png`）的文件也按归属判一次
    （`_prune_unrecorded`）：manifest 被另一轮覆盖过、或 `--clean-flow` 之后又被旧图占位时，
    那张图就永远没人认领；别的文件（用户自己放进去的）不碰。
    """
    records = manifest.get("images")
    if not isinstance(records, dict):
        return 0
    target = scene_dir(outdir)
    keep = keep_issues or set()
    confirmed = set(written)
    written_issues = {
        str(records[name].get("issue") or "")
        for name in confirmed
        if isinstance(records.get(name), dict)
    }
    removed = _prune_recorded(records, target, confirmed, issues, keep, written_issues)
    removed += _prune_unrecorded(target, records, confirmed, issues, keep, written_issues)
    return removed


def _prune_recorded(
    records: dict,
    target: str,
    confirmed: set[str],
    issues: set[str],
    keep: set[str],
    written_issues: set[str],
) -> int:
    """有记录的那些：按 issue 归属删。"""
    removed = 0
    for name in sorted(records):
        record = records.get(name)
        if os.path.basename(name) != name or not isinstance(record, dict):
            records.pop(name, None)
            continue
        path = os.path.join(target, name)
        if not os.path.isfile(path):
            records.pop(name, None)  # 文件早就不在了：别让记录一直挂着
            continue
        issue = str(record.get("issue") or "")
        origin = str(record.get("origin") or "")
        if not issue or issue in keep:
            continue
        if issue not in issues:
            reason = "不在本次 issue 集合内"
        elif (
            issue in written_issues
            and name not in confirmed
            and origin in ("", "bulk")
        ):
            # 单包（`origin == "case"`）导出的图不动：那是用户按某次运行单独导的，
            # 批量这一轮导的是别的图名，删掉就再也找不回来（与产物清理同一取舍）
            reason = "已被本次重画取代"
        else:
            continue
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("提示：删不掉旧现场图 %s（%s），下次再试", path, exc)
            continue
        records.pop(name, None)
        removed += 1
        log.info("删掉旧现场图（%s）：%s", reason, name)
    return removed


def _prune_unrecorded(
    target: str,
    records: dict,
    confirmed: set[str],
    issues: set[str],
    keep: set[str],
    written_issues: set[str],
) -> int:
    """没有记录、但名字正好是本工具现场图命名的那些（记录丢了 / 并发跑过 / `--clean-flow` 之后
    又被旧图占位）：issue 不在集合内、或已被本次确认的那张取代 → 删；别的文件不碰。

    这一条兜住实测遇到的一次：另一台命令在同一个目录里跑过、写下的记录被这次的 manifest 覆盖，
    磁盘上就留了一张没人认领的 `<问题号>_<问题名>_日志包.png`，而新图已经叫 `<问题号>_<问题名>.png`。
    """
    removed = 0
    try:
        names = sorted(os.listdir(target))
    except OSError:
        return 0
    for name in names:
        if name in records or name in confirmed or not name.lower().endswith(".png"):
            continue
        path = os.path.join(target, name)
        if not os.path.isfile(path):
            continue
        issue, _rest = artifact_issue(name)
        if not issue or issue in keep:
            continue
        if issue not in issues:
            reason = "不在本次 issue 集合内"
        elif issue in written_issues:
            reason = "已被本次重画取代"
        else:
            continue
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("提示：删不掉旧现场图 %s（%s），下次再试", path, exc)
            continue
        removed += 1
        log.info("删掉旧现场图（没有记录、%s）：%s", reason, name)
    return removed
