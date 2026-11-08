"""`temp/` 的保留策略：删掉超过 N 天的文件，再清掉空目录。

只处理"文件年龄"，不猜哪些目录是缓存：忽略目录由配置给（见 `config.py` 的默认值与
`tools/cleanup/config.json`）。不跟随符号链接 / 目录联接；被忽略的子树整个不进入。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .fs import human_size, is_link


@dataclass
class CleanReport:
    removed_files: list[Path] = field(default_factory=list)
    removed_dirs: list[Path] = field(default_factory=list)
    freed_bytes: int = 0
    failed: list[tuple[Path, OSError]] = field(default_factory=list)

    @property
    def freed_text(self) -> str:
        return human_size(self.freed_bytes)


def _under_ignored(rel: Path, ignore: set[str]) -> bool:
    return any(part in ignore for part in rel.parts)


def clean(
    root: Path,
    days: int,
    ignore: set[str],
    dry_run: bool,
    now: float | None = None,
) -> CleanReport:
    """删 `root` 下 mtime 早于 `days` 天的文件，再自底向上删空目录。

    `dry_run=True` 时只统计，不落盘（空目录也只看当前状态）。
    """
    report = CleanReport()
    cutoff = (now if now is not None else time.time()) - days * 86400
    visited: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        current = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            child = current / name
            if name in ignore or is_link(child):
                continue  # 忽略目录的整棵子树都不进入；链接不跟随
            keep.append(name)
        dirnames[:] = keep
        visited.append(current)

        for name in filenames:
            path = current / name
            try:
                stat = path.lstat()
            except OSError as exc:
                report.failed.append((path, exc))
                continue
            if stat.st_mtime >= cutoff:
                continue
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    report.failed.append((path, exc))
                    continue
            report.removed_files.append(path.relative_to(root))
            report.freed_bytes += stat.st_size

    # 自底向上删空目录；`visited` 已经排除了被忽略的子树
    for current in sorted(visited, key=lambda p: len(p.parts), reverse=True):
        if current == root:
            continue
        rel = current.relative_to(root)
        if _under_ignored(rel, ignore):
            continue
        try:
            if any(current.iterdir()):
                continue
            if not dry_run:
                current.rmdir()
        except OSError as exc:
            report.failed.append((current, exc))
            continue
        report.removed_dirs.append(rel)

    return report
