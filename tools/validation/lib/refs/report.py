"""命令行：解析参数、按域跑规则、输出 text / github / json 并决定退出码。
入口 main 只做编排（pipeline → 规范版本 → interface / tasks / agent / image → C6/C7），
不含任何规则判断；report 负责 --ignore 过滤与 --fail-on 判定。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from .jsonc import iter_json_files, load_jsonc
from .loaders import load_agents, load_pipeline, load_tasks
from .model import Ctx, Finding, NAMING_SPEC, NAMING_SPEC_VERSION, to_list
from .rules_misc import (
    check_agent,
    check_agent_templates,
    check_images,
    check_interface,
    check_interface_paths,
    check_spec_version,
    check_tasks,
)
from .rules_pipeline_a import (
    check_a10_maxhit_in_loop,
    check_a11_expected,
    check_a12_focus,
    check_a13_naming,
    check_a13_references,
    check_a14_loop_fallback,
    check_a15_error_branch,
    check_a17_guard_in_next,
    check_a18_next_length,
    check_a1_dangling,
    check_a2_custom,
    check_a3_custom_mapping,
    check_a4_shadowed,
    check_a5_spin_trap,
    check_a6_complement,
    check_a7_jumpback,
    check_a8_loop_guard,
)
from .rules_pipeline_b import (
    check_a19_desc_markers,
    check_a20_jp_self_loop,
    check_a21_self_loop_error_next,
    check_a22_error_next_first,
    check_a23_section_comment,
    check_a25_ensure_banned,
    check_a26_single_consumer_probe,
    check_a27_click_ok,
    check_a28_next_list_cap,
    check_a29_and_box_index,
    check_a30_and_inline_sub,
    check_a31_unreferenced_node,
)
from .selftest import self_test


SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_ctx(args) -> Ctx:
    # 仓库根 = tools/validation/ 上溯两级；改目录层级要同步改这里。
    """按参数装配 Ctx：解析 interface、定位各目录、载入 pipeline / tasks / agent 与显式路径。"""
    repo_root = Path(__file__).resolve().parents[4]
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
    """五个域是否启用：一个域参数都没给时全开，否则只开显式给了的。"""
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
    """按 --ignore 过滤后输出 findings（text / github / json），并返回退出码。"""
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
    # 用 `contextlib.suppress` 而不是 try/except：抑制注释的放置规则会随 ruff 版本变
    # （0.17 起多行 `except (\n Exception\n):` 后再挂抑制，会被判成"抑制未使用"并连带报
    # S110/BLE001；被格式化工具折行后又会重新变红），suppress 两个版本都不报，也不怕折行。
    with contextlib.suppress(Exception):
        # 探测 isatty 失败就当"不是终端"，继续往下改编码
        if sys.stdout.isatty():
            return
    with contextlib.suppress(Exception):
        # 老解释器/被包装过的 stdout 没有 reconfigure，保持原样即可
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    """命令行入口：--self-test 走自检，否则装配 ctx、按域跑规则、交给 report。"""
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
        check_a10_maxhit_in_loop(ctx)
        check_a11_expected(ctx)
        check_a12_focus(ctx)
        check_a13_naming(ctx)
        check_a13_references(ctx)
        check_a14_loop_fallback(ctx)
        check_a15_error_branch(ctx)
        check_a17_guard_in_next(ctx)
        check_a18_next_length(ctx)
        check_a19_desc_markers(ctx)
        check_a20_jp_self_loop(ctx)
        check_a21_self_loop_error_next(ctx)
        check_a22_error_next_first(ctx)
        check_a23_section_comment(ctx)
        check_a25_ensure_banned(ctx)
        check_a26_single_consumer_probe(ctx)
        check_a27_click_ok(ctx)
        check_a28_next_list_cap(ctx)
        check_a29_and_box_index(ctx)
        check_a30_and_inline_sub(ctx)
        check_a31_unreferenced_node(ctx)
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
    if domains["image"] or domains["agent"]:
        check_agent_templates(ctx)

    ignores = {
        i.strip() for group in args.ignore for i in group.split(",") if i.strip()
    }
    return report(ctx, args.format, args.fail_on, ignores)
