#!/usr/bin/env python3
"""本仓库 Python 工具链的静态检查（纯标准库）。

    python tools/validation/check_python.py                 # 全量检查
    python tools/validation/check_python.py --format github  # CI 注解格式
    python tools/validation/check_python.py --root agent     # 只查某个根

检查四项，都是**这个仓库特有的、别的工具查不出来**的东西：

  1. 语法           —— `ast.parse` 每个文件，报出文件:行:原因。
  2. 工具路径引用   —— 扫 .md/.yml/.json/.mjs/.py 里 `tools/<...>.<ext>` 形的引用，
                       断言目标存在。2026-10 把校验/绘图脚本收进 tools/validation
                       与 tools/drawing 时，靠这条兜住了漏改的引用。
  3. `__doc__` 直取 —— 报 `__doc__` 后直接跟属性访问的写法（`__doc__` 可为 None，
                       直接用会抛 AttributeError；模块内用 `module_summary(__doc__)`）。
  4. 仓库根定位深度 —— 报出每个脚本声明的上溯级数与它实际所处深度不符的情况。
                       **改了目录层级最容易静默搞错的就是这个**：默认产出目录会落到
                       `tools/<子目录>/temp/...` 而不是仓库根 `temp/...`。

退出码：0 = 无 error；1 = 有 error；2 = 用法/环境错误。
`warn` 不影响退出码。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 扫描范围：只查自己写的 Python，不动 vendored 的运行时/依赖（那里有几千个文件）。
DEFAULT_ROOTS = ("tools", "agent", "scripts", "tests")
# 这些目录一律跳过（名字里出现任一片段就跳）
SKIP_PARTS = (
    "__pycache__", "node_modules", ".git", ".python-runtime", "install", "build",
    "deps", "temp", "ffmpeg", "docs",
)
SKIP_PREFIXES = (".python-runtime",)

# 路径引用检查认这些扩展名（内联在下面的正则里），避免把 `python -m tools.sentry.cli`
# 之类模块路径当文件。前面的 `(?<![\w/.])` 很关键：schema 快照放在 `deps/` 下的同名
# `tools/...` 子目录里，不排除会把那些引用误报成仓库自己的 tools/ 路径。
PATH_REF_RE = re.compile(r"(?<![\w/.])tools/[A-Za-z0-9_./-]+\.(?:py|mjs|json|md|txt)")
# 说明性文字里的占位路径（例如本文件解释检查项时给的示例），不当引用检查
PATH_REF_SKIP_RE = re.compile(r"tools/(?:<[^>]*>|\*)/")
# 文本类文件才扫（二进制/大 JSON 不必）
TEXT_EXT = (".py", ".md", ".yml", ".yaml", ".json", ".mjs", ".mts", ".txt")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Finding:
    level: str          # error | warn | info
    check: str
    message: str
    path: str = ""
    line: int = 0
    def as_github(self) -> str:
        if self.path:
            kind = "error" if self.level == "error" else "warning"
            return f"::{kind} file={self.path},line={self.line},title={self.check}::{self.message}"
        return f"::{self.level} title={self.check}::{self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0

    def add(self, level: str, check: str, message: str, path: str | Path = "", line: int = 0) -> None:
        self.findings.append(Finding(level, check, message, str(path), line))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]


def iter_py_files(roots: list[str]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            rel_parts = path.relative_to(REPO_ROOT).parts
            if any(part in SKIP_PARTS or part.startswith(SKIP_PREFIXES) for part in rel_parts):
                continue
            out.append(path)
    return sorted(set(out))


def iter_text_files() -> list[Path]:
    """扫路径引用用的文本文件集合：仓库根下的说明/配置 + tools/ 全部 + .agents/。"""
    out: list[Path] = []
    for pattern in ("*.md", "*.json", "*.yml", "*.yaml"):
        out.extend(sorted(REPO_ROOT.glob(pattern)))
    for folder in ("tools", ".agents", ".github", "docs/zh_cn/develop", "agent"):
        base = REPO_ROOT / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TEXT_EXT:
                continue
            rel_parts = path.relative_to(REPO_ROOT).parts
            if any(part in SKIP_PARTS for part in rel_parts):
                continue
            out.append(path)
    return sorted(set(out))


# ---------------------------------------------------------------- 检查 1：语法


def check_syntax(files: list[Path], report: Report) -> None:
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.add("error", "syntax", f"读不了：{exc}", path.relative_to(REPO_ROOT))
            continue
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.add("error", "syntax", f"{exc.msg}", path.relative_to(REPO_ROOT), exc.lineno or 0)


# ---------------------------------------------------------------- 检查 2：工具路径引用


def check_path_refs(report: Report) -> None:
    for path in iter_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in PATH_REF_RE.finditer(line):
                ref = match.group(0)
                if PATH_REF_SKIP_RE.search(ref):
                    continue
                if (REPO_ROOT / ref).exists():
                    continue
                report.add(
                    "error", "tool-path",
                    f"引用了不存在的路径 {ref}",
                    rel, lineno,
                )


# ---------------------------------------------------------------- 检查 3：__doc__ 直取


def check_doc_access(files: list[Path], report: Report) -> None:
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue   # 语法错已由检查 1 报过
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "__doc__":
                report.add(
                    "error", "doc-access",
                    f"`__doc__.{node.attr}` 会因 __doc__ 为 None 而抛 AttributeError；"
                    "改用 report_common.module_summary(__doc__)",
                    path.relative_to(REPO_ROOT), node.lineno,
                )


# ---------------------------------------------------------------- 检查 4：仓库根定位深度


ROOT_DEPTH_RE = re.compile(
    r"RepoRoot\s*=\s*(?P<expr>.+)", re.IGNORECASE,
)


def _depth_of(path: Path) -> int:
    """文件相对仓库根的层数（= 需要上溯的级数）。

    `<仓库根>/tools/drawing/<文件>` 是两层，所以返回 2；`<仓库根>/<文件>` 返回 0。
    """
    return len(path.relative_to(REPO_ROOT).parts) - 1


def _parents_index(expr: str) -> int | None:
    m = re.search(r"parents\[(\d+)\]", expr)
    return int(m.group(1)) if m else None


def _dirname_steps(expr: str) -> int | None:
    n = len(re.findall(r"dirname\(", expr))
    return n - 1 if n >= 1 else None


def check_repo_root_depth(files: list[Path], report: Report) -> None:
    """找出每个文件里声称"这是仓库根"的表达式，核对它的上溯级数对不对。

    只认**有名字**的（`REPO_ROOT` / `repo_root`）——无名表达式无法判断意图。
    对 `dirname` 链数 `dirname(` 个数减一；对 `parents[N]` 取 N。
    """
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        want = _depth_of(path)
        for lineno, line in enumerate(source.splitlines(), 1):
            m = re.search(r"\b(?P<name>[A-Za-z_]*repo_root)\s*=\s*(?P<expr>.+)$", line, re.IGNORECASE)
            if not m:
                continue
            expr = m.group("expr")
            if "__file__" not in expr:
                continue
            got = _parents_index(expr)
            how = "parents[N]"
            if got is None:
                got = _dirname_steps(expr)
                how = "dirname 链"
            if got is None:
                continue
            if got != want:
                report.add(
                    "error", "repo-root",
                    f"{m.group('name')} 用 {how} 上溯 {got} 级，但文件在仓库根下第 {want} 层"
                    f"（应为 {want}）；算错会让默认产出目录落到 tools/ 子目录里",
                    path.relative_to(REPO_ROOT), lineno,
                )


# ---------------------------------------------------------------- 入口


def run(roots: list[str]) -> Report:
    report = Report()
    py_files = iter_py_files(roots)
    report.files_checked = len(py_files)
    check_syntax(py_files, report)
    check_path_refs(report)
    check_doc_access(py_files, report)
    check_repo_root_depth(py_files, report)
    return report


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="本仓库 Python 工具链的静态检查（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", action="append", default=None,
                    help=f"要扫描的目录，可重复（默认 {'/'.join(DEFAULT_ROOTS)}）")
    ap.add_argument("--format", choices=("console", "github"), default="console",
                    help="输出格式（github = CI 注解）")
    return ap


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and hasattr(stream, "buffer"):
            import io
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    report = run(args.root or list(DEFAULT_ROOTS))

    if args.format == "github":
        for finding in report.findings:
            print(finding.as_github())
        counts = {level: sum(1 for f in report.findings if f.level == level) for level in ("error", "warn", "info")}
        print(f"::notice::check_python: error {counts['error']} / warn {counts['warn']} / info {counts['info']}"
              f"（{report.files_checked} 个文件）")
    else:
        for finding in report.findings:
            where = f"{finding.path}:{finding.line}" if finding.path else "-"
            print(f"[{finding.level.upper():5}] {finding.check:11} {where}  {finding.message}")
        counts = {level: sum(1 for f in report.findings if f.level == level) for level in ("error", "warn", "info")}
        print(f"\n{report.files_checked} 个 Python 文件；"
              f"error {counts['error']} / warn {counts['warn']} / info {counts['info']}")

    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
