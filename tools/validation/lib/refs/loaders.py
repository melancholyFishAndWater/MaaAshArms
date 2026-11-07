"""把 pipeline / tasks / agent 三类输入读进 Ctx：只做解析与文件级事实（重复节点、
重复 option、注册名与类名、文件归属），不做规则判断；解析失败的记 A0/B0 后跳过。
"""

from __future__ import annotations

import re
from pathlib import Path

from .jsonc import load_jsonc
from .model import Ctx, Node, Option, Reg, find_line, lines_of, to_list



def load_pipeline(ctx: Ctx, paths: list[Path]) -> None:
    """把 pipeline 文件读进 ctx.nodes；同名节点记 A0-duplicate-node，文件记入 pipeline_files。"""
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
    """把 tasks 片段的 option / preset 读进 ctx；同名 option 记 B0-duplicate-option。"""
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
    """扫 agent 源码里的 @AgentServer.custom_* 注册，记 name / kind / 类名 / 行号。"""
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
