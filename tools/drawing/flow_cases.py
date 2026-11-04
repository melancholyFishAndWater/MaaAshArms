#!/usr/bin/env python
"""把一个"证据包"渲染成流程图，产出统一落到 temp/flow/。

证据包指的是"排一次失败所要看的那些东西"：

  * 本机安装根：`<安装根>/debug/`（含 `maafw.log`、`on_error/`、`vision/`）；
  * MFA 手动导出包：`log_20261014_003000.zip`。超过 23.75 MB 会分成
    `…-part01.zip` / `…-part02.zip` 多卷，本工具把它们当**同一棵逻辑目录树**处理，
    所以多卷要一起传给 --case；
  * 遥测工件解出来的目录（Sentry 失败附件只含 `maafw.log` 的 tail 与 `on_error/`
    图片，**没有 `vision/`** —— 这是物理限制，见 tools/sentry/README.md）。

与 `tools/drawing/flow_from_log.py` 的分工：那个文件是**渲染器**（解析 + 布局，唯一的实现处），
本文件只做**编排**（找证据、跑批量、记 manifest、保证幂等），不复制任何渲染逻辑。

用法示例：
  # 本机跑某个 task
  python tools/drawing/flow_cases.py --case D:/MaaAshArms/debug --task-id 200000118

  # 别人给的多卷导出包
  python tools/drawing/flow_cases.py --case log_20261014-part01.zip log_20261014-part02.zip \\
      --task-id 200000118 --from "2026-10-14 00:30:00" --to "2026-10-14 00:32:00"

  # 只列一遍有哪些证据包，不画
  python tools/drawing/flow_cases.py --case D:/MaaAshArms/debug --list-cases
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import logging

# 兄弟模块与本包都在同一目录：先把这里加进 sys.path，`from flow_from_log import ...` 才成立。
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 仓库根 = tools/drawing/ 上溯两级；`tools.sentry` 的 import 靠它。
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._logging import configure_tool_logging

log = logging.getLogger("tools.flow_cases")

from flow_from_log import (
    DEFAULT_OUTDIR,
    RENDERER_VERSION,
    VERSION_SOURCE_CLI,
    VERSION_SOURCE_TELEMETRY,
    FlowOptions,
    FlowResult,
    MediaResolver,
    Note,
    _is_zip,
    parse_time,
    render_flow,
    render_overview,
    scan_tasks,
    stale_summary,
)

MANIFEST_NAME = "manifest.json"
LOG_NAMES = ("maafw.log", "maa.log")
# 轮转后的日志（`maafw.bak.2026.11.02-08.57.51.341.log`）：MFA 每次启动轮转一次，名字里带时间戳。
# **只在没有确切名字时**才认，多份取 mtime 最新的那份——别把 `maafw.log` 本身漏给这个兜底。
ROTATED_LOG_RE = re.compile(r"^(?:maafw|maa)[\w.\-]*\.log$", re.IGNORECASE)
# MFA 分卷命名：`<名字>-partNN.zip`；分组时按去掉这一段的名字归并
PART_RE = re.compile(r"-part\d+$", re.IGNORECASE)
# 遥测 release（Sentry 事件字段）：`MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38`
# 取最后一个 `vX.Y.Z` 段——拼接格式下它永远是本仓库的资源版本，前面的 MFA 版本段忽略。
RELEASE_VERSION_RE = re.compile(r"v[0-9][0-9A-Za-z.\-]*")


def resource_version_from_release(release: str) -> str | None:
    """从遥测 release 字符串里抠出资源版本（`v1.0.0-beta.38`）。

    与 `tools/sentry/report_common.version_label` 同一结论，但这里只依赖正则、不依赖 sentry 包，
    让 `flow_cases` 不必反向 import `tools.sentry`。拿不到返回 None。
    """
    if not release:
        return None
    matches = RELEASE_VERSION_RE.findall(release)
    return matches[-1] if matches else None


def _version_from_evidence(case: Case) -> str | None:
    """遥测证据包（Sentry 拉下来的）里记着那次运行的 release：`evidence.json` 的 `event.release`。

    比从 maafw.log 抠「资源版本」可靠——原生 maafw.log 根本没有那行，同装 MFA 界面日志
    也不会被 `evidence` 下载进来。证据包目录按 issue 短号建（`temp/evidence/<SHORT-ID>/`），
    所以 `debug/` 的上一级就是 evidence.json 所在目录。
    """
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
        rel = ((data.get("event") or {}).get("release")) or ""
        return resource_version_from_release(rel)
    return None


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


def _safe_part(text: str) -> str:
    """文件名里只留字母数字与 `-_`，其余换成 `-`（中文、空格、斜杠都换掉）。"""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)


def base_filename(
    case: Case, options: FlowOptions, *, issue_only: bool = False
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
    if explicit_task:
        parts.append(f"task{options.task_id}")
    elif issue and task and not issue_only:
        # 有 issue 名但没有 task_id：把任务名也带上，图才读得出画的是哪个任务
        parts.append(_safe_part(task))
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
) -> str:
    """产物文件名：`<问题名或任务名>[_task<id>][_<日志时间>][_<短指纹>].png`.

    相比旧版去掉了 `flow_` 前缀（目录已经是 `temp/flow/`，前缀是重复的）。

    同名判定看两处：磁盘上已有同名文件，或 manifest 里已登记过**另一份**输入（日志指纹
    不同或参数不同）。任一冲突就追加日志时间标签；加了标签仍冲突时再追加日志短指纹，
    保证结果确定且不覆盖别人。

    「疑似过时」的后缀不在这里加：那项检查要等渲染完才知道有没有命中，
    所以后缀由 `stale_name()` 在渲染后补上。
    """
    base = base_filename(case, options, issue_only=issue_only)
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


def run_case(
    case: Case,
    options: FlowOptions,
    *,
    outdir: str,
    mermaid: bool,
    force: bool,
    manifest: dict,
    vision_override: Any = None,
    on_error_override: Any = None,
    issue_only: bool = False,
) -> FlowResult | None:
    """渲染一个 case；命中 manifest 且产物还在就跳过（幂等）。

    「疑似过时」只有在渲染完才知道（检查要拿到日志里实际跑过的节点名），所以文件名里的
    `_疑似过时` 是**画完之后**补的：先按普通名字渲染，命中就 `os.replace` 改名，再把
    manifest 记到新名字上。幂等检查因此要同时认普通名与带后缀的名——上一轮画出来的
    是哪一个，这一轮还不知道。
    """
    # 资源版本基线：命令行 --version 优先；否则从遥测证据包 evidence.json 的 release 取
    # （原生 maafw.log 没有「资源版本」那行，同装 MFA 界日志也不会被下载进来）。
    version, version_source = options.version, options.version_source
    if version is None:
        found = _version_from_evidence(case)
        if found:
            version, version_source = found, VERSION_SOURCE_TELEMETRY
    case_options = (
        replace(options, version=version, version_source=version_source)
        if (version != options.version or version_source != options.version_source)
        else options
    )

    name = out_filename(case, case_options, outdir, manifest, issue_only=issue_only)
    vision = vision_override if vision_override is not None else case.vision()
    on_error = on_error_override if on_error_override is not None else case.on_error()
    fingerprint = {
        "log": log_digest(case),
        "options": case_options.summary(),
        "source": case.describe(),
        "has_vision": bool(vision),
        "has_on_error": bool(on_error),
        # 渲染器版本：解析口径/版面改了必须换指纹，否则同一个证据包会一直命中旧记录"跳过"
        "renderer": RENDERER_VERSION,
    }
    if not force:
        for candidate in (name, stale_name(name)):
            recorded = manifest["cases"].get(candidate)
            path = os.path.join(outdir, candidate)
            if (
                recorded
                and recorded.get("fingerprint") == fingerprint
                and os.path.isfile(path)
            ):
                log.info("跳过（已画过）：%s", path)
                return None

    target = os.path.join(outdir, name)
    result = render_flow(
        case.log_source(),
        vision=vision,
        on_error=on_error,
        out=target,
        options=case_options,
        mermaid=mermaid,
    )
    assert isinstance(result, FlowResult)

    # 疑似过时命中：把 _疑似过时 追加到文件名最末尾（扩展名之前），.mmd 跟着改
    stale_hit = result.stale is not None and bool(result.stale.nodes)
    if stale_hit:
        renamed = stale_name(name)
        renamed_path = os.path.join(outdir, renamed)
        os.replace(target, renamed_path)
        if result.mermaid and os.path.isfile(result.mermaid):
            new_mermaid = os.path.splitext(renamed_path)[0] + ".mmd"
            os.replace(result.mermaid, new_mermaid)
            result.mermaid = new_mermaid
        result.path = renamed_path
        name = renamed

    manifest["cases"][name] = {
        "fingerprint": fingerprint,
        "path": result.path,
        "mermaid": result.mermaid,
        "width": result.width,
        "height": result.height,
        "steps": result.steps,
        "boxes": result.boxes,
        "rounds": result.rounds,
        "columns": result.columns,
        "overlay_groups": result.overlay_groups,
        "truncated_start": result.truncated_start,
        "derived_from": result.derived_from,
        "out_of_order": result.out_of_order,
        "notes": [n.text for n in result.notes],
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "stale": _stale_record(result.stale),
    }
    merged = (
        f"（{result.overlay_groups} 段重复已合并 → {result.columns} 列）"
        if result.overlay_groups
        else ""
    )
    print(
        f"步骤 {result.steps}（折叠后 {result.boxes} 个框）/ 轮次 {result.rounds}{merged}"
        f" → {result.path}  ({result.width}×{result.height})"
    )
    # 疑似过时命中：画完图后显式打一行（notes 里也有，但那是 stderr 的注记）
    summary = stale_summary(result.stale)
    if summary:
        log.info(summary)
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    if result.truncated_start:
        log.info(
            "提示：日志第一行晚于 --from，这份日志只含窗口后半段（遥测附件只带 tail）"
        )
    for note in result.notes:
        log.info("提示：%s", note.text)
    if case.bad_packages:
        log.warning("以下卷读不了，结果可能不完整：%s", case.bad_packages)
    return result


def _fmt_local(text: str) -> str:
    """Sentry 的时刻是 UTC（`...Z`），概览卡上显示**本地时间**。

    复用遥测模块的解析/偏移 helper（模块级、稳定）；解析不出返回占位符。
    """
    telemetry = _telemetry_module()
    ts = telemetry._parse_time(text)
    if ts is None:
        return "—"
    return (ts + telemetry._utc_offset()).strftime("%Y-%m-%d %H:%M:%S")


def _evidence_on_error(short_id: str) -> list[str]:
    """这个 issue 的失败现场图：只有 `evidence` 命令把 `temp/evidence/<SHORT>/debug/on_error/`
    下下来过才有；没下过返回空（概览卡就纯文字）。"""

    d = os.path.join(REPO_ROOT, "temp", "evidence", short_id, "debug", "on_error")
    if not os.path.isdir(d):
        return []
    pngs = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
    return [os.path.join(d, f) for f in pngs[:4]]


def _evidence_case(short_id: str) -> Case | None:
    """这个 issue 有本地证据包（`temp/evidence/<SHORT>/debug/` 带日志）时，构造可画的 Case。

    这是批量出图「未命中日志包」时的**完整**来源：证据包里就是那次运行的 `maafw.log` 尾部
    + `on_error/` 现场图，足够画真实流程图——不该退化成只有文字/图片的概览卡。
    """
    root = os.path.join(REPO_ROOT, "temp", "evidence", short_id, "debug")
    if not os.path.isdir(root):
        return None
    try:
        return load_case([root])
    except (FileNotFoundError, ValueError):
        return None


def _fetch_evidence(short_id: str) -> None:
    """调 `evidence` 补下这个 issue 的证据包（联网）；失败只提示，不影响后面画图。"""
    try:
        from tools.sentry import evidence as evidence_mod

        code = evidence_mod.main(["--issue", short_id])
        if code != 0:
            log.warning("补证据 %s 失败（exit %s）", short_id, code)
    except (ImportError, RuntimeError, OSError) as exc:
        log.warning("补证据 %s 失败：%s", short_id, exc)


def overview_filename(
    short_id: str, outdir: str, manifest: dict, fingerprint: dict
) -> str:
    """概览卡文件名：`<SHORT>.png`；被别的指纹占用时追加指纹短哈希。"""

    import hashlib
    import json

    base = f"{short_id}.png"

    def taken(name: str) -> bool:
        path = os.path.join(outdir, name)
        if os.path.isfile(path):
            return name not in manifest["cases"] or manifest["cases"][name].get(
                "fingerprint"
            ) != fingerprint
        return (
            name in manifest["cases"]
            and manifest["cases"][name].get("fingerprint") != fingerprint
        )

    if not taken(base):
        return base
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{short_id}_{digest}.png"


def run_overview(
    card: Any,
    *,
    outdir: str,
    mermaid: bool,
    force: bool,
    manifest: dict,
    no_log_hint: bool = True,
) -> FlowResult | None:
    """画一条遥测 issue 的概览卡（没有可画的完整日志时的降级产物）。

    `no_log_hint=True` 时在卡上补一句"怎么补完整输出"；证据包在但解析不出流程时传 False。
    """

    rel = card.release or ""
    fields = [
        ("问题号", card.short_id),
        ("标题", card.title or "—"),
        ("失败节点", f"{card.node_name}/{card.stage}" if card.node_name else "—"),
        ("任务入口", card.label or "—"),
        ("次数", str(card.count if card.count is not None else "—")),
        ("状态", card.status or "—"),
        ("最后出现", _fmt_local(card.last_seen)),
        ("首次收到", _fmt_local(card.received)),
        ("资源版本", rel or "—"),
        ("链接", card.permalink or "—"),
    ]
    images = _evidence_on_error(card.short_id)
    notes = [Note(text, kind) for text, kind in _telemetry_module().card_notes(card, [])]
    if no_log_hint:
        notes.append(
            Note(
                f"无本地日志：要完整流程图，先跑 "
                f"`python -m tools.sentry.cli evidence {card.short_id}`（需联网）",
                "fail",
            )
        )
    fingerprint = {
        "short_id": card.short_id,
        "last_seen": card.last_seen,
        "release": rel,
        "images": len(images),
        "renderer": RENDERER_VERSION,
    }
    name = overview_filename(card.short_id, outdir, manifest, fingerprint)
    target = os.path.join(outdir, name)
    if not force:
        rec = manifest["cases"].get(name)
        if (
            rec
            and rec.get("fingerprint") == fingerprint
            and os.path.isfile(target)
        ):
            log.info("跳过（已画过）：%s", target)
            return None
    try:
        result = render_overview(
            target,
            title=f"遥测问题 {card.short_id}",
            subtitle=f"{card.count or 0} 次 · {card.status or '?'}",
            fields=fields,
            images=images,
            notes=notes,
            mermaid=mermaid,
        )
    except ModuleNotFoundError as exc:
        log.error("错误：%s: %s", card.short_id, exc)
        return None
    assert isinstance(result, FlowResult)
    manifest["cases"][name] = {
        "fingerprint": fingerprint,
        "path": result.path,
        "mermaid": result.mermaid,
        "width": result.width,
        "height": result.height,
        "steps": result.steps,
        "boxes": result.boxes,
        "rounds": result.rounds,
        "columns": result.columns,
        "overlay_groups": result.overlay_groups,
        "truncated_start": result.truncated_start,
        "derived_from": result.derived_from,
        "out_of_order": result.out_of_order,
        "notes": [n.text for n in result.notes],
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "stale": None,
    }
    print(f"概览 {card.short_id} → {result.path}  ({result.width}×{result.height})")
    return result


def _telemetry_module():
    """延迟 import `tools.sentry.telemetry_index`。

    它反过来要用 `tools.sentry.evidence`，而 `evidence` 在模块级 import 本模块——
    所以只能在真正用遥测时才拉进来，避免 import 环。
    """
    from tools.sentry import telemetry_index

    return telemetry_index


def _with_telemetry(options: FlowOptions, annotation) -> FlowOptions:
    """把遥测结论并进 options：产物名用问题号、注记带上、release 当版本基线。"""
    version = options.version or annotation.version or None
    source = options.version_source or (
        VERSION_SOURCE_TELEMETRY if annotation.version else ""
    )
    return replace(
        options,
        extra_notes=list(annotation.notes) or options.extra_notes,
        issue=annotation.chosen or options.issue,
        version=version,
        version_source=source,
    )


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="把一个证据包（debug 目录 / MFA 导出 zip / 多卷）渲染成流程图，产出落到 temp/flow/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--case",
        nargs="+",
        required=False,
        help="证据包：一个 debug 目录、一个/多个归档（多卷一起给）、或一份日志文件；"
        "配合 --telemetry-bulk 时是可选的（不带就只画遥测概览卡）",
    )
    ap.add_argument(
        "--vision",
        default=None,
        help="截图目录（默认取 `<case>/vision`；传单个日志文件时取日志所在目录的 `vision/`）",
    )
    ap.add_argument(
        "--on-error",
        dest="on_error",
        default=None,
        help="on_error 现场图目录（默认取 `<case>/on_error`，单日志文件同理取日志所在目录）",
    )
    ap.add_argument(
        "--outdir", default=DEFAULT_OUTDIR, help=f"输出目录（默认 {DEFAULT_OUTDIR}）"
    )
    ap.add_argument("--from", dest="t_from", default=None, help="起始时间")
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间")
    ap.add_argument("--task-id", type=int, default=None, help="只看某个 task_id")
    ap.add_argument(
        "--pid",
        default=None,
        help="只看某个进程（Px<进程号>）。一份 maafw.log 里常有两个进程复用同一 task_id，"
        "只给 --task-id 会把两趟混进同一张图，步骤数对不上就是它",
    )
    ap.add_argument("--cut-on", default=None, help="手动指定分轮节点（逗号分隔）")
    ap.add_argument(
        "--max-rounds", type=int, default=40, help="最多画多少轮（默认 40）"
    )
    ap.add_argument(
        "--collapse", type=int, default=3, help="连续同名节点折叠阈值（默认 3）"
    )
    ap.add_argument("--no-thumbs", dest="thumbs", action="store_false", help="不贴截图")
    ap.add_argument(
        "--no-parents",
        dest="parents",
        action="store_false",
        help="框里不写父节点那一行（父节点 = 本轮的派发者，来自 Node.NextList 事件）",
    )
    ap.add_argument(
        "--no-overlay",
        dest="overlay",
        action="store_false",
        help="不合并重复轮次（默认把连续、同形、整轮无截图且无失败框的轮次并成一列）",
    )
    ap.add_argument(
        "--overlay-min",
        type=int,
        default=2,
        help="连续几轮同形才合并（默认 2；0 或 1 等于关闭）",
    )
    ap.add_argument("--mermaid", action="store_true", help="额外输出 .mmd")
    ap.add_argument(
        "--version",
        default=None,
        help="手动指定资源版本（vX.Y.Z）作为「疑似过时」的基线；不传时遥测证据包会自己从 "
        "evidence.json 的 release 取，本地日志从「资源版本：vX.Y.Z」或同装 MFA 日志取",
    )
    ap.add_argument(
        "--telemetry",
        dest="telemetry",
        action="store_true",
        help="关联遥测问题（默认开）：按日志里的失败节点在本地遥测库反查，把问题号/次数/release "
        "写进注记与产物名，并用那个 release 当「疑似过时」基线",
    )
    ap.add_argument(
        "--no-telemetry", dest="telemetry", action="store_false", help="不关联遥测（纯离线）"
    )
    ap.add_argument(
        "--telemetry-offline",
        action="store_true",
        help="只读本地遥测库、不联网刷新（库不存在时就不反查）",
    )
    ap.add_argument(
        "--telemetry-issue", default=None, help="指定问题号（如 PYTHON-2X），不再自动反查"
    )
    ap.add_argument(
        "--telemetry-days", type=int, default=14, help="本地遥测库保留最近 N 天（默认 14）"
    )
    ap.add_argument(
        "--telemetry-gap", type=int, default=3, help="反查的时间窗（天，默认 3）"
    )
    ap.add_argument(
        "--telemetry-keep-resolved",
        action="store_true",
        help="本地遥测库保留已解决/已忽略的 issue（默认清掉）",
    )
    ap.add_argument(
        "--telemetry-only", action="store_true", help="只打印反查到的遥测问题，不画图"
    )
    ap.add_argument("--force", action="store_true", help="忽略 manifest，强制重画")
    ap.add_argument(
        "--telemetry-bulk",
        action="store_true",
        help="批量出图：遍历本地遥测库里所有未解决 issue，每个出一张图；"
        "给了 --case 就用它给命中的 issue 出完整流程（作为补充），"
        "没命中的用该 issue 自己的证据包（temp/evidence/<SHORT>/debug）出完整流程；"
        "连证据包都没有才出概览卡",
    )
    ap.add_argument(
        "--fetch-evidence",
        action="store_true",
        help="批量出图时，对没有本地证据包的 issue 调 `evidence` 补下（需联网）；下了就用真实日志出流程",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）；DEBUG 会多打解析/匹配细节",
    )
    ap.add_argument(
        "--log-file",
        default=None,
        help="日志文件路径（默认按运行时间戳建 temp/logs/flow_cases_<时间戳>_<pid>.log）；"
        "传 none / 空 只走控制台不写文件",
    )
    ap.add_argument(
        "--list-cases", action="store_true", help="只列出发现的证据包，不画"
    )
    ap.add_argument(
        "--all-tasks",
        action="store_true",
        help="按任务逐个输出（一个 (进程, task_id) 一张图）。任务号在各进程内复用，"
        "不加这个参数时整份日志混画一张；加了之后文件名带 _task<id>_px<进程>",
    )
    ap.set_defaults(thumbs=True, telemetry=True)
    return ap


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    _, log_path = configure_tool_logging(
        "flow_cases", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)
    if args.telemetry_only and not args.telemetry:
        log.error(
            "--telemetry-only 要开着遥测（别同时给 --no-telemetry）。"
        )
        return 1
    if not args.case and not args.telemetry_bulk:
        log.error(
            "需要 --case <证据包> 或 --telemetry-bulk（两者都不给画不了）。"
        )
        return 1
    if args.telemetry_only and not args.case:
        log.error("--telemetry-only 需要 --case <日志/证据包> 才能反查。")
        return 1
    if args.telemetry_bulk:
        # 批量出图以遥测库为驱动，遥测必须开（默认就开，这里只是兜底 --no-telemetry）
        args.telemetry = True
    try:
        t_from, t_to = parse_time(args.t_from), parse_time(args.t_to)
    except SystemExit as exc:
        log.error("错误：%s", exc)
        return 1

    options = FlowOptions(
        t_from=t_from,
        t_to=t_to,
        task_id=args.task_id,
        px=args.pid,
        cut_on=args.cut_on,
        max_rounds=args.max_rounds,
        collapse=args.collapse,
        thumbs=args.thumbs,
        parents=args.parents,
        overlay=args.overlay,
        overlay_min=args.overlay_min,
        version=args.version,
        version_source=VERSION_SOURCE_CLI if args.version else "",
    )

    # 遥测（默认开）：先刷新一次本地库，之后每个包都只在本地查。
    # 刷新失败只提示，不影响画图——遥测是加分项，不是画图的前提。
    telemetry = _telemetry_module()
    telemetry_data: dict | None = None
    if args.telemetry:
        if args.telemetry_offline:
            telemetry_data = telemetry.load_index()
        else:
            try:
                telemetry_data, changes = telemetry.refresh_default(
                    days=args.telemetry_days,
                    keep_resolved=args.telemetry_keep_resolved,
                )
                for change in changes:
                    log.info("提示：%s", change)
            except (RuntimeError, OSError, ImportError) as exc:
                log.warning(
                    "遥测库没刷新成（%s），用本地已有数据；若报「unable to open database file」，"
                    "把 SENTRY_CONFIG_DIR 指到含可写 cli.db 的目录",
                    exc,
                )
                telemetry_data = telemetry.load_index()
    cards = telemetry.load_cards(telemetry_data) if telemetry_data else []

    single = args.case[0] if args.case else None
    whole_dir = (
        bool(single)
        and len(args.case) == 1
        and os.path.isdir(single)
        and not _pick_log(single)
    )
    cases: list[Case] = []
    if args.case:
        try:
            cases = (
                discover_cases(single)
                if whole_dir and single is not None
                else [load_case(args.case)]
            )
        except (FileNotFoundError, ValueError) as exc:
            log.error("错误：%s", exc)
            return 1
        if not cases:
            log.error("错误：%s 下没找到带日志的证据包。", single)
            return 1

    if args.list_cases:
        for case in cases:
            print(f"{case.label}\t{case.describe()}")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    manifest = load_manifest(args.outdir)
    drawn = skipped = failed = 0

    # 每个证据包要画的任务。默认一个（`--task-id` 指定，或全部混画一张）；
    # `--all-tasks` / `--telemetry-bulk` 时按 `(进程, task_id)` 逐个输出——task_id 在各进程内复用，
    # 只按编号切会把两次不同运行的任务混进同一张图。批量出图必须按任务切：
    # 命中的 issue 才能各出一张完整流程、文件名用问题号。
    drawn_issues: set[str] = set()
    jobs: list[tuple[Case, FlowOptions]] = []
    for case in cases:
        if args.all_tasks or args.telemetry_bulk:
            try:
                tasks = scan_tasks(case.log_source(), options.t_from, options.t_to)
            except (OSError, TypeError) as exc:
                failed += 1
                log.error("错误：%s: 列任务失败：%s", case.label, exc)
                continue
            if not tasks:
                log.info("提示：%s 里没有可画的任务，跳过", case.label)
                continue
            for task in tasks:
                jobs.append((case, replace(options, task_id=task.task_id, px=task.px)))
        else:
            jobs.append((case, options))

    telemetry_shown = 0
    for case, case_options in jobs:
        if args.telemetry:
            annotation = telemetry.annotate(
                case.log_source(),
                index=telemetry_data,
                offline=args.telemetry_offline,
                issue=args.telemetry_issue,
                task_id=case_options.task_id,
                days=args.telemetry_days,
                gap=args.telemetry_gap,
            )
            for warning in annotation.warnings:
                log.info("提示：%s: %s", case.label, warning)
            if args.telemetry_only:
                print(f"\n== {case.label} ==", file=sys.stdout)
                telemetry.format_matches(annotation.matches, sys.stdout)
                telemetry_shown += 1
                continue
            case_options = _with_telemetry(case_options, annotation)
        try:
            result = run_case(
                case,
                case_options,
                outdir=args.outdir,
                mermaid=args.mermaid,
                force=args.force,
                manifest=manifest,
                vision_override=args.vision,
                on_error_override=args.on_error,
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            failed += 1
            log.error("错误：%s: %s", case.label, exc)
            continue
        drawn += result is not None
        skipped += result is None
        if result is not None and args.telemetry and annotation.chosen:
            # 这个任务对应的问题号已经被日志包出成完整流程了，概览卡就跳过它
            drawn_issues.add(annotation.chosen)
    overview_drawn = 0
    evidence_drawn = 0
    if args.telemetry_bulk:
        if not cards:
            log.warning(
                "本地遥测库是空的，没东西可画。若还没配 Sentry CLI，先把 SENTRY_CONFIG_DIR "
                "指到含可写 cli.db 的目录，再跑 "
                "`python -m tools.sentry.cli telemetry --refresh`（需联网）。"
            )
        for card in cards:
            if card.short_id in drawn_issues:
                continue  # 已经被日志包出成完整流程了
            # 只有 pipeline 节点失败（标题里有 <节点>/<阶段>）才用证据包日志画流程；
            # SocketException 这类非节点失败，日志尾部画不出它，硬画反而是误导，走概览卡。
            ev_case = _evidence_case(card.short_id) if card.node_name else None
            if ev_case is None and args.fetch_evidence and card.node_name:
                _fetch_evidence(card.short_id)
                ev_case = _evidence_case(card.short_id)
            if ev_case is not None:
                ev_options = replace(
                    options,
                    issue=card.short_id,
                    task_id=None,
                    px=None,
                    t_from=None,
                    t_to=None,
                )
                try:
                    res = run_case(
                        ev_case,
                        ev_options,
                        outdir=args.outdir,
                        mermaid=args.mermaid,
                        force=args.force,
                        manifest=manifest,
                        vision_override=None,
                        on_error_override=None,
                        issue_only=True,
                    )
                except (FileNotFoundError, ValueError, OSError) as exc:
                    # 证据包在但解析不出流程（例如日志尾部没有 pipeline 步骤）：
                    # 退概览卡，而不是把这条 issue 整个丢掉
                    log.warning(
                        "证据包画不出流程（%s），退概览卡：%s", card.short_id, exc
                    )
                else:
                    evidence_drawn += res is not None
                    continue
            try:
                res = run_overview(
                    card,
                    outdir=args.outdir,
                    mermaid=args.mermaid,
                    force=args.force,
                    manifest=manifest,
                    no_log_hint=bool(card.node_name) and ev_case is None,
                )
            except (OSError, ValueError) as exc:
                failed += 1
                log.error("错误：%s: %s", card.short_id, exc)
                continue
            overview_drawn += res is not None

    save_manifest(args.outdir, manifest)
    if args.telemetry_only:
        print(f"\n共 {telemetry_shown} 个证据包（未画图）。")
        return 0
    tail = f"，失败 {failed}" if failed else ""
    parts = [f"绘制 {drawn} 个", f"跳过 {skipped} 个"]
    if evidence_drawn:
        parts.append(f"证据包流程 {evidence_drawn} 个")
    if overview_drawn:
        parts.append(f"概览 {overview_drawn} 个")
    print(
        f"\n完成：{('，'.join(parts))}{tail}；"
        f"manifest → {os.path.join(args.outdir, MANIFEST_NAME)}"
    )
    return 1 if failed and not drawn else 0


if __name__ == "__main__":
    raise SystemExit(main())
