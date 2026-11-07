"""pipeline 规则 A1–A18：引用完整性、自定义名、可达性陷阱、命名规范与识别预算。
只读 ctx.nodes（节点定义）与 ctx.regs（注册名），不写文件、不调 report；每条规则
自己决定级别并把 Finding 交给 ctx.add，输出顺序由 report 统一决定。
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import (
    BOUNDED_QUANTIFIER_RE,
    CLASS_SUFFIXES,
    CLICK_SYNONYMS,
    CONFIRM_NODE_NAMES,
    CONFIRM_WORDS,
    Ctx,
    DYNAMIC_NODES,
    FOCUS_KEYS,
    GENERIC_ERROR_BRANCH,
    LEGACY_PREFIXES,
    MAX_NEXT_LOOP,
    Node,
    VERBS,
    _TAG_RE,
    complement_of,
    find_line,
    guard_probe,
    hits_always,
    iter_custom_refs,
    leading_word,
    parse_ref,
    ref_lists,
    to_list,
    walk_key,
    word_suffixes,
)



# --------------------------------------------------------------------------
# A. pipeline
# --------------------------------------------------------------------------


def check_a1_dangling(ctx: Ctx) -> None:
    """A1：next / on_error 里引用的节点必须已定义（含框架动态注入名）。"""
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
    """A2：custom_* 引用的注册名必须存在且种类对；反过来报没被任何节点引用的注册名。"""
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
        if any(f.id == "A0-parse-error" for f in ctx.findings):
            # 有 pipeline 文件解析不出来时，"引用集"是不完整的，不能下"没被引用"的结论
            # （2026-11-06：factory_fast.json 的末尾逗号被当语法错误，两个注册名误报 unused）
            return
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
    """`next` 里"必然命中且没有退路"的候选会把后面的候选永远挡住（pipeline-structure.md §八）。

    只报**没有 `max_hit`** 的那种：它每一轮都命中，后面那几项永远轮不到。

    两种不报的情况（v3.4 / v3.7）：

    - **带 `max_hit`**：额度用尽即让位，是显式的"先做一次/几次再继续"契约。
      `[jp]` / `[once]` 的一次性动作（`EntrustDelegatingReward`）与 `*Init` 都是这个写法，
      排在 `next` 前段是设计意图（例如 `EntrustBranchDelegating.next[0]`）。
    - **名字以 `Init` 结尾**：真初始化（写全局变量 / 读参数 / 清计数），本质单次必须执行、
      基本不需要 `reco`，规范本来就要求它放前段配 `max_hit: 1`（见结构文档「`Init` 的两类」）。
    """
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
                if name.endswith("Init") or target.data.get("max_hit") is not None:
                    continue
                ctx.add(
                    "A4-shadowed-candidate",
                    "error",
                    node.file,
                    find_line(node.file, node.name),
                    f"{node.name}.{key} 第 {idx + 1} 项 {name!r} 必然命中且没有 max_hit："
                    f"每一轮都会命中，其后 {len(items) - idx - 1} 项永远轮不到",
                )


def check_a5_spin_trap(ctx: Ctx) -> None:
    """A5：被 [JumpBack] 引用、必然命中且没有 next / max_hit 的节点会永久空转。"""
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
    """A6：一对互补守卫排在候选表里时，后面的候选永远轮不到（按 cut 位置报）。"""
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


def jumpback_descendants(
    root: str, nodes: dict[str, "Node"], limit: int = 64
) -> set[str]:
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


def jumpback_parent_loops(
    node: "Node", nodes: dict[str, "Node"], limit: int = 64
) -> set[str]:
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
                    target_file = (
                        ctx.nodes[name].file if name in ctx.nodes else node.file
                    )
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
    """A8：工作循环（*Loop）没有 recognition 守卫（旧写法，仅存量）。"""
    for node in ctx.nodes.values():
        if node.name.endswith("Loop") and not node.data.get("recognition"):
            ctx.add(
                "A8-loop-guard",
                "warn",
                node.file,
                find_line(node.file, node.name),
                f"{node.name} 是工作循环但没有 recognition（页面守卫），结构文档 §3 不建议",
            )


def check_a10_maxhit_in_loop(ctx: Ctx) -> None:
    """A10：循环自愈路径上的候选带 max_hit，额度耗尽只会被跳过、不报错。"""
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
    """A11：expected 里的正则必须能编译；带界的量词给 info（可能漏匹配）。"""
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
    """A12：focus 的键必须是框架认的消息类型（schema 的 FocusTemplateMap）。"""
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
    """A13：命名规范（动词、层级、参数键、文件命名）与旧前缀/同义词检查。"""
    generic = ctx.generic_nodes
    # 每个文件接受的"任务前缀"：出现 ≥2 次的第一个 CamelCase 词
    # （不看 entry 名，因为一个文件可能有 BattleAuto / TaskBattleReplay 这类入口名）
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
        is_branch = "Branch" in name
        if is_branch:
            if has_action:
                ctx.add(
                    "A13-branch-action",
                    "warn",
                    info.file,
                    line,
                    f"{name} 是分支节点却带 action（node-naming.md §3：分支只路由，不做动作）",
                )
            if not data.get("recognition"):
                ctx.add(
                    "A13-branch-recognition",
                    "warn",
                    info.file,
                    line,
                    f"{name} 是分支节点却没有 recognition（那等于无条件跳转，把 next 写在上一个节点里即可）",
                )
            if not to_list(data.get("next")):
                ctx.add(
                    "A13-branch-next",
                    "warn",
                    info.file,
                    line,
                    f"{name} 是分支节点却没有 next（没有可交出去的候选）",
                )
            continue
        for synonym in CLICK_SYNONYMS:
            if synonym in name:
                ctx.add(
                    "A13-click-synonym",
                    "warn",
                    info.file,
                    line,
                    f"{name} 用了同义写法 {synonym}（node-naming.md §6：统一 ClickConfirm / BaseClickCancel / ClickFinish）",
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
        if name.endswith("Loop"):
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
    """片段文件名与参数键的写法（node-naming.md §7/§8）。

    这几条都是**今天一条都不报**的护栏：写它们是为了以后别写错，不是为了现在挑错。
    判定只看结构，不看语义，所以规则本身也有限——见 README「功能取舍」。
    """
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
    """工作循环末项兜底取反守卫与循环守卫取同一个探针（结构文档 §3 末段）。

    结构文档把它写成硬规则（"兜底的探针不能与本 `Loop` 的守卫探针相同（方向相反也算）"），
    所以报 warn 而不是 info。v3.1 起兜底改写 `[JumpBack]BaseBranchError`，存量 `Loop` 里仍可能
    引用 `Ensure`——这里按**结构**判取反守卫（顶层 `inverse: true`），不再看名字前缀。
    确实合法的页面级循环（候选全是子守卫，全 miss 只可能意味着漂走）
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
        if not jump or not last:
            continue
        target = ctx.nodes.get(last)
        if target is None or not _is_inverse_guard(target.data):
            continue
        if guard_probe(target.data) != probe:
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


