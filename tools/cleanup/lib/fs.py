"""文件系统小工具：判链接、清只读位、递归删除、体积格式化。

只被 `pytest_cache` 与 `temp_clean` 用；这里不放任何"删什么"的策略。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link(path: Path) -> bool:
    """符号链接或目录联接（junction）——两者都不能跟进去删。"""
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def make_writable(path: Path) -> None:
    """清掉只读位：Windows 上带只读位的文件/目录删不掉，报的是"拒绝访问"。"""
    try:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def remove_tree(path: Path, errors: list[tuple[Path, OSError]]) -> bool:
    """删掉 `path` 整个子树，不跟随符号链接/联接（链接本身删掉，指向的内容不动）。

    全部删干净返回 True；任何一个子项失败就记进 `errors` 并返回 False，
    父目录不再尝试 rmdir（否则会多报一条 ENOTEMPTY 掩盖真正的原因）。
    """
    ok = True
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        errors.append((path, exc))
        return False

    for entry in entries:
        child = Path(entry.path)
        try:
            if entry.is_dir(follow_symlinks=False):
                ok = remove_tree(child, errors) and ok
            else:
                make_writable(child)
                os.unlink(child)
        except OSError as exc:
            errors.append((child, exc))
            ok = False

    if not ok:
        return False
    make_writable(path)
    try:
        os.rmdir(path)
    except OSError as exc:
        errors.append((path, exc))
        return False
    return True


def human_size(num_bytes: float) -> str:
    """字节数 → 便于读的 `1.2 MB`；只用于输出。"""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"
