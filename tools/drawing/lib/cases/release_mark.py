"""「已修复待发版」：提交里已经 `Fixes` 这个 issue，但新版本还没打 tag / 没推。

判据与 `issue-resolve` 手动运行**同一段提交**（`<最新 tag>..HEAD`；最新 tag 之后没有提交时
退回 `<次新 tag>..<最新 tag>`），实现直接复用
`tools.sentry.lib.issue_resolve.fixed_in_unreleased()`——两边的口径必须一致，否则会出现
"图说没修、关 issue 说修了"。

Sentry 上这类 issue 仍是 `unresolved`（带 `Fixes` 的提交还没有发布版本可关联），所以
`--telemetry-bulk` 照旧会画它们：图本身还是失败现场，只是名字上多一段"已经修了、等发版"，
不必再去翻提交。

**只改文件名，不改图**：绘图逻辑与产物像素都没变，所以这里不 bump `RENDERER_VERSION`、
也不往图上加注记。标记出现或消失时，`runner` / `overview` 靠"去掉标记再查一遍旧记录"
命中上一轮的图，直接 `os.replace` 改名——不重画、不重新解析。
"""

from __future__ import annotations

import os
from functools import lru_cache

# 文件名后缀：`PYTHON-3D_已修复待发版.png`。作为最后一个名字段，读的人一眼看到。
RELEASE_MARK = "已修复待发版"
_PIECE = f"_{RELEASE_MARK}"

# `--no-release-mark` 时关掉（不想让 git 状态影响产物名，或在不带 git 的目录里画图）
_enabled = True


def set_enabled(enabled: bool) -> None:
    """开关标记（默认开）。关掉之后 `fixed_issue_ids()` 一律当"没有标记"。"""
    global _enabled
    _enabled = enabled


def enabled() -> bool:
    return _enabled


@lru_cache(maxsize=1)
def fixed_issue_ids() -> tuple[str, frozenset[str]]:
    """`(范围标签, 已修复待发版的 issue 集合)`；进程内只问一次 git。

    任何失败都退化成空集合：画图不该因为"这段 git 历史读不出来"而拒绝出图，也不该
    因为标记而丢掉一张图。
    """
    if not _enabled:
        return ("", frozenset())
    try:
        from tools.sentry.lib.issue_resolve import fixed_in_unreleased
    except ImportError:  # 单独拷走画图工具时也不该整条命令失败
        return ("", frozenset())
    try:
        label, ids = fixed_in_unreleased()
    except (OSError, RuntimeError, ValueError):
        return ("", frozenset())
    return (label, frozenset(ids))


def is_marked(issue: str) -> bool:
    """这个 issue 要不要加 `_已修复待发版`。"""
    if not issue:
        return False
    return issue.upper() in fixed_issue_ids()[1]


def mark_suffix(
    issue: str, marked: frozenset[str] | set[str] | None = None
) -> str:
    """产物名要追加的那段（带前导下划线）；不用加时返回空串。

    `marked` 显式给了就按它判（自检用），不给就查 `fixed_issue_ids()`。
    """
    if not issue:
        return ""
    pool = fixed_issue_ids()[1] if marked is None else marked
    return _PIECE if issue.upper() in pool else ""


def unmarked_name(name: str) -> str:
    """去掉名字里的 `_已修复待发版`（连「疑似过时」一起处理）。

    `PYTHON-3D_已修复待发版.png` → `PYTHON-3D.png`；
    `PYTHON-3D_已修复待发版_疑似过时.png` → `PYTHON-3D_疑似过时.png`。
    用来在标记刚出现/刚消失时找回上一轮的产物记录（然后只改名，不重画）。
    """
    stem, ext = os.path.splitext(name)
    if _PIECE not in stem:
        return name
    return f"{stem.replace(_PIECE, '')}{ext}"
