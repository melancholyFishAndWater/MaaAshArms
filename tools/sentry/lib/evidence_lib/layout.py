"""证据包目录布局与增量：`temp/evidence/<SHORT>/debug/`、`evidence.json`、按 run 分桶、复用旧包。

只读写"证据包这一棵树"；Sentry 字段怎么解释在 model.py，下载在 download.py。
"""

from __future__ import annotations

import zipfile

from typing import Any
from tools.drawing.lib.flow_cases import LOG_NAMES
from pathlib import Path
from collections.abc import Sequence
import io
from .traces import safe_members

def existing_artifacts(directory: Path) -> tuple[str | None, list[str]]:
    """扫一眼证据目录里已经有什么，返回 (日志相对路径, 现场图相对路径列表)。

    清单记的是**目录里有什么**，不是"本次下了什么"：`--force` 配合 `--no-log` / `--no-images` 只下一半时，
    上一趟留下的文件仍在原地（工具不删旧文件），清单必须照实写，否则照清单走的人会以为画不了图。
    """
    log_name = next(
        (name for name in LOG_NAMES if (directory / "debug" / name).is_file()), None
    )
    log_rel = f"debug/{log_name}" if log_name else None
    on_error = directory / "debug" / "on_error"
    # 递归扫：现场图按失败事件放在 `debug/on_error/<run_id>/` 子目录里，
    # 只 glob 顶层会漏掉（老包是平铺的，递归同样认）。
    images = (
        sorted(
            "debug/on_error/" + item.relative_to(on_error).as_posix()
            for item in on_error.rglob("*.png")
        )
        if on_error.is_dir()
        else []
    )
    return log_rel, images


def existing_run_ids(images: Sequence[str]) -> set[str]:
    """从 `existing_artifacts()` 的图片列表里取出"已经下过现场图的 run_id"。

    只认按 run 分了目录的（`debug/on_error/<run_id>/<文件>.png`）：平铺的老图不带 run，
    不算已下过——那样下一次会把新的那份补上，不会因为"目录里已经有点图"就整个跳过。
    """
    runs: set[str] = set()
    for rel in images:
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 4 and parts[0] == "debug" and parts[1] == "on_error":
            runs.add(parts[2])
    return runs


def event_index(images: Sequence[str], jobs: Sequence[dict]) -> list[dict]:
    """把磁盘上的现场图按 run_id 归拢，补上来自附件的 event_id / task_id，供清单展示。

    `evidence.json` 的 `events` 就是它的产物：一眼看出哪份图属于哪次失败（哪个事件、哪个 task）。
    """
    by_run: dict[str, list[str]] = {}
    for rel in images:
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 4 and parts[0] == "debug" and parts[1] == "on_error":
            by_run.setdefault(parts[2], []).append(rel)
    meta = {str(j["run_id"]): j for j in jobs}
    out: list[dict] = []
    for run_id in sorted(by_run):
        job = meta.get(run_id)
        out.append(
            {
                "run_id": run_id,
                "event_id": str(job["event_id"]) if job else None,
                "received": str(job.get("received") or "") if job else "",
                "task_id": job.get("task_id") if job else None,
                "images": sorted(by_run[run_id]),
            }
        )
    return out


def reconcile_written(
    directory: Path,
    written: dict[str, Any],
    log_rel: str | None,
    images: Sequence[str],
    notes: list[str],
) -> None:
    """把磁盘上已有的证据补进 `written`，并记一条"哪来的"。

    补齐后再让 `written` 走一遍：清单与磁盘不一致的地方在这里收敛。
    日志只补"本次没写"的情况——本次写了就留着本次的字节数。
    """
    log_added = log_rel if written.get("log") is None else None
    if log_added is not None:
        written["log"] = log_added
        try:
            written["log_bytes"] = (directory / log_added).stat().st_size
        except OSError:
            written["log_bytes"] = 0
    existing_images = list(written.get("images") or [])
    image_added = [name for name in images if name not in existing_images]
    if image_added:
        written["images"] = existing_images + image_added
    if not (log_added or image_added):
        return
    parts = []
    if log_added is not None:
        parts.append("日志")
    if image_added:
        parts.append(f"{len(image_added)} 张现场图")
    notes.append(
        f"{'、'.join(parts)}是目录里原有的（本次没下，工具不删旧文件），已一并记进清单"
    )


def write_members(blob: bytes, dest: Path, *, strip_prefix: str = "") -> list[str]:
    """把附件 zip 的安全成员写到 dest 下，返回写下去的**相对路径**。

    `strip_prefix` 开头的成员名会去掉该前缀再落盘：现场图 zip 里的成员是
    `debug/on_error/<文件>.png`，按事件分目录时要写成 `<run_id>/<文件>.png`，
    所以调用方 strip 掉 `debug/on_error/`。
    """
    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = set(safe_members(archive.namelist()))
        for name in archive.namelist():
            if name not in members:
                continue
            rel = name
            if strip_prefix and name.startswith(strip_prefix):
                rel = name[len(strip_prefix):]
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            written.append(rel)
    return written


def write_event_images(blob: bytes, directory: Path, run_id: str) -> list[str]:
    """把一个失败事件的现场图写到 `debug/on_error/<run_id>/`，返回相对 `directory` 的路径。

    按 run_id 分目录是这里的关键：一个 issue 的多次失败各有一份 on_error 图，文件名里的时间戳
    可能撞（同一秒失败两次），平铺进 `debug/on_error/` 会互相覆盖；而只取一个事件的老做法
    会把其余几次的现场图整个丢掉。绘图侧的 `MediaResolver` / `OnErrorIndex` 都是递归扫目录、
    按文件名匹配，所以子目录天然支持。
    """
    dest = directory / "debug" / "on_error" / run_id
    members = write_members(blob, dest, strip_prefix="debug/on_error/")
    return [f"debug/on_error/{run_id}/{rel}" for rel in members]
