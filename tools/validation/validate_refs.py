#!/usr/bin/env python3
"""资源一致性校验：跨文件的名字与约定。

分工：
  * 字段类型 / schema          -> tools/validation/validate_schema.py
  * 引用图的通用错误            -> npx @nekosu/maa-tools check
  * 跨文件的名字与命名/结构约定 -> 本脚本

五个可选域（都不给 = 全开，路径由 interface.json 推导）：
  --interface   interface.json：entry / option / group / preset / pipeline_override 接线
  --pipeline    节点引用完整性、自定义名与 agent 注册名、静态可达性陷阱、
                命名规范（docs/zh_cn/develop/node-naming.md）与任务链结构（pipeline-structure.md）约定
  --tasks       tasks/ 片段：option 形状、case.option、pipeline_override 目标、preset 任务名
  --agent       agent/*.py：模块是否被 main.py 导入、注册名唯一性与后缀
  --image       image/：模板引用存在性（含 agent 里的模板字面量）、未引用图片、非 png

只读，不修改任何文件。纯标准库：本机装不上 jsonschema（.agents/project.md），
所以这里不 import tools/validation/validate_schema.py。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

# --------------------------------------------------------------------------
# JSONC
# --------------------------------------------------------------------------


def strip_jsonc_comments(text: str) -> str:
    """移除 JSONC 注释，保持结构（与 tools/validation/validate_schema.py:23-70 同源）。"""
    result: list[str] = []
    state = 0  # 0=正常, 1=字符串中, 2=转义
    i = 0
    while i < len(text):
        char = text[i]
        if state == 0:
            if char == '"':
                result.append(char)
                state = 1
                i += 1
            elif text[i : i + 2] == "//":
                while i < len(text) and text[i] != "\n":
                    i += 1
                if i < len(text):
                    result.append("\n")
                    i += 1
            elif text[i : i + 2] == "/*":
                i += 2
                while i + 1 < len(text) and text[i : i + 2] != "*/":
                    if text[i] == "\n":
                        result.append("\n")
                    i += 1
                i += 2
            else:
                result.append(char)
                i += 1
        elif state == 1:
            result.append(char)
            if char == "\\":
                state = 2
            elif char == '"':
                state = 0
            i += 1
        else:
            result.append(char)
            state = 1
            i += 1
    return "".join(result)


def load_jsonc(path: Path):
    return json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# deps/tools/pipeline.schema.json 的 $defs.FocusTemplateMap（:334-397）
FOCUS_KEYS = frozenset(
    {
        "Node.PipelineNode.Starting",
        "Node.PipelineNode.Succeeded",
        "Node.PipelineNode.Failed",
        "Node.RecognitionNode.Starting",
        "Node.RecognitionNode.Succeeded",
        "Node.RecognitionNode.Failed",
        "Node.ActionNode.Starting",
        "Node.ActionNode.Succeeded",
        "Node.ActionNode.Failed",
        "Node.NextList.Starting",
        "Node.NextList.Succeeded",
        "Node.NextList.Failed",
        "Node.Recognition.Starting",
        "Node.Recognition.Succeeded",
        "Node.Recognition.Failed",
        "Node.Action.Starting",
        "Node.Action.Succeeded",
        "Node.Action.Failed",
        "Node.WaitFreezes.Starting",
        "Node.WaitFreezes.Succeeded",
        "Node.WaitFreezes.Failed",
    }
)

# node-naming.md §4 动词表
VERBS = (
    "TouchDown",
    "TouchUp",
    "Choose",
    "Click",
    "Enter",
    "Start",
    "Close",
    "Move",
    "Stop",
    "Init",
    "Get",
    "Enable",
)
# node-naming.md §2 里已成类的后缀，不按动词表判
CLASS_SUFFIXES = ("DailyUpdate", "Success", "Failed", "Init", "End", "Loop")
# node-naming.md §3：旧写法（并入 Probe + 状态词）
LEGACY_PREFIXES = ("Is", "Has", "In", "At", "Can", "Able", "Unable")

# 同义写法（§6 要求同一个按钮在各任务里叫法一致）：只在真的见过有人写成这样时才列进来，
# 不做"猜同义词"。`GiftClickSortConfirm` 这类宾语本身就是"排序确认"的不算。
CLICK_SYNONYMS = ("ClickYes", "ClickNo", "ClickSure")

# --------------------------------------------------------------------------
# 规范版本
# --------------------------------------------------------------------------

# 两份规范各自的版本：脚本按哪一版实现。文档头部写着「版本号: vX.Y」；改了收录的规则就同步 +0.1。
# 不一致时报 E2，提醒人复核新规则要不要收进脚本——版本号只印在输出里而没人对，等于没有。
# 两份都收：结构类判据（A4/A5/A7/A8/A9/A14…）按 pipeline-structure.md 实现，只对命名规范算版本
# 等于让结构文档改了规则没人提醒。
NAMING_SPEC_VERSION = "v1.7"
NAMING_SPEC = Path("docs/zh_cn/develop/node-naming.md")
STRUCT_SPEC_VERSION = "v1.5"
STRUCT_SPEC = Path("docs/zh_cn/develop/pipeline-structure.md")


@dataclass(frozen=True)
class Spec:
    """一份"有版本号"的规范：文档标题里的名字、文件、脚本实现的版本、对应的常量名。"""

    label: str    # 文档标题，也是头部注解里对它的称呼（「节点命名与分类」/「任务链结构」）
    path: Path
    version: str  # 脚本按哪一版实现；改收录的规则就 +0.1
    const: str    # 常量名，写进 E2 的提示里（改哪一处）

    def older_than(self, other: str) -> bool:
        return version_tuple(self.version) < version_tuple(other)


SPEC_NAMING = Spec("节点命名与分类", NAMING_SPEC, NAMING_SPEC_VERSION, "NAMING_SPEC_VERSION")
SPEC_STRUCTURE = Spec("任务链结构", STRUCT_SPEC, STRUCT_SPEC_VERSION, "STRUCT_SPEC_VERSION")
SPECS: tuple[Spec, ...] = (SPEC_NAMING, SPEC_STRUCTURE)
# 头部注解里对同一份规范可能出现的几种说法（同一份文档的历史写法都认，否则老文件永远报不出来）。
# 「命名规范版本」是 node-naming.md 改名前（naming.md）的写法，「命名规范」是写法模板里的简写。
ANNOTATION_SPECS: dict[str, Spec] = {
    "命名规范版本": SPEC_NAMING,
    "命名规范": SPEC_NAMING,
    "节点命名与分类": SPEC_NAMING,
    "任务链结构": SPEC_STRUCTURE,
}
# 编辑器里在用的写法模板：新节点的字段骨架从它来（`$comment` 上写着对应规范版本）
PIPELINE_SNIPPETS = Path("tools/vscode/pipeline.snippets.schema.json")
# 文件头部注释的写法：「使用<规范名> vX.Y」，后面可以跟「校验年月日 YY/MM/DD」。
# 实测三种写法并存：`使用命名规范版本 v1.2 校验年月日 26/10/13`（旧）、
# `使用节点命名与分类 v1.5 校验年月日 26/10/16` + `使用任务链结构 v1.3 …`（新，一行一份）、
# `使用命名规范 v1.6; 使用任务链结构 v1.4`（写法模板的 `$comment`，没有校验日期）。
# 前两种都带 `校验年月日`，第三种不带——所以日期那一组是可选的。
ANNOTATION_RE = re.compile(
    r"使用(命名规范版本|命名规范|节点命名与分类|任务链结构)\s*v(\d+\.\d+)"
    r"(?:\s*校验年月日\s*(\d{2}/\d{2}/\d{2}))?"
)
DOC_VERSION_RE = re.compile(r"版本号\s*[:：]\s*v(\d+\.\d+)")
ANY_VERSION_RE = re.compile(r"v(\d+\.\d+)")
# Ensure 的骨架候选，不算"业务 Loop 混进 next"（node-naming.md §3 的节点内部顺序里有它）
ENSURE_SKELETON_REFS = frozenset({"LoopErrorByTimes"})


def version_tuple(text: str) -> tuple[int, int]:
    """'v1.3' → (1, 3)，用来比大小。"""
    major, _, minor = text.lstrip("v").partition(".")
    return int(major), int(minor)


def version_finding(
    found: str | None,
    current: str = NAMING_SPEC_VERSION,
    spec: Spec = SPEC_NAMING,
) -> tuple[str, str] | None:
    """比对"文档/模板写的版本"与"脚本实现的版本"，返回 `(finding_id, 说明)` 或 None。

    两处都要说清是**哪一份**规范（节点命名与分类 / 任务链结构）：两份都有版本号，
    只说"文档已到 vX.Y"会让人对着另一份找。`current` 与 `spec` 分开是为了自检能造任意版本，
    正常调用用 `spec.version`。

    抽成纯函数是为了自检能试它——自检不写文件（受限沙箱下系统临时目录不可写）。
    """
    if found is None:
        return ("E1-spec-missing", f"找不到{spec.label}文档的「版本号: vX.Y」")
    if f"v{found}" == current:
        return None
    if version_tuple(f"v{found}") > version_tuple(current):
        return (
            "E2-spec-version",
            f"{spec.label}文档已到 v{found}，脚本按 {current} 实现："
            f"复核新规则要不要收进脚本，收完把 {spec.const} 一起改",
        )
    return (
        "E2-spec-version",
        f"{spec.label}文档是 v{found}，脚本按 {current} 实现："
        f"两边要一致，改文档版本号或改 {spec.const}",
    )


# 由 agent 在运行时注入的节点名（override_pipeline / override_next 里出现、
# pipeline 文件里没有定义的）。目前为空：本仓库注入的名字都已在 pipeline 里定义。
DYNAMIC_NODES: frozenset[str] = frozenset()

ENSURE_RE = re.compile(r"Ensure[A-Z]")
IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
BOUNDED_QUANTIFIER_RE = re.compile(r"\{\d+(?:,\d+)?\}")

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Finding:
    id: str
    severity: str
    file: str
    line: int | None
    message: str


@dataclass
class Node:
    name: str
    data: dict
    file: Path


@dataclass
class Reg:
    name: str
    kind: str  # recognition / action
    cls: str
    file: Path
    line: int


@dataclass
class Option:
    name: str
    data: dict
    file: Path


@dataclass
class Ctx:
    repo_root: Path
    interface_file: Path | None = None
    interface: dict | None = None
    interface_dir: Path | None = None
    nodes: dict[str, Node] = field(default_factory=dict)
    node_dups: dict[str, list[Path]] = field(default_factory=dict)
    regs: dict[str, Reg] = field(default_factory=dict)
    reg_dups: dict[str, list[Reg]] = field(default_factory=dict)
    options: dict[str, Option] = field(default_factory=dict)
    option_dups: dict[str, list[Path]] = field(default_factory=dict)
    presets: list[tuple[Path, dict]] = field(default_factory=list)
    import_files: list[Path] = field(default_factory=list)
    pipeline_files: list[Path] = field(default_factory=list)
    task_files: list[Path] = field(default_factory=list)
    agent_files: list[Path] = field(default_factory=list)
    image_roots: list[Path] = field(default_factory=list)
    explicit: dict[str, list[Path]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(
        self, id_: str, severity: str, file: Path | str, line: int | None, message: str
    ) -> None:
        self.findings.append(Finding(id_, severity, str(file), line, message))

    # ---- 派生 ----
    @property
    def generic_nodes(self) -> set[str]:
        """定义在 base.json 里的通用节点（不带任务前缀）。"""
        return {n for n, i in self.nodes.items() if i.file.name == "base.json"}


# --------------------------------------------------------------------------
# 行号定位
# --------------------------------------------------------------------------

_LINE_CACHE: dict[Path, list[str]] = {}


def lines_of(path: Path) -> list[str]:
    if path not in _LINE_CACHE:
        try:
            _LINE_CACHE[path] = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            _LINE_CACHE[path] = []
    return _LINE_CACHE[path]


def find_line(path: Path, needle: str) -> int | None:
    """找 `"needle":` 所在行（退回找 `"needle"` 字面量）。"""
    lines = lines_of(path)
    pat = re.compile(r'"' + re.escape(needle) + r'"\s*:')
    for i, line in enumerate(lines, 1):
        if pat.search(line):
            return i
    for i, line in enumerate(lines, 1):
        if f'"{needle}"' in line:
            return i
    return None


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def to_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def walk_key(data, key: str, path: str = ""):
    """递归产出 (路径, 值)：所有名为 key 的字段。"""
    if isinstance(data, dict):
        for k, v in data.items():
            p = f"{path}.{k}" if path else str(k)
            if k == key:
                yield p, v
            else:
                yield from walk_key(v, key, p)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from walk_key(v, key, f"{path}[{i}]")


_TAG_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")


def parse_ref(item) -> tuple[str | None, bool, bool]:
    """把 next/on_error 的一项解析成 (节点名, 是否 [JumpBack], 是否 enabled)。

    支持字符串（可带 `[JumpBack]` 前缀）与 NodeAttr 对象（`name` / `jump_back` / `enabled`）。
    内联定义（对象里没有 name）返回 (None, False, True)。
    """
    if isinstance(item, str):
        jump = item.lstrip().startswith("[JumpBack]")
        return _TAG_RE.sub("", item).strip(), jump, True
    if isinstance(item, dict):
        raw = item.get("name")
        enabled = item.get("enabled", True) is not False
        jump = bool(item.get("jump_back"))
        if isinstance(raw, str):
            jump = jump or raw.lstrip().startswith("[JumpBack]")
            return _TAG_RE.sub("", raw).strip(), jump, enabled
        return None, jump, enabled
    return None, False, True


def ref_lists(node: dict):
    for key in ("next", "on_error"):
        if key in node:
            yield key, to_list(node[key])


def always_hit(node: dict) -> bool:
    """必然命中：没有 recognition（默认 DirectHit）或显式 DirectHit。"""
    rec = node.get("recognition")
    if rec is None:
        return True
    if isinstance(rec, str):
        return rec == "DirectHit"
    if isinstance(rec, dict):
        return rec.get("type") == "DirectHit"
    return False


# 内置输入动作：Click / Swipe / TouchDown … 这类节点当 [JumpBack] 候选是
# "做一下再回来重扫" 的惯用写法（滚动找目标、点掉弹窗后重试），不按自旋判。
BUILTIN_ACTIONS = frozenset(
    {
        "Click",
        "LongPress",
        "Swipe",
        "MultiSwipe",
        "TouchDown",
        "TouchMove",
        "TouchUp",
        "ClickKey",
        "LongPressKey",
        "KeyDown",
        "KeyUp",
        "InputText",
        "StartApp",
        "StopApp",
        "StopTask",
        "Command",
    }
)


def hits_always(target: Node | None) -> TypeGuard[Node]:
    """候选目标的识别是否必然命中（并考虑 enabled: false 与内置输入动作）。

    返回 `TypeGuard[Node]`：True 的时候 `target` 一定不是 None，调用方在
    `if not hits_always(x): continue` 之后可以直接用 `x.data` / `x.file`。
    """
    if target is None:
        return False
    if target.data.get("enabled") is False:
        return False
    if not always_hit(target.data):
        return False
    action = target.data.get("action")
    if isinstance(action, str) and action in BUILTIN_ACTIONS:
        # 内置输入动作：自旋与否取决于动作有没有效果，静态判不了
        return False
    return True


def complement_of(node: dict) -> str | None:
    """`And all_of [X] + inverse` 这种严格取反节点：返回被取反的节点名 X。"""
    if node.get("recognition") != "And" or node.get("inverse") is not True:
        return None
    all_of = node.get("all_of")
    if isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], str):
        return _TAG_RE.sub("", all_of[0]).strip()
    return None


def guard_probe(node: dict) -> str | None:
    """`And all_of [X]` 这种单探针守卫：返回 X（`inverse` 与否都算）。

    用来判"两个守卫是不是同一个判据"：工作循环的守卫与它末项兜底 `Ensure` 的守卫
    取同一个探针时，兜底等于把"守卫不成立"修成"守卫成立"（结构文档 §3）。
    """
    if node.get("recognition") != "And":
        return None
    all_of = node.get("all_of")
    if isinstance(all_of, list) and len(all_of) == 1 and isinstance(all_of[0], str):
        return _TAG_RE.sub("", all_of[0]).strip()
    return None


def iter_custom_refs(data):
    """递归产出 (字段名, 注册名)：custom_recognition / custom_action。"""
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("custom_recognition", "custom_action") and isinstance(v, str):
                yield k, v
            else:
                yield from iter_custom_refs(v)
    elif isinstance(data, list):
        for v in data:
            yield from iter_custom_refs(v)


def leading_word(name: str) -> str:
    """名字的第一个 CamelCase 词（`BattleEnsureHome` -> `Battle`、`TaskBattleAuto` -> `Task`）。"""
    m = re.match(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])", name)
    return m.group(0) if m else name


def word_suffixes(name: str) -> list[str]:
    """名字在所有 CamelCase 边界处的后缀（含自身）。

    `BattleClickReplay` -> ["BattleClickReplay", "ClickReplay", "Replay"]；
    用来表达"`<前缀…><动词><宾语>`"，不要求前缀只有一个词（`StartUpClickIcon` 也成立）。
    """
    out: list[str] = []
    for i, ch in enumerate(name):
        if i == 0 or not ch.isupper():
            continue
        prev_upper = name[i - 1].isupper()
        next_lower = i + 1 < len(name) and name[i + 1].islower()
        if not prev_upper or next_lower:
            out.append(name[i:])
    return [name, *out]


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------


def iter_json_files(dirs: list[Path]) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        if d.is_file():
            out.append(d)
            continue
        for pat in ("*.json", "*.jsonc"):
            out.extend(sorted(d.rglob(pat)))
    # 去重并保序
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def load_pipeline(ctx: Ctx, paths: list[Path]) -> None:
    for path in paths:
        try:
            data = load_jsonc(path)
        except Exception as e:  # noqa: BLE001
            ctx.add("A0-parse-error", "error", path, None, f"JSONC 解析失败：{e}")
            continue
        if not isinstance(data, dict):
            ctx.add(
                "A0-parse-error", "error", path, None, "pipeline 文件的顶层必须是对象"
            )
            continue
        ctx.pipeline_files.append(path)
        for name, node in data.items():
            if not isinstance(node, dict):
                continue
            if name in ctx.nodes:
                ctx.node_dups.setdefault(name, [ctx.nodes[name].file]).append(path)
                continue
            ctx.nodes[name] = Node(name, node, path)
    for name, files in ctx.node_dups.items():
        ctx.add(
            "A0-duplicate-node",
            "error",
            files[-1],
            find_line(files[-1], name),
            f"节点 {name} 在多个文件里定义：{', '.join(str(f) for f in files)}",
        )


def load_tasks(ctx: Ctx, paths: list[Path]) -> None:
    for path in paths:
        try:
            data = load_jsonc(path)
        except Exception as e:  # noqa: BLE001
            ctx.add("B0-parse-error", "error", path, None, f"JSONC 解析失败：{e}")
            continue
        if not isinstance(data, dict):
            continue
        ctx.task_files.append(path)
        for name, opt in (data.get("option") or {}).items():
            if not isinstance(opt, dict):
                continue
            if name in ctx.options:
                ctx.option_dups.setdefault(name, [ctx.options[name].file]).append(path)
                continue
            ctx.options[name] = Option(name, opt, path)
        for preset in to_list(data.get("preset")):
            if isinstance(preset, dict):
                ctx.presets.append((path, preset))
    for name, files in ctx.option_dups.items():
        ctx.add(
            "B0-duplicate-option",
            "error",
            files[-1],
            find_line(files[-1], name),
            f"option {name} 在多个片段里定义：{', '.join(str(f) for f in files)}",
        )


def load_agents(ctx: Ctx, files: list[Path]) -> None:
    reg_pat = re.compile(
        r"@AgentServer\.custom_(recognition|action)\(\s*[\"']([^\"']+)[\"']"
    )
    cls_pat = re.compile(r"^\s*class\s+(\w+)")
    # 注意：不能 append 到 ctx.agent_files —— 传进来的往往就是它，会边遍历边长。
    ctx.agent_files = list(files)
    for path in ctx.agent_files:
        lines = lines_of(path)
        for i, line in enumerate(lines):
            code = line.split("#", 1)[
                0
            ]  # 注释掉的注册（如 train.py 的 TrainAcceptReco）不算
            m = reg_pat.search(code)
            if not m:
                continue
            kind = "recognition" if m.group(1) == "recognition" else "action"
            name = m.group(2)
            cls = ""
            for j in range(i + 1, min(i + 6, len(lines))):
                cm = cls_pat.match(lines[j])
                if cm:
                    cls = cm.group(1)
                    break
            reg = Reg(name, kind, cls, path, i + 1)
            if name in ctx.regs:
                ctx.reg_dups.setdefault(name, [ctx.regs[name]]).append(reg)
            else:
                ctx.regs[name] = reg
    for name, regs in ctx.reg_dups.items():
        where = ", ".join(f"{r.file}:{r.line}" for r in regs)
        ctx.add(
            "D2-duplicate-registration",
            "error",
            regs[-1].file,
            regs[-1].line,
            f"注册名 {name} 重复：{where}",
        )


def list_images(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_dir():
            out.extend(sorted(p for p in root.rglob("*") if p.is_file()))
    return out


# --------------------------------------------------------------------------
# A. pipeline
# --------------------------------------------------------------------------


def check_a1_dangling(ctx: Ctx) -> None:
    known = set(ctx.nodes) | set(DYNAMIC_NODES)
    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            for item in items:
                name, _jump, _enabled = parse_ref(item)
                if not name:
                    continue
                if name not in known:
                    ctx.add(
                        "A1-dangling-node",
                        "error",
                        node.file,
                        find_line(node.file, node.name),
                        f"{node.name}.{key} 引用了未定义节点 {name!r}",
                    )


def check_a2_custom(ctx: Ctx) -> None:
    used: set[str] = set()
    for node in ctx.nodes.values():
        for key, name in iter_custom_refs(node.data):
            used.add(name)
            reg = ctx.regs.get(name)
            if reg is None:
                ctx.add(
                    "A2-unknown-custom",
                    "error",
                    node.file,
                    find_line(node.file, node.name),
                    f"{node.name}.{key} = {name!r}，agent 侧没有这个注册名（静默不命中）",
                )
                continue
            want = "recognition" if key == "custom_recognition" else "action"
            if reg.kind != want:
                ctx.add(
                    "A2-unknown-custom",
                    "error",
                    node.file,
                    find_line(node.file, node.name),
                    f"{node.name}.{key} = {name!r}，但它注册的是 {reg.kind}（{reg.file}:{reg.line}）",
                )
    if ctx.regs:
        for name, reg in ctx.regs.items():
            if name not in used:
                ctx.add(
                    "A2-unused-registration",
                    "warn",
                    reg.file,
                    reg.line,
                    f"注册名 {name!r} 没有被任何 pipeline 节点引用",
                )


def check_a3_custom_mapping(ctx: Ctx) -> None:
    """node-naming.md §7：注册名 = 节点名 + Reco/Act。

    只查节点自身的 custom_* 字段（内联子识别是共用识别，不按父节点名判）；
    只报"只被一个节点引用"的注册名 —— 被多个节点共用是刻意的复用，不算改名漏改。
    """
    users: dict[str, list[str]] = {}
    for node in ctx.nodes.values():
        for key in ("custom_recognition", "custom_action"):
            value = node.data.get(key)
            if isinstance(value, str):
                users.setdefault(value, []).append(node.name)

    for node in ctx.nodes.values():
        for key, suffix in (("custom_recognition", "Reco"), ("custom_action", "Act")):
            value = node.data.get(key)
            if not isinstance(value, str):
                continue
            want = node.name + suffix
            if value == want:
                continue
            if len(users.get(value, [])) > 1:
                continue
            ctx.add(
                "A3-custom-name-mapping",
                "warn",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}.{key} = {value!r}，按 node-naming.md §7 应为 {want!r}",
            )


def check_a4_shadowed(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            for idx, item in enumerate(items):
                name, _jump, enabled = parse_ref(item)
                if not name or not enabled:
                    continue
                target = ctx.nodes.get(name)
                if not hits_always(target):
                    continue
                if idx == len(items) - 1:
                    continue
                has_max = target.data.get("max_hit") is not None
                ctx.add(
                    "A4-shadowed-candidate",
                    "warn" if has_max else "error",
                    node.file,
                    find_line(node.file, node.name),
                    f"{node.name}.{key} 第 {idx + 1} 项 {name!r} 必然命中"
                    + ("（带 max_hit，用尽后才轮到后面）" if has_max else "")
                    + f"，其后 {len(items) - idx - 1} 项在它之前不可达",
                )


def check_a5_spin_trap(ctx: Ctx) -> None:
    jump_targets: set[str] = set()
    for node in ctx.nodes.values():
        for _key, items in ref_lists(node.data):
            for item in items:
                name, jump, _enabled = parse_ref(item)
                if jump and name:
                    jump_targets.add(name)
    for name in sorted(jump_targets):
        node = ctx.nodes.get(name)
        if not hits_always(node):
            continue
        if node.data.get("next"):
            continue
        if node.data.get("max_hit") is not None:
            continue
        ctx.add(
            "A5-spin-trap",
            "error",
            node.file,
            find_line(node.file, name),
            f"{name} 是 [JumpBack] 候选、必然命中、没有 next 也没有 max_hit：弹回重扫时会再次命中自己，永久空转",
        )


def check_a6_complement(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            parsed = []
            for idx, item in enumerate(items):
                name, jump, enabled = parse_ref(item)
                parsed.append((idx, name, jump, enabled))
            reported = False
            for idx, name, jump, enabled in parsed:
                if reported or not name or not enabled:
                    continue
                target = ctx.nodes.get(name)
                if target is None or target.data.get("enabled") is False:
                    continue
                pair = complement_of(target.data)
                if pair is None:
                    continue
                for jdx, jname, jjump, jenabled in parsed:
                    if reported:
                        break
                    if jname != pair or not jenabled or jump or jjump:
                        continue
                    cut = max(idx, jdx)
                    if cut < len(parsed) - 1:
                        rest = [n for _, n, _, _ in parsed[cut + 1 :]]
                        ctx.add(
                            "A6-complement-unreachable",
                            "warn",
                            node.file,
                            find_line(node.file, node.name),
                            f"{node.name}.{key} 里 {name!r}(第 {idx + 1} 项) 与 {pair!r}(第 {jdx + 1} 项) 严格互补，"
                            f"其后的 {rest} 不可达",
                        )
                    reported = True


def jumpback_descendants(root: str, nodes: dict[str, "Node"], limit: int = 64) -> set[str]:
    """从 `root` 出发、沿不带 `[JumpBack]` 的 next 引用能到达的节点集合（含 root）。

    这些节点是从 `root` 往下栈住的"后代"：若某节点能通过它们再 `[JumpBack]` 回到
    `root`，那 `root` 就是它的祖先，弹回层次会绕圈。
    """
    seen: set[str] = set()
    stack = [root]
    while stack and len(seen) < limit:
        cur = stack.pop()
        if cur in seen:
            continue  # 出栈时判：环上的节点仍要能再次入栈，否则祖先判定会漏掉自己
        seen.add(cur)
        node = nodes.get(cur)
        if node is None:
            continue
        for _key, items in ref_lists(node.data):
            for item in items:
                name, jump, enabled = parse_ref(item)
                if name and enabled and not jump:
                    stack.append(name)
    return seen


def jumpback_parent_loops(node: "Node", nodes: dict[str, "Node"], limit: int = 64) -> set[str]:
    """`node` 用 `[JumpBack]` 指向的那些"祖先 Loop"。

    判定：某 `[JumpBack]` 目标 `L` 是祖先，当且仅当 `node` 能从 `L` 沿不带
    `[JumpBack]` 的引用到达（拓扑上的"后代"）。只看 `L` 以 `Loop` 结尾的引用。
    """
    parents: set[str] = set()
    for _key, items in ref_lists(node.data):
        for item in items:
            name, jump, _enabled = parse_ref(item)
            if not jump or not name or "Loop" not in name:
                continue
            if node.name in jumpback_descendants(name, nodes, limit):
                parents.add(name)
    return parents


def check_a7_jumpback(ctx: Ctx) -> None:
    """`[JumpBack]` 的落点规则（结构文档 §4）。

    - 指向自己：那是自循环候选，不该带 `[JumpBack]` → error。
    - 指向 `Loop`：同任务子 Loop 是允许的（"先干完这段再回来"）→ info；
      指向自己的祖先 Loop（弹回层次会绕圈）→ error。
    """
    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            for item in items:
                name, jump, _enabled = parse_ref(item)
                if not jump or not name:
                    continue
                if name == node.name:
                    ctx.add(
                        "A7-jumpback-target",
                        "error",
                        node.file,
                        find_line(node.file, node.name),
                        f"{node.name}.{key} 用 [JumpBack] 指向自己；自循环应写成普通候选（结构文档 §4）",
                    )
                elif "Loop" in name:
                    target_file = ctx.nodes[name].file if name in ctx.nodes else node.file
                    scope = "本文件" if target_file == node.file else "跨文件"
                    if name in jumpback_parent_loops(node, ctx.nodes):
                        ctx.add(
                            "A7-jumpback-target",
                            "error",
                            node.file,
                            find_line(node.file, node.name),
                            f"{node.name}.{key} 用 [JumpBack] 指向自己的祖先工作循环 {name}"
                            f"（弹回层次会绕圈，结构文档 §4）",
                        )
                    else:
                        ctx.add(
                            "A7-jumpback-target",
                            "info",
                            node.file,
                            find_line(node.file, node.name),
                            f"{node.name}.{key} 用 [JumpBack] 指向工作循环 {name}（{scope}）："
                            f'结构文档 §4 允许"先干完这段再回来"的子 Loop 串联，确认它不是祖先即可',
                        )


def check_a8_loop_guard(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        if node.name.endswith("Loop") and not node.data.get("recognition"):
            ctx.add(
                "A8-loop-guard",
                "warn",
                node.file,
                find_line(node.file, node.name),
                f"{node.name} 是工作循环但没有 recognition（页面守卫），结构文档 §3 不建议",
            )


def check_a9_ensure(ctx: Ctx) -> None:
    """就位节点的 `next` 顺序（结构文档 §2）。

    **不查 `max_hit`**：§2 现在没有"就位节点不要用 max_hit 卡"这句话，而 §5 明确把 `max_hit`
    划给"任务级导航（从枢纽到页面）"——就位节点正是导航，`BattleEnsureHome` 的 `max_hit: 2` 是
    合规预算。真正该管的是"在循环自愈路径上依赖它"，那条由 A10 负责（`--ignore A10-…` 可放行）。
    """
    for node in ctx.nodes.values():
        if not ENSURE_RE.search(node.name):
            continue
        items = to_list(node.data.get("next"))
        if not items:
            continue
        names = [parse_ref(i)[0] for i in items]
        first, last = names[0], names[-1]
        if first and "Probe" not in first:
            ctx.add(
                "A9-ensure-order",
                "warn",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}.next 第一项是 {first!r}；结构文档 §2 要求先放目标页面的探针（*Probe*）",
            )
        if last is None:
            continue  # 未定义由 A1-dangling-node 负责
        if ENSURE_RE.search(last):
            continue  # 末项是上一级 Ensure（嵌套）或兜底用的 Ensure，符合 §2
        if any(n and ENSURE_RE.search(n) for n in names[:-1]):
            ctx.add(
                "A9-ensure-order",
                "warn",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}.next 末项是 {last!r}；嵌套 Ensure 的末项是上一级 Ensure（结构文档 §2）",
            )
            continue
        # 单层：末项若是"必然命中 + 有 next + 没有 max_hit / 有限 timeout"，
        # 它每轮都会抢走，这个 Ensure 自己的 timeout 永远触发不到。
        target = ctx.nodes.get(last)
        if target is None or not always_hit(target.data):
            continue
        if not to_list(target.data.get("next")):
            continue
        if target.data.get("max_hit") is not None:
            continue
        timeout = target.data.get("timeout")
        if isinstance(timeout, int) and timeout > 0:
            continue
        ctx.add(
            "A9-ensure-order",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}.next 末项 {last!r} 必然命中且带 next，又没有 max_hit / 有限 timeout："
            f"它每轮都会抢走，守卫的 timeout 永远触发不到（结构文档 §2）",
        )


def check_a10_maxhit_in_loop(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        if not node.name.endswith("Loop"):
            continue
        for key, items in ref_lists(node.data):
            for item in items:
                name, _jump, _enabled = parse_ref(item)
                target = ctx.nodes.get(name) if name else None
                if target is None or target.data.get("max_hit") is None:
                    continue
                ctx.add(
                    "A10-maxhit-in-loop",
                    "warn",
                    node.file,
                    find_line(node.file, node.name),
                    f"{node.name}.{key} 的候选 {name} 带 max_hit: {target.data['max_hit']}；结构文档 §5 不建议在循环自愈路径上依赖它（有意的预算可 --ignore A10）",
                )


def check_a11_expected(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        for _path, value in walk_key(node.data, "expected"):
            for pattern in to_list(value):
                if not isinstance(pattern, str):
                    continue
                try:
                    re.compile(pattern)
                except re.error as e:
                    ctx.add(
                        "A11-bad-regex",
                        "error",
                        node.file,
                        find_line(node.file, node.name),
                        f"{node.name}.expected 正则无法编译：{pattern!r}（{e}）",
                    )
                else:
                    if BOUNDED_QUANTIFIER_RE.search(pattern):
                        ctx.add(
                            "A11-bounded-quantifier",
                            "info",
                            node.file,
                            find_line(node.file, node.name),
                            f"{node.name}.expected 用了有上限的量化：{pattern!r}（值域扩大后会漏配）",
                        )


def check_a12_focus(ctx: Ctx) -> None:
    for node in ctx.nodes.values():
        for _path, value in walk_key(node.data, "focus"):
            if not isinstance(value, dict):
                continue
            for k in value:
                if k not in FOCUS_KEYS:
                    ctx.add(
                        "A12-bad-focus-key",
                        "error",
                        node.file,
                        find_line(node.file, node.name),
                        f"{node.name}.focus 的键 {k!r} 不是合法的消息类型（schema 的 FocusTemplateMap）",
                    )


def check_a13_naming(ctx: Ctx) -> None:
    generic = ctx.generic_nodes
    # 每个文件接受的"任务前缀"：出现 ≥2 次的第一个 CamelCase 词
    # （不看 entry 名，因为一个文件可能有 TaskBattleAuto / TaskBattleReplay 这类入口名）
    prefixes: dict[Path, set[str]] = {}
    by_file: dict[Path, list[str]] = {}
    for name, info in ctx.nodes.items():
        by_file.setdefault(info.file, []).append(name)
    for file, names in by_file.items():
        counts: dict[str, int] = {}
        for name in names:
            word = leading_word(name)
            counts[word] = counts.get(word, 0) + 1
        prefixes[file] = {p for p, c in counts.items() if c >= 2}

    for name, info in ctx.nodes.items():
        data = info.data
        line = find_line(info.file, name)
        # 旧前缀：所有文件（含 base.json）
        for legacy in LEGACY_PREFIXES:
            if re.match(legacy + r"[A-Z]", name):
                ctx.add(
                    "A13-legacy-prefix",
                    "warn",
                    info.file,
                    line,
                    f"{name} 用了旧前缀 {legacy}（node-naming.md §3：并入 Probe + 状态词）",
                )
                break
        if info.file.name == "base.json" or name in generic:
            continue
        pset = prefixes.get(info.file, set())
        suffixes = word_suffixes(name)
        verb_after_prefix = any(s.startswith(v) for s in suffixes for v in VERBS)
        prefixed = name.startswith("Task") or any(name.startswith(p) for p in pset)
        legacy_hit = (
            re.match(r"(?:" + "|".join(LEGACY_PREFIXES) + r")[A-Z]", name) is not None
        )
        if pset and not prefixed and not verb_after_prefix and not legacy_hit:
            ctx.add(
                "A13-missing-prefix",
                "warn",
                info.file,
                line,
                f"{name} 既不带本任务前缀（{', '.join(sorted(pset))}）也不符合 <动词><宾语> 形状，node-naming.md §1/§4",
            )
        has_action = "action" in data
        is_probe = name.startswith("Probe") or "Probe" in name
        if is_probe:
            if has_action:
                ctx.add(
                    "A13-probe-action",
                    "warn",
                    info.file,
                    line,
                    f"{name} 是探测节点却有 action（node-naming.md §3：探测只探，动作拆出去）",
                )
            if to_list(data.get("next")):
                ctx.add(
                    "A13-probe-next",
                    "warn",
                    info.file,
                    line,
                    f"{name} 是探测节点却有 next（node-naming.md §3：探测不做行为，是终点节点）",
                )
            continue
        for synonym in CLICK_SYNONYMS:
            if synonym in name:
                ctx.add(
                    "A13-click-synonym",
                    "warn",
                    info.file,
                    line,
                    f"{name} 用了同义写法 {synonym}（node-naming.md §6：统一 ClickOk / ClickCancel / ClickFinish）",
                )
                break
        if name.endswith("Loop") and not to_list(data.get("next")):
            ctx.add(
                "A13-loop-next",
                "warn",
                info.file,
                line,
                f"{name} 是工作循环却没有 next（node-naming.md §3：业务候选排在里面）",
            )
        if ENSURE_RE.search(name) or name.endswith("Loop"):
            continue
        if name.startswith(CLASS_SUFFIXES) or name.endswith(CLASS_SUFFIXES):
            continue
        if has_action and not verb_after_prefix:
            ctx.add(
                "A13-verb",
                "warn",
                info.file,
                line,
                f"{name} 是动作节点但名字里没有动词表里的词（node-naming.md §4），候选写法：<前缀><动词><宾语>",
            )


def check_a13_references(ctx: Ctx) -> None:
    """就位层的引用规则与片段/参数键的写法（node-naming.md §3/§7/§8）。

    这几条都是**今天一条都不报**的护栏：写它们是为了以后别写错，不是为了现在挑错。
    判定只看结构，不看语义，所以规则本身也有限——见 README「功能取舍」。
    """
    ensure_nodes = {n for n in ctx.nodes if ENSURE_RE.search(n)}

    # §3 就位：被引用时必须带 [JumpBack]——不带的话点了没进去就被弹回，页面保证不住
    for name, info in ctx.nodes.items():
        for key, items in ref_lists(info.data):
            for item in items:
                target, jump, enabled = parse_ref(item)
                if target in ensure_nodes and not jump:
                    ctx.add(
                        "A13-ensure-jumpback",
                        "warn",
                        info.file,
                        find_line(info.file, name),
                        f"{name}.{key} 引用了就位节点 {target} 却没带 [JumpBack]（node-naming.md §3）",
                    )
                if (
                    ENSURE_RE.search(name)
                    and key == "next"
                    and target
                    and target.endswith("Loop")
                    and target not in ENSURE_SKELETON_REFS
                ):
                    ctx.add(
                        "A13-ensure-loop-next",
                        "warn",
                        info.file,
                        find_line(info.file, name),
                        f"{name}.next 里放了工作循环 {target}（node-naming.md §3：Loop 在 Task* 入口命中）",
                    )

    # §7 参数键用 snake_case（键名进 agent，改名要动两处，所以越早拦住越省事）
    def custom_keys(data, where: str) -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                if key in (
                    "custom_recognition_param",
                    "custom_action_param",
                ) and isinstance(value, dict):
                    for k in value:
                        if not re.fullmatch(r"[a-z][a-z0-9_]*", k):
                            ctx.add(
                                "A13-key-case",
                                "warn",
                                where,
                                find_line(Path(where), k),
                                f"{k} 不是 snake_case（node-naming.md §7：参数键用 snake_case）",
                            )
                custom_keys(value, where)
        elif isinstance(data, list):
            for item in data:
                custom_keys(item, where)

    for _name, info in ctx.nodes.items():
        custom_keys(info.data, str(info.file))
    for _name, opt in ctx.options.items():
        custom_keys(opt.data, str(opt.file))

    # §8 pipeline 片段文件名：任务名全小写 snake_case
    for path in ctx.pipeline_files:
        if path.name == "base.json":
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.json", path.name):
            ctx.add(
                "A13-file-case",
                "warn",
                path,
                None,
                f"pipeline 片段文件名 {path.name} 不是小写 snake_case（node-naming.md §8）",
            )


def check_a14_loop_fallback(ctx: Ctx) -> None:
    """工作循环末项兜底 `Ensure` 与循环守卫取同一个探针（结构文档 §3 末段）。

    结构文档把它写成硬规则（"兜底的探针不能与本 `Loop` 的守卫探针相同（方向相反也算）"），
    所以报 warn 而不是 info。确实合法的页面级循环（候选全是子守卫，全 miss 只可能意味着漂走）
    用 `--ignore A14-fallback-same-probe` 放行——判不了的事交给人点名豁免，比让规则常年只当提示强。
    """
    for node in ctx.nodes.values():
        if "Loop" not in node.name:
            continue
        probe = guard_probe(node.data)
        if not probe:
            continue
        items = to_list(node.data.get("next"))
        if not items:
            continue
        last, jump, _enabled = parse_ref(items[-1])
        if not jump or not last or not ENSURE_RE.search(last):
            continue
        target = ctx.nodes.get(last)
        if target is None or guard_probe(target.data) != probe:
            continue
        ctx.add(
            "A14-fallback-same-probe",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}.next 末项 {last!r} 与该循环的守卫用同一个探针 {probe!r}："
            f"兜底会把『守卫不成立』修成『守卫成立』，循环永远结束不了（结构文档 §3）。"
            f"页面级循环确实要这么写时用 --ignore A14-fallback-same-probe 放行",
        )


def annotations_of(text: str) -> list[tuple[Spec, str, str | None]]:
    """把一段文本里所有「使用<规范名> vX.Y [校验年月日 YY/MM/DD]」抽出来。

    返回 `[(规范, 版本, 校验日期)]`，按出现顺序；认不出的说法直接跳过。抽成纯函数是为了自检：
    三种写法（旧名 `命名规范版本`、新名 `节点命名与分类` + `任务链结构`、模板里不带日期的简写）
    都要能解析出来，否则"文件头部到底按哪版看过的"就查不出来。
    """
    found: list[tuple[Spec, str, str | None]] = []
    for match in ANNOTATION_RE.finditer(text):
        spec = ANNOTATION_SPECS.get(match.group(1))
        if spec is not None:
            found.append((spec, match.group(2), match.group(3)))
    return found


def check_spec_version(ctx: Ctx) -> None:
    """规范版本落地：脚本按哪版实现、每份文档到哪版、哪些文件还没按当前版本重看过。

    三处判据：① 文档头部的「版本号: vX.Y」对各自的常量（命名规范 / 任务链结构，两份都要比，
    结构类判据按 pipeline-structure.md 实现，只对一份算版本等于让另一份改了没人提醒）；
    ② 写法模板 `$comment` 里那句 `使用命名规范 vX.Y; 使用任务链结构 vX.Y`（两份分别比）；
    ③ 各文件头部注解落后于对应文档的（没有注解的不报——老文件太多，报了等于噪音）。
    """
    for spec in SPECS:
        doc = ctx.repo_root / spec.path
        if not doc.is_file():
            ctx.add(
                "E1-spec-missing",
                "error",
                doc,
                None,
                f"找不到{spec.label}文档 {spec.path}（本脚本按 {spec.version} 实现）",
            )
            continue
        m = DOC_VERSION_RE.search(doc.read_text(encoding="utf-8"))
        verdict = version_finding(m.group(1) if m else None, spec.version, spec)
        if verdict:
            severity = "error" if verdict[0] == "E1-spec-missing" else "warn"
            ctx.add(verdict[0], severity, doc, find_line(doc, "版本号"), verdict[1])

    # 写法模板：`$comment` 上那句也带两份规范的版本，逐份比（只比第一处 vX.Y 会漏掉结构那一份）
    snippets = ctx.repo_root / PIPELINE_SNIPPETS
    if snippets.is_file():
        text = snippets.read_text(encoding="utf-8")
        comment = next((l for l in text.splitlines() if "$comment" in l), "")
        for spec, version, _date in annotations_of(comment):
            verdict = version_finding(version, spec.version, spec)
            if verdict:
                ctx.add(
                    verdict[0],
                    "warn",
                    snippets,
                    find_line(snippets, "$comment"),
                    f"写法模板写着{spec.label} v{version}，脚本按 {spec.version} 实现："
                    f"复核骨架要不要跟着改，改完把 {spec.const} 与模板的版本一起对",
                )

    # 头部注解落后的文件：没有注解的不报，只报"看过的"。一行可以同时标两份规范（新写法），
    # 所以逐个注解报，而不是只看第一条。
    for path in (*ctx.pipeline_files, *ctx.task_files, *ctx.agent_files):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stale = [(spec, version, date) for spec, version, date in annotations_of(text)
                 if version_tuple(f"v{version}") < version_tuple(spec.version)]
        if not stale:
            continue
        lines = lines_of(path)
        for spec, version, date in stale:
            line = next(
                (i for i, l in enumerate(lines, 1)
                 if any(s is spec and v == version for s, v, _d in annotations_of(l))),
                None,
            )
            when = f"（{date}）" if date else ""
            ctx.add(
                "E3-stale-annotation",
                "info",
                path,
                line,
                f"头部标注按{spec.label} v{version}{when}校验过，该文档已到 {spec.version}："
                f"重看一遍后把版本号改成 {spec.version}",
            )


# --------------------------------------------------------------------------
# B. interface / tasks
# --------------------------------------------------------------------------


def check_override_targets(ctx: Ctx, override, file: Path, label: str) -> None:
    if not isinstance(override, dict):
        return
    for target, patch in override.items():
        if target not in ctx.nodes and target not in DYNAMIC_NODES:
            ctx.add(
                "B5-bad-override-target",
                "error",
                file,
                find_line(file, label) if label else None,
                f"{label} 的 pipeline_override 目标 {target!r} 不是已定义节点（覆写静默失效）",
            )
        if isinstance(patch, dict):
            for key in ("next", "on_error"):
                for item in to_list(patch.get(key)):
                    name, _jump, _enabled = parse_ref(item)
                    if name and name not in ctx.nodes and name not in DYNAMIC_NODES:
                        ctx.add(
                            "B5-bad-override-target",
                            "error",
                            file,
                            find_line(file, label) if label else None,
                            f"{label} 覆写 {target}.{key} 指向未定义节点 {name!r}",
                        )


def check_option_shape(ctx: Ctx) -> None:
    def one(name: str, data: dict, file: Path) -> None:
        line = find_line(file, name)
        cases = to_list(data.get("cases"))
        default_case = data.get("default_case")
        if default_case is not None and cases and isinstance(cases[0], dict):
            if cases[0].get("name") != default_case:
                ctx.add(
                    "B7-default-case",
                    "warn",
                    file,
                    line,
                    f"option {name} 的 default_case={default_case!r} 与 cases[0]={cases[0].get('name')!r} 不一致；"
                    "MFA 上 default_case 失效，默认项必须是 cases[0]",
                )
        for case in cases:
            if not isinstance(case, dict):
                continue
            for opt in to_list(case.get("option")):
                if opt not in ctx.options:
                    ctx.add(
                        "B3-bad-option",
                        "error",
                        file,
                        line,
                        f"option {name} 的 case {case.get('name')!r} 引用了不存在的 option {opt!r}",
                    )
            check_override_targets(ctx, case.get("pipeline_override"), file, name)
        for item in to_list(data.get("inputs")):
            if not isinstance(item, dict):
                continue
            verify = item.get("verify")
            if isinstance(verify, str) and not (
                verify.startswith("^") and verify.endswith("$")
            ):
                ctx.add(
                    "B7-verify-anchor",
                    "warn",
                    file,
                    line,
                    f"option {name} 的 verify={verify!r} 没有 ^...$ 收尾（编辑器与面板不会提醒，用户可能永远过不了校验）",
                )

    for name, opt in ctx.options.items():
        one(name, opt.data, opt.file)
    if ctx.interface and ctx.interface_file:
        for name, opt in (ctx.interface.get("option") or {}).items():
            if isinstance(opt, dict):
                one(name, opt, ctx.interface_file)


def check_interface(ctx: Ctx) -> None:
    iface = ctx.interface
    if not iface or not ctx.interface_file:
        return
    file = ctx.interface_file
    tasks = [t for t in to_list(iface.get("task")) if isinstance(t, dict)]
    task_names = {t.get("name") for t in tasks}
    group_names = {
        g.get("name") for g in to_list(iface.get("group")) if isinstance(g, dict)
    }

    for task in tasks:
        label = task.get("name") or "?"
        line = find_line(file, label)
        entry = task.get("entry")
        if (
            isinstance(entry, str)
            and entry not in ctx.nodes
            and entry not in DYNAMIC_NODES
        ):
            ctx.add(
                "B1-bad-entry",
                "error",
                file,
                line,
                f"任务 {label!r} 的 entry {entry!r} 不是已定义节点",
            )
        for g in to_list(task.get("group")):
            if g not in group_names:
                ctx.add(
                    "B2-bad-group",
                    "error",
                    file,
                    line,
                    f"任务 {label!r} 引用了不存在的 group {g!r}",
                )
        for opt in to_list(task.get("option")):
            if opt not in ctx.options:
                ctx.add(
                    "B3-bad-option",
                    "error",
                    file,
                    line,
                    f"任务 {label!r} 引用了不存在的 option {opt!r}",
                )
        check_override_targets(ctx, task.get("pipeline_override"), file, label)

    for path, preset in ctx.presets:
        plabel = preset.get("name") or path.name
        for t in to_list(preset.get("task")):
            name = t.get("name") if isinstance(t, dict) else t
            if isinstance(name, str) and name not in task_names:
                ctx.add(
                    "B6-bad-preset-task",
                    "error",
                    path,
                    find_line(path, plabel),
                    f"preset {plabel!r} 引用了不存在的任务 {name!r}",
                )


def check_interface_paths(ctx: Ctx) -> None:
    iface = ctx.interface
    # `iface` 与 `interface_file` 只可能一起被赋值，但类型上是两个独立可选字段；
    # 显式一起判，省得靠"实现上不会为 None"的推断。
    if not iface or not ctx.interface_dir or not ctx.interface_file:
        return
    base = ctx.interface_dir
    file = ctx.interface_file
    for res in to_list(iface.get("resource")):
        if not isinstance(res, dict):
            continue
        for p in to_list(res.get("path")):
            if isinstance(p, str) and not (base / p).exists():
                ctx.add(
                    "B8-bad-path",
                    "error",
                    file,
                    find_line(file, res.get("name") or ""),
                    f"resource 路径不存在：{p}",
                )
    for key in ("welcome", "doc"):
        v = iface.get(key)
        if isinstance(v, str) and v and not (base / v).exists():
            ctx.add("B8-bad-path", "warn", file, None, f"{key} 指向的文件不存在：{v}")
    for arg in to_list((iface.get("agent") or {}).get("child_args")):
        if isinstance(arg, str) and arg.endswith(".py"):
            if not any((c / arg).exists() for c in (base, ctx.repo_root)):
                ctx.add(
                    "B8-bad-path",
                    "error",
                    file,
                    None,
                    f"agent.child_args 的 {arg!r} 在 interface 目录与仓库根下都找不到",
                )
    for imp in ctx.import_files:
        if not imp.exists():
            ctx.add(
                "B4-bad-import", "error", file, None, f"import 指向的文件不存在：{imp}"
            )


def check_tasks(ctx: Ctx) -> None:
    check_option_shape(ctx)


# --------------------------------------------------------------------------
# C. image
# --------------------------------------------------------------------------


def collect_template_refs(ctx: Ctx) -> list[tuple[Path, str, str, int | None]]:
    """(文件, 说明, 模板值, 行号)"""
    refs: list[tuple[Path, str, str, int | None]] = []
    for node in ctx.nodes.values():
        for _path, value in walk_key(node.data, "template"):
            for v in to_list(value):
                if isinstance(v, str):
                    refs.append(
                        (node.file, node.name, v, find_line(node.file, node.name))
                    )
    lit_re = re.compile(
        r"[\"']([A-Za-z0-9_][A-Za-z0-9_\-. ]*(?:/[A-Za-z0-9_\-. ]+)*/?)[\"']"
    )
    for path in ctx.agent_files:
        lines = lines_of(path)
        for i, line in enumerate(lines, 1):
            for m in lit_re.finditer(line):
                s = m.group(1)
                if IMAGE_EXT_RE.search(s) or s.endswith("/"):
                    refs.append((path, "agent 字面量", s, i))
    return refs


def check_images(ctx: Ctx) -> None:
    if not ctx.image_roots:
        return
    refs = collect_template_refs(ctx)
    used: set[Path] = set()
    used_dirs: set[Path] = set()
    for file, owner, value, line in refs:
        hit = False
        for root in ctx.image_roots:
            target = root / value.rstrip("/")
            if value.endswith("/"):
                if target.is_dir():
                    used.add(target)
                    used_dirs.add(target)
                    imgs = [p for p in target.iterdir() if p.is_file()]
                    if not imgs:
                        ctx.add(
                            "C4-empty-folder-template",
                            "error",
                            file,
                            line,
                            f"{owner} 的目录模板 {value!r} 里没有图片",
                        )
                    hit = True
                    break
            elif target.is_file():
                used.add(target)
                hit = True
                break
        if not hit:
            ctx.add(
                "C1-missing-template",
                "error",
                file,
                line,
                f"{owner} 引用的模板不存在：{value!r}",
            )

    def is_used(path: Path) -> bool:
        if path in used:
            return True
        return any(d in path.parents for d in used_dirs)

    for path in list_images(ctx.image_roots):
        rel = None
        for root in ctx.image_roots:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            break
        if rel is None:
            continue
        if not IMAGE_EXT_RE.search(path.name):
            ctx.add(
                "C3-non-png", "warn", path, None, f"image 目录下有非 png 文件：{rel}"
            )
        if not is_used(path):
            ctx.add(
                "C2-unused-image",
                "info",
                path,
                None,
                f"没有被任何 pipeline/agent 引用的图片：{rel}",
            )
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", path.stem):
            ctx.add(
                "C5-image-name",
                "warn",
                path,
                None,
                f"模板元素名不是英文 CamelCase（node-naming.md §8）：{rel}",
            )


# --------------------------------------------------------------------------
# D. agent
# --------------------------------------------------------------------------


def import_targets(line: str) -> set[str]:
    """一行源码里 import 的模块名（带点的原名，如 `rect`）。

    只做文本匹配，不建 AST；`from __future__ import ...` 与字符串里的内容不会命中。
    """
    out: set[str] = set()
    m = re.match(r"\s*from\s+([A-Za-z_][\w.]*)\s+import\b", line)
    if m:
        return {m.group(1)}  # from a.b import c  → a.b
    m = re.match(r"\s*import\s+([A-Za-z_][\w., ]*)", line)
    if m:
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if name:
                out.add(name)  # import a, b.c  → a / b.c
    return out


def local_module(name: str, local: dict[str, Path]) -> str | None:
    """把 import 名解析成已发现的模块名。

    先整名匹配（`rect`），再取最左段（`shared`）与所有模块的顶层段比（`rect`）——
    后者是兄弟式 import：`from rect import ...` 里的 rect 没有点，靠 agent/main.py 把
    agent/shared/ 注入 sys.path 才能解析，这里按"某个子目录里有 rect.py"认。
    """
    if name in local:
        return name
    top = name.split(".")[0]
    if top in local:
        return top
    for known in local:
        # 子目录里的模块：`from rect import ...` 要靠 `rect` 认出来。
        # 比的是点分段（不是子串），免得 `abc` 误配 `x.abc.y`。
        if top in known.split("."):
            return known
    return None


def agent_module_root(files: list[Path]) -> Path:
    """这些 agent 源文件所属的根目录（= 公共父目录，正常是 `<仓库根>/agent`）。

    公共父目录正好把 `agent/shared/rect.py` 还原成模块名 `rect`，与 main.py 的
    sys.path 注入（agent/ 与 agent/shared/ 各自入 path）对得上。
    """
    if not files:
        return Path(".")
    try:
        # 取文件路径的公共目录（commonpath 对 `agent/main.py` 与 `agent/shared/rect.py` 给 `<根>/agent`）；
        # 只有一个文件时 commonpath 就是文件本身，要退到它的目录。
        common = Path(os.path.commonpath([str(p) for p in files]))
        return common if common.is_dir() else common.parent
    except ValueError:  # 跨盘符（Windows）等极端情况
        return files[0].parent


def check_agent(ctx: Ctx) -> None:
    # 模块名是"相对 agent/ 的点分名"：顶层 base.py → base，子目录 agent/shared/rect.py → rect。
    root = agent_module_root([p for p in ctx.agent_files if p.suffix == ".py"])
    local: dict[str, Path] = {}
    for p in ctx.agent_files:
        if p.suffix != ".py":
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = Path(p.name)
        local[".".join(rel.with_suffix("").parts)] = p

    graph: dict[str, set[str]] = {}
    for name, path in local.items():
        deps: set[str] = set()
        for line in lines_of(path):
            for target in import_targets(line):
                dep = local_module(target, local)
                if dep is not None:
                    deps.add(dep)
        graph[name] = deps

    reachable: set[str] = set()
    stack = [d for d in graph.get("main", set())]
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(graph.get(cur, set()))
    for name, path in local.items():
        if name == "main":
            continue
        if name not in reachable:
            rel = f"{name.replace('.', '/')}.py"
            ctx.add(
                "D1-unimported-module",
                "error",
                path,
                None,
                f"agent/{rel} 没有被 main.py 导入（装饰器不执行 -> 注册不生效）",
            )

    for name, reg in ctx.regs.items():
        want = "Reco" if reg.kind == "recognition" else "Act"
        if not name.endswith(want):
            ctx.add(
                "D2-registration-suffix",
                "warn",
                reg.file,
                reg.line,
                f"注册名 {name!r} 后缀不是 {want}（node-naming.md §7）",
            )
        if reg.cls and reg.cls != name:
            ctx.add(
                "D2-class-name",
                "warn",
                reg.file,
                reg.line,
                f"注册名 {name!r} 与类名 {reg.cls!r} 不一致（node-naming.md §7）",
            )


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


def self_test() -> int:
    checks = [
        (
            parse_ref("[JumpBack]Foo")[:2] == ("Foo", True),
            "parse_ref 字符串 + JumpBack",
        ),
        (
            parse_ref({"name": "Bar", "max_hit": 1})[:2] == ("Bar", False),
            "parse_ref 对象",
        ),
        (parse_ref({"recognition": "OCR"})[0] is None, "parse_ref 内联定义"),
        (
            always_hit({}) and always_hit({"recognition": "DirectHit"}),
            "always_hit DirectHit",
        ),
        (not always_hit({"recognition": "OCR"}), "always_hit 非 DirectHit"),
        (
            complement_of({"recognition": "And", "inverse": True, "all_of": ["Z"]})
            == "Z",
            "complement_of",
        ),
        (
            complement_of({"recognition": "And", "all_of": ["Z"]}) is None,
            "complement_of 非取反",
        ),
        (
            strip_jsonc_comments('{"a": 1, // x\n "b": "// not comment"}')
            == '{"a": 1, \n "b": "// not comment"}',
            "strip_jsonc_comments",
        ),
        (
            [v for _p, v in walk_key({"x": {"template": ["a.png"]}}, "template")]
            == [["a.png"]],
            "walk_key 嵌套",
        ),
        (
            re.fullmatch(r"[A-Z][A-Za-z0-9]*", "BattleButton") is not None,
            "CamelCase 判定",
        ),
        (version_tuple("v1.10") > version_tuple("v1.3"), "版本号按数字比大小"),
        (
            ANNOTATION_RE.search("// INFO 使用命名规范版本 v1.2 校验年月日 26/10/13").group(2) == "1.2",  # type: ignore[union-attr]
            "头部注解版本号（旧写法）",
        ),
        (
            [spec.label for spec, _v, _d in annotations_of(
                "使用节点命名与分类 v1.5 校验年月日 26/10/16\n使用任务链结构 v1.3 校验年月日 26/10/16")]
            == ["节点命名与分类", "任务链结构"],
            "头部注解（新写法，一行一份）两份都认",
        ),
        (
            [(spec.label, date) for spec, _v, date in annotations_of(
                '"$comment": "使用命名规范 v1.6; 使用任务链结构 v1.4"')]
            == [("节点命名与分类", None), ("任务链结构", None)],
            "写法模板那种没有校验日期的简写也认",
        ),
        (DOC_VERSION_RE.search("版本号: v1.3").group(1) == "1.3", "文档版本号"),  # type: ignore[union-attr]
    ]

    # 新增的护栏规则：合成一棵最小的节点树，确认真的会响（也为"别误报"留反例）
    def synth_ctx(nodes: dict[str, dict], files: tuple[str, ...] = ("p.json",)) -> Ctx:
        ctx = Ctx(repo_root=Path("."))
        ctx.nodes = {n: Node(n, d, Path("p.json")) for n, d in nodes.items()}
        ctx.pipeline_files = [Path(f) for f in files]
        return ctx

    ref_ctx = synth_ctx(
        {
            "TaskX": {"next": ["XEnterHome"]},
            "XEnterHome": {"action": "Click", "next": ["XEnsureHome"]},
            "XEnsureHome": {"next": ["XLoop"]},
            "XLoop": {"next": ["XEnsureHome", "LoopErrorByTimes"]},
        }
    )
    check_a13_references(ref_ctx)
    ref_ids = {f.id for f in ref_ctx.findings}
    checks += [
        ("A13-ensure-jumpback" in ref_ids, "引用就位节点未带 [JumpBack] 会被拦"),
        ("A13-ensure-loop-next" in ref_ids, "就位节点的 next 里放 Loop 会被拦"),
        (
            not any("LoopErrorByTimes" in f.message for f in ref_ctx.findings),
            "LoopErrorByTimes 属骨架，不误报",
        ),
    ]

    naming_ctx = synth_ctx(
        {
            "TaskY": {"next": ["YProbeHome", "YClickYes", "YLoop", "YEnterHome"]},
            "YProbeHome": {"next": ["YEnterHome"]},
            "YClickYes": {"action": "Click"},
            "YLoop": {"action": "Click"},
            "YEnterHome": {"action": "Click", "custom_action_param": {"badKey": 1}},
        },
        files=("BadName.json",),
    )
    check_a13_naming(naming_ctx)
    check_a13_references(naming_ctx)
    naming_ids = {f.id for f in naming_ctx.findings}
    checks += [
        ("A13-probe-next" in naming_ids, "探测节点带 next 会被拦"),
        ("A13-click-synonym" in naming_ids, "ClickYes 这类同义写法会被拦"),
        ("A13-loop-next" in naming_ids, "工作循环没有 next 会被拦"),
        ("A13-key-case" in naming_ids, "参数键非 snake_case 会被拦"),
        ("A13-file-case" in naming_ids, "pipeline 片段文件名不符会被拦"),
    ]

    # 本轮新增规则：子 Loop 串联允许（只 info）、指向祖先 Loop 仍报 error。
    # 合成节点名必须以 `Loop` 结尾，否则走不到这条判据（`XLoopMain` 这种写法本身
    # 就违反 §3 的"以 Loop 结尾"；判据按 `endswith("Loop")` 识别目标类别）。
    jb_ok = synth_ctx(
        {
            "XEnterHome": {"action": "Click", "next": ["XLoopMainLoop"]},
            "XLoopMainLoop": {
                "recognition": "And",
                "all_of": ["XProbeHome"],
                "next": ["[JumpBack]XLoopSubLoop", "LoopErrorByTimes"],
            },
            "XLoopSubLoop": {"recognition": "And", "all_of": ["XProbeHome"]},
            "XProbeHome": {"recognition": "TemplateMatch"},
        }
    )
    check_a7_jumpback(jb_ok)
    checks.append(
        (
            any(
                f.id == "A7-jumpback-target"
                and f.severity == "info"
                and "XLoopSubLoop" in f.message
                for f in jb_ok.findings
            ),
            "指向子 Loop 的 [JumpBack] 只报 info",
        )
    )

    jb_ancestor = synth_ctx(
        {
            "XLoopMainLoop": {"recognition": "And", "next": ["XLoopChildLoop"]},
            "XLoopChildLoop": {"recognition": "And", "next": ["[JumpBack]XLoopMainLoop"]},
        }
    )
    check_a7_jumpback(jb_ancestor)
    checks.append(
        (
            any(
                f.id == "A7-jumpback-target" and f.severity == "error"
                for f in jb_ancestor.findings
            ),
            "指向祖先 Loop 的 [JumpBack] 仍报 error",
        )
    )

    # 嵌套 Ensure 的末项是上一级 Ensure：不报 A9-ensure-order
    ens_ctx = synth_ctx(
        {
            "XProbeTavern": {"recognition": "OCR"},
            "XEnterTavern": {"action": "Click"},
            "XEnsureHome": {"next": ["XProbeHome", "[JumpBack]XEnsureTavern", "LoopErrorByTimes"]},
            "XProbeHome": {"recognition": "OCR"},
            "XEnsureTavern": {"next": ["XProbeTavern", "[JumpBack]XEnterTavern", "XEnsureHome"]},
        }
    )
    check_a9_ensure(ens_ctx)
    checks.append(
        (
            not any(
                "XEnsureTavern" in f.message and f.id == "A9-ensure-order"
                for f in ens_ctx.findings
            ),
            "嵌套 Ensure 以上一级 Ensure 收尾不报 A9-ensure-order",
        )
    )

    # A9：嵌套但末项不是上一级 Ensure → 报
    ens_bad = synth_ctx(
        {
            "XProbeTavern": {"recognition": "OCR"},
            "XEnterTavern": {"recognition": "OCR", "action": "Click"},
            "XProbeHome": {"recognition": "OCR"},
            "XEnsureHome": {"next": ["XProbeHome"]},
            "XEnsureTavern": {"next": ["XProbeTavern", "[JumpBack]XEnsureHome", "XEnterTavern"]},
        }
    )
    check_a9_ensure(ens_bad)
    checks.append(
        (
            any(
                "XEnsureTavern" in f.message and f.id == "A9-ensure-order"
                for f in ens_bad.findings
            ),
            "嵌套 Ensure 末项不是上一级 Ensure → 报 A9-ensure-order",
        )
    )

    # A9：单层末项必然命中且带 next、没有 max_hit / 有限 timeout → 报；带 max_hit → 不报
    def ensure_with_last(last: dict) -> Ctx:
        synth = synth_ctx(
            {
                "XProbeHome": {"recognition": "OCR"},
                "XForward": last,
                "XEnsureHome": {"next": ["XProbeHome", "XForward"]},
            }
        )
        check_a9_ensure(synth)
        return synth

    forward_ctx = ensure_with_last({"next": ["XProbeHome"]})
    checks.append(
        (
            any(f.id == "A9-ensure-order" for f in forward_ctx.findings),
            "单层 Ensure 末项必然命中且无收尾 → 报 A9-ensure-order",
        )
    )
    bounded_ctx = ensure_with_last({"max_hit": 2, "next": ["XProbeHome"]})
    checks.append(
        (
            not any(f.id == "A9-ensure-order" for f in bounded_ctx.findings),
            "末项带 max_hit 有收尾 → 不报 A9-ensure-order",
        )
    )

    # A14：循环末项兜底 Ensure 与循环守卫同探针 → info；不同探针 → 不报
    fallback_ctx = synth_ctx(
        {
            "XProbeWork": {"recognition": "OCR"},
            "XProbeElse": {"recognition": "OCR"},
            "XEnsureWork": {"recognition": "And", "all_of": ["XProbeWork"], "inverse": True},
            "XEnsureElse": {"recognition": "And", "all_of": ["XProbeElse"], "inverse": True},
            "XLoopWork": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "next": ["[JumpBack]XEnsureWork"],
            },
            "XLoopElse": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "next": ["[JumpBack]XEnsureElse"],
            },
        }
    )
    check_a14_loop_fallback(fallback_ctx)
    checks += [
        (
            any(
                f.id == "A14-fallback-same-probe"
                and f.severity == "warn"
                and "XLoopWork" in f.message
                for f in fallback_ctx.findings
            ),
            "兜底 Ensure 与循环守卫同探针 → 报 A14（warn，结构文档 §3 是硬规则）",
        ),
        (
            not any("XLoopElse" in f.message for f in fallback_ctx.findings),
            "兜底 Ensure 换一个探针 → 不报 A14",
        ),
    ]

    # A9 不再查 max_hit（§5 把 max_hit 划给任务级导航，就位节点正是导航），但仍要查 next 顺序
    a9_ctx = synth_ctx(
        {
            "XProbeHome": {"recognition": "OCR"},
            "XEnsureHome": {"max_hit": 2, "next": ["XProbeHome", "XEnterHome"]},
            "XEnterHome": {"action": "Click"},
        }
    )
    check_a9_ensure(a9_ctx)
    checks += [
        (
            not any(f.id == "A9-ensure-maxhit" for f in a9_ctx.findings),
            "就位节点带 max_hit 不再报 A9（§5 允许导航预算）",
        ),
        (
            not a9_ctx.findings,
            "就位节点 next 顺序合规时 A9 不报",
        ),
    ]

    # 动词表新增 Enable：pipeline 内的开关型动作不再报 A13-verb
    enable_ctx = synth_ctx({"XEnableReward": {"action": "Custom"}})
    check_a13_naming(enable_ctx)
    checks.append(
        (
            not any(
                f.id == "A13-verb" and "XEnableReward" in f.message
                for f in enable_ctx.findings
            ),
            "Enable 进动词表：XEnableReward 不报 A13-verb",
        )
    )

    # 规范版本：文档/模板与脚本不一致就要报，一致时不报（自检不写文件，只试纯函数）
    def vf_id(found: str | None) -> str:
        verdict = version_finding(found)
        return verdict[0] if verdict else ""

    checks += [
        (vf_id("1.9") == "E2-spec-version", "文档版本更高 → 报 E2"),
        (vf_id("1.2") == "E2-spec-version", "文档版本更低 → 也报 E2"),
        (vf_id(NAMING_SPEC_VERSION.lstrip("v")) == "", "版本一致 → 不报"),
        (vf_id(None) == "E1-spec-missing", "找不到版本号 → E1"),
        (
            version_tuple("v1.2") < version_tuple(NAMING_SPEC_VERSION),
            "旧注解比当前版本低",
        ),
        (
            "任务链结构" in (version_finding("1.2", STRUCT_SPEC_VERSION, SPEC_STRUCTURE) or ("", ""))[1],
            "E2 说清是哪一份规范的版本不一致",
        ),
        (
            version_finding(STRUCT_SPEC_VERSION.lstrip("v"), STRUCT_SPEC_VERSION, SPEC_STRUCTURE) is None,
            "结构文档版本一致 → 不报 E2",
        ),
    ]

    bad = [name for ok, name in checks if not ok]
    if bad:
        for name in bad:
            print(f"self-test FAIL: {name}")
        return 1
    print(f"self-test ok（{len(checks)} 项）")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_ctx(args) -> Ctx:
    # 仓库根 = tools/validation/ 上溯两级；改目录层级要同步改这里。
    repo_root = Path(__file__).resolve().parents[2]
    ctx = Ctx(repo_root=repo_root)

    interface_path = (
        Path(args.interface).resolve()
        if args.interface
        else repo_root / "assets" / "interface.json"
    )
    if interface_path.is_file():
        ctx.interface_file = interface_path
        ctx.interface_dir = interface_path.parent
        try:
            data = load_jsonc(interface_path)
            if isinstance(data, dict):
                ctx.interface = data
            else:
                ctx.add(
                    "B0-parse-error",
                    "error",
                    interface_path,
                    None,
                    "interface.json 顶层必须是对象",
                )
        except Exception as e:  # noqa: BLE001
            ctx.add(
                "B0-parse-error", "error", interface_path, None, f"JSONC 解析失败：{e}"
            )
    elif args.interface:
        ctx.add(
            "B0-parse-error",
            "error",
            Path(args.interface),
            None,
            "--interface 指定的文件不存在",
        )

    pipeline_dirs: list[Path] = []
    task_dirs: list[Path] = []
    image_dirs: list[Path] = []

    if ctx.interface and ctx.interface_dir:
        base = ctx.interface_dir
        for res in to_list(ctx.interface.get("resource")):
            if not isinstance(res, dict):
                continue
            for p in to_list(res.get("path")):
                if not isinstance(p, str):
                    continue
                root = (base / p).resolve()
                if (root / "pipeline").is_dir():
                    pipeline_dirs.append(root / "pipeline")
                if (root / "tasks").is_dir():
                    task_dirs.append(root / "tasks")
                if (root / "image").is_dir():
                    image_dirs.append(root / "image")
        for imp in to_list(ctx.interface.get("import")):
            if isinstance(imp, str):
                ctx.import_files.append((base / imp).resolve())
        for arg in to_list((ctx.interface.get("agent") or {}).get("child_args")):
            if isinstance(arg, str) and arg.endswith(".py"):
                # 源码树里 interface 在 assets/ 下、agent 在仓库根下，打包后才同层，两个基准都试。
                # **仓库根优先**：`assets/agent` 是打包脚本留下的副本且在 .gitignore 里（第 464 行），
                # 先试 base 会解析到那份陈旧副本，改了 `agent/` 却校验旧代码，还没有任何提示。
                # 用 rglob：注册类可能落在 agent/ 的子目录（agent/shared/**），只 glob 顶层会静默漏掉，
                # 让 A2/A3/D2 与"注册名必须能在 agent 里搜到"这几条对这些模块不再生效。
                for cand in (repo_root / arg, base / arg):
                    if cand.is_file():
                        ctx.agent_files = sorted(cand.parent.rglob("*.py"))
                        break
                if ctx.agent_files:
                    break

    # 显式参数（追加扫描范围；同时作为"只报这些路径"的过滤条件）
    ctx.explicit = {
        "pipeline": [Path(p).resolve() for p in to_list(args.pipeline)],
        "tasks": [Path(p).resolve() for p in to_list(args.tasks)],
        "agent": [Path(p).resolve() for p in to_list(args.agent)],
        "image": [Path(p).resolve() for p in to_list(args.image)],
    }
    for extra in ctx.explicit["pipeline"]:
        pipeline_dirs.append(extra)
    for extra in ctx.explicit["tasks"]:
        task_dirs.append(extra)
    for extra in ctx.explicit["image"]:
        image_dirs.append(extra)
    explicit_agent: list[Path] = []
    for p in ctx.explicit["agent"]:
        explicit_agent.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])
    if explicit_agent:
        # 取并集，不是替换：载入集决定"引用能不能解析"，换掉它会让 `--agent agent/entrust.py`
        # 这种单文件调用把同一棵树里注册的自定义名全变成 A2-unknown-custom（实测 3 条假 error，
        # 其中 D1-unimported-module 还会误报"没被 main.py 导入"）。报告范围由 report().in_scope 决定，
        # 与载入集无关，所以这里放宽不会让输出变脏。
        ctx.agent_files = sorted({*ctx.agent_files, *explicit_agent})

    # 兜底默认布局
    if not pipeline_dirs and not args.pipeline:
        d = repo_root / "assets" / "resource" / "pipeline"
        if d.is_dir():
            pipeline_dirs.append(d)
    if not task_dirs and not args.tasks:
        d = repo_root / "assets" / "resource" / "tasks"
        if d.is_dir():
            task_dirs.append(d)
    if not image_dirs and not args.image:
        d = repo_root / "assets" / "resource" / "image"
        if d.is_dir():
            image_dirs.append(d)
    if not ctx.agent_files and not args.agent:
        d = repo_root / "agent"
        if d.is_dir():
            ctx.agent_files = sorted(d.rglob("*.py"))

    ctx.image_roots = image_dirs

    # 载入
    if pipeline_dirs or args.pipeline:
        load_pipeline(ctx, iter_json_files(pipeline_dirs))
    task_files = iter_json_files(task_dirs) if task_dirs else []
    if task_files:
        load_tasks(ctx, task_files)
    if ctx.agent_files:
        load_agents(ctx, ctx.agent_files)
    return ctx


def domain_enabled(args) -> dict[str, bool]:
    given = {
        "interface": args.interface is not None,
        "pipeline": args.pipeline is not None,
        "tasks": args.tasks is not None,
        "agent": args.agent is not None,
        "image": args.image is not None,
    }
    if not any(given.values()):
        return {k: True for k in given}
    return given


def report(ctx: Ctx, fmt: str, fail_on: str, ignores: set[str]) -> int:
    def in_scope(f: Finding) -> bool:
        """显式给了某域的路径时，只报落在这些路径里的发现（扫描范围仍是全树，保证引用可解析）。"""
        dom = {"A": "pipeline", "C": "image", "D": "agent"}.get(f.id[:1])
        paths = ctx.explicit.get(dom, []) if dom else []
        if not paths:
            return True
        fp = Path(f.file)
        for p in paths:
            if p.is_dir():
                try:
                    fp.relative_to(p)
                    return True
                except ValueError:
                    continue
            elif fp == p:
                return True
        return False

    findings = [f for f in ctx.findings if f.id not in ignores and in_scope(f)]
    findings.sort(
        key=lambda f: (f.file, f.line or 0, SEVERITY_ORDER.get(f.severity, 9), f.id)
    )
    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    if fmt == "json":
        print(
            json.dumps(
                {
                    # 报告自己声明按哪一版规范算的：结论会随规范版本变，读报告的人要能看到
                    "spec_version": NAMING_SPEC_VERSION,
                    "spec": NAMING_SPEC.as_posix(),
                    "findings": [f.__dict__ for f in findings],
                    "counts": counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif fmt == "github":
        for f in findings:
            level = {"error": "error", "warn": "warning", "info": "notice"}.get(
                f.severity, "notice"
            )
            loc = f"file={f.file}" + (f",line={f.line}" if f.line else "")
            print(f"::{level} {loc},title={f.id}::{f.message}")
        print(
            f"::notice::validate_refs: 命名规范 {NAMING_SPEC_VERSION} / error {counts['error']} / "
            f"warn {counts['warn']} / info {counts['info']}"
        )
    else:
        print(
            f"命名规范 {NAMING_SPEC_VERSION}（本脚本实现的版本；文档 {NAMING_SPEC.as_posix()}）"
        )
        cur = None
        for f in findings:
            if f.file != cur:
                cur = f.file
                print(f"\n{cur}")
            loc = f":{f.line}" if f.line else ""
            print(f"  [{f.severity.upper():5}] {f.id:<28}{loc} {f.message}")
        print(
            f"\nerror {counts['error']} / warn {counts['warn']} / info {counts['info']}"
        )

    threshold = {"error": 0, "warn": 1, "info": 2, "none": 99}.get(fail_on, 0)
    worst = min((SEVERITY_ORDER.get(f.severity, 9) for f in findings), default=99)
    return 1 if worst <= threshold else 0


def _ensure_printable_stdout() -> None:
    """重定向（CI、管道）时按 UTF-8 输出；真控制台交给 Python 的控制台 API。

    Windows 上管道默认是 GBK（`sys.stdout.encoding == "gbk"`），而读端（PowerShell 7、
    GitHub Actions）按 UTF-8 解，会出现乱码；反过来在真控制台上强行改编码也会花。
    """
    if sys.stdout is None:
        return
    # `except` 与抑制注释必须写在同一行：ruff 0.17 起，多行 `except (\n Exception\n):  # 抑制` 会把
    # 抑制当成"未使用"（RUF100）而把 S110/BLE001 报出来，那是两个版本行为不同造成的假红。
    try:
        if sys.stdout.isatty():
            return
    except Exception:  # noqa: BLE001, S110 - 探测 isatty 失败就当"不是终端"，继续往下改编码
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001, S110 - 老解释器/被包装过的 stdout 没有 reconfigure，保持原样即可
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_printable_stdout()

    parser = argparse.ArgumentParser(description="资源一致性校验（跨文件名字与约定）")
    parser.add_argument("--interface", help="interface.json 路径")
    parser.add_argument(
        "--pipeline", action="append", help="pipeline 目录/文件（可重复）"
    )
    parser.add_argument("--tasks", action="append", help="tasks 目录（可重复）")
    parser.add_argument(
        "--agent", action="append", help="agent 目录（可重复；目录递归扫描）"
    )
    parser.add_argument("--image", action="append", help="image 根目录（可重复）")
    parser.add_argument("--format", choices=["text", "github", "json"], default="text")
    parser.add_argument(
        "--fail-on", choices=["error", "warn", "info", "none"], default="error"
    )
    parser.add_argument(
        "--ignore", action="append", default=[], help="忽略的检查 id，逗号分隔，可重复"
    )
    parser.add_argument("--self-test", action="store_true", help="跑内置自检后退出")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    domains = domain_enabled(args)
    ctx = build_ctx(args)

    if domains["pipeline"]:
        check_a1_dangling(ctx)
        check_a2_custom(ctx)
        check_a3_custom_mapping(ctx)
        check_a4_shadowed(ctx)
        check_a5_spin_trap(ctx)
        check_a6_complement(ctx)
        check_a7_jumpback(ctx)
        check_a8_loop_guard(ctx)
        check_a9_ensure(ctx)
        check_a10_maxhit_in_loop(ctx)
        check_a11_expected(ctx)
        check_a12_focus(ctx)
        check_a13_naming(ctx)
        check_a13_references(ctx)
        check_a14_loop_fallback(ctx)
    # 规范版本与注解新鲜度：不属于任何域，给了 --pipeline 之类也要报
    check_spec_version(ctx)
    if domains["interface"]:
        check_interface(ctx)
        check_interface_paths(ctx)
    if domains["tasks"]:
        check_tasks(ctx)
    if domains["agent"]:
        check_agent(ctx)
    if domains["image"]:
        check_images(ctx)

    ignores = {
        i.strip() for group in args.ignore for i in group.split(",") if i.strip()
    }
    return report(ctx, args.format, args.fail_on, ignores)


if __name__ == "__main__":
    raise SystemExit(main())