def _resolve_candidate(item, nodes: dict[str, Node]) -> tuple[str, dict] | None:
    """next/on_error 的一项 → (节点名, 节点数据)。

    `enabled: false`、内联定义（没有节点名）、定义不存在的候选都返回 None——它们不参与 §9 的位次判定。
    """
    name, _jump, enabled = parse_ref(item)
    if not enabled or not name:
        return None
    target = nodes.get(name)
    return (name, target.data) if target else None


def _has_confirm_mark(name: str | None, data: dict) -> bool:
    """这个候选自身是不是「确认按钮」：名字是通用确认节点，或 expected 里含「确认」。"""
    if name in CONFIRM_NODE_NAMES:
        return True
    for _path, value in walk_key(data, "expected"):
        for pattern in to_list(value):
            if isinstance(pattern, str) and any(w in pattern for w in CONFIRM_WORDS):
                return True
    return False


def _is_inverse_guard(data: dict) -> bool:
    """全取反守卫：按结构判，不看名字。

    两种写法都算：顶层 `inverse: true` 的节点（含取反的终点探针），以及识别配置写成对象、
    在对象里带 `inverse: true` 的探针。v3.1 起 `Ensure` 类别停用，不能再按 `Ensure` 名前缀判。
    """
    if data.get("inverse") is True:
        return True
    recognition = data.get("recognition")
    return isinstance(recognition, dict) and recognition.get("inverse") is True


def _clicks_confirm(
    name: str | None, data: dict, nodes: dict[str, Node], depth: int = 3
) -> bool:
    """这个候选会不会把「确认」点掉：自己有 `action`，且自身或引用的子识别里出现确认按钮。"""
    if "action" not in data:
        return False
    if _has_confirm_mark(name, data):
        return True
    if depth <= 0:
        return False
    for key in ("all_of", "any_of"):
        for item in to_list(data.get(key)):
            sub = _resolve_candidate(item, nodes)
            if sub and _clicks_confirm(sub[0], sub[1], nodes, depth - 1):
                return True
    return False


