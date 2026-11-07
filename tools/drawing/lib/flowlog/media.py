"""图片证据：目录 / zip / 多卷归档里的 `vision/` 与 `on_error/` 条目、按 reco_id
与时间对齐、裁切与缩略缓存。

`MediaResolver` 是对外入口（`FlowOptions` 里也可直接给字节）。找不到图不是错误：
框里少一张缩略图，注记会说明原因。"""

from __future__ import annotations


import io
import logging
import os
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .constants import ON_ERROR_BACKFILL_SECONDS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Step

log = logging.getLogger("tools.flow_from_log")


# --------------------------------------------------------------------------- #
# 证据来源：目录 / zip / 内存
# --------------------------------------------------------------------------- #


def _is_zip(path: str) -> bool:
    """按文件头魔数判断 zip，不按扩展名（MFA 导出分卷叫 archive-part01.zip，名字不可靠）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


@dataclass(frozen=True)
class MediaEntry:
    """一条图片证据。`path` 为 None 时用 `data`（bytes 或 `_ZipMember`）。"""

    member: str  # 归档内相对路径或文件绝对路径，仅用于日志与去重
    path: str | None = None
    data: Any = None


@dataclass(frozen=True)
class _ZipMember:
    """归档里的一个条目：真正要画时才解压，避免一次性把整包图片读进内存。"""

    package: str
    member: str

    def read(self) -> bytes:
        with zipfile.ZipFile(self.package) as zf:
            return zf.read(self.member)


class MediaResolver:
    """把"证据来源"统一成可枚举的条目，来源可以是：

      * 一个目录（`debug/vision`、`debug/on_error`，本机常态）；
      * 一个 zip 或**多个** zip（MFA 导出包，超过 23.75 MB 会分成 archive-partNN.zip）；
      * 内存字节（遥测从 Sentry 取回的图片，本地没有文件）。

    两类索引都只依赖 `iter_entries()`，因此加一种来源不用改渲染代码。
    """

    def __init__(
        self, source: Any = None, *, members: Iterable[tuple[str, Any]] | None = None
    ):
        """`members` 的值可以给 bytes，也可以给**可调用对象**（延迟读，返回 bytes）。

        延迟读很重要：MFA 导出包里的图片动辄几百 KB、一包几百张；一次性
        `zf.read()` 全部条目会把内存顶起来，而实际只有命中节点的几张会被画。
        """
        self.roots: list[tuple[str, str]] = []  # (kind, locator)；kind = dir | zip
        self.members: list[tuple[str, Any]] = list(members or [])
        if source is None:
            return
        if isinstance(source, MediaResolver):  # 已经是 resolver，直接接管
            self.roots = list(source.roots)
            self.members.extend(source.members)
            return
        if hasattr(source, "read"):  # 字节流
            self.members.append(("stream.bin", source.read()))
            return
        for item in (source if isinstance(source, (list, tuple)) else [source]):
            item = str(item)
            if os.path.isdir(item):
                self.roots.append(("dir", os.path.abspath(item)))
            elif _is_zip(item):
                self.roots.append(("zip", os.path.abspath(item)))
            # 既不是目录也不是 zip 的路径直接忽略，交给上层报"没有证据"

    def __bool__(self) -> bool:
        return bool(self.roots or self.members)

    def iter_entries(self) -> Iterator[MediaEntry]:
        """按**成员名**字典序产出全部条目；zip 分卷因此天然合并成一棵逻辑树。"""
        seen: set[str] = set()
        for member, data in sorted(self.members, key=lambda kv: kv[0]):
            if member in seen:
                continue
            seen.add(member)
            yield MediaEntry(member=member, data=data)
        for kind, locator in self.roots:
            if kind == "dir":
                for dirpath, _dirs, files in os.walk(locator):
                    for fn in sorted(files):
                        full = os.path.join(dirpath, fn)
                        member = os.path.relpath(full, locator).replace("\\", "/")
                        if member in seen:
                            continue
                        seen.add(member)
                        yield MediaEntry(member=member, path=full)
            else:
                try:
                    with zipfile.ZipFile(locator) as zf:
                        for info in sorted(zf.infolist(), key=lambda i: i.filename):
                            if info.is_dir() or info.filename in seen:
                                continue
                            seen.add(info.filename)
                            yield MediaEntry(
                                member=info.filename,
                                data=_ZipMember(locator, info.filename),
                            )
                except (OSError, zipfile.BadZipFile) as exc:
                    log.warning("提示：跳过读不了的 zip %s：%s", locator, exc)

    @staticmethod
    def open_image(entry: MediaEntry):
        """把一条证据打开成 PIL.Image。"""
        from PIL import Image

        data = entry.data
        if isinstance(data, _ZipMember):
            return Image.open(io.BytesIO(data.read())).convert("RGB")
        if data is not None:
            return Image.open(io.BytesIO(data)).convert("RGB")
        if entry.path is None:
            raise ValueError(
                f"证据 {entry.member} 既没有文件路径也没有字节内容，打不开。"
            )
        return Image.open(entry.path).convert("RGB")


class VisionIndex:
    """`debug/vision/<时间>_<节点名>_<reco_id>.jpg`。

    MaaFramework 只给"有东西可画"的识别（TemplateMatch / OCR / ColorMatch…）存图，
    `Custom` / `DirectHit` 节点没有自己的图。所以取图有三级回退：
      1. (节点名, reco_id) 精确命中 —— 那张图里带 ROI 框和模板缩略图；
      2. 同一节点的 `*_wait_freezes.jpg`（该节点执行期间的画面）；
      3. 同一时刻任意节点的邻近帧（只当作"当时画面"，会标注出来）。

    **遥测侧没有这一层**：MFA 的失败附件显式跳过 `vision/`（`TaskDiagnostics` 的目录
    遍历里 `vision` 直接 continue），且 `MaxImageBundleBytes` 只有 5 MiB，而本机
    `debug/vision` 有 1078 张 / 248.6 MB。所以远程只能出"节点流程图 + 失败现场图"，
    要看识别帧得让对方手动导出（导出对话框里勾"包含 vision 图片"）。
    """

    NAME_RE = re.compile(
        r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.*?)_(?P<tail>wait_freezes|\d+)\.jpg$"
    )

    def __init__(self, source: Any = None):
        self.source = (
            source if isinstance(source, MediaResolver) else MediaResolver(source)
        )
        self.by_reco: dict[tuple[str, int], MediaEntry] = {}
        self.freezes: list[tuple[datetime, str, MediaEntry]] = []
        self.frames: list[tuple[datetime, MediaEntry]] = []
        for entry in self.source.iter_entries():
            m = self.NAME_RE.match(os.path.basename(entry.member))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
            except ValueError:
                continue
            if m.group("tail") == "wait_freezes":
                self.freezes.append((ts, m.group("name"), entry))
            else:
                self.by_reco[(m.group("name"), int(m.group("tail")))] = entry
            self.frames.append((ts, entry))
        # 按时间排序，但**用 member 名做次键**：文件名毫秒段位数不固定（`.51` 与 `.510`
        # 解析成同一个 datetime），同一时刻有多张图时，裸 `sort()` 会继续比较 `MediaEntry`
        # 抛 `TypeError: '<' not supported between instances of 'MediaEntry' …`
        # （实测 37803 张 vision 图里有 26 组同时间戳）。`find()` 是线性扫、取第一个进 tol 的，
        # 所以次键只决定"同时间戳时哪张胜出"——给个稳定顺序就够了，不必语义上挑哪张。
        self.freezes.sort(key=lambda x: (x[0], x[1], x[2].member))
        self.frames.sort(key=lambda x: (x[0], x[1].member))

    def find(self, step: Step, tol: float = 3.0, neighbor_tol: float = 2.0):
        """返回 (MediaEntry, 是否该节点自己的识别图)。找不到返回 (None, False)。"""
        if step.reco_id is not None:
            p = self.by_reco.get((step.name, int(step.reco_id)))
            if p:
                return p, True
        best, best_d = None, tol
        for ts, name, entry in self.freezes:
            if name != step.name:
                continue
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = entry, d
        if best:
            return best, False
        best, best_d = None, neighbor_tol
        for ts, entry in self.frames:
            d = abs((ts - step.ts).total_seconds())
            if d < best_d:
                best, best_d = entry, d
        return best, False


class OnErrorIndex:
    """`debug/on_error/<时间>_<节点名>.png`：节点失败时框架抓的现场图。

    这是**遥测里唯一有的图片证据**（失败附件就是这个目录），所以远程路径上它
    是"能贴的全部"。
    """

    NAME_RE = re.compile(
        r"^(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2}\.\d+)_(?P<name>.+)\.png$"
    )

    def __init__(self, source: Any = None):
        self.source = (
            source if isinstance(source, MediaResolver) else MediaResolver(source)
        )
        self.items: list[tuple[datetime, str, MediaEntry]] = []
        for entry in self.source.iter_entries():
            m = self.NAME_RE.match(os.path.basename(entry.member))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S.%f")
            except ValueError:
                continue
            self.items.append((ts, m.group("name"), entry))
        # 同一张现场图可能同时落在 on_error/ 与 on_error/<附件id>/ 下（`--force` 重复下载留下的），
        # 文件名与时间戳完全一致：按 (时间, 节点名) 去重只留一条。排序也必须给 key ——
        # `MediaEntry` 不可比较，两个同 (ts, name) 的元组会让 `sort()` 抛
        # `'<' not supported between instances of 'MediaEntry'`，整张图渲染失败。
        unique: dict[tuple[datetime, str], MediaEntry] = {}
        for ts, name, entry in self.items:
            unique.setdefault((ts, name), entry)
        self.items = [(ts, name, e) for (ts, name), e in sorted(unique.items())]

    def find(self, step: Step, tol: float = 8.0) -> MediaEntry | None:
        """按时间就近找；`Task timeout X` / `invalid node id` 事件的 action 里存着失败节点名，优先同名。"""
        target = step.action.strip()
        best, best_d = None, tol
        for ts, name, entry in self.items:
            d = abs((ts - step.ts).total_seconds())
            if target and name == target:
                d *= 0.15
            if d < best_d:
                best, best_d = entry, d
        return best


def member_clock(entry: MediaEntry) -> str:
    """现场图文件名里的时刻：`2026.11.03-08.52.54.334_BaseTaskStopError.png` → `08:52:54.334`。

    框里写它才能对上 `debug/on_error/<文件名>`；解析不出返回空串（不猜）。
    """
    match = re.match(
        r"^\d{4}\.\d{2}\.\d{2}-(\d{2})\.(\d{2})\.(\d{2})\.(\d+)",
        os.path.basename(entry.member),
    )
    if not match:
        return ""
    return f"{match.group(1)}:{match.group(2)}:{match.group(3)}.{match.group(4)[:3]}"


def _pad_hi(hi: datetime, seconds: float) -> datetime:
    """右端放宽 `seconds` 秒；`datetime.max` 不能再加（会 OverflowError）。"""
    if hi == datetime.max:
        return hi
    return hi + timedelta(seconds=seconds)


def backfill_failure_thumbs(
    columns: list[list[Step]],
    items: Sequence[tuple[datetime, str, MediaEntry]],
    thumb_of: dict[tuple[int, int], tuple[MediaEntry, bool, str]],
    limit_seconds: float = ON_ERROR_BACKFILL_SECONDS,
    window: tuple[datetime, datetime] | None = None,
) -> list[str]:
    """把 `on_error/` 的失败现场图贴回**同名命中节点**；返回实际贴上去的文件名。

    四条限定，缺一条就会把图贴错（实测踩过：本次跑次的 `BaseTaskStopError` 框贴着 45 分钟后
    另一次失败的图，而同一批失败事件框贴着本次的图，读图的人分不出哪张是真的）：

    - 只贴给**时间上紧挨着**的那一步（`limit_seconds`）：同名节点一天失败好几次时，
      45 分钟后那张不该贴到这一步上；
    - 一格只留**时间最近**的那张，不是"最后遍历到的"（原来按列表顺序覆盖，最后那张赢）；
    - 该格已经有**自己的精确识别帧**时不覆盖：识别帧是"这个节点当时看到了什么"，比事后截图直接；
    - 给了 `window`（选中任务的判定窗口）就只认落在里面的图：队列里两个 task 首尾只差 51 ms 时，
      光靠 `±8 s` 会把**上一个** task 的失败现场图算进本 task（实测 `TaskEntrust` 08:52:54.333
      失败、`TaskTraining` 08:52:54.384 起）。左端不放宽——现场图在失败那一刻，
      早于本 task 起始的必然是别人的。

    纯函数（不画图、不需要 Pillow），自检直接测它。
    """
    by_name: dict[str, list[tuple[int, int, Step]]] = {}
    for ci, col in enumerate(columns):
        for ri, st in enumerate(col):
            if st.kind == "hit":
                by_name.setdefault(st.name, []).append((ci, ri, st))
    lo = hi = None
    if window is not None:
        lo, hi = window[0], _pad_hi(window[1], limit_seconds)
    best: dict[tuple[int, int], tuple[float, MediaEntry]] = {}
    for ts, name, entry in items:
        if lo is not None and (ts < lo or (hi is not None and ts > hi)):
            continue
        cands = [x for x in by_name.get(name, []) if x[2].ts <= ts]
        if not cands:
            continue
        ci, ri, st = max(cands, key=lambda x: x[2].ts)
        gap = (ts - st.ts).total_seconds()
        if gap > limit_seconds:
            continue
        current = best.get((ci, ri))
        if current is None or gap < current[0]:
            best[(ci, ri)] = (gap, entry)
    used: list[str] = []
    for (ci, ri), (_gap, entry) in best.items():
        existing = thumb_of.get((ci, ri))
        if existing and existing[1]:
            continue
        thumb_of[(ci, ri)] = (entry, False, "失败现场")
        used.append(entry.member)
    return used


def window_failure_images(
    items: Sequence[tuple[datetime, str, MediaEntry]],
    lo: datetime | None,
    hi: datetime | None,
) -> list[str]:
    """落在 `[lo, hi]` 里的现场图文件名（按时间排）。

    图上标了「失败现场」还不够——同名节点一天失败好几次时，要能对上是**哪一个文件**。
    这条注记直接把 `on_error/<文件名>.png` 写出来，读图的人不用猜。
    """
    out: list[str] = []
    for ts, _name, entry in items:
        if lo is not None and ts < lo:
            continue
        if hi is not None and ts > hi:
            continue
        out.append(os.path.basename(entry.member))
    return out
