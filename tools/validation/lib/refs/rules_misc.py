"""其余三个域：B（interface / tasks 片段）、C（image 与 agent 模板）、D（agent 注册与
import）、E（规范版本与文件头注解）。
只读文件与 ctx；C 域会把 agent 里 JTemplateMatch 的字面量模板和 image/ 下的文件对齐，
不在 image 根之外找图。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .model import (
    ANNOTATION_RE,
    ANNOTATION_SPECS,
    Ctx,
    DOC_VERSION_RE,
    DYNAMIC_NODES,
    IMAGE_EXT_RE,
    PIPELINE_SNIPPETS,
    SPECS,
    Spec,
    find_line,
    lines_of,
    parse_ref,
    to_list,
    version_finding,
    version_tuple,
    walk_key,
)

# 注册副作用的标志：`@AgentServer.custom_recognition("Xxx")` / `custom_action(...)`。
# 只用来判"这个模块没被导入会不会真丢注册"，不判名字与类名（那是 D2 的事）。
AGENT_REGISTER_RE = re.compile(r"@AgentServer\.custom_(?:recognition|action)\b")



def list_images(roots: list[Path]) -> list[Path]:
    """递归列出 image 根下的图片文件（按路径排序）。"""
    out: list[Path] = []
    for root in roots:
        if root.is_dir():
            out.extend(sorted(p for p in root.rglob("*") if p.is_file()))
    return out


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
        stale = [
            (spec, version, date)
            for spec, version, date in annotations_of(text)
            if version_tuple(f"v{version}") < version_tuple(spec.version)
        ]
        if not stale:
            continue
        lines = lines_of(path)
        for spec, version, date in stale:
            line = next(
                (
                    i
                    for i, l in enumerate(lines, 1)
                    if any(s is spec and v == version for s, v, _d in annotations_of(l))
                ),
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


def check_custom_params(ctx: Ctx, *, pipeline: bool, interface: bool, tasks: bool) -> None:
    """A32/B9：自定义参数只能写在对应类型为 Custom 的识别/动作上。"""
    def check(name: str, base: dict, patch: dict, file: Path, label: str, rule: str) -> None:
        for kind in ("recognition", "action"):
            key = f"custom_{kind}_param"
            nested = patch.get(kind)
            has_flat = key in patch
            has_nested = isinstance(nested, dict) and isinstance(nested.get("param"), dict) and key in nested["param"]
            if not (has_flat or has_nested):
                continue
            effective = patch.get(kind, base.get(kind))
            if isinstance(effective, dict) and "type" not in effective and kind in patch:
                effective = base.get(kind)
            typ = effective.get("type") if isinstance(effective, dict) else effective
            if typ == "Custom":
                continue
            ctx.add(rule, "error", file, find_line(file, label),
                    f"{label} 的节点 {name}.{key} 要求 {kind}: Custom；当前为 {typ or '默认 DirectHit/DoNothing'}")

    if pipeline:
        for name, node in ctx.nodes.items():
            check(name, {}, node.data, node.file, name, "A32-custom-param-type")

    def visit(value, file: Path, label: str) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, file, label)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key == "pipeline_override" and isinstance(child, dict):
                    for target, patch in child.items():
                        node = ctx.nodes.get(target)
                        if node is not None and isinstance(patch, dict):
                            check(target, node.data, patch, file, label, "B9-custom-param-type")
                else:
                    visit(child, file, label)

    if interface and ctx.interface_file and ctx.interface:
        visit(ctx.interface, ctx.interface_file, "interface")
    if tasks:
        for name, option in ctx.options.items():
            visit(option.data, option.file, name)
        for file, preset in ctx.presets:
            visit(preset, file, str(preset.get("name") or file.name))


def check_override_targets(ctx: Ctx, override, file: Path, label: str) -> None:
    """B 域：interface / tasks 里 pipeline_override 的目标节点必须已在 pipeline 中定义。"""
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
    """B 域：option 的 cases / default_case 形状与默认项约定（B7）。"""
    def one(name: str, data: dict, file: Path) -> None:
        line = find_line(file, name)
        cases = to_list(data.get("cases"))
        default_case = data.get("default_case")
        if default_case is not None and cases and isinstance(cases[0], dict):
            first = cases[0].get("name")
            if isinstance(default_case, list):
                # checkbox（SelectedCases）：default_case 是默认勾选的 case 名数组，约定
                # cases[0] 须在其中（与 switch/select「默认项必须是 cases[0]」同义）。
                if first not in default_case:
                    ctx.add(
                        "B7-default-case",
                        "warn",
                        file,
                        line,
                        f"option {name} 的 checkbox default_case={default_case!r} 不含 cases[0]={first!r}；"
                        "默认勾选项应含 cases[0]",
                    )
            else:
                if first != default_case:
                    ctx.add(
                        "B7-default-case",
                        "warn",
                        file,
                        line,
                        f"option {name} 的 default_case={default_case!r} 与 cases[0]={first!r} 不一致；"
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
    """B 域：interface.json 的任务入口、option 分组、preset 与 task 引用接线。"""
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
    """B 域：interface 引用的路径（资源目录、child_args 等）是否存在。"""
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
    """B 域：tasks 片段里 option / preset 的校验入口（转发到各 check_b*）。"""
    check_option_shape(ctx)


# --------------------------------------------------------------------------
# C. image
# --------------------------------------------------------------------------


def find_image(roots: list[Path], value: str) -> Path | None:
    """模板值 → 命中的路径（目录模板以 `/` 结尾，返回目录本身）。"""
    for root in roots:
        target = root / value.rstrip("/")
        if value.endswith("/"):
            if target.is_dir():
                return target
        elif target.is_file():
            return target
    return None


TEMPLATE_CALL = "JTemplateMatch"


def _is_template_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == TEMPLATE_CALL
    if isinstance(func, ast.Attribute):
        return func.attr == TEMPLATE_CALL
    return False


def _template_values(call: ast.Call) -> list[str | None]:
    """`JTemplateMatch(...)` 的模板实参：字面量原样给出，变量 / f-string 记 None。"""
    arg = next((kw.value for kw in call.keywords if kw.arg == "template"), None)
    if arg is None and call.args:
        arg = call.args[0]
    if arg is None:
        return []
    items = arg.elts if isinstance(arg, (ast.List, ast.Tuple, ast.Set)) else [arg]
    return [
        item.value
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
        else None
        for item in items
    ]


def template_calls_in_source(text: str) -> list[tuple[int, int, str, list[str | None]]]:
    """一处 `JTemplateMatch(...)`：`(起始行, 结束行, 归属, 模板值)`。

    `归属` 是最近的类/方法名（`类.方法`）；值里的 None = 变量 / f-string，
    静态查不出它指向哪张图片——那是"查不了"，不是"不存在"。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    scopes = [
        (node.lineno, getattr(node, "end_lineno", node.lineno), node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    calls: list[tuple[int, int, str, list[str | None]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_template_call(node.func):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        inside = sorted(
            scope for scope in scopes if scope[0] <= start and end <= scope[1]
        )
        owner = ".".join(scope[2] for scope in inside[:2]) or "模块级"
        values = _template_values(node)
        if values:
            calls.append((start, end, owner, values))
    return calls


def template_call_spans(files: list[Path]) -> dict[Path, list[tuple[int, int]]]:
    """每个 agent 文件里 `JTemplateMatch(...)` 占的行区间：C1 的字面量扫描要排掉这些行。"""
    spans: dict[Path, list[tuple[int, int]]] = {}
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        spans[path] = [
            (start, end) for start, end, _owner, _values in template_calls_in_source(text)
        ]
    return spans


def collect_template_refs(
    ctx: Ctx,
) -> list[tuple[Path, str, str, int | None, bool]]:
    """(文件, 说明, 模板值, 行号, 是否已被 C6 接管)"""
    refs: list[tuple[Path, str, str, int | None, bool]] = []
    for node in ctx.nodes.values():
        for _path, value in walk_key(node.data, "template"):
            for v in to_list(value):
                if isinstance(v, str):
                    refs.append(
                        (
                            node.file,
                            node.name,
                            v,
                            find_line(node.file, node.name),
                            False,
                        )
                    )
    lit_re = re.compile(
        r"[\"']([A-Za-z0-9_][A-Za-z0-9_\-. ]*(?:/[A-Za-z0-9_\-. ]+)*/?)[\"']"
    )
    spans = template_call_spans(ctx.agent_files)
    for path in ctx.agent_files:
        lines = lines_of(path)
        for i, line in enumerate(lines, 1):
            covered = any(a <= i <= b for a, b in spans.get(path, []))
            for m in lit_re.finditer(line):
                s = m.group(1)
                if IMAGE_EXT_RE.search(s) or s.endswith("/"):
                    refs.append((path, "agent 字面量", s, i, covered))
    return refs


def check_images(ctx: Ctx) -> None:
    """C 域：模板引用是否存在、有没有没被引用的图、扩展名与元素命名（C1–C5）。"""
    if not ctx.image_roots:
        return
    refs = collect_template_refs(ctx)
    used: set[Path] = set()
    used_dirs: set[Path] = set()
    for file, owner, value, line, covered in refs:
        target = find_image(ctx.image_roots, value)
        if target is None:
            # C6 已经在 `JTemplateMatch(...)` 那一行报过同样的缺失，这里不重复报；
            # 但"用到了这张图"仍然要记，否则 C2 会把这些图当没被引用。
            if not covered:
                ctx.add(
                    "C1-missing-template",
                    "error",
                    file,
                    line,
                    f"{owner} 引用的模板不存在：{value!r}",
                )
            continue
        used.add(target)
        if not value.endswith("/"):
            continue
        used_dirs.add(target)
        if not any(p.is_file() for p in target.iterdir()):
            ctx.add(
                "C4-empty-folder-template",
                "error",
                file,
                line,
                f"{owner} 的目录模板 {value!r} 里没有图片",
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


def check_agent_templates(ctx: Ctx) -> None:
    """agent 侧自定义 TemplateMatch 的模板图片（C6 error / C7 info）。

    只解析 `JTemplateMatch(...)` 自己的实参：字面量要求图片存在；变量 / f-string 记一条
    info，说明静态查不了。C1 的 agent 字面量扫描会跳过这些调用，同一处不会报两遍。
    """
    if not ctx.image_roots:
        return
    for path in ctx.agent_files:
        if path.suffix != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line, _end, owner, values in template_calls_in_source(text):
            if any(value is None for value in values):
                ctx.add(
                    "C7-agent-template-dynamic",
                    "info",
                    path,
                    line,
                    f"{owner} 的 JTemplateMatch 模板不是字面量（变量 / f-string）："
                    f"静态查不了图片是否存在",
                )
            for value in values:
                if value is None:
                    continue
                if find_image(ctx.image_roots, value) is None:
                    ctx.add(
                        "C6-agent-template-missing",
                        "error",
                        path,
                        line,
                        f"{owner} 的 JTemplateMatch 引用的图片不存在：{value!r}",
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
    """D 域：agent 模块是否被 main.py 间接导入、注册名后缀与类名是否一致（D1/D2）。"""
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
    # 哪些模块里有注册副作用（`@AgentServer.custom_*`）：只有这类模块"没被导入"才会真丢注册
    # （实测 `agent/shared/direct.py` 只是还没用上的工具模块，没有任何装饰器，报 D1 是假红）
    registers: set[str] = set()
    for name, path in local.items():
        deps: set[str] = set()
        for line in lines_of(path):
            if AGENT_REGISTER_RE.search(line.split("#", 1)[0]):
                registers.add(name)
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
        if name in reachable:
            continue
        if name not in registers:
            # 没有注册副作用：没接进 main 也不会丢注册（可能只是还没用上的工具模块）。
            # D1 的理由是"装饰器不执行 -> 注册不生效"，没有装饰器就无从谈起。
            continue
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