def check_a15_error_branch(ctx: Ctx) -> None:
    """通用错误分支的引用写法与位次（pipeline-structure.md §9）。

    三条都是**今天一条都不报**的护栏：写它们是为了接线时别挂错位置，不是为了现在挑错。

    - 引用必须带 `[JumpBack]`（候选位，"办完弹回我这里继续"）。
    - 排在取反守卫（顶层 `inverse: true`，或识别配置里带 `inverse`）之后等于没接：弹窗挡住页面时那个守卫必然命中。
    - 排在自己会点「确认」的候选之前会抢点，把正常流程记成"错误弹窗"。v3.2 起这条按 **error** 报
      （`*Confirm` 必须排在 `BaseBranchError` 之前）。

    只判同一个 next/on_error 列表内的先后；跨列表的冲突、"哪个模板在弹窗下仍会命中"判不了。
    """
    for node in ctx.nodes.values():
        for key, items in ref_lists(node.data):
            resolved = [(item, _resolve_candidate(item, ctx.nodes)) for item in items]
            slots = [
                i
                for i, (_item, cand) in enumerate(resolved)
                if cand is not None and cand[0] == GENERIC_ERROR_BRANCH
            ]
            line = find_line(node.file, node.name)
            for item, cand in resolved:
                if cand is None or cand[0] != GENERIC_ERROR_BRANCH:
                    continue
                if not parse_ref(item)[1]:
                    ctx.add(
                        "A15-error-branch-jumpback",
                        "warn",
                        node.file,
                        line,
                        f"{node.name}.{key} 引用了通用错误分支 {GENERIC_ERROR_BRANCH} 却没带 [JumpBack]"
                        f"（结构文档 §9：候选位）",
                    )
            if not slots:
                continue
            first = slots[0]
            for _item, cand in resolved[:first]:
                if cand is None:
                    continue
                if _is_inverse_guard(cand[1]):
                    ctx.add(
                        "A15-error-branch-shadowed",
                        "warn",
                        node.file,
                        line,
                        f"{node.name}.{key} 里 {GENERIC_ERROR_BRANCH} 排在取反守卫 {cand[0]} 之后："
                        f"弹窗挡住页面时那个守卫必然命中，错误分支永远轮不到（结构文档 §9）",
                    )
                    break
            for _item, cand in resolved[first + 1 :]:
                if cand is None:
                    continue
                if _clicks_confirm(cand[0], cand[1], ctx.nodes):
                    ctx.add(
                        "A15-error-branch-conflict",
                        "error",
                        node.file,
                        line,
                        f"{node.name}.{key} 里 {GENERIC_ERROR_BRANCH} 排在自己会点「确认」的候选 {cand[0]} 之前："
                        f"同一个确认会被当成错误弹窗点掉（结构文档 §9）",
                    )
                    break


def guard_probes(node: dict) -> set[str]:
    """本节点 `recognition` 直接引用到的探针名（`And` / `Or` 的字符串子项）。

    只取一层：`And[Probe]` 与 `Or[A, B]` 都算，内联对象子项跳过（拿不到名字）。
    """
    if node.get("recognition") not in ("And", "Or"):
        return set()
    subs = (
        node.get("all_of") if node.get("recognition") == "And" else node.get("any_of")
    )
    if not isinstance(subs, list):
        return set()
    return {
        _TAG_RE.sub("", s).strip()
        for s in subs
        if isinstance(s, str) and _TAG_RE.sub("", s).strip()
    }


def check_a17_guard_in_next(ctx: Ctx) -> None:
    """守卫探针在 `next` 里重复（pipeline-structure.md §10）。

    `next` 首项常常是守卫里那个探针（"刚说不在，再问一遍"），救回率高的该留、
    低的该删。这里只报"同一轮会问两遍"这个事实，留删由人按实测救回率定。
    """
    for node in ctx.nodes.values():
        probes = guard_probes(node.data)
        if not probes:
            continue
        for key, items in ref_lists(node.data):
            hits = []
            for item in items:
                name = parse_ref(item)[0]
                if name and name in probes:
                    hits.append(name)
            if not hits:
                continue
            ctx.add(
                "A17-guard-in-next",
                "info",
                node.file,
                find_line(node.file, node.name),
                f"{node.name}.{key} 含守卫已用过的探针 {', '.join(hits)}："
                f"同一轮会识别两遍；救回率 <25% 时删掉它（结构文档 §10）",
            )


def check_a18_next_length(ctx: Ctx) -> None:
    """`next` 列表长度上限（pipeline-structure.md §10）。

    失败候选也要付识别，列表越长单轮成本越高。上限按类别分：`Loop`（废弃）5 项。

    `Task*` 入口**完全不判**（v3.8）：入口的候选表由人工把关，规则只对非入口节点生效；
    非入口的 5 项上限由 `A28` 管。`Ensure` 自 v3.1 停用，也不再按 `Ensure` 名判。
    超限通常是"把好几件事塞进一个列表"，该拆 `Branch` 或子 `Loop`。
    """
    for node in ctx.nodes.values():
        items = to_list(node.data.get("next"))
        if not items:
            continue
        name = node.name
        if name.startswith("Task") or not name.endswith("Loop"):
            continue
        if len(items) <= MAX_NEXT_LOOP:
            continue
        ctx.add(
            "A18-next-length",
            "warn",
            node.file,
            find_line(node.file, name),
            f"{name}.next 有 {len(items)} 项（Loop 上限 {MAX_NEXT_LOOP}）："
            f"每项失败也要付识别；结构文档 §10 建议拆 Branch 或子 Loop",
        )
