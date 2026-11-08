"""删 pytest 留在仓库里的缓存目录：`.pytest_cache` 与 `pytest-cache-files-*`。

为什么会有后者：pytest 9 的 `_pytest/cacheprovider.py:_make_cachedir` 先建临时目录再 rename 成
`.pytest_cache`，rename 被拒（父目录权限、杀软/索引占用）时临时目录留下，而收尾的
`shutil.rmtree(..., ignore_errors=True)` 会吞掉失败。两者都在 `.gitignore` 里，git 看不到，
但文件系统里一直在。扫法与跳过名单见模块常量。
"""

from __future__ import annotations

import os
from pathlib import Path

from .fs import is_link, remove_tree

#: pytest 的正规缓存目录名（`cacheprovider.py` 的 `cache_dir_default`）。
CACHE_DIR = ".pytest_cache"
#: pytest 建缓存目录时用的临时目录前缀（`cacheprovider.py:_make_cachedir`）。
CACHE_TMP_PREFIX = "pytest-cache-files-"

#: 不进去找的目录：第三方代码与构建产物，里面即使有缓存也不该由本工具处理；
#: `.python-runtime*` 单个就有几万个文件，扫一遍要几秒。
SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "build",
    "install",
    "deps",
    "temp",
    "debug",
    ".ruff_cache",
}
SKIP_DIR_PREFIXES = (".python-runtime",)


def is_pytest_cache_name(name: str) -> bool:
    """目录名是不是 pytest 留下的缓存目录。"""
    return name == CACHE_DIR or name.startswith(CACHE_TMP_PREFIX)


def _skipped(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(SKIP_DIR_PREFIXES)


def find_pytest_caches(root: Path) -> list[Path]:
    """按名字找出 root 下所有 pytest 缓存目录，返回绝对路径，按路径排序。"""
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        keep: list[str] = []
        for name in dirnames:
            path = Path(dirpath) / name
            if is_pytest_cache_name(name):
                found.append(path)
            elif not _skipped(name) and not is_link(path):
                keep.append(name)
        dirnames[:] = keep
    return sorted(found)


def clean(root: Path, dry_run: bool) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    """删 root 下所有 pytest 缓存目录，返回 `(删掉的, 失败的)`；路径都是相对 root 的。"""
    removed: list[Path] = []
    failed: list[tuple[Path, OSError]] = []
    for path in find_pytest_caches(root):
        rel = path.relative_to(root)
        if dry_run:
            removed.append(rel)
            continue
        errors: list[tuple[Path, OSError]] = []
        if remove_tree(path, errors):
            removed.append(rel)
        else:
            failed.append((rel, errors[0][1] if errors else OSError("未知原因")))
    return removed, failed
