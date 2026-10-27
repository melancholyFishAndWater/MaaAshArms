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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 兄弟模块与本包都在同一目录：先把这里加进 sys.path，`from flow_from_log import ...` 才成立。
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 仓库根 = tools/drawing/ 上溯两级；`tools.sentry` 的 import 靠它。
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from flow_from_log import (
    DEFAULT_OUTDIR,
    RENDERER_VERSION,
    FlowOptions,
    FlowResult,
    MediaResolver,
    _is_zip,
    parse_time,
    render_flow,
)

MANIFEST_NAME = "manifest.json"
LOG_NAMES = ("maafw.log", "maa.log")
# MFA 分卷命名：`<名字>-partNN.zip`；分组时按去掉这一段的名字归并
PART_RE = re.compile(r"-part\d+$", re.IGNORECASE)


@dataclass
class Case:
    """一个证据包：日志正文 + 图片证据（可为归档或目录）。"""

    label: str
    log_text: str | None = None       # 归档来源：日志正文
    log_path: str | None = None       # 目录来源：日志文件路径
    packages: list[str] = field(default_factory=list)   # 归档来源：全部卷路径
    root: str | None = None           # 目录来源的根
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
                            entries.append((info.filename, _LazyMember(package, info.filename)))
            except (OSError, zipfile.BadZipFile):
                continue
        return MediaResolver(members=entries) if entries else None

    def vision(self):
        if self.root:
            sub = os.path.join(self.root, "vision")
            return MediaResolver(sub) if os.path.isdir(sub) else None
        return self._zip_subdir("vision")

    def on_error(self):
        if self.root:
            sub = os.path.join(self.root, "on_error")
            return MediaResolver(sub) if os.path.isdir(sub) else None
        return self._zip_subdir("on_error")

    def describe(self) -> str:
        if self.root:
            return self.root
        return f"归档 {len(self.packages)} 卷"


@dataclass
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
    candidates: list[tuple[int, str, str]] = []   # (日志名优先级, package, member)；排序后用最低优先级那条
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
        for name in LOG_NAMES:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                return Case(label=os.path.basename(os.path.normpath(root)) or "case",
                            log_path=cand, root=root)
        raise FileNotFoundError(
            f"{root} 下找不到 {' / '.join(LOG_NAMES)}。给它 debug/ 那一层，"
            "或直接把日志文件路径当 --case 传。"
        )

    if zips:
        log_text, bad = _read_log_from_zips(zips)
        if log_text is None:
            raise FileNotFoundError(
                f"{len(zips)} 个归档里都没有 {' / '.join(LOG_NAMES)}（读不了的卷：{bad or '无'}）。"
            )
        label = PART_RE.sub("", os.path.splitext(os.path.basename(sorted(zips)[0]))[0])
        return Case(label=label or "archive", log_text=log_text,
                    packages=sorted(zips), bad_packages=bad)

    if len(rest) == 1 and os.path.isfile(rest[0]):
        only = rest[0]
        return Case(label=os.path.splitext(os.path.basename(only))[0], log_path=only)

    missing = [p for p in items if not os.path.exists(p)]
    raise FileNotFoundError(f"--case 指向的路径既不是目录也不是归档：{missing or items}")


def discover_cases(root: str) -> list[Case]:
    """列出 root 下的候选证据包（只读，不画图、不写盘）。

    多卷导出包的每一卷都会被识别成独立候选，这里**不自动归并**——归并需要知道
    用户想画哪一段，猜错会画出一张看着合理但缺卷的图。多卷请显式一起传 --case。
    """
    found: list[Case] = []
    if not os.path.isdir(root):
        return found
    if any(os.path.isfile(os.path.join(root, n)) for n in LOG_NAMES):
        found.append(load_case([root]))
        return found
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub):
            if any(os.path.isfile(os.path.join(sub, n)) for n in LOG_NAMES):
                found.append(load_case([sub]))
        elif os.path.isfile(sub) and _is_zip(sub):
            try:
                found.append(load_case([sub]))
            except (FileNotFoundError, ValueError) as exc:
                print(f"跳过 {sub}：{exc}", file=sys.stderr)
    return found


def log_digest(case: Case) -> str:
    """日志内容的短指纹：manifest 用它判断"是不是同一份输入"。"""
    if case.log_text is not None:
        data = case.log_text.encode("utf-8", errors="replace")
        return hashlib.sha256(data).hexdigest()[:16]
    if case.log_path is None:
        # load_case 保证二者必有一个；这里显式报错，避免 `open(None)` 那种看不出原因的崩溃。
        raise ValueError(f"证据包 {case.label} 既没有日志正文也没有日志路径，无法计算指纹。")
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
TASK_ENTRY_RE = re.compile(r'\[msg=Tasker\.Task\.\w+\] \[details(?:_json)?=\{"entry":"([^"\\]+)"')


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
            r'\[task_id_=' + tid + r"\]"
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


