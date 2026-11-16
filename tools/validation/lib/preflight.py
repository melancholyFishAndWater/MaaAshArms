"""本地完整校验编排：与 CI 对齐并补齐工具自检，不驱动设备或访问遥测。

仅 --list 列出计划；正常执行会同步锁定依赖，所有失败/依赖跳过均使退出码为 1。
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPORT_PATH = "{requirements_export}"
_RUFF_RULES = "F401,F841,E722,FURB162,S110,PLW1510,BLE001,RUF100"


@dataclass(frozen=True)
class Step:
    """可执行步骤；requires 引用先前步骤名称，不满足时记为 SKIP 而非 PASS。"""

    name: str
    command: tuple[str, ...]
    requires: tuple[str, ...] = ()
    compare_requirements: bool = False


def build_steps() -> list[Step]:
    """构造完整校验计划，不执行命令、不创建文件。"""
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    npm = shutil.which(npm_name) or npm_name
    maa_tools = str(Path("node_modules") / "@nekosu" / "maa-tools" / "dist" / "index.mjs")
    python = ("uv", "run", "--locked", "python")
    validation = ("tools/validation/cli.py",)
    local_validation = (sys.executable, *validation)
    py_deps = ("python-deps",)
    node_deps = ("node-deps",)
    return [
        Step("python-deps", ("uv", "sync", "--locked")),
        Step("node-deps", (npm, "ci")),
        Step("requirements-export", (
            "uv", "export", "--locked", "--no-default-groups", "--no-emit-project",
            "--no-hashes", "--no-header", "--format", "requirements-txt",
            "--output-file", _EXPORT_PATH,
        ), py_deps, compare_requirements=True),
        # These checks only require the interpreter running preflight, not third-party packages.
        Step("check-python", (*local_validation, "check-python")),
        Step("maa-tools", ("node", maa_tools, "check"), node_deps),
        Step("validate-schema", (*python, *validation, "validate-schema",
            "--schema-dir", "deps/tools", "--resource-dirs", "assets/resource",
            "--exclude-dirs", "assets/resource/announcement", "assets/resource/tasks",
            "--interface-files", "assets/interface.json", "--task-dirs", "assets/resource/tasks"), py_deps),
        Step("validate-refs", (*local_validation, "validate-refs")),
        Step("refs-selftest", (*local_validation, "validate-refs", "--self-test")),
        Step("pytest", (*python, "-m", "pytest", "-q", "tests"), py_deps),
        Step("ruff", ("uv", "run", "--locked", "ruff", "check", "tools", "--select", _RUFF_RULES), py_deps),
        # pyright's wrapper uses npx and does not depend on npm ci's node_modules.
        Step("pyright", ("node", "tools/validation/check_pyright.mjs")),
        Step("sentry-selftest", (*python, "-m", "tools.sentry.cli", "selftest"), py_deps),
        Step("sentry-offline", (*python, *validation, "check-sentry-offline"), py_deps),
        Step("perf-bench-selftest", (*python, "tools/perf/cli.py", "bench", "--selftest"), py_deps),
        Step("perf-rounds-selftest", (*python, "tools/perf/cli.py", "rounds", "--selftest"), py_deps),
        Step("flow-help", (*python, "tools/drawing/cli.py", "flow", "--help"), py_deps),
        Step("cases-help", (*python, "tools/drawing/cli.py", "cases", "--help"), py_deps),
        Step("logs-help", (*python, "tools/logs/cli.py", "inspect", "--help"), py_deps),
        Step("markdownlint", (npm, "run", "lint:md"), node_deps),
        Step("git-diff-check", ("git", "diff", "--check")),
    ]


def compare_requirements(exported: Path, expected: Path) -> bool:
    """比较临时导出与受版本控制的 requirements；只归一化文本换行。"""
    actual = exported.read_text(encoding="utf-8")
    baseline = expected.read_text(encoding="utf-8")
    if actual == baseline:
        return True
    print("requirements.txt 与锁定依赖的发布导出不一致：", flush=True)
    print("".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), actual.splitlines(keepends=True),
        fromfile=str(expected), tofile="locked requirements export",
    )), end="", flush=True)
    return False


def _command_text(command: Sequence[str]) -> str:
    """显示用的引号，不交给 shell 解释。"""
    return subprocess.list2cmdline(list(command))


def run_steps(steps: Sequence[Step], repo: Path = REPO_ROOT) -> int:
    """依次运行并汇总；子进程直接继承 stdout/stderr，绝不吞掉错误。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    results: dict[str, str] = {}
    details: list[tuple[str, str, float, str]] = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="maa-preflight-") as directory:
        exported = Path(directory) / "requirements.txt"
        for index, step in enumerate(steps, 1):
            command = tuple(str(exported) if arg == _EXPORT_PATH else arg for arg in step.command)
            print(f"\n[{index}/{len(steps)}] {step.name}: {_command_text(command)}", flush=True)
            missing = [name for name in step.requires if results.get(name) != "PASS"]
            if missing:
                reason = "依赖未通过：" + ", ".join(missing)
                results[step.name] = "SKIP"
                details.append((step.name, "SKIP", 0.0, reason))
                print(f"SKIP {reason}", flush=True)
                continue
            step_started = time.perf_counter()
            reason = ""
            try:
                completed = subprocess.run(command, cwd=repo, env=env, check=False)
                passed = completed.returncode == 0
                if not passed:
                    reason = f"exit {completed.returncode}"
                elif step.compare_requirements:
                    passed = compare_requirements(exported, repo / "requirements.txt")
                    if not passed:
                        reason = "requirements 导出内容不一致"
            except OSError as error:
                passed = False
                reason = f"启动命令或读取文件失败：{error}"
            status = "PASS" if passed else "FAIL"
            duration = time.perf_counter() - step_started
            results[step.name] = status
            details.append((step.name, status, duration, reason))
            print(f"{status} {step.name} ({duration:.2f}s) {reason}", flush=True)
    print("\n=== preflight 汇总 ===", flush=True)
    for name, status, duration, reason in details:
        print(f"{status:4} {name} ({duration:.2f}s) {reason}", flush=True)
    counts = {status: sum(row[1] == status for row in details) for status in ("PASS", "FAIL", "SKIP")}
    print(f"PASS {counts['PASS']} / FAIL {counts['FAIL']} / SKIP {counts['SKIP']}；"
          f"总耗时 {time.perf_counter() - started:.2f}s", flush=True)
    return 1 if counts["FAIL"] or counts["SKIP"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="只列出全部命令和依赖，不执行")
    args = parser.parse_args(argv)
    steps = build_steps()
    if args.list:
        for index, step in enumerate(steps, 1):
            suffix = f" [requires: {', '.join(step.requires)}]" if step.requires else ""
            comparison = " [compare with requirements.txt]" if step.compare_requirements else ""
            print(f"{index:2}. {step.name}: {_command_text(step.command)}{suffix}{comparison}")
        return 0
    return run_steps(steps)


if __name__ == "__main__":
    raise SystemExit(main())
