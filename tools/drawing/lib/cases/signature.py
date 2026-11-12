"""两个代码摘要：`parse_impl`（读日志/解释数据的那批模块）与 `raster_impl`（画像素的那批）。

增量绘制要能回答"这次脚本改动影不影响这张图"，而"模块 → 哪些图"没有可靠的对照表；
能机械判定的只有两件事：

* **像素**只由 `flowlog/{layout,paint,draw,media,render}.py` 决定 → 取这五个文件的摘要，
  变了就整批重画（数据一字不差，图仍可能不同，没有别的办法）。
* **数据**由解析与选材决定 → 那批文件的摘要只作**快路**开关：它没变、输入与参数也没变时，
  解析结果必然相同（`logparse.parse_log()` 只读日志正文/窗口/task/px），可以不读日志直接跳过；
  它变了就下来读一遍再比数据步，数据步全同照样不重画。

摘要按**规范化后的 AST** 算（去 docstring、不带行号），所以改注释、改文档字符串不会重画——
本仓库这类改动很多，按原始字节算会让每次改注释都整批重画。
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .pack import REPO_ROOT

# 读日志、拼数据的那批模块（改了要读一遍日志重新比数据，不一定重画）
_PARSE_IMPL_FILES = (
    "tools/drawing/lib/flowlog/logparse.py",
    "tools/drawing/lib/flowlog/synthesize.py",
    "tools/drawing/lib/flowlog/agentmark.py",
    "tools/drawing/lib/flowlog/model.py",
    "tools/drawing/lib/flowlog/notes.py",
    "tools/drawing/lib/flowlog/stale.py",
    "tools/drawing/lib/flowlog/constants.py",
    "tools/drawing/lib/flowlog/paths.py",
    # 「疑似过时」的节点 → 模块对照与 git 查询都在这里，它变了标记就可能变
    "tools/_pipeline_state.py",
)

# 画像素的那批模块（改了整批重画：数据没变，像素会变）
_RASTER_IMPL_FILES = (
    "tools/drawing/lib/flowlog/layout.py",
    "tools/drawing/lib/flowlog/paint.py",
    "tools/drawing/lib/flowlog/draw.py",
    "tools/drawing/lib/flowlog/media.py",
    "tools/drawing/lib/flowlog/render.py",
)


def _normalized(source: bytes) -> str:
    """源码 → 去掉 docstring、不含行号的 AST 文本（只差注释/空行的两份源码得到同一串）。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            # 全去掉会得到空 body，补一个 Pass 让 AST 合法（`pass` 不影响运行语义）
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def module_digest(path: str | Path) -> str:
    """一个 `.py` 的摘要。读不了、语法不完整（改到一半）时退回按原始字节算，不抛异常。"""
    data = Path(path).read_bytes()
    try:
        text: str = _normalized(data)
    except (SyntaxError, ValueError, RecursionError):
        text = data.decode("utf-8", errors="replace")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _group_digest(paths: Iterable[str]) -> str:
    """一组文件的摘要：文件名 + 各自摘要，按文件名排序（与遍历顺序无关）。"""
    items = []
    for name in sorted(paths):
        try:
            items.append([name, module_digest(REPO_ROOT / name)])
        except OSError:
            # 文件不在（分支里删掉/改名）：记成空串，摘要照样变
            items.append([name, ""])
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def parse_signature() -> str:
    return _group_digest(_PARSE_IMPL_FILES)


@lru_cache(maxsize=1)
def raster_signature() -> str:
    return _group_digest(_RASTER_IMPL_FILES)


def code_stages() -> dict[str, str]:
    """`trace` 里的两个实现步（每次进程只算一遍）。"""
    return {"parse_impl": parse_signature(), "raster_impl": raster_signature()}


def _git(root: Path, *args: str) -> str:
    """跑一条 git 命令，失败一律当空串（没有 git、不是仓库都不该让画图中断）。"""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


@lru_cache(maxsize=1)
def repo_state() -> dict[str, str]:
    """仓库状态：只取 `HEAD`（提交号）。

    「疑似过时」比的是"日志所属版本之后该节点的模块有没有提交"，所以只有提交会改标记。
    提交一变就重新读一遍日志核对标记（数据没变照样跳过），工作区里改了资源定义但没提交
    不触发——那种改动要重画用 `--force`。这里**不含** pipeline 文件内容：修资源时几乎每次都
    会改它们，把内容算进来等于每改一行都重读一遍日志。
    """
    head = _git(REPO_ROOT, "rev-parse", "HEAD")
    return {"head": head[:12]}
