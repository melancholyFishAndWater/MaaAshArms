"""JSONC 读取：去注释与末尾逗号、解析成对象、枚举目录下的 JSON 文件。
口径以运行时为准——MaaFW 的 meojson 接受末尾逗号，所以这里也丢；真正的语法错误才抛。
只负责读，不猜节点语义。
"""

from __future__ import annotations

import json
from pathlib import Path


# --------------------------------------------------------------------------
# JSONC
# --------------------------------------------------------------------------


def _next_significant(text: str, start: int) -> str:
    """跳过空白与注释，返回下一个有意义的字符（判断逗号是不是末尾那个）。"""
    i, n = start, len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            return text[i]
    return ""


def strip_jsonc_comments(text: str) -> str:
    """移除 JSONC 注释与末尾逗号，保持结构（与 tools/validation/lib/validate_schema.py:31-* 同源）。

    末尾逗号（`"a": [1,]`）要一起丢掉：MaaFW 用的 meojson 接受它——实测 `post_bundle` 一个
    `"next": ["B",]` 的 pipeline 仍加载出 A / B 两个节点，只有真正的语法错误才 `json::open failed`。
    """
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
            elif char == "," and _next_significant(text, i + 1) in ("]", "}"):
                i += 1  # 末尾逗号：运行时接受，解析前丢掉
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
    """读一份 JSONC 文件并解析（注释与末尾逗号先丢掉）。"""
    return json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------


def iter_json_files(dirs: list[Path]) -> list[Path]:
    """展开目录：递归收 *.json / *.jsonc，去重并保持每个目录内的字典序。"""
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