def base_filename(case: Case, options: FlowOptions) -> str:
    """不含去重后缀的文件名主干：优先遥测问题名，其次任务名，最后证据包目录名.

    带 `--task-id` 时追加 `_task<id>`：一个 issue 里往往有多个任务失败（实测
    `PYTHON-1Z` 有 6 个），不追加会让它们互相覆盖到同一个文件名上.

    指定了 `--task-id` 时**不把任务名放进主干**：那个 task 的入口行不一定在导出的那一段里
    （实测 200000007 只留下 `ReturnMain`，它不是任务入口），而 `task<id>` 本身就唯一、
    也一定对——宁可少一个名字，不放一个可能错的名字.
    """
    issue = issue_name_of(case)
    task = task_name_of(case, options)
    explicit_task = options.task_id is not None
    stem = issue or (None if explicit_task else task) or task or case.label or "case"
    parts = [_safe_part(stem)]
    if explicit_task:
        parts.append(f"task{options.task_id}")
    elif issue and task:
        # 有 issue 名但没有 task_id：把任务名也带上，图才读得出画的是哪个任务
        parts.append(_safe_part(task))
    return "_".join(p for p in parts if p)


def out_filename(case: Case, options: FlowOptions, outdir: str,
                 manifest: dict | None = None, stale: "StaleCheck | None" = None) -> str:
    """产物文件名：`<问题名或任务名>[_task<id>][_<日志时间>][_stale].png.

    相比旧版去掉了 `flow_` 前缀；在节点有疑似过时标记时，文件名末尾追加 `_stale`。
    同名判定看两处：磁盘上已有同名文件，或 manifest 里已登记过**另一份**输入（日志指纹
    不同或参数不同）。任一冲突就追加日志时间标签；加了标签仍冲突时再追加日志短指纹，
    保证结果确定且不覆盖别人。
    """
    base = base_filename(case, options)
    digest = log_digest(case)
    recorded = (manifest or {}).get("cases", {})

    def taken(name: str) -> bool:
        path = os.path.join(outdir, name)
        if not os.path.isfile(path):
            # 文件不在，但 manifest 里记着另一份输入用过这个名字：也算被占（产物被清掉过）
            return name in recorded and recorded[name].get("fingerprint", {}).get("log") != digest
        if name not in recorded:
            return True                      # 磁盘上有同名文件、但不是本工具画的
        return recorded[name].get("fingerprint", {}).get("log") != digest

    # 先根据是否有 stale 节点确定文件名干串
    if stale is not None and stale.nodes:
        # 有疑似过时节点：在不冲突的情况下于基础文件名末尾追加 "_stale"
        base_stale = f"{base}_stale"
        name = f"{base_stale}.png"
        if not taken(name):
            return name
        # 冲突了再走时间/指纹补全（保留 _stale 后缀）
        stamp = log_stamp(case, options)
        if stamp:
            name = f"{base_stale}_{stamp}.png"
            if not taken(name):
                return name
        return f"{base_stale}_{digest}.png"
    else:
        # 无 stale：去掉 flow_ 前缀，直接用 base
        name = f"{base}.png"
        if not taken(name):
            return name
        stamp = log_stamp(case, options)
        if stamp:
            name = f"{base}_{stamp}.png"
            if not taken(name):
                return name
        return f"{base}_{digest}.png"


