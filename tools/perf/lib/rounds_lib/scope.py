"""日志与当前 pipeline 的对账：模块归属、缺失节点补齐、基线修订与「改过」判定。

这里会读 `assets/resource/pipeline/*.json` 与 git 历史（`tools/_pipeline_state.py`），
但不打印报告；结论放在 `Baseline` / 返回值里，由 report 决定怎么呈现。
"""

from __future__ import annotations

import glob
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tools._pipeline_state import (
    TS_RE,
    definition_changed,
    git,
    node_definitions,
    revision_before,
    strip_jsonc,
    version_in_effect,
)

from .consts import DEFAULT_LOG_DIR, FRAMEWORK_PREFIXES, LOG_GLOB

from .logs import Window, parse_ts

def pipeline_nodes(root: Path) -> dict[str, str]:
    """当前 pipeline 的顶层节点名 → 文件名；读不到就返回空 dict。"""
    owner: dict[str, str] = {}
    if not root.is_dir():
        return owner
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(
                strip_jsonc(path.read_text(encoding="utf-8", errors="replace"))
            )
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for name in data:
                owner.setdefault(name, path.name)
    return owner


def is_framework(name: str) -> bool:
    """框架合成的伪节点（例如 `@mse/action`），不在 pipeline JSON 里，不算「已删除」。"""
    return name.startswith(FRAMEWORK_PREFIXES)


def normalize_module(value: str) -> str:
    """`escort`、`escort.json`、`pipeline/escort.json` 都归成 `escort`。"""
    name = Path(value.replace("\\", "/")).name.strip()
    if name.lower().endswith(".json"):
        name = name[: -len(".json")]
    return name


def module_scope(
    pipeline: dict[str, str] | None, modules: list[str]
) -> tuple[dict[str, str], list[str]]:
    """按模块名（pipeline 文件名去后缀）收窄节点表，返回 (模块节点表, 找不到的模块)。"""
    scoped: dict[str, str] = {}
    if pipeline is None:
        return scoped, []
    wanted = {normalize_module(item) for item in modules if normalize_module(item)}
    known = {Path(file).stem for file in pipeline.values()}
    unknown = sorted(wanted - known)
    scoped = {name: file for name, file in pipeline.items() if Path(file).stem in wanted}
    return scoped, unknown


def filter_windows(windows: list[Window], nodes: set[str]) -> tuple[list[Window], int]:
    """只留派发者属于这个模块的窗口，返回 (留下的窗口, 被丢掉的数量)。"""
    kept = [window for window in windows if window.parent in nodes]
    return kept, len(windows) - len(kept)


@dataclass
class Baseline:
    """这份日志跑的时候 pipeline 长什么样，以及哪些派发者之后被改过。"""

    version: str = ""
    source: str = ""
    updates: int = 0
    rev: str = ""
    stale: list[str] = field(default_factory=list)
    changed_modules: list[str] = field(default_factory=list)
    reason: str = ""


def discover(specs: list[str]) -> tuple[list[Path], list[str]]:
    """文件 / 目录 / 通配 → 日志清单（未排序、未去重）；第二个值是展开时的提示。"""
    items: list[Path] = []
    notes: list[str] = []
    for spec in specs or [str(DEFAULT_LOG_DIR)]:
        path = Path(spec)
        if path.is_dir():
            found = sorted(path.glob(LOG_GLOB))
            if not found:
                notes.append(f"{path} 下没有 {LOG_GLOB}")
            items.extend(found)
        elif any(ch in spec for ch in "*?["):
            items.extend(sorted(Path(item) for item in glob.glob(spec)))
        else:
            items.append(path)
    return items, notes


def _first_ts(path: Path) -> datetime:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _ in range(80):
            line = fh.readline()
            if not line:
                break
            match = TS_RE.match(line)
            if match:
                return parse_ts(match.group(1))
    raise ValueError(f"{path} 里没有行首时间戳")


def _last_ts(path: Path, fallback: datetime) -> datetime:
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - 200_000))
        tail = fh.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        match = TS_RE.match(line)
        if match:
            return parse_ts(match.group(1))
    return fallback


def log_span(path: Path) -> tuple[datetime, datetime]:
    """日志内容的起止时间。文件名里的时刻是**换名**那一刻，不能拿来排序。"""
    first = _first_ts(path)
    return first, _last_ts(path, first)


