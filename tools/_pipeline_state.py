#!/usr/bin/env python
"""pipeline 的 git 侧事实：节点定义、模块归属、修订查询、资源版本标记。

两个工具共用这一份：

* `tools/drawing/lib/flow_from_log.py` —— 按日志里的**资源版本**（tag）比节点定义，标「疑似过时」；
* `tools/perf/lib/rounds.py` —— 按日志**会话起点**的资源版本（取不到就退到时间点）比节点定义，
  标出「这份日志之后改过的节点」，好判断统计里的耗时还能不能代表现在的 pipeline。

只放"能查证的事实"，不放结论文案：谁算过时、怎么呈现由调用方决定。

`rev` 可以是 tag、commit 或空（空 = 当前工作区）。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

# 日志行首时间戳（`[2026-11-02 08:38:15.973]`）
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")
# MFA 自己报的资源版本：`[cfg=Default] 资源版本：v1.0.0-beta.58`
RESOURCE_VERSION_RE = re.compile(r"资源版本[:：]\s*(v[0-9][0-9A-Za-z.\-]*)")
# MFA 界面日志文件名（`logs/log-20261102.log`），资源版本只写在这种文件里
MFA_LOG_NAME_RE = re.compile(r"^log-\d{8}\.log$", re.IGNORECASE)
PIPELINE_REL = "assets/resource/pipeline"


def parse_ts(text: str) -> datetime:
    """`2026-11-02 08:38:15.973` → datetime。日志行首是 ISO 格式，`fromisoformat` 比
    `strptime` 快一个量级（`strptime` 每次还要 `setlocale`），不认的写法才回退。"""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def git(repo: Path, *args: str) -> tuple[int, str]:
    """跑一条 git 命令，返回 (退出码, 输出)。失败不抛——仓库状态不对时只是查不了。"""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def strip_jsonc(text: str) -> str:
    """去掉 JSONC 注释与行尾逗号，好让 pipeline 文件用标准 json 解析。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


_PIPELINE_MODULES_CACHE: dict[tuple[str, tuple[tuple[str, int, int], ...]], dict[str, str]] = {}


def pipeline_modules(repo: Path) -> dict[str, str]:
    """当前 pipeline 的节点名 → 模块文件名（读每个文件的顶层 key）。

    每画一张图都会问一次（18 个 JSON，约 10~20 ms），bulk 里 26 张图白读 26 遍；
    按"仓库 + 各文件的 (名字, mtime, 大小)"缓存，工作区改过就自动失效。
    """
    root = repo / PIPELINE_REL
    if not root.is_dir():
        return {}
    paths = sorted(root.glob("*.json"))
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((path.name, stat.st_mtime_ns, stat.st_size))
    key = (str(repo), tuple(signature))
    cached = _PIPELINE_MODULES_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    owner: dict[str, str] = {}
    for path in paths:
        try:
            data = json.loads(
                strip_jsonc(path.read_text(encoding="utf-8", errors="replace"))
            )
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for name in data:
                owner.setdefault(name, path.name)
    _PIPELINE_MODULES_CACHE[key] = owner
    return dict(owner)


def _show_many(repo: Path, rev: str, paths: list[Path]) -> dict[str, str]:
    """一次 `git cat-file --batch` 取多个 blob：文件名的 basename → 文本。

    逐个 `git show` 每次都要起一个 git 进程（Windows 上约 57 ms），18 个 pipeline 文件就是
    1 s；而这一步每画一张图都要做一遍。批处理实测 18 个文件 1.02 s → 0.06 s。
    失败（不是仓库、rev 写错、git 不可用）返回空 dict，调用方退回逐个 `git show`。
    """
    if not paths:
        return {}
    query = "".join(
        f"{rev}:{path.relative_to(repo).as_posix()}\n" for path in paths
    ).encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "cat-file", "--batch"],
            cwd=str(repo),
            input=query,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    data = proc.stdout
    out: dict[str, str] = {}
    pos = 0
    for path in paths:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode("utf-8", "replace")
        pos = nl + 1
        parts = header.split(" ")
        if len(parts) < 3 or parts[-1] == "missing":
            continue  # `<rev>:<path> missing`：这个修订里还没有这个文件
        try:
            size = int(parts[-1])
        except ValueError:
            continue
        out[path.name] = data[pos : pos + size].decode("utf-8", "replace")
        pos += size + 1  # 内容后面那个换行
    return out


_NODE_DEFINITION_CACHE: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}


def node_definitions(repo: Path, rev: str | None = None) -> dict[str, dict[str, Any]]:
    """`rev` 那一刻各 pipeline 文件里的节点定义：模块名 → (节点名 → 定义)。

    `rev` 为空时读当前工作区。取不到（文件在该修订里还不存在、解析失败）就跳过——
    调用方拿不到就以空定义处理，不当成"没变"。

    `rev` 非空时按 `(仓库, rev)` 缓存在进程内（同一批图共用同一个基线版本），并返回浅拷贝：
    调用方改了不影响缓存。
    """
    root = repo / PIPELINE_REL
    if not root.is_dir():
        return {}
    paths = sorted(root.glob("*.json"))
    key: tuple[str, str] | None = None
    texts: dict[str, str] = {}
    if rev is None:
        texts = {}
    else:
        key = (str(repo), rev)
        cached = _NODE_DEFINITION_CACHE.get(key)
        if cached is not None:
            return {name: dict(nodes) for name, nodes in cached.items()}
        texts = _show_many(repo, rev, paths)
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        if rev is None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        else:
            text = texts.get(path.name, "")
            if not text.strip():
                code, text = git(
                    repo, "show", f"{rev}:{path.relative_to(repo).as_posix()}"
                )
                if code != 0 or not text.strip():
                    continue
        try:
            data = json.loads(strip_jsonc(text))
        except ValueError:
            continue
        if isinstance(data, dict):
            out[path.name] = {k: v for k, v in data.items() if isinstance(v, dict)}
    if key is not None:
        _NODE_DEFINITION_CACHE[key] = out
    return {name: dict(nodes) for name, nodes in out.items()}


