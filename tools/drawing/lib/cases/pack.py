"""证据包这一层：日志挑选、`Case`、`load_case`/`discover_cases`、问题号与文件名要素。

输入是一个 `debug/` 目录、一组多卷归档或一份日志文件；输出是 `Case`（日志正文 + 图片证据句柄）。
这里不画图、不写 manifest，也不做遥测接口调用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.drawing.lib.flow_from_log import (
    FlowOptions,
    MediaResolver,
    _is_zip,
    _open_log_lines,
    resource_version,
)

REPO_ROOT = Path(__file__).resolve().parents[4]

log = logging.getLogger("tools.flow_cases")

LOG_NAMES = ("maafw.log", "maa.log")
# 轮转后的日志（`maafw.bak.2026.11.02-08.57.51.341.log`）：MFA 每次启动轮转一次，名字里带时间戳。
# **只在没有确切名字时**才认，多份取 mtime 最新的那份——别把 `maafw.log` 本身漏给这个兜底。
ROTATED_LOG_RE = re.compile(r"^(?:maafw|maa)[\w.\-]*\.log$", re.IGNORECASE)
# MFA 分卷命名：`<名字>-partNN.zip`；分组时按去掉这一段的名字归并
PART_RE = re.compile(r"-part\d+$", re.IGNORECASE)
# 遥测 release（Sentry 事件字段）：`MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38`
# 取最后一个 `vX.Y.Z` 段——拼接格式下它永远是本仓库的资源版本，前面的 MFA 版本段忽略。
RELEASE_VERSION_RE = re.compile(r"v[0-9][0-9A-Za-z.\-]*")
# 证据包根目录：默认 `temp/evidence`（`evidence` 命令落到这里）；`--evidence-dir` 可再加几个，
# 搜索顺序是用户给的在前、默认在最后。
EVIDENCE_ROOT = str(REPO_ROOT / "temp" / "evidence")


def resource_version_from_release(release: str) -> str | None:
    """从遥测 release 字符串里抠出资源版本（`v1.0.0-beta.38`）。

    与 `tools/sentry/report_common.version_label` 同一结论，但这里只依赖正则、不依赖 sentry 包，
    让 `flow_cases` 不必反向 import `tools.sentry`。拿不到返回 None。
    """
    if not release:
        return None
    matches = RELEASE_VERSION_RE.findall(release)
    return matches[-1] if matches else None


def _evidence_manifest(case: Case) -> dict[str, Any]:
    """遥测证据包的清单（`evidence.json`）读成 dict；不是遥测证据包或读不出时返回 `{}`。

    证据包目录按 issue 短号建（`temp/evidence/<SHORT-ID>/`），`debug/` 的上一级就是它。
    清单里的 `event` 段记着这包属于哪次运行（`event.id` / `event.release` /
    `failure_task_id`），版本基线与失败 task 定位都从这里取。
    """
    if not case.root:
        return {}
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
        if isinstance(data, dict):
            return data
    return {}


def _version_from_evidence(case: Case) -> str | None:
    """遥测证据包（Sentry 拉下来的）里记着那次运行的 release：`evidence.json` 的 `event.release`。

    比从 maafw.log 抠「资源版本」可靠——原生 maafw.log 平时没有那行。
    """
    event = _evidence_manifest(case).get("event") or {}
    return resource_version_from_release(str(event.get("release") or ""))


def _version_from_log(case: Case) -> str | None:
    """日志自己报的「资源版本：vX.Y.Z」：老证据包的清单里没有 `release` 字段时的兜底。

    一份日志里可能有多条（运行中更新过资源），取**最后一条**——与「疑似过时」的基线口径一致
    （`flowlog/stale.py:_log_version_of`）。原生 maafw.log 平时没有这行，MFA 更新资源时才打，
    所以拿不到就返回 None，不要猜。
    """
    found: str | None = None
    try:
        for line in _open_log_lines(case.log_source()):
            version = resource_version(line)
            if version:
                found = version
    except (OSError, TypeError):
        return None
    return found


def _pack_version(case: Case) -> str | None:
    """这个证据包属于哪次运行的哪个资源版本：清单的 `event.release` → 日志的「资源版本」行。

    按版本筛 issue 时用它判断这个包还能不能用来画流程
    （见 `cases/evidence.py` 的 `_usable_evidence_case`）。
    """
    return _version_from_evidence(case) or _version_from_log(case)


_WARNED_LOG_DIRS: set[str] = set()


def _warn_extra_logs(directory: str, chosen: str) -> None:
    """目录里有多份日志时，说清用了哪份、忽略了哪几份。

    原来只取 `maafw.log`，同目录的 `maafw.bak.*` 静默丢掉——那些轮转日志里往往正是
    更早几次失败的完整过程（实测 11-04 的三连败就在 `.bak` 里），不提示会让人以为
    "只有这一天有记录"。同一个目录只提示一次。
    """
    if directory in _WARNED_LOG_DIRS:
        return
    try:
        others = sorted(
            name
            for name in os.listdir(directory)
            if name != os.path.basename(chosen)
            and ROTATED_LOG_RE.match(name)
            and os.path.isfile(os.path.join(directory, name))
        )
    except OSError:
        return
    _WARNED_LOG_DIRS.add(directory)
    if others:
        log.warning(
            "目录 %s 有多份日志：只用 %s，忽略 %s（要画别的请把 --case 指到那一份）",
            directory,
            os.path.basename(chosen),
            "、".join(others),
        )


def _pick_log(directory: str) -> str | None:
    """目录里的日志文件：先认确切名字（`LOG_NAMES` 顺序），没有再认轮转名（取 mtime 最新）。

    实测用户目录里只有轮转后的 `maafw.bak.2026.11.02-08.57.51.341.log`，按确切名字找会报
    "没找到带日志的证据包"——明明有日志。轮转名归一主多份时按 mtime 排序，最新的那份才是
    当前这一次运行；多份时用 `_warn_extra_logs` 把"忽略了哪几份"打出来。
    """
    chosen: str | None = None
    for name in LOG_NAMES:
        cand = os.path.join(directory, name)
        if os.path.isfile(cand):
            chosen = cand
            break
    if chosen is None:
        rotated = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if ROTATED_LOG_RE.match(name)
            and os.path.isfile(os.path.join(directory, name))
        ]
        if not rotated:
            return None
        chosen = max(rotated, key=os.path.getmtime)
    _warn_extra_logs(directory, chosen)
    return chosen


@dataclass
class Case:
    """一个证据包：日志正文 + 图片证据（可为归档或目录）。"""

    label: str
    log_text: str | None = None  # 归档来源：日志正文
    log_path: str | None = None  # 目录来源：日志文件路径
    packages: list[str] = field(default_factory=list)  # 归档来源：全部卷路径
    root: str | None = None  # 目录来源的根
    bad_packages: list[str] = field(default_factory=list)

    def log_source(self):
        return self.log_text if self.log_text is not None else self.log_path

    def _zip_subdir(self, name: str):
        """归档里 `.../on_error/...` 这类子目录 → 内存索引；延迟解压由 MediaResolver 负责。"""
        if not self.packages:
            return None
        entries: list[tuple[str, object]] = []
        for package in self.packages:
            try:
                with zipfile.ZipFile(package) as zf:
                    for info in sorted(zf.infolist(), key=lambda i: i.filename):
                        if info.is_dir():
                            continue
                        if f"/{name}/" in info.filename.replace("\\", "/"):
                            entries.append(
                                (info.filename, _LazyMember(package, info.filename))
                            )
            except (OSError, zipfile.BadZipFile):
                continue
        return MediaResolver(members=entries) if entries else None

    def _media_dir(self, name: str) -> str | None:
        """媒体子目录：目录 case 用 `<root>/<name>`；**单个日志文件**用日志所在目录的 `<name>/`。

        传单个日志文件时 `root` 为空，原来直接落到 `_zip_subdir()`（没有归档 → None），
        于是"能出图但一张截图都不贴"（实测 184 KB vs 带图的 731 KB）。日志旁边就是
        `vision/` / `on_error/`，与目录 case 同一个约定。
        """
        base = self.root or (
            os.path.dirname(self.log_path) if self.log_path else None
        )
        if not base:
            return None
        sub = os.path.join(base, name)
        return sub if os.path.isdir(sub) else None

    def vision(self):
        sub = self._media_dir("vision")
        return MediaResolver(sub) if sub else self._zip_subdir("vision")

    def on_error(self):
        sub = self._media_dir("on_error")
        return MediaResolver(sub) if sub else self._zip_subdir("on_error")

    def describe(self) -> str:
        if self.root:
            return self.root
        return f"归档 {len(self.packages)} 卷"


@dataclass(frozen=True)
class _LazyMember:
    """归档条目：真正要画时才解压，避免把整包图片一次性读进内存。"""

    package: str
    member: str

    def read(self) -> bytes:
        with zipfile.ZipFile(self.package) as zf:
            return zf.read(self.member)


def _read_log_from_zips(packages: list[str]) -> tuple[str | None, list[str]]:
    """从归档（可多卷）里取日志正文。先看条目名，再按需只解压那一条。"""
    bad: list[str] = []
    candidates: list[tuple[int, str, str]] = (
        []
    )  # (日志名优先级, package, member)；排序后用最低优先级那条
    for package in sorted(packages):
        try:
            with zipfile.ZipFile(package) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    base = os.path.basename(info.filename).lower()
                    for rank, name in enumerate(LOG_NAMES):
                        if base == name:
                            candidates.append((rank, package, info.filename))
        except (OSError, zipfile.BadZipFile) as exc:
            bad.append(f"{package}: {exc}")
    if not candidates:
        return None, bad
    candidates.sort()
    _rank, package, member = candidates[0]
    try:
        with zipfile.ZipFile(package) as zf:
            return zf.read(member).decode("utf-8", errors="replace"), bad
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        bad.append(f"{package}!{member}: {exc}")
        return None, bad


def load_case(case_arg: list[str]) -> Case:
    """把 --case 的入参变成 Case：目录、一个/多个归档、或单份日志文件。"""
    items = [str(p) for p in case_arg]
    dirs = [p for p in items if os.path.isdir(p)]
    zips = [p for p in items if os.path.isfile(p) and _is_zip(p)]
    rest = [p for p in items if p not in dirs and p not in zips]

    if dirs and (zips or rest):
        raise ValueError("--case 不能把目录和归档混在一起；一个 case 只对应一处证据。")
    if len(dirs) > 1:
        raise ValueError(f"--case 只接受一个目录，收到 {len(dirs)} 个：{dirs}")

    if dirs:
        root = dirs[0]
        log = _pick_log(root)
        if log:
            return Case(
                label=os.path.basename(os.path.normpath(root)) or "case",
                log_path=log,
                root=root,
            )
        raise FileNotFoundError(
            f"{root} 下找不到 {' / '.join(LOG_NAMES)}（也没有轮转后的 {root}/maafw*.log）。"
            "给它 debug/ 那一层，或直接把日志文件路径当 --case 传。"
        )

    if zips:
        log_text, bad = _read_log_from_zips(zips)
        if log_text is None:
            raise FileNotFoundError(
                f"{len(zips)} 个归档里都没有 {' / '.join(LOG_NAMES)}（读不了的卷：{bad or '无'}）。"
            )
        label = PART_RE.sub("", os.path.splitext(os.path.basename(sorted(zips)[0]))[0])
        return Case(
            label=label or "archive",
            log_text=log_text,
            packages=sorted(zips),
            bad_packages=bad,
        )

    if len(rest) == 1 and os.path.isfile(rest[0]):
        only = rest[0]
        return Case(label=os.path.splitext(os.path.basename(only))[0], log_path=only)

    missing = [p for p in items if not os.path.exists(p)]
    raise FileNotFoundError(
        f"--case 指向的路径既不是目录也不是归档：{missing or items}"
    )


def discover_cases(root: str) -> list[Case]:
    """列出 root 下的候选证据包（只读，不画图、不写盘）。

    多卷导出包的每一卷都会被识别成独立候选，这里**不自动归并**——归并需要知道
    用户想画哪一段，猜错会画出一张看着合理但缺卷的图。多卷请显式一起传 --case。
    """
    found: list[Case] = []
    if not os.path.isdir(root):
        return found
    if _pick_log(root):
        found.append(load_case([root]))
        return found
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub):
            if _pick_log(sub):
                found.append(load_case([sub]))
        elif os.path.isfile(sub) and _is_zip(sub):
            try:
                found.append(load_case([sub]))
            except (FileNotFoundError, ValueError) as exc:
                log.warning("跳过 %s：%s", sub, exc)
    return found


def log_digest(case: Case) -> str:
    """日志内容的短指纹：manifest 用它判断"是不是同一份输入"。"""
    if case.log_text is not None:
        data = case.log_text.encode("utf-8", errors="replace")
        return hashlib.sha256(data).hexdigest()[:16]
    if case.log_path is None:
        # load_case 保证二者必有一个；这里显式报错，避免 `open(None)` 那种看不出原因的崩溃。
        raise ValueError(
            f"证据包 {case.label} 既没有日志正文也没有日志路径，无法计算指纹。"
        )
    hasher = hashlib.sha256()
    with open(case.log_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


# 遥测问题号：`PYTHON-1Z` 这种（字母段 + 短横 + 字母数字）。
# 证据包目录就是按 issue 的 short_id 建的（`temp/evidence/<SHORT-ID>/`），所以目录名
# 命中这个形状就当作"遥测问题名称"。
ISSUE_RE = re.compile(r"^[A-Z][A-Z0-9]+-[A-Z0-9]+$")
# 任务入口名：日志里 `Tasker.Task.*` 事件的 `entry` 字段（`TaskTrain` / `TaskEscort`）。
TASK_ENTRY_RE = re.compile(
    r'\[msg=Tasker\.Task\.\w+\] \[details(?:_json)?=\{"entry":"([^"\\]+)"'
)


def issue_name_of(case: Case) -> str | None:
    """遥测问题名（`PYTHON-1Z`）。

    只看证据包目录名：`evidence` 把每个 issue 落到 `temp/evidence/<SHORT-ID>/`，目录名
    就是问题号；`--case` 指到 `debug/` 那一层时往上找一层。归档来源看归档文件名
    （`PYTHON-1Z-part01.zip` → `PYTHON-1Z`）。认不出返回 None。
    """
    cands: list[str] = []
    if case.root:
        norm = os.path.normpath(case.root)
        cands.append(os.path.basename(norm))
        cands.append(os.path.basename(os.path.dirname(norm)))
    for package in case.packages:
        stem = os.path.splitext(os.path.basename(package))[0]
        cands.append(PART_RE.sub("", stem))
    for cand in cands:
        if cand and ISSUE_RE.match(cand):
            return cand
    return None


def task_name_of(case: Case, options: FlowOptions) -> str | None:
    """任务名（`TaskTrain` 这种入口名）。

    **给了 `--task-id`** 就只认那个 task：从 `PipelineTask node done [...] [task_id_=N]`
    里取它的入口——`entry` 型节点（`Task` 开头）就是任务入口；没有 entry 型时取它跑的第一个
    节点名。**没给** `--task-id` 才退回"日志里第一个 `Tasker.Task.*` 入口"。
    日志里一条都没有 → None（调用方退回证据包目录名）。

    为什么不用 `task start` 的 `cb_detail` 找：那个字段只出现在**没被导出的那部分**（实测
    `PYTHON-1Z` 有 6 个 task，`task start` 一条都没有），而 `node done` 行总能拿到 `task_id_`。
    """
    text = case.log_text
    if text is None and case.log_path:
        try:
            with open(case.log_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = None
    if not text:
        return None
    if options.task_id is not None:
        tid = str(options.task_id)
        pat = re.compile(
            r'PipelineTask node done \[result=\{[^}]*?"name":"([^"\\]+)"[^}]*\}\] '
            r"\[task_id_=" + tid + r"\]"
        )
        names = [m.group(1) for m in pat.finditer(text)]
        # 先找 entry 型（任务入口节点就叫 TaskXxx）
        for name in names:
            if name.startswith("Task"):
                return name
        return names[0] if names else None
    for m in TASK_ENTRY_RE.finditer(text):
        return m.group(1)
    return None


def log_stamp(case: Case, options: FlowOptions) -> str | None:
    """日志的时间标签（`20261024-031849`），用于同名文件去重。

    优先用 `--from`/`--to`（那是用户指定的窗口，最贴合这张图），都没有时取日志第一行
    的时间戳。取不到返回 None。
    """
    if options.t_from:
        return options.t_from.strftime("%Y%m%d-%H%M%S")
    if options.t_to:
        return options.t_to.strftime("%Y%m%d-%H%M%S")
    text = case.log_text
    if text is None and case.log_path:
        try:
            with open(case.log_path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(64 * 1024)
        except OSError:
            head = ""
        text = head
    if not text:
        return None
    m = re.search(r"\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", text)
    return "".join(m.groups()) if m else None
def _fmt_local(text: str) -> str:
    """Sentry 的时刻是 UTC（`...Z`），概览卡上显示**本地时间**。

    复用遥测模块的解析/偏移 helper（模块级、稳定）；解析不出返回占位符。
    """
    telemetry = _telemetry_module()
    ts = telemetry._parse_time(text)
    if ts is None:
        return "—"
    return (ts + telemetry._utc_offset()).strftime("%Y-%m-%d %H:%M:%S")


def _telemetry_module():
    """延迟 import `tools.sentry.lib.telemetry_index`。

    它反过来要用 `tools.sentry.lib.evidence`，而 `evidence` 在模块级 import 本模块——
    所以只能在真正用遥测时才拉进来，避免 import 环。
    """
    from tools.sentry.lib import telemetry_index

    return telemetry_index
