"""「疑似过时」检查：按日志的资源版本（取不到再借同装 MFA 日志）确定基线修订，
再逐节点比较「那一版的定义」和「现在的定义」。

git / JSONC / 节点定义全部来自 `tools/_pipeline_state.py`，本模块只负责"选哪个基线、
哪些节点算改过"以及文案。判不出来时 `reason` 写清原因，不当成"没过时"。"""

from __future__ import annotations


import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tools._pipeline_state import (
    definition_changed as _definition_changed,
    git as _git,
    node_definitions as _node_definitions,
    strip_jsonc as _strip_jsonc,
    main_last_commit,
    pipeline_modules,
)

from .logparse import _open_log_lines

from .model import Step, parse_ts


# --------------------------------------------------------------------------- #
# 节点是否"疑似过时"：日志所属版本之后，该节点所在模块有没有提交
# --------------------------------------------------------------------------- #

# **资源版本**是唯一可信来源：MFA 日志里那行 `[cfg=Default] 资源版本：v1.0.0-beta.58`。
# **不拿安装目录名当版本**：正常安装目录本来就不带版本；带了也是装机时的名字，更新资源后
# 只会是错的（实测同一份日志目录名 `beta.42`、资源版本 `beta.58`）。判不出来就照实说，
# 不猜——猜错的基线会把几个月前的重构全标成"疑似过时"（2026-11-02 用户两次反馈）。
RESOURCE_VERSION_RE = re.compile(r"资源版本[:：]\s*(v[0-9][0-9A-Za-z.\-]*)")
# 日志行首的时间戳（`[2026-11-02 08:38:15.973]`）：判断"这条资源版本是什么时候报的"要用。
_LINE_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
# MFA 的界面日志文件名：`log-20261102.log`。资源版本只写在它里面（原生 maafw.log 没有）。
MFA_LOG_NAME_RE = re.compile(r"^log-\d{8}\.log$", re.IGNORECASE)
# 版本从哪来：日志自己写的 / 同装 MFA 界面日志里借的（后者要在图注里说明，读图的人才不疑惑）
VERSION_SOURCE_LOG = "日志"
VERSION_SOURCE_MFA_LOG = "MFA日志"
# 遥测来的版本：可以是 `evidence.json` 的 `event.release`，也可以是本地遥测库里那张卡片
# （`--telemetry` 反查到的 issue）。两者都不是从 maafw.log 抠的。
VERSION_SOURCE_TELEMETRY = "遥测"
# 用 --version 手动指定基线
VERSION_SOURCE_CLI = "手动指定"


@dataclass(frozen=True)
class StaleNode:
    """一个"疑似过时"的节点。"""

    name: str
    module: str  # 所在模块（assets/resource/pipeline/<module>）
    last_commit: str  # 最近一次改动该模块的提交（`hash 日期 标题`），供人核对
    commits: int = 1  # 该模块在版本之后的提交数（只用于展示"这文件动过几次"）


@dataclass
class StaleCheck:
    """ "这批节点里哪些疑似过时"的结论。

    `reason` 非空 = 检查做不成（取不到版本号 / 不是 git 仓库 / 那个 tag 不存在），
    人要照它说的去补信息，不能当成"没有过时节点"。
    """

    version: str = ""  # 日志所属资源版本（v1.0.0-beta.58）
    tag: str = ""  # 对应的 git tag
    nodes: list[StaleNode] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # 当前 pipeline 里已不存在的节点名
    reason: str = ""  # 非空 = 检查没做成的原因
    # 版本是从哪来的：`日志`（自己写的）或 `MFA日志`（同装 logs/log-*.log 里借的）
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def resource_version(text: str) -> str | None:
    """从一行日志里取**资源版本**（`资源版本：v1.0.0-beta.58`），取不到返回 None。

    这是 MFA 自己报的、与资源包对得上的版本。安装目录名**不作来源**（见上面常量的注释）。
    """
    m = RESOURCE_VERSION_RE.search(text)
    return m.group(1) if m else None


def _version_marks(lines) -> list[tuple[datetime, str]]:
    """`[(行首时间, 资源版本)]`：只收"有行首时间戳、也有资源版本"的行。

    一份日志里可能出现**多条**（实测 `log-20261102.log` 里 08:38:15 是 beta.58、08:38:54
    更新完资源后变成 beta.61）。调用方按时间挑，不要一律取第一条。
    """
    out: list[tuple[datetime, str]] = []
    for raw in lines:
        ver = resource_version(raw)
        if not ver:
            continue
        m = _LINE_TS_RE.match(raw)
        if m:
            out.append((parse_ts(m.group(1)), ver))
    return out