def definition_changed(old: Any, new: Any) -> bool:
    """两份节点定义是否不同（按键排序后比，避免键顺序造成的假差异）。"""
    return json.dumps(old, sort_keys=True, ensure_ascii=False) != json.dumps(
        new, sort_keys=True, ensure_ascii=False
    )


def last_commit_touching(repo: Path, rev: str, path: str) -> str:
    """`rev..HEAD` 里最后改动某个文件的提交（`hash 日期 标题`）。"""
    _code, out = git(repo, "log", "-1", "--format=%h %cI %s", f"{rev}..HEAD", "--", path)
    return out.strip()


_LAST_COMMIT_CACHE: dict[tuple[str, str, str], str] = {}


def main_last_commit(repo: Path, rev: str, module: str) -> str:
    """带缓存的 `last_commit_touching`：同一模块只查一次 git，几十个节点共用一条结果。"""
    key = (str(repo), rev, module)
    if key not in _LAST_COMMIT_CACHE:
        _LAST_COMMIT_CACHE[key] = last_commit_touching(
            repo, rev, f"{PIPELINE_REL}/{module}"
        )
    return _LAST_COMMIT_CACHE[key]


def revision_before(repo: Path, when: datetime, pathspec: str = PIPELINE_REL) -> str:
    """`when` 之前最后一次改动 pipeline 的提交号；查不到（不是 git 仓库 / 早于历史）返回空串。

    传 `when` 时要带时区：日志是本地时间，`strptime` 出来的是 naive，
    这里按本地时区补上偏移，免得 git 把它当 UTC。
    """
    stamp = when.astimezone().isoformat()
    code, out = git(repo, "log", "-1", "--before", stamp, "--format=%H", "--", pathspec)
    return out.strip() if code == 0 else ""


def resource_version(text: str) -> str | None:
    """从一行日志里取资源版本（`资源版本：v1.0.0-beta.58`），取不到返回 None。"""
    m = RESOURCE_VERSION_RE.search(text)
    return m.group(1) if m else None


def version_marks(lines: Iterable[str]) -> list[tuple[datetime, str]]:
    """`[(行首时间, 资源版本)]`：只收"有行首时间戳、也有资源版本"的行。

    一份日志里可能有多条（更新资源后会再报一次），调用方按时间挑，不要一律取第一条。
    """
    out: list[tuple[datetime, str]] = []
    for raw in lines:
        ver = resource_version(raw)
        if not ver:
            continue
        m = TS_RE.match(raw)
        if m:
            out.append((parse_ts(m.group(1)), ver))
    return out


def sibling_mfa_logs(path: Path) -> list[Path]:
    """同一套安装里的 MFA 界面日志：`<日志目录>/logs` 与 `<日志目录>/../logs`。

    安装布局是 `<根>\\debug\\maafw.log` + `<根>\\logs\\log-*.log`；单独拷出来的
    `maafw.log` 旁边没有 `logs/`，这时返回空表（调用方要能区分"查不了"与"没变"）。
    """
    here = path.resolve().parent
    found: list[Path] = []
    for log_dir in (here / "logs", here.parent / "logs"):
        if not log_dir.is_dir():
            continue
        found.extend(
            item for item in sorted(log_dir.iterdir())
            if item.is_file() and MFA_LOG_NAME_RE.match(item.name)
        )
    return found


def version_in_effect(
    path: Path, start: datetime, end: datetime, extra_dirs: Iterable[Path] = ()
) -> tuple[str, str, int]:
    """日志会话期间生效的资源版本：`(版本, 来源, 会话内变过的次数)`。

    优先取**会话起点之前**最后一条标记（开始跑的时候用的就是它）；起点之前一条都没有
    （日志与 MFA 日志不同源、或日志比 MFA 日志早），退回起点之后第一条并注明来源。
    查不到返回 `("", "", 0)`。
    """
    marks: list[tuple[datetime, str, str]] = []
    own = _file_marks(path, "日志")
    marks.extend(own)
    if not own:
        extra = [
            item
            for log_dir in extra_dirs
            for item in sorted(Path(log_dir).glob("log-*.log"))
        ]
        for item in [*sibling_mfa_logs(path), *extra]:
            marks.extend(_file_marks(item, "MFA日志"))
    if not marks:
        return "", "", 0
    marks.sort(key=lambda item: item[0])
    before = [item for item in marks if item[0] <= start]
    inside = [item for item in marks if start < item[0] <= end]
    if before:
        return before[-1][1], before[-1][2], len(inside)
    first = marks[0]
    return first[1], first[2], len(inside)


def _file_marks(path: Path, source: str) -> list[tuple[datetime, str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [(ts, ver, source) for ts, ver in version_marks(text.splitlines())]
