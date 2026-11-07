"""数据结构与通用小工具：Finding / Node / Reg / Option / Ctx、常量表、引用与名字解析。
只提供「读结构、比字段」的原语（to_list / parse_ref / walk_key / find_line），不做规则判断，
也不读文件；所有规则模块都从这里取类型与常量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TypeGuard



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

# node-naming.md §6（v3.2）：确认按钮统一以 `Confirm` 结尾，不许用 `ClickOk`。
OK_SUFFIX = "Ok"

# pipeline-structure.md §9：通用错误分支（base.json 的 BaseBranchError，:36-92）。
# 它只在"中央弹窗区出现确认"时命中，所以位置只影响谁先抢画面，不影响正常流程语义。
GENERIC_ERROR_BRANCH = "BaseBranchError"
# 会把「确认」点掉的候选：通用的确认节点名，或 expected 里含这个词的带动作节点。
CONFIRM_NODE_NAMES = frozenset({"BaseProbeConfirm", "BaseClickConfirm"})
CONFIRM_WORDS = ("确认",)

# pipeline-structure.md §10：识别预算上限。
# 失败候选也要付识别，列表越长单轮成本越高；超限通常是"把好几件事塞进一个列表"。
# v3.8 起 `Task*` 入口完全不判（作者裁定：入口的候选表由人工把关），A18 只剩 `Loop` 一档，
# 保留到存量 `*Loop` 节点退役；非入口根节点的 5 项上限由 A28 管。
MAX_NEXT_LOOP = 5
# pipeline-structure.md §10（v3.10；v3.3 起为 7 项）：非任务入口的 `next` 一律 ≤ 5 项。
# `Task*` 入口不判（v3.8）；框架兜底候选不计入长度——它们是每张表都该有的，不是"业务候选"。
NEXT_LIST_CAP = 5
NEXT_CAP_EXCLUDED = frozenset({"BaseBranchError", "BaseErrorNext"})

# --------------------------------------------------------------------------
# 规范版本
# --------------------------------------------------------------------------

# 两份规范各自的版本：脚本按哪一版实现。文档头部写着「版本号: vX.Y」；改了收录的规则就同步 +0.1。
# 不一致时报 E2，提醒人复核新规则要不要收进脚本——版本号只印在输出里而没人对，等于没有。
# 两份都收：结构类判据（A4/A5/A7/A8/A14…）按 pipeline-structure.md 实现，只对命名规范算版本
# 等于让结构文档改了规则没人提醒。
# v2.1：两份规范升到 v2.1（新增「Probe + 主动作 + 兜底动作 + 分支」四角色）。
# v3.0：四角色作废，改为「动作自环 + BaseErrorNext」。新增四条判据（A20–A23），见各自函数：
#   A20 `[jp]` 不许自环、A21 自环必须带 BaseErrorNext、A22 BaseErrorNext 必须在 next[0]、
#   A23 分节注释下面不许紧跟非通用 Probe。
# v3.1：Ensure 类别停用，删 A9/A16；新增 A25（Ensure 存量 warn）、A26（单探针内联 info）。
# v3.2：确认按钮统一 `*Confirm`、禁 `ClickOk`（A27 error）；`*Confirm` 必须排在
#   `BaseBranchError` 之前（`A15-error-branch-conflict` 由 warn 提为 error，结构文档 §9）。
# v3.3：非任务入口的 `next` 一律 ≤ 7 项、不计 BaseBranchError/BaseErrorNext（A28 warn，结构文档 §10）。
# v3.4：`*Init` 不再触发 A4——单次初始化必须在前段执行，不是"后面不可达"（结构文档 §八）。
# v3.5：`And` 组合多个子识别时要显式写 `box_index`（A29 warn，结构文档 §十一）。
# v3.6：A29 只管**有 `action` 的节点**——没 action 的框只进日志，换索引没有行为差别。
# v3.7：A4 只报**没有 `max_hit`** 的必然命中候选——带额度的（`[jp]`/`[once]` 一次性动作、
#   `EntrustDelegatingReward` 这种）"用尽即让位"，排前段是设计意图。
# v3.8：A18 对 `Task*` 入口**完全不判**（作者裁定：入口候选表由人工把关），只留 `Loop` 档。
# v3.9：A26 口径收窄为「单探针包装」（`all_of` 恰好一项）；新增 A30（多条件 `And` 里写内联
#   子识别对象 → warn，改独立 `Probe` 节点）。命名规范同步 v3.3。
# v3.10：A28 的列表长度上限由 7 项下调到 5 项（结构文档 §10，作者裁定；不计入项不变）。
# v4.0：动作节点默认不自环、不带 BaseErrorNext；自环只给成功率不高的动作，BaseErrorNext 只随自环出现（结构文档 §〇）。
NAMING_SPEC_VERSION = "v3.3"
NAMING_SPEC = Path("docs/zh_cn/develop/node-naming.md")
STRUCT_SPEC_VERSION = "v4.0"
STRUCT_SPEC = Path("docs/zh_cn/develop/pipeline-structure.md")


@dataclass(frozen=True)
class Spec:
    """一份"有版本号"的规范：文档标题里的名字、文件、脚本实现的版本、对应的常量名。"""

    label: (
        str  # 文档标题，也是头部注解里对它的称呼（「节点命名与分类」/「任务链结构」）
    )
    path: Path
    version: str  # 脚本按哪一版实现；改收录的规则就 +0.1
    const: str  # 常量名，写进 E2 的提示里（改哪一处）

    def older_than(self, other: str) -> bool:
        return version_tuple(self.version) < version_tuple(other)


SPEC_NAMING = Spec(
    "节点命名与分类", NAMING_SPEC, NAMING_SPEC_VERSION, "NAMING_SPEC_VERSION"
)
SPEC_STRUCTURE = Spec(
    "任务链结构", STRUCT_SPEC, STRUCT_SPEC_VERSION, "STRUCT_SPEC_VERSION"
)
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

IMAGE_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
BOUNDED_QUANTIFIER_RE = re.compile(r"\{\d+(?:,\d+)?\}")


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
    """按行读文件（带缓存；读不出来当空表，不抛）。"""
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
    """把标量或 None 统一成列表，已经是列表的原样返回。"""
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
    """列出节点的引用型字段：next 与 on_error（值为 None 也当没有）。"""
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

    用来判"两个守卫是不是同一个判据"：工作循环的守卫与它末项兜底的取反守卫
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
    """名字的第一个 CamelCase 词（`TrainEnterHome` -> `Train`、`BattleAuto` -> `Task`）。"""
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
