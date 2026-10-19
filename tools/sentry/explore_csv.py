"""把 Sentry Explore 导出的 CSV 还原成日志正文，并抽出一份给分析和绘图用的清单。

    python -m tools.sentry.cli explore-csv <csv>
    python tools/sentry/explore_csv.py <csv>          # 等价，直接跑文件

产出两个（都落在 `temp/`，不入库）：

- **日志正文** `temp/flow/<csv 名>.log` —— 直接喂 `tools/drawing/flow_from_log.py` 或
  `flow_cases.py --case`，绘图链路不需要任何改动；
- **清单** `temp/telemetry/<csv 名>.json` —— 任务、失败标记、面板行、丢失量、进程分布。

## 这个 CSV 的形状（实测 `Explore_2026-October-15_190806.csv`，3.35 MB / 934 行）

表头只有 `timestamp,message` 两列，`message` 里含真实换行（必须用 `csv` 模块读）。三件事必须知道：

1. **`timestamp` 不是行时间**，是这一批日志的**上报时刻**（UTC；实测全程只有 3 个值，每批约 310 行），
   批内也不按行时间排（批内回跳 150~161 次）。行时间只能取行首的 `[2026-10-14 13:03:48.225]`。
2. **正文是按块切碎的**：每格约 3.3 KB（min 391 / max 4044），934 格**每格首行都是半截行**（头部丢失）、
   209 格末行是断头（尾部丢失）。所以既不能逐格独立解析，也不能直接把格子首尾相接（顺序是乱的）。
   能救回来的接续判据只有一个：**上一行是断头、且"上一行 + 本格首行"能匹配完整日志行**（实测接回 85 处）。
3. **导出里有两类非 maafw 的行**：MFA 面板行（`[ts][WRN] [cfg=Default] 中文消息`，画面/解密/焦点这些
   只在面板里说）与 JSON/属性碎片（被切断的 `ls={…}` / `…}]` 尾巴）。前者的关键词是级别括号后**有空格**，
   maafw 原生行没有——用这一点区分，别用"能不能匹配 LINE_RE"。

还原后（实测）：10744 行 = 完整 maafw 行 8785 + 面板行 766 + 断头行 104 + 碎片 1080 + 空行 9；
交给 `flow_from_log.parse_log()` 得到 steps=31（node done）、attempts=153、events=5、
`truncated_skipped=53`（reco/action/node-done 被裁断到 JSON 读不出）。

**取舍**：被截断的行不还原（猜出来的内容会变成假事实，只统计并提示）；`!!!OnEventNotify!!!` 与
`ctx_event_sink` 两份记录不合并（只计数，去重留给按 `reco_id` 计数的下游）；不自动画图（只打印命令）；
CSV 里没有图片，所以图上不会有画面格。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

# 兄弟模块在本目录；`tools.drawing` 的绝对 import 需要仓库根（tools/ 上溯一级）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from . import report_common
except ImportError:
    import report_common

from tools.drawing.flow_from_log import (
    LINE_RE,
    NODE_DONE_RE,
    TRUNCATION_HEADS,
    parse_log,
    parse_ts,
)

# 清单的 schema 号：与 fetch 的遥测工件（`report_common.ARTIFACT_SCHEMA`）是两种东西，
# 用 kind 区分，免得读的人按 measurement 形状去解。
LOG_EXTRACT_SCHEMA = 1
LOG_EXTRACT_KIND = "explore-csv-log"

DEFAULT_LOG_DIR = "temp/flow"
DEFAULT_JSON_DIR = "temp/telemetry"

TS_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]")
# MFA 面板行：级别括号后**有空格**（maafw 原生行没有），消息多为中文
PANEL_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]\[(?P<lvl>[A-Z]{3})\] "
    r"\[cfg=[^\]]*\]\s?(?P<msg>.*)$"
)
PANEL_SAMPLE_LIMIT = 10


# --------------------------------------------------------------------------- #
# 读 CSV
# --------------------------------------------------------------------------- #


def _open_text(source: Any) -> tuple[TextIO, bool]:
    """把 路径 / 正文 / 文本流 三种入参统一成可读文本；返回值第二位表示要不要关。

    单行入参是路径还是正文，靠"有没有逗号/换行"分：CSV 正文至少有一个逗号（表头就有），
    没有逗号的单行按路径处理——这样路径写错会报"没有这个文件"，而不是被当成正文去解析出
    一句莫名其妙的"没有 message 列"（`flow_from_log` 那边踩过同一个坑）。
    """
    if hasattr(source, "read"):
        return source, False
    if isinstance(source, os.PathLike):
        source = os.fspath(source)
    if not isinstance(source, str):
        raise TypeError(f"CSV 入参只接受路径、正文或文本流，收到 {type(source).__name__}")
    if not source.strip():
        raise ValueError("CSV 入参是空的：既不是文件路径，也不是 CSV 正文")
    if os.path.isfile(source):
        return open(source, encoding="utf-8-sig", errors="replace", newline=""), True
    if "\n" not in source and "," not in source:
        raise FileNotFoundError(f"没有这个文件：{source}")
    return io.StringIO(source), False


@dataclass
class Cell:
    """CSV 的一行：上报时刻 + 这一块日志正文（含换行，可能以半截行开头/结尾）。"""

    timestamp: str
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()


@dataclass
class CsvSource:
    """读出来的 CSV：列名、数据格、上报时刻的取值表。"""

    columns: list[str]
    cells: list[Cell]
    origin: str = ""

    @property
    def timestamp_values(self) -> list[str]:
        return list(dict.fromkeys(cell.timestamp for cell in self.cells))


def read_cells(source: Any) -> CsvSource:
    """读 Explore 导出。列里没有 `message` 就报错并列出实际列名（spans 导出走这里）。"""
    stream, should_close = _open_text(source)
    try:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError("CSV 是空的：连表头都没有") from None
        columns = [name.strip() for name in header]
        if "message" not in columns:
            raise ValueError(
                "这份 CSV 没有 message 列，不是 Explore 的 logs 导出。"
                f"实际列名：{', '.join(columns) or '（空）'}"
            )
        index = columns.index("message")
        stamp_at = columns.index("timestamp") if "timestamp" in columns else None
        cells: list[Cell] = []
        for row in reader:
            if not row or index >= len(row):
                continue
            stamp = row[stamp_at].strip() if stamp_at is not None and stamp_at < len(row) else ""
            cells.append(Cell(timestamp=stamp, text=row[index]))
        if not cells:
            raise ValueError("CSV 只有表头，没有数据行")
        return CsvSource(columns=columns, cells=cells)
    finally:
        if should_close:
            stream.close()


# --------------------------------------------------------------------------- #
# 还原日志正文
# --------------------------------------------------------------------------- #


def _ts_of(line: str) -> datetime | None:
    m = TS_RE.match(line)
    return parse_ts(m.group("ts")) if m else None


def join_cells(cells: Sequence[Cell]) -> tuple[list[str], int]:
    """把格子摊成行，并接回"上一行是断头 + 本格首行"的跨格断行。

    只在这两段拼起来是一条**完整**日志行时才接（用 flow_from_log 的 LINE_RE，不另立口径）；
    其余块边界处的半截行原样留着——它们匹配不上日志格式，解析器会跳过，清单里按"碎片/断头"计数。
    """
    lines: list[str] = []
    joined = 0
    for cell in cells:
        current = cell.lines
        if (lines and current and _ts_of(lines[-1]) is not None
                and not LINE_RE.match(lines[-1]) and LINE_RE.match(lines[-1] + current[0])):
            lines[-1] = lines[-1] + current[0]
            current = current[1:]
            joined += 1
        lines.extend(current)
    return lines, joined


def sort_lines(lines: Sequence[str]) -> tuple[list[str], int]:
    """按行内时间戳稳定排序；碎片沿用前一行的时间戳，保持紧跟其后。

    返回 (排序后的行, 排序前的回跳数)。排序是为了让产物能被"按顺序读日志"的工具直接用：
    CSV 是按上报批次排的，批内乱序。

    回跳的判据与 `flow_from_log._read_lines` **完全一致**：只在 `LINE_RE` 命中的行之间比，
    而且是比"目前见过的最大时间戳"小就算一次（不是比前一行小）。两处报的必须是同一个数，
    否则 JSON 里的值与图上注记会互相打脸。
    """
    keys: list[tuple[datetime, int]] = []
    back = 0
    current: datetime | None = None
    biggest: datetime | None = None
    for index, line in enumerate(lines):
        matched = LINE_RE.match(line)
        ts = parse_ts(matched.group("ts")) if matched else _ts_of(line)
        if ts is not None:
            if matched is not None:
                if biggest is not None and ts < biggest:
                    back += 1
                if biggest is None or ts > biggest:
                    biggest = ts
            # 面板行 / 断头行不参与计数，但给紧跟其后的碎片当时间锚
            current = ts
        keys.append((current or datetime.min, index))
    order = sorted(range(len(lines)), key=lambda i: keys[i])
    return [lines[i] for i in order], back


def classify_lines(lines: Sequence[str]) -> dict[str, int]:
    """五分类计数：完整 maafw 行 / MFA 面板行 / 断头行 / 碎片 / 空行。

    判据顺序要紧：面板行也以 `[ts][LVL]` 开头，但它级别括号后是空格，永远匹配不上 LINE_RE，
    所以先判完整的、再判面板的，剩下的才按"有没有时间戳"分断头与碎片。
    """
    kinds = {"complete": 0, "panel": 0, "head_cut": 0, "fragment": 0, "blank": 0}
    for line in lines:
        if LINE_RE.match(line):
            kinds["complete"] += 1
        elif PANEL_RE.match(line):
            kinds["panel"] += 1
        elif not line.strip():
            kinds["blank"] += 1
        elif _ts_of(line) is not None:
            kinds["head_cut"] += 1
        else:
            kinds["fragment"] += 1
    return kinds


def truncated_records(lines: Sequence[str]) -> dict[str, int]:
    """按记录类型统计"头对得上、整条读不出"的行数（reco / action / node_done / …）。

    `flow_from_log.parse_log()` 只给总数（`truncated_skipped`），这里再拆一层，
    是因为不同记录丢掉的后果不同：丢 `reco` 少一次识别，丢 `Task timeout` 少一个失败原因。
    """
    counts: dict[str, int] = {head: 0 for head, _pattern in TRUNCATION_HEADS}
    for line in lines:
        for head, pattern in TRUNCATION_HEADS:
            if head in line and not pattern.search(line):
                counts[head] += 1
                break
    return {head: count for head, count in counts.items() if count}


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #


@dataclass
class Extract:
    """一次提取的产物：正文 + 清单 + 给人看的提示。"""

    log_text: str
    inventory: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return self.inventory.get("tasks", [])

    def drawable_tasks(self, limit: int = 3) -> list[dict[str, Any]]:
        return [task for task in self.tasks if task["drawable"]][:limit]


def _panel_summary(lines: Sequence[str]) -> dict[str, Any]:
    by_level: dict[str, int] = {}
    samples: list[dict[str, str]] = []
    for line in lines:
        m = PANEL_RE.match(line)
        if not m:
            continue
        level = m.group("lvl")
        by_level[level] = by_level.get(level, 0) + 1
        if level in ("WRN", "ERR", "FTL") and len(samples) < PANEL_SAMPLE_LIMIT:
            samples.append({"ts": m.group("ts"), "level": level, "text": m.group("msg").strip()[:200]})
    return {"count": sum(by_level.values()), "by_level": by_level, "samples": samples}


STALL_MISS_MIN = 5   # 连续这么多次识别未命中、中间没有任何 node done，就认为这段时间"卡在派发层"


def _task_rows(parsed, lines: Sequence[str]) -> list[dict[str, Any]]:
    """每个 task 一行：node done 数、时间范围、结束结果、能不能画，以及**卡住的那段**。

    识别记录（`reco [result=]`）里**没有 task_id**，所以只能按任务的活动窗口摊：窗口 = 该 task 的
    `Node.NextList` 记录首末时间 ∪ 它自己的 node done 首末时间 ∪ `task end`。没有 NextList 的日志上
    窗口退回 node done 的范围（那种日志里识别记录本来也归不到任务上）。

    `stalled_at` 是"连续 ≥ STALL_MISS_MIN 次未命中、其间没有任何 node done"那一段的起点：`200000167`
    在 09:39:23–42 空了 19 秒、只有 2 步 node done，默认窗口根本看不出这段，靠这个字段才认得出。
    """
    by_task: dict[int, list[Any]] = {}
    for step in parsed.steps:
        if step.task_id is not None:
            by_task.setdefault(step.task_id, []).append(step)
    ends: dict[int, bool] = {}
    end_ts: dict[int, Any] = {}
    for ts, _entry, task_id, ret in parsed.task_ends:
        ends[task_id] = ret
        end_ts[task_id] = ts
    nl_first: dict[int, Any] = {}
    nl_last: dict[int, Any] = {}
    for (tid, _name), stat in parsed.nextlist_stats.items():
        if tid is None or stat.first_ts is None or stat.last_ts is None:
            continue
        nl_first[tid] = min(nl_first.get(tid, stat.first_ts), stat.first_ts)
        nl_last[tid] = max(nl_last.get(tid, stat.last_ts), stat.last_ts)

    misses = sorted((a for a in parsed.attempts if a.kind == "miss"), key=lambda a: a.ts)
    rows: list[dict[str, Any]] = []
    for task_id, steps in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        lo = min([min(s.ts for s in steps)] + ([nl_first[task_id]] if task_id in nl_first else []))
        hi = max([max(s.ts for s in steps), end_ts.get(task_id, max(s.ts for s in steps))]
                 + ([nl_last[task_id]] if task_id in nl_last else []))
        in_window = [a for a in misses if lo <= a.ts <= hi]
        # 连续未命中的段：段与段之间必然隔着一次 node done，所以按"两步之间有没有 node done"切
        step_ts = sorted(s.ts for s in steps)
        runs: list[list[Any]] = []
        current: list[Any] = []
        for a in in_window:
            if current and any(current[-1].ts < t <= a.ts for t in step_ts):
                runs.append(current)
                current = []
            current.append(a)
        if current:
            runs.append(current)
        best = max(runs, key=len) if runs else []
        rows.append({
            "task_id": task_id,
            "node_done": len(steps),
            "first_ts": min(s.ts for s in steps).isoformat(timespec="milliseconds"),
            "last_ts": max(s.ts for s in steps).isoformat(timespec="milliseconds"),
            "ended": task_id in ends,
            "end_ret": ends.get(task_id),
            "drawable": bool(steps),
            "window": [lo.isoformat(timespec="milliseconds"), hi.isoformat(timespec="milliseconds")],
            "misses": len(in_window),
            "miss_first": in_window[0].ts.isoformat(timespec="milliseconds") if in_window else None,
            "miss_last": in_window[-1].ts.isoformat(timespec="milliseconds") if in_window else None,
            "stalled_at": (best[0].ts.isoformat(timespec="milliseconds")
                           if len(best) >= STALL_MISS_MIN else None),
            "stalled_misses": len(best),
        })
    return rows


def _marker_rows(parsed) -> list[dict[str, Any]]:
    """失败标记：超时 / invalid node id / node not completed / 任务结束（失败也记，方便对照）。"""
    rows: list[dict[str, Any]] = []
    for event in parsed.events:
        if event.name.startswith("任务开始"):
            continue
        kind = "task end" if event.name.startswith("任务结束") else event.name.split(" ")[0]
        row: dict[str, Any] = {
            "kind": kind,
            "name": event.name,
            "ts": event.ts.isoformat(timespec="milliseconds"),
            "detail": event.action,
            "task_id": event.task_id,
            "ok": event.completed,
        }
        rows.append(row)
    return rows


def _process_rows(lines: Sequence[str]) -> list[dict[str, Any]]:
    """进程分布 + 各进程的 node done（行级统计，与 `parse.steps` 同判据）。"""
    per: dict[str, dict[str, int]] = {}
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        px = m.group("px")
        row = per.setdefault(px, {"lines": 0, "node_done": 0})
        row["lines"] += 1
        if NODE_DONE_RE.search(m.group("msg")):
            row["node_done"] += 1
    return [{"px": px, **row} for px, row in sorted(per.items(), key=lambda kv: -kv[1]["lines"])]


def build_inventory(source: CsvSource, lines: Sequence[str], parsed, *,
                    boundaries_joined: int, out_of_order: int, origin: str = "") -> dict[str, Any]:
    """组装清单（形状见模块文档；`schema` + `kind` 两个字段让读的人先认出来是什么）。"""
    kinds = classify_lines(lines)
    stats = parsed.stats
    tasks = _task_rows(parsed, lines)
    notes: list[str] = []
    if not any(task["drawable"] for task in tasks):
        notes.append("这份导出没有 PipelineTask node done：绘图只能出仅事件模式（用识别记录合成步骤）")
    # 可画但"主体在空转"的任务要点名：默认窗口只看得到 node done，看不出那段时间在反复试候选
    for task in tasks:
        if task["stalled_at"] and task["drawable"]:
            notes.append(
                f"task {task['task_id']} 只有 {task['node_done']} 步 node done，"
                f"但 {task['stalled_at'][11:19]} 起有连续 {task['stalled_misses']} 次识别未命中、其间没有任何节点跑完："
                f"画它时要带 --from/--to 圈住那段（窗口 {task['window'][0][11:19]}~{task['window'][1][11:19]}）")
    if stats.truncated_skipped:
        notes.append(f"{stats.truncated_skipped} 条记录被截断读不出（reco/action/node done），已跳过")
    notes.append("这份导出不含图片，绘图时不会有画面格")
    if len(source.timestamp_values) == 1:
        notes.append("CSV 的 timestamp 只有 1 个值：它是上报时刻，不是行时间，行时间取行首的 [时间戳]")
    return {
        "schema": LOG_EXTRACT_SCHEMA,
        "kind": LOG_EXTRACT_KIND,
        "generated_at": report_common.utc_now_iso(),
        "source": {
            "path": origin,
            **({} if not origin or not os.path.isfile(origin) else {
                "bytes": os.path.getsize(origin),
                "sha256": _sha256(origin),
            }),
            "csv_rows": len(source.cells),
            "columns": list(source.columns),
            "timestamp_values": len(source.timestamp_values),
            "timestamp_note": "该列是每批日志的上报时刻，不是行时间",
        },
        "recovered": {
            "chars": sum(len(line) + 1 for line in lines),
            "lines": len(lines),
            "lines_complete": kinds["complete"],
            "lines_panel": kinds["panel"],
            "lines_head_cut": kinds["head_cut"],
            "lines_fragment": kinds["fragment"],
            "boundaries_joined": boundaries_joined,
            "out_of_order": out_of_order,
            "sorted": True,
        },
        "parse": {
            "steps": len(parsed.steps),
            "attempts": len(parsed.attempts),
            "events": len(parsed.events),
            "task_ends": len(parsed.task_ends),
            "internal_skipped": stats.lines_internal_skipped,
            "truncated_skipped": stats.truncated_skipped,
            "truncated_kept": stats.truncated_kept,
            "truncated_by_head": truncated_records(lines),
            "first_ts": stats.first_ts.isoformat(timespec="milliseconds") if stats.first_ts else None,
            "last_ts": stats.last_ts.isoformat(timespec="milliseconds") if stats.last_ts else None,
        },
        "processes": _process_rows(lines),
        "tasks": tasks,
        "markers": _marker_rows(parsed),
        "panel_lines": _panel_summary(lines),
        "images": False,
        "notes": notes,
    }


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(source: Any, *, origin: str = "") -> Extract:
    """一站式入口：读 CSV → 还原正文 → 解析 → 出清单。"""
    csv_source = read_cells(source)
    lines, joined = join_cells(csv_source.cells)
    lines, out_of_order = sort_lines(lines)
    log_text = "\n".join(lines) + "\n"
    parsed = parse_log(log_text, None, None, None, False)
    inventory = build_inventory(csv_source, lines, parsed, boundaries_joined=joined,
                                out_of_order=out_of_order, origin=origin)
    return Extract(log_text=log_text, inventory=inventory, notes=list(inventory["notes"]))


# --------------------------------------------------------------------------- #
# 落盘与输出
# --------------------------------------------------------------------------- #


def default_log_path(csv_path: str) -> Path:
    return report_common.repo_root() / DEFAULT_LOG_DIR / (Path(csv_path).stem + ".log")


def default_json_path(csv_path: str) -> Path:
    return report_common.repo_root() / DEFAULT_JSON_DIR / (Path(csv_path).stem + ".json")


def write_log(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def write_json(inventory: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(inventory, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def _display(path: Path | None) -> str:
    """报告里显示成相对仓库根的路径：短、能直接粘贴（命令都在仓库根跑）。

    用户自己给的 `--log-out temp/x.log` 是相对路径，也要按同一口径显示成 `temp/x.log`
    （Windows 上直接 `str(path)` 会给出 `temp\\x.log`，粘到文档里就成了转义）。
    """
    if path is None:
        return ""
    resolved = path if path.is_absolute() else report_common.repo_root() / path
    try:
        return resolved.relative_to(report_common.repo_root()).as_posix()
    except ValueError:
        return path.as_posix()


def format_report(inventory: dict[str, Any], *, log_path: Path | None, json_path: Path | None,
                  output: TextIO, origin: str) -> None:
    """终端报告：任务表 + 丢失量 + 面板行 + 可复制的下一步命令。"""
    recovered = inventory["recovered"]
    parse = inventory["parse"]
    print(f"来源: {origin or '（正文）'}", file=output)
    print(f"CSV {inventory['source']['csv_rows']} 行 × {len(inventory['source']['columns'])} 列"
          f"（上报时刻 {inventory['source']['timestamp_values']} 个值）", file=output)
    print(f"还原: {recovered['lines']} 行 = 完整 {recovered['lines_complete']}"
          f" + 面板 {recovered['lines_panel']} + 断头 {recovered['lines_head_cut']}"
          f" + 碎片 {recovered['lines_fragment']}"
          f"（接回跨格断行 {recovered['boundaries_joined']} 处，排序前回跳 {recovered['out_of_order']} 处）",
          file=output)
    print(f"解析: node done {parse['steps']} / 识别 {parse['attempts']} / 事件 {parse['events']}"
          f" / 截断跳过 {parse['truncated_skipped']}"
          f" / 时间 {parse['first_ts']} ~ {parse['last_ts']}", file=output)

    tasks = inventory["tasks"]
    if tasks:
        print("\n任务（按 node done 数排）", file=output)
        report_common.write_console_table(
            ("task_id", "node done", "开始", "结束", "结束结果", "可画"),
            [(
                str(task["task_id"]),
                str(task["node_done"]),
                task["first_ts"][11:23],
                task["last_ts"][11:23],
                "未记录" if not task["ended"] else ("成功" if task["end_ret"] else "失败"),
                "是" if task["drawable"] else "否",
            ) for task in tasks],
            output,
            right_aligned={1},
        )
    else:
        print("\n没有按 task 分组的 node done 记录", file=output)

    panel = inventory["panel_lines"]
    if panel["count"]:
        levels = " / ".join(f"{k} {v}" for k, v in sorted(panel["by_level"].items()))
        print(f"\n面板行 {panel['count']} 条（{levels}），例：", file=output)
        for sample in panel["samples"][:5]:
            print(f"  {sample['ts']} [{sample['level']}] {sample['text']}", file=output)

    markers = [row for row in inventory["markers"] if row.get("ok") is False]
    if markers:
        print(f"\n失败标记 {len(markers)} 条：", file=output)
        for row in markers[:8]:
            task = f" task_id={row['task_id']}" if row["task_id"] else ""
            print(f"  {row['ts'][11:23]} [{row['kind']}] {row['name']}{task} {row['detail'] or ''}".rstrip(),
                  file=output)

    if log_path:
        print(f"\n日志正文: {_display(log_path)}", file=output)
    if json_path:
        print(f"清单:     {_display(json_path)}", file=output)

    if log_path and tasks:
        print("\n下一步（绘制）：", file=output)
        for task in [t for t in tasks if t["drawable"]][:3]:
            out = log_path.with_name(f"{log_path.stem}_{task['task_id']}.png")
            print(f"  python tools/drawing/flow_from_log.py {_display(log_path)} "
                  f"--task-id {task['task_id']} --out {_display(out)}", file=output)
    for note in inventory["notes"]:
        print(f"提示：{note}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=report_common.module_summary(__doc__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csv", help="Explore 导出的 CSV（timestamp,message 两列）")
    parser.add_argument("--log-out", help=f"日志正文输出路径（默认 {DEFAULT_LOG_DIR}/<csv 名>.log）")
    parser.add_argument("--json-out", help=f"清单输出路径（默认 {DEFAULT_JSON_DIR}/<csv 名>.json）")
    parser.add_argument("--no-write", action="store_true", help="只打印报告，不落盘")
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = create_argument_parser(prog).parse_args(argv)
    result = extract(arguments.csv, origin=arguments.csv)

    log_path = None if arguments.no_write else (
        Path(arguments.log_out) if arguments.log_out else default_log_path(arguments.csv))
    json_path = None if arguments.no_write else (
        Path(arguments.json_out) if arguments.json_out else default_json_path(arguments.csv))
    if log_path:
        write_log(result.log_text, log_path)
    if json_path:
        write_json(result.inventory, json_path)

    format_report(result.inventory, log_path=log_path, json_path=json_path,
                  output=sys.stdout, origin=arguments.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