def dedupe_logs(paths: list[Path]) -> tuple[list[Path], int]:
    """按「内容起点 + 文件大小」去重，返回 (保留的清单, 丢掉的份数)。"""
    seen: set[tuple[datetime, int]] = set()
    kept: list[Path] = []
    for path in paths:
        try:
            key = (_first_ts(path), path.stat().st_size)
        except (OSError, ValueError):
            kept.append(path)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(path)
    return kept, len(paths) - len(kept)


def stale_in(
    names: set[str],
    then: dict[str, dict[str, Any]],
    now: dict[str, dict[str, Any]],
) -> list[str]:
    """`names` 里哪些节点在 `then` 和现在的工作区之间被改过（只比定义）。"""
    owner = {name: module for module, defs in now.items() for name in defs}
    changed: list[str] = []
    for name in sorted(names):
        module = owner.get(name)
        if module is None or module not in then:
            continue
        before = then[module].get(name)
        after = now[module].get(name)
        if before is None or after is None:
            continue
        if definition_changed(before, after):
            changed.append(name)
    return changed


def baseline_for(
    path: Path,
    names: set[str],
    repo: Path,
    cache: dict[str, dict[str, dict[str, Any]]],
    span: tuple[datetime, datetime] | None,
    mfa_log_dir: Path | None = None,
) -> Baseline:
    """这份日志的基线修订 + 改过的节点。

    基线优先取**会话起点**的资源版本（旁边 `logs/log-*.log` 里那行 `资源版本：vX`）；
    版本查不到才退到「会话起点之时 git 里 pipeline 的修订」。
    """
    result = Baseline()
    if span is None:
        result.reason = "读不出日志的时间跨度"
        return result
    start, end = span
    extra = [mfa_log_dir] if mfa_log_dir else []
    version, source, updates = version_in_effect(path, start, end, extra)
    result.version, result.source, result.updates = version, source, updates
    rev = ""
    if version:
        code, tags = git(repo, "tag", "--list", version)
        if code == 0 and version in tags.split():
            rev = version
        else:
            result.reason = f"本地没有 {version} 这个 tag（先 git fetch --tags），改用时间点"
    if rev:
        result.rev = rev
    else:
        rev = revision_before(repo, start)
        if not rev:
            result.reason = result.reason or "不是 git 仓库或日志早于历史，查不了节点改没改过"
            return result
        result.rev = rev[:8]
        if not version:
            result.source = "时间点"
            result.version = rev[:8]
    then = cache.get(rev)
    if then is None:
        then = node_definitions(repo, rev)
        cache[rev] = then
    now = cache.get("")
    if now is None:
        now = node_definitions(repo, None)
        cache[""] = now
    result.stale = stale_in(names, then, now)
    owner = {name: module for module, defs in now.items() for name in defs}
    result.changed_modules = sorted({owner[name] for name in result.stale})
    return result


def node_flags(
    present: set[str], pipeline: dict[str, str] | None
) -> tuple[list[str], list[str]]:
    """日志里的派发者与当前 pipeline 对不上的两类：日志独有 / 当前独有。"""
    if pipeline is None:
        return [], []
    missing = sorted(
        name for name in present if name not in pipeline and not is_framework(name)
    )
    unseen = sorted(name for name in pipeline if name not in present)
    return missing, unseen


def impute_missing(
    windows: list[Window], pipeline: dict[str, str] | None
) -> tuple[float | None, int]:
    """把「当前 pipeline 已没有」的窗口时长换成现有窗口的平均值。

    平均只由现有节点（当前 pipeline 里的 + 框架伪节点）算；被替换的旧时长不参与。
    返回 (平均值, 被替换的窗口数)；没有可替换的或读不到 pipeline 时返回 (None, 0)。
    """
    if pipeline is None:
        return None, 0
    missing = [
        window
        for window in windows
        if window.parent not in pipeline and not is_framework(window.parent)
    ]
    existing = [
        window
        for window in windows
        if window.parent in pipeline or is_framework(window.parent)
    ]
    if not missing or not existing:
        return None, 0
    average = statistics.mean(window.ms for window in existing)
    for window in missing:
        window.ms = average
    return average, len(missing)