def check_stale_nodes(
    repo: Path, node_names: set[str], version: str | None, source: str = ""
) -> StaleCheck:
    """查这批节点里哪些"疑似过时"。

    口径（**只标疑似，不判断是否真的过时**）：

    - 版本只认 **`资源版本：vX.Y.Z`**（MFA 自己报的，与资源包对得上）：优先取日志自己的，
      没有就从同装 `logs/log-*.log` 借（`source` 记来源）。**不看安装目录名**——正常安装目录
      不带版本，带了也是装机时的旧名字（更新资源后就是错的）。取不到就查不了，`reason` 写明
      原因，不是"没有过时节点"；
    - 节点名在**当前** `assets/resource/pipeline/` 里找不到 → 记进 `missing`
      （这是"已被改名或删除"的事实，不是推测）；
    - 找得到 → 拿它**在 `vX.Y.Z` 那一刻的定义**和**现在的定义**逐字段比：
      不同 → 标"疑似过时"，把最近一次改动它所在文件的提交写出来供人核对。
      只比定义，不比 diff 里的名字出现（那种粒度会把"文件被重排"也算进来，实测
      一整份文件 47 个节点会被一起标上）；定义一字未动的节点不标；
    - 版本对应的 tag 不存在（本地没拉到那个 tag）→ `reason` 说清，不当成"没提交"。
    """
    if not version:
        return StaleCheck(
            reason="日志和同装的 MFA 日志（logs/log-*.log）里都没有「资源版本：vX.Y.Z」，"
            "判断不了这批资源属于哪个版本，跳过「疑似过时」检查"
        )
    code, tags = _git(repo, "tag", "--list", version)
    if code != 0:
        return StaleCheck(
            version=version,
            source=source,
            reason="不是 git 仓库或 git 不可用，查不了提交历史",
        )
    if version not in tags.split():
        return StaleCheck(
            version=version,
            source=source,
            reason=f"本地没有 {version} 这个 tag（先 git fetch --tags），"
            "查不了该版本之后的提交",
        )
    owner = pipeline_modules(repo)
    then = _node_definitions(repo, version)
    now: dict[str, dict[str, Any]] = {}
    for module, nodes in then.items():
        path = repo / "assets" / "resource" / "pipeline" / module
        try:
            data = json.loads(
                _strip_jsonc(path.read_text(encoding="utf-8", errors="replace"))
            )
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            now[module] = {k: v for k, v in data.items() if isinstance(v, dict)}

    out = StaleCheck(version=version, tag=version, source=source)
    for name in sorted(node_names):
        module = owner.get(name)
        if module is None:
            out.missing.append(name)
            continue
        before = then.get(module, {}).get(name)
        after = now.get(module, {}).get(name)
        if before is None or after is None:
            # tag 时还没有这个节点（版本之后新增）→ 不是"过时"，不标
            continue
        if not _definition_changed(before, after):
            continue
        last = main_last_commit(repo, version, module)
        out.nodes.append(
            StaleNode(name=name, module=module, last_commit=last, commits=1)
        )
    return out


def _sibling_mfa_logs(log: Any) -> list[str]:
    """找同一套安装里的 MFA 界面日志（`logs/log-YYYYMMDD.log`）。

    原生 `debug/maafw.log` 里**没有** `资源版本` 行——那是 MFAAvalonia 写的，只进它自己的
    `logs/log-*.log`。安装布局是 `<根>\\debug\\maafw.log` + `<根>\\logs\\log-*.log`，
    所以按"日志所在目录"和"它的上一级"各自找一次 `logs/`。
    """
    path = log if isinstance(log, str) else getattr(log, "name", None)
    if not isinstance(path, str) or "\n" in path or not os.path.isfile(path):
        return []
    here = os.path.dirname(os.path.abspath(path))
    out: list[str] = []
    for logdir in (os.path.join(here, "logs"), os.path.join(os.path.dirname(here), "logs")):
        if not os.path.isdir(logdir):
            continue
        for name in sorted(os.listdir(logdir)):
            if MFA_LOG_NAME_RE.match(name):
                out.append(os.path.join(logdir, name))
    return out


def _version_from_siblings(log: Any, since: datetime | None) -> str | None:
    """从同装的 MFA 界面日志里取**那一刻**生效的资源版本。

    取 `since`（这段内容的末尾时刻）之前最后一条；都在它之后（日志比 MFA 日志还早）
    就取最早的一条。判不出来返回 None。
    """
    marks: list[tuple[datetime, str]] = []
    for path in _sibling_mfa_logs(log):
        try:
            marks += _version_marks(_open_log_lines(path))
        except OSError:
            continue
    if not marks:
        return None
    marks.sort(key=lambda m: m[0])
    if since is None:
        return marks[-1][1]
    before = [m for m in marks if m[0] <= since]
    return (before[-1] if before else marks[0])[1]


def _log_version_of(log: Any, since: datetime | None = None) -> tuple[str, str] | None:
    """找这批资源所属的**资源版本**，返回 `(版本, 来源)`；找不到返回 None。

    来源：`日志`（日志自己写了 `资源版本`，取最后一条——更新过资源就以更新后的为准）或
    `MFA日志`（从同装的 `logs/log-*.log` 里按 `since` 挑）。

    **不看安装目录名**：正常安装目录不带版本，带了也是装机时的旧名字（更新资源后就是错的）。
    判不出来就说判不出来——猜一个错基线比不查更糟。
    """
    try:
        marks = _version_marks(_open_log_lines(log))
    except (OSError, TypeError):
        return None
    if marks:
        return marks[-1][1], VERSION_SOURCE_LOG
    found = _version_from_siblings(log, since)
    return (found, VERSION_SOURCE_MFA_LOG) if found else None


def _apply_stale(check: StaleCheck) -> None:
    """把"疑似过时"的节点名记成类级集合，供框里的名字加后缀。

    走类属性而不是给 `Step` 加字段：`Step` 是 `@dataclass` 且被大量拷贝（`overlay_repeats`
    会重造列），加字段要一路带参数；而"哪些节点疑似过时"整张图只有一份，类属性正好。
    """
    Step.stale_names = frozenset(n.name for n in check.nodes)


def stale_label(name: str) -> str:
    """框里/`--list` 里的节点名：疑似过时的加后缀。后缀只用有字形的字符（`⚠` 画成方框）。"""
    return f"{name}（疑似过时）" if name in Step.stale_names else name
