"""pipeline 规则 A19–A30：desc 标记、自环与 BaseErrorNext、分节注释、Ensure 存量、
单探针内联、确认按钮命名、next 长度与 And 写法。
只读 ctx.nodes；与 A1–A18 同样：不写文件，只往 ctx 里加 Finding。
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import Counter

from .model import (
    Ctx,
    NEXT_CAP_EXCLUDED,
    NEXT_LIST_CAP,
    OK_SUFFIX,
    _TAG_RE,
    find_line,
    guard_probe,
    parse_ref,
    ref_lists,
    to_list,
)



def check_a28_next_list_cap(ctx: Ctx) -> None:
    """非任务入口的 `next` 不得超过 5 项（pipeline-structure.md §10，v3.10；v3.3 起为 7 项）。

    与 `A18` 分工：任务入口（`Task*`）由 `A18` 管（更严的 4 项），本规则跳过入口、管其余节点。
    计长度时**不计** `BaseBranchError` / `BaseErrorNext`——这两项是每张表都该留的框架兜底，
    不是"业务候选"，算进去会把正常表逼到超限。
    """
    for node in ctx.nodes.values():
        if node.name.startswith("Task"):
            continue
        items = to_list(node.data.get("next"))
        if not items:
            continue
        counted = [
            name
            for name, _jump, _enabled in (parse_ref(item) for item in items)
            if name not in NEXT_CAP_EXCLUDED
        ]
        if len(counted) <= NEXT_LIST_CAP:
            continue
        ctx.add(
            "A28-next-list-cap",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}.next 有 {len(counted)} 项（上限 {NEXT_LIST_CAP}，"
            f"不计 BaseBranchError / BaseErrorNext）：每项失败也要付识别；"
            f"结构文档 §10 建议拆 Branch",
        )


def check_a29_and_box_index(ctx: Ctx) -> None:
    """`And` 组合多个子识别时要显式写 `box_index`（pipeline-structure.md §十一，v3.5/v3.6）。

    `And` 的输出框默认取 `all_of[0]`（vendor `pipeline.schema.json:1520-1525`：
    「指定输出哪个子识别的识别框…可选，默认 0」）。组合里各项的框基本不重合，
    默认值意味着"落点跟着第一个子识别走"——这是隐式约定，调整 `all_of` 顺序就会
    悄悄换掉落点。写出来让读者看得见，也免得下次调序踩坑。

    只管**有 `action` 的节点**（v3.6）：框是被 `Click` / `target_offset` 当落点用的；
    纯 `Probe` / `Branch` / `Loop` 的框只进日志与事件，换个索引没有任何行为差别。
    """
    for node in ctx.nodes.values():
        if node.data.get("recognition") != "And":
            continue
        if "action" not in node.data:
            continue
        items = to_list(node.data.get("all_of"))
        if len(items) <= 1 or node.data.get("box_index") is not None:
            continue
        ctx.add(
            "A29-and-box-index",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name} 的 And 组合了 {len(items)} 个子识别却没写 box_index："
            f"取框默认 all_of[0]，显式写出来（0..{len(items) - 1}）免得调序时悄悄换落点",
        )


def check_a30_and_inline_sub(ctx: Ctx) -> None:
    """多条件 `And` 的子识别不要写成内联对象（node-naming.md v3.3、pipeline-structure.md 第十节）。

    `all_of` 里写 `{...}` 内联对象时，子识别没有名字——命名文档 §7 的注册名校验、`box_index`
    的落点指认（只能指到"第几项"）、单独复测，三样全部落空。多条件 `And` 的子识别一律拆成
    独立 `Probe` 节点、按名字引用。

    只报多条件（`all_of` 两项以上）里的对象项：单探针包装内联后引用方往往**就是**一个对象
    子项（`recognition: And` + `all_of: [{…}]`），那是 A26 认可的写法。
    """
    for node in ctx.nodes.values():
        if node.data.get("recognition") != "And":
            continue
        items = to_list(node.data.get("all_of"))
        if len(items) <= 1:
            continue
        inline = [item for item in items if isinstance(item, dict)]
        if not inline:
            continue
        ctx.add(
            "A30-and-inline-sub",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name} 的 And 有 {len(inline)} 项内联子识别（all_of 共 {len(items)} 项）："
            f"命名节点才带得上注册名校验 / box_index 落点指认 / 单独复测，建议拆成独立 Probe",
        )


# node-naming.md §6：`desc` 的方括号标记一共五个，含义固定。
DESC_MARKERS = ("end", "jp", "once", "max", "Loop")
# 「命中即终点」的终点节点：`[end]` 的 next 只收在这些节点上才算"不往下走"。
DESC_END_TARGETS = frozenset({"BaseTaskStop", "BaseTaskStopError"})


def desc_tags(desc: str) -> list[str]:
    """`desc` 开头的方括号标记序列。

    只认**开头连续**的那几个（`[once][jp] …`）：标记是前缀，后面才是正文（node-naming.md §6
    的五个标记、写法模板 `pipeline.snippets.schema.json` 的 `"desc": "[end] $1"` 都是前缀）。
    正文里当括号用的字（如"点击[确认]按钮"）不算标记，所以不参与判据。
    """
    tags: list[str] = []
    for match in re.finditer(r"\[([^\[\]]+)\]", desc):
        if match.start() != len("".join(f"[{t}]" for t in tags)):
            break
        tags.append(match.group(1))
    return tags


def check_a19_desc_markers(ctx: Ctx) -> None:
    """`desc` 的方括号标记（node-naming.md §6）。

    三条判据，都只查"标了标记、但节点字段与标记含义对不上"：

    - 五个固定标记之外的写法（含大小写不符）→ `A19-desc-marker-unknown`（warn）；
    - `[max]` 没有 `max_hit`、`[once]` 的 `max_hit` 不是 1、`[Loop]` 标在名字不含 `Loop`
      的节点上 → `A19-desc-marker-mismatch`（warn）；
    - `[end]` 却有 `next`，且 next 里不是只收终点节点 → `A19-desc-marker-end`（info：
      "这一段的终点"未必是 `BaseTaskStop`，人工复核；标了 `[end]` 又整条 next 收在
      `BaseTaskStop` / `BaseTaskStopError` 上的不报）。

    **不查"该标没标"**：`desc` 由模板生成，而 `[jp]` 按 §3 只是提示标记——本仓库大量一次性动作
    没带 `[jp]`，存量 9 个 `Ensure*` 节点（v3.1 停用）却全带上、又不是动作节点，所以缺标记不构成判据。
    """
    for node in ctx.nodes.values():
        desc = node.data.get("desc")
        if not isinstance(desc, str):
            continue
        tags = desc_tags(desc)
        if not tags:
            continue
        line = find_line(node.file, node.name)
        unknown = [tag for tag in tags if tag not in DESC_MARKERS]
        if unknown:
            ctx.add(
                "A19-desc-marker-unknown",
                "warn",
                node.file,
                line,
                f"{node.name}.desc 用了五个固定标记之外的 "
                f"{', '.join(f'[{tag}]' for tag in unknown)}：只认 "
                f"{' / '.join(f'[{m}]' for m in DESC_MARKERS)}（node-naming.md §6）",
            )
        if "max" in tags and node.data.get("max_hit") is None:
            ctx.add(
                "A19-desc-marker-mismatch",
                "warn",
                node.file,
                line,
                f"{node.name}.desc 标了 [max]（命中次数有上限）却没有 max_hit",
            )
        if "once" in tags and node.data.get("max_hit") != 1:
            ctx.add(
                "A19-desc-marker-mismatch",
                "warn",
                node.file,
                line,
                f"{node.name}.desc 标了 [once]（一个 task 内只做一次）但 max_hit="
                f"{node.data.get('max_hit')!r}：这个形态写 max_hit: 1",
            )
        if "Loop" in tags and "Loop" not in node.name:
            ctx.add(
                "A19-desc-marker-mismatch",
                "warn",
                node.file,
                line,
                f"{node.name}.desc 标了 [Loop]（工作循环）但名字里没有 Loop："
                f"工作层节点按 node-naming.md §3 命名",
            )
        if "end" in tags:
            names = [parse_ref(item)[0] for item in to_list(node.data.get("next"))]
            loose = [n for n in names if n and n not in DESC_END_TARGETS]
            if loose:
                ctx.add(
                    "A19-desc-marker-end",
                    "info",
                    node.file,
                    line,
                    f"{node.name}.desc 标了 [end]（命中即终点，不往下走）但 next 里还有 "
                    f"{', '.join(loose)}：要么收在 BaseTaskStop / BaseTaskStopError 上，要么不标 [end]",
                )


# pipeline-structure.md §0：自环动作的兜底节点，全局唯一一份（base.json）。
ERROR_NEXT = "BaseErrorNext"


def check_a20_jp_self_loop(ctx: Ctx) -> None:
    """`[jp]` 节点不许自环（pipeline-structure.md §0、node-naming.md §3）。

    `desc` 带 `[jp]` 的语义是「兜底动作，只补一次」。`next` 里出现自己就把"补一次"写成了
    "补到成功"：两种语义在日志里分不开，而且会绕过调用方（调用方以为它已经补完了）。

    要重复补就去掉 `[jp]`、按自环动作写（那会被 A21 要求配 `BaseErrorNext`）；
    要只补一次就 `next` 里不放自己。
    """
    for node in ctx.nodes.values():
        desc = node.data.get("desc")
        if not isinstance(desc, str) or "jp" not in desc_tags(desc):
            continue
        names = [parse_ref(item)[0] for item in to_list(node.data.get("next"))]
        if node.name in names:
            ctx.add(
                "A20-jp-self-loop",
                "error",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}.desc 带 [jp]（只补一次）但 next 里有自己："
                f"要重复补就去掉 [jp] 按自环动作写（并配 {ERROR_NEXT}），要补一次就别放自己",
            )


def check_a21_self_loop_error_next(ctx: Ctx) -> None:
    """自环动作必须引用 `BaseErrorNext`（pipeline-structure.md §0、§3、§5）。

    框架的 `timeout` 只限制「`next` 里一直找不到命中」的轮询。自环动作每轮都命中自己，
    `timeout` 永远不触发；`[JumpBack]` 弹回的 jp 节点反复命中也一样。所以自环必须另有一个
    "连续几轮没进展就报错"的出口，就是 `BaseErrorNext`（`LoopErrorByTimes` 改名而来）。

    实测样本：`EscortClickConfirm` 用同一个 box 连点 8 次、26.9 s，任务既不失败也不结束。
    """
    for node in ctx.nodes.values():
        names = [parse_ref(item)[0] for item in to_list(node.data.get("next"))]
        if node.name not in names or ERROR_NEXT in names:
            continue
        ctx.add(
            "A21-self-loop-without-error-next",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}.next 含自己（自环动作）但没有 {ERROR_NEXT}："
            f"自环每轮都命中自己，timeout 永远不会触发（结构文档 §0）",
        )


def check_a22_error_next_first(ctx: Ctx) -> None:
    """用了 `BaseErrorNext` 就必须排在 `next[0]`（pipeline-structure.md §0）。

    它是"这张表连续 N 轮没有别的节点成功"的判据，只有排在第一位才会在每一轮先被问一次；
    排到后面时，前面那些"总会命中"的候选会把它挡在门外，计数永远走不到上限——等于没写。

    排在第一位不影响正常路径：它只在别的候选都 miss 的那一轮才递增计数，正常命中的轮次
    由 `_BaseErrorNextSink` 清零。
    """
    for node in ctx.nodes.values():
        names = [parse_ref(item)[0] for item in to_list(node.data.get("next"))]
        if ERROR_NEXT not in names or names[0] == ERROR_NEXT:
            continue
        ctx.add(
            "A22-error-next-not-first",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}.next 里 {ERROR_NEXT} 排在第 {names.index(ERROR_NEXT) + 1} 位："
            f"必须放 next[0]，否则前面的候选会把它挡在门外、计数走不到上限（结构文档 §0）",
        )


# 分节注释：`/* -------------------- 探测 -------------------- */` 这种带横线的。
SECTION_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)
# 允许注释下面紧跟 Probe 的分节名：通用探测（`base.json` 与各文件头部那段）。
GENERIC_PROBE_SECTION = "通用探测"


def is_probe_shape(data: dict) -> bool:
    """结构上的探测节点：有 recognition、无 action、无 next（node-naming.md §3）。"""
    return (
        "recognition" in data and "action" not in data and not to_list(data.get("next"))
    )


def is_auxiliary(data: dict) -> bool:
    """纯辅助节点：没有 `next`（只做一下，不往下走）。

    这类节点（`EscortClickDeliveryButton`、`BaseClickHomeButton`、`BaseMoveUp` 这种）是给主干节点当"补一刀"
    或工具用的，读的时候是跟着引用它的节点一起看的，所以**节点前面不要求写注释**；
    主干节点（有 `next` 的：动作 / 分支 / 任务入口）才是分节注释要覆盖的对象。
    """
    return not to_list(data.get("next"))


def preceding_comment(text: str, key_start: int) -> tuple[str, str] | None:
    """某个 key 前面紧挨着的那条注释 → (类型, 正文)；没有返回 None。

    只认**紧挨着**的：从 key 往前跳过空白与上一个节点的结尾（`},` 之类），
    遇到的第一个注释才算"这个节点自己的注释"。中间隔着别的节点就不算——那是上一段的注释。
    """
    before = text[:key_start]
    # 从后往前找最后一个注释与最后一个 "}" 的位置，注释必须在 "}" 之后才算紧挨着本节点
    blocks = list(re.finditer(r"/\*(.*?)\*/", before, re.DOTALL))
    lines = list(re.finditer(r"//([^\n]*)$", before, re.MULTILINE))
    last_block = blocks[-1] if blocks else None
    last_line = lines[-1] if lines else None
    cands = []
    if last_block is not None:
        cands.append((last_block.end(), "block", last_block.group(1)))
    if last_line is not None:
        cands.append((last_line.end(), "line", last_line.group(1)))
    if not cands:
        return None
    end, kind, body = max(cands)
    # 注释与 key 之间不能有别的节点（有 `"名字": {` 就说明隔了一个节点）
    gap = before[end:]
    if re.search(r'"\s*:', gap):
        return None
    return kind, body


def check_a23_section_comment(ctx: Ctx) -> None:
    """注释的覆盖与位置（人工可读性约定，pipeline-structure.md §0）。

    三条判据：

    - `A23-section-comment-probe`（warn）：分节注释（`/* ---- 探测 ---- */` 这种带横线的）
      下面紧跟的是**非通用** `Probe`。分节开头应放动作 / 分支 / 任务入口；
      `/* ---- 通用探测 ---- */` 下面允许 `Probe`；非通用 `Probe` 放到用到它的节点后面。
    - `A24-node-comment-missing`（warn）：**主干节点**（有 `next`）前面没有注释。
      辅助节点（没有 `next`，如 `EscortClickDeliveryButton` / `BaseClickHomeButton` / `BaseMoveUp`）不要求——
      它们是跟着引用它的节点一起读的，逐个加注释只会把文件撑长。

    注释没紧挨着节点（中间隔了别的节点）时 `preceding_comment()` 返回 None，同样报 `A24`：
    那种情况等于这个节点的注释丢了、上一段多了半句，修法是把它挪到本节点上方。

    只查带横线的分节注释与节点级注释；文件头那种多行 INFO 注释（写版本注解的）不在此列。
    """
    for path in ctx.pipeline_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # key 在文本里的位置（同名 key 取第一个；pipeline 里不允许重名，A0 已管）
        key_pos: dict[str, int] = {}
        for m in re.finditer(r'^\s*"([^"]+)"\s*:', text, re.MULTILINE):
            key_pos.setdefault(m.group(1), m.start())
        for match in SECTION_COMMENT_RE.finditer(text):
            body = match.group(1)
            if "----" not in body:
                continue
            rest = text[match.end() :]
            key = re.search(r'^\s*"([^"]+)"\s*:', rest, re.MULTILINE)
            if not key:
                continue
            name = key.group(1)
            node = ctx.nodes.get(name)
            if node is None or not is_probe_shape(node.data):
                continue
            section = body.strip().strip("-").strip()
            if GENERIC_PROBE_SECTION in body:
                continue
            ctx.add(
                "A23-section-comment-probe",
                "warn",
                path,
                find_line(path, name),
                f"分节注释「{section}」下面紧跟的是探测节点 {name}："
                f"分节开头应放动作 / 分支 / 任务入口；"
                f"非通用 Probe 放到用到它的节点后面（通用探针请归到「{GENERIC_PROBE_SECTION}」段）",
            )

        for name, start in key_pos.items():
            node = ctx.nodes.get(name)
            # 不比较路径：`Node.file` 可能是相对路径（自检合成树就是），比 resolved 也会失配。
            # 这份文本里出现了这个 key、且 ctx 里有同名节点，就说明它定义在本文件（重名由 A0 管）。
            if node is None:
                continue
            found = preceding_comment(text, start)
            if is_auxiliary(node.data):
                continue  # 纯辅助节点不要求注释
            if found is None:
                ctx.add(
                    "A24-node-comment-missing",
                    "warn",
                    path,
                    find_line(path, name),
                    f"主干节点 {name}（有 next）前面没有注释："
                    f"每个主干节点前写一行说明它做什么；"
                    f"纯辅助节点（没有 next）不要求",
                )


# v3.1：Ensure 类别停用。存量 9 个节点已于 2026-10-31 全部删除（A25 = 0）；判据保持 warn
# 不用 error，留作回归护栏。
BANNED_ENSURE_RE = re.compile(r"Ensure[A-Z]")


def check_a25_ensure_banned(ctx: Ctx) -> None:
    """Ensure 类别停用（node-naming.md v3.1、pipeline-structure.md v3.1）。

    2026-10-30 与作者定：不再新增 `Ensure` 节点，"保证当前处于某页"改由调用方 `next` 里的
    `Branch`（自带 recognition）+ 入口动作 + `[JumpBack]BaseBranchError` 兜底表达。
    存量 9 个节点已于 2026-10-31 全部删除（`A25` = 0）；判据保持 warn 不用 error。
    """
    for node in ctx.nodes.values():
        if not BANNED_ENSURE_RE.search(node.name):
            continue
        ctx.add(
            "A25-ensure-banned",
            "warn",
            node.file,
            find_line(node.file, node.name),
            f"{node.name}：v3.1 起 Ensure 类别停用，此节点待删；"
            f"改用调用方 Branch + 入口动作 + [JumpBack]BaseBranchError",
        )


def _string_mention_counts(ctx: Ctx) -> Counter[str]:
    """所有节点数据里"整串等于某节点名"的字符串出现次数。

    `next` / `on_error` / `roi` / `target` / `all_of` 都算引用；`[JumpBack]X` 去掉标签后
    按 X 计。用来判"只被一处引用"——比只数 `next` 保守，`roi` / `target` 里用到的探针
    不会被误判成可以内联。
    """
    counts: Counter[str] = Counter()

    def walk(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            name = _TAG_RE.sub("", value).strip()
            if name:
                counts[name] += 1

    for node in ctx.nodes.values():
        walk(node.data)
    return counts


def _external_mentions(ctx: Ctx) -> set[str]:
    """interface / tasks / agent 里提到过的节点名（保守：出现就算被外部引用）。

    这些文件不归 pipeline 域，但 `pipeline_override`、`next` 覆写、agent 里按名字取节点
    都会引用到；只要文本里出现这个名字，就不建议内联。
    """
    chunks: list[str] = []
    if ctx.interface is not None:
        chunks.append(json.dumps(ctx.interface, ensure_ascii=False))
    for option in ctx.options.values():
        chunks.append(json.dumps(option.data, ensure_ascii=False))
    for _path, preset in ctx.presets:
        chunks.append(json.dumps(preset, ensure_ascii=False))
    for path in ctx.agent_files:
        with contextlib.suppress(OSError):
            chunks.append(path.read_text(encoding="utf-8"))
    text = "\n".join(chunks)
    return {name for name in ctx.nodes if name in text}


def _reference_kinds(ctx: Ctx) -> dict[str, list[tuple[str, str, bool]]]:
    """每个节点被引用的位置：`[(引用方, 字段, 是否 JumpBack)]`。

    `recognition` 的 `all_of` / `any_of` 记为 `recognition`，用来识别单探针包装；
    `next` / `on_error` 按字段名记。
    """
    refs: dict[str, list[tuple[str, str, bool]]] = {}

    def add(name: str | None, consumer: str, kind: str, jump: bool) -> None:
        if name:
            refs.setdefault(name, []).append((consumer, kind, jump))

    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            for item in items:
                name, jump, enabled = parse_ref(item)
                if enabled:
                    add(name, node.name, key, jump)
        for key in ("all_of", "any_of"):
            for item in to_list(node.data.get(key)):
                name, _jump, enabled = parse_ref(item)
                if enabled:
                    add(name, node.name, "recognition", False)
    return refs


def check_a26_single_consumer_probe(ctx: Ctx) -> None:
    """只被一处引用的探针可以内联（node-naming.md v3.3「单探针内联」）。

    判据同时满足，宁少勿多：

    - 纯探针形状：有 `recognition`、无 `action`、无 `next`；
    - 全库只被一处引用（`next` / `on_error` / `roi` / `target` / `all_of` 都算）；
    - 唯一那处是**单探针包装**——`And all_of` 恰好一项（`guard_probe()` 只认这种），识别直接
      落到引用方，内联前后语义等价；或者是"引用方没有 recognition / action、next 只有
      这一项"的纯路由。**多条件 `And`（`all_of` 两项以上）不算**：子识别要留成有名字的
      `Probe`（v3.3，见 `check_a30_and_inline_sub`）；
    - 探针自身没有 `custom_recognition` / `custom_action`；
    - 不是 `Task` 开头的入口；
    - 唯一引用不带 `[JumpBack]`；
    - 不被 interface / tasks / agent 提到。

    只按 `next` 候选内联会改变语义（`Loop` / `Branch` 的候选是"命中就转"，不是守卫），
    所以那类不报——这正是"拿不准就不报"。引用方是待删的 `Ensure` 或 `Task` 入口时也不报。
    """
    refs = _reference_kinds(ctx)
    counts = _string_mention_counts(ctx)
    external = _external_mentions(ctx)
    for name, node in ctx.nodes.items():
        data = node.data
        if not is_probe_shape(data):
            continue
        if "Probe" not in name or name.startswith("Task"):
            continue
        if data.get("custom_recognition") is not None:
            continue
        if data.get("custom_action") is not None:
            continue
        if counts.get(name, 0) != 1:
            continue
        if name in external:
            continue
        entries = refs.get(name, [])
        if len(entries) != 1:
            continue
        consumer_name, kind, jump = entries[0]
        if jump:
            continue
        consumer = ctx.nodes.get(consumer_name)
        if consumer is None:
            continue
        if BANNED_ENSURE_RE.search(consumer_name) or consumer_name.startswith("Task"):
            continue
        if kind == "recognition":
            if guard_probe(consumer.data) != name:
                continue
        elif kind == "next":
            # 纯路由：引用方自己没有识别与动作，next 只有这一项，内联才不改变派发语义
            if "recognition" in consumer.data or "action" in consumer.data:
                continue
            if to_list(consumer.data.get("next")) != [name]:
                continue
        else:
            continue
        ctx.add(
            "A26-single-consumer-probe",
            "info",
            node.file,
            find_line(node.file, name),
            f"{name} 只被 {consumer_name} 引用一次：可内联进引用它的节点 recognition",
        )


def check_a27_click_ok(ctx: Ctx) -> None:
    """确认按钮的命名（node-naming.md §6 v3.2）。

    `A27-click-ok-banned`（error）：节点名以 `Ok` 结尾（`EscortClickOk`、`FactoryClickSkipOk`）。
    确认按钮统一以 `Confirm` 结尾（`BaseClickConfirm`、`EscortClickConfirm`），
    同一个按钮在各任务里叫法要一致。

    确认按钮的**位次**不在这里判：`*Confirm` 必须排在 `BaseBranchError` 之前，
    由 `A15-error-branch-conflict`（v3.2 起 error）管，见 pipeline-structure.md §9。
    """
    for node in ctx.nodes.values():
        if "ClickOk" in node.name or node.name.endswith(OK_SUFFIX):
            ctx.add(
                "A27-click-ok-banned",
                "error",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}：确认按钮统一命名 *ClickConfirm（如 BaseClickConfirm），"
                f"不要用 ClickOk / …Ok 后缀（node-naming.md §6）",
            )