def load_manifest(outdir: str) -> dict:
    path = os.path.join(outdir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {"schema": 1, "cases": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"提示：{path} 读不了（{exc}），本次当作空 manifest。", file=sys.stderr)
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


def run_case(case: Case, options: FlowOptions, *, outdir: str, mermaid: bool,
             force: bool, manifest: dict) -> FlowResult | None:
    """渲染一个 case；命中 manifest 且产物还在就跳过（幂等）。"""
    # 从 manifest 获取本次渲染的 stale 判断结果（由前次 run_case 存入）
    stale_manifest = manifest["cases"].get(name) if "cases" in manifest else None
    stale = stale_manifest.get("stale") if stale_manifest else None
    name = out_filename(case, options, outdir, manifest, stale)
    target = os.path.join(outdir, name)
    vision, on_error = case.vision(), case.on_error()
    fingerprint = {
        "log": log_digest(case),
        "options": options.summary(),
        "source": case.describe(),
        "has_vision": bool(vision),
        "has_on_error": bool(on_error),
        # 渲染器版本：解析口径/版面改了必须换指纹，否则同一个证据包会一直命中旧记录"跳过"
        "renderer": RENDERER_VERSION,
    }
    recorded = manifest["cases"].get(name)
    if not force and recorded and recorded.get("fingerprint") == fingerprint and os.path.isfile(target):
        print(f"跳过（已画过）: {target}")
        return None

    result = render_flow(
        case.log_source(),
        vision=vision,
        on_error=on_error,
        out=target,
        options=options,
        mermaid=mermaid,
    )
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
        "notes": list(result.notes),
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "stale": result.stale,
    }
    merged = (f"（{result.overlay_groups} 段重复已合并 → {result.columns} 列）"
              if result.overlay_groups else "")
    print(f"步骤 {result.steps}（折叠后 {result.boxes} 个框）/ 轮次 {result.rounds}{merged}"
          f" → {result.path}  ({result.width}×{result.height})")
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    if result.truncated_start:
        print("提示：日志第一行晚于 --from，这份日志只含窗口后半段（遥测附件只带 tail）",
              file=sys.stderr)
    for note in result.notes:
        print(f"提示：{note}", file=sys.stderr)
    if case.bad_packages:
        print(f"警告：以下卷读不了，结果可能不完整：{case.bad_packages}", file=sys.stderr)
    return result


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="把一个证据包（debug 目录 / MFA 导出 zip / 多卷）渲染成流程图，产出落到 temp/flow/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--case", nargs="+", required=True,
                    help="证据包：一个 debug 目录、一个/多个归档（多卷一起给）、或一份日志文件")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help=f"输出目录（默认 {DEFAULT_OUTDIR}）")
    ap.add_argument("--from", dest="t_from", default=None, help="起始时间")
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间")
    ap.add_argument("--task-id", type=int, default=None, help="只看某个 task_id")
    ap.add_argument("--cut-on", default=None, help="手动指定分轮节点（逗号分隔）")
    ap.add_argument("--max-rounds", type=int, default=40, help="最多画多少轮（默认 40）")
    ap.add_argument("--collapse", type=int, default=3, help="连续同名节点折叠阈值（默认 3）")
    ap.add_argument("--no-thumbs", dest="thumbs", action="store_false", help="不贴截图")
    ap.add_argument("--no-parents", dest="parents", action="store_false",
                    help="框里不写父节点那一行（父节点 = 本轮的派发者，来自 Node.NextList 事件）")
    ap.add_argument("--no-overlay", dest="overlay", action="store_false",
                    help="不合并重复轮次（默认把连续、同形、整轮无截图且无失败框的轮次并成一列）")
    ap.add_argument("--overlay-min", type=int, default=2,
                    help="连续几轮同形才合并（默认 2；0 或 1 等于关闭）")
    ap.add_argument("--mermaid", action="store_true", help="额外输出 .mmd")
    ap.add_argument("--force", action="store_true", help="忽略 manifest，强制重画")
    ap.add_argument("--list-cases", action="store_true", help="只列出发现的证据包，不画")
    ap.set_defaults(thumbs=True)
    return ap


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    try:
        t_from, t_to = parse_time(args.t_from), parse_time(args.t_to)
    except SystemExit as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    options = FlowOptions(
        t_from=t_from, t_to=t_to, task_id=args.task_id, cut_on=args.cut_on,
        max_rounds=args.max_rounds, collapse=args.collapse, thumbs=args.thumbs,
        parents=args.parents, overlay=args.overlay, overlay_min=args.overlay_min,
    )

    single = args.case[0]
    whole_dir = len(args.case) == 1 and os.path.isdir(single) and not any(
        os.path.isfile(os.path.join(single, n)) for n in LOG_NAMES)
    try:
        cases = discover_cases(single) if whole_dir else [load_case(args.case)]
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if not cases:
        print(f"错误：{single} 下没找到带日志的证据包。", file=sys.stderr)
        return 1

    if args.list_cases:
        for case in cases:
            print(f"{case.label}\t{case.describe()}")
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    manifest = load_manifest(args.outdir)
    drawn = skipped = failed = 0
    for case in cases:
        try:
            result = run_case(case, options, outdir=args.outdir, mermaid=args.mermaid,
                              force=args.force, manifest=manifest)
        except (FileNotFoundError, ValueError, OSError) as exc:
            failed += 1
            print(f"错误：{case.label}: {exc}", file=sys.stderr)
            continue
        drawn += result is not None
        skipped += result is None
    save_manifest(args.outdir, manifest)
    tail = f"，失败 {failed}" if failed else ""
    print(f"\n完成：绘制 {drawn} 个，跳过 {skipped} 个{tail}；"
          f"manifest → {os.path.join(args.outdir, MANIFEST_NAME)}")
    return 1 if failed and not drawn else 0


if __name__ == "__main__":
    raise SystemExit(main())