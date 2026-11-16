"""只读本地日志清点、任务索引与精确筛选；不将 framework_ret 当作业务完成。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from tools.logs.lib.formats import (
    LINE_RE, NODE_DONE_RE, TASK_END_RE, TASK_START_RE,
)

_PREFIX = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\[(?P<level>[A-Z]+)\]")
_PID = re.compile(r"\[(Px\d+)\]")
_TASK = re.compile(r'(?<![\w])(?:"task_id_?"\s*:\s*|task_id_?\s*=\s*)(\d+)(?![\w.])')


@dataclass(frozen=True)
class LogLine:
    line: int
    ts: datetime | None
    level: str | None
    pid: str | None
    text: str


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return number


def _time(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?", value):
        raise argparse.ArgumentTypeError("须为完整本地 ISO 时间 YYYY-MM-DDTHH:MM:SS[.ffffff]，不带时区")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _pid(value: str) -> str:
    if not re.fullmatch(r"Px\d+", value):
        raise argparse.ArgumentTypeError("进程格式须为 Px 后接数字")
    return value


def _files(paths: Sequence[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for text in paths:
        path = Path(text).expanduser().resolve(strict=True)
        candidates = path.rglob("*.log") if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                found[str(resolved).casefold()] = resolved
    return sorted(found.values(), key=lambda path: str(path).casefold())


def _lines(path: Path, warnings: list[str]) -> list[LogLine]:
    rows: list[LogLine] = []
    previous: datetime | None = None
    replacements = 0
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for number, raw in enumerate(handle, 1):
            text = raw.rstrip("\r\n")
            replacements += text.count("\ufffd")
            prefix = _PREFIX.match(text)
            level = None
            pid = None
            if prefix:
                try:
                    previous = datetime.fromisoformat(prefix.group("ts"))
                except ValueError:
                    previous = None
                level = prefix.group("level")
                match = _PID.match(text, prefix.end())
                pid = match.group(1) if match else None
            rows.append(LogLine(number, previous, level, pid, text))
    if replacements:
        warnings.append(f"{path}: UTF-8 解码含 {replacements} 个替换字符（可能为原文字符）")
    return rows


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") if value else None


def _matches(row: LogLine, args: argparse.Namespace) -> bool:
    if args.t_from and (row.ts is None or row.ts < args.t_from):
        return False
    if args.t_to and (row.ts is None or row.ts > args.t_to):
        return False
    if args.pid and row.pid != args.pid:
        return False
    if args.task_id is not None and args.task_id not in {int(m.group(1)) for m in _TASK.finditer(row.text)}:
        return False
    if args.node and not re.search(r"(?<![\w])" + re.escape(args.node) + r"(?![\w])", row.text):
        return False
    if args.contains is not None and args.contains not in row.text:
        return False
    return not args.level or row.level in args.level


def _excerpt(row: LogLine, max_chars: int) -> dict[str, Any]:
    return {"line": row.line, "text": row.text[:max_chars], "truncated": len(row.text) > max_chars}


def _inventory(path: Path, rows: list[LogLine]) -> dict[str, Any]:
    stamps = [row.ts for row in rows if row.ts is not None]
    return {
        "path": str(path), "size": path.stat().st_size, "line_count": len(rows),
        "first_ts": _stamp(stamps[0]) if stamps else None,
        "last_ts": _stamp(stamps[-1]) if stamps else None,
        "levels": dict(sorted(Counter(row.level for row in rows if row.level).items())),
        "image_directories": [str(path.parent / name) for name in ("vision", "on_error") if (path.parent / name).is_dir()],
    }


def _tasks(path: Path, rows: list[LogLine], args: argparse.Namespace, warnings: list[str]) -> list[dict[str, Any]]:
    tasks: dict[tuple[str | None, int], dict[str, Any]] = {}
    for row in rows:
        framework = LINE_RE.match(row.text)
        message = framework.group("msg") if framework else row.text
        start = TASK_START_RE.search(message)
        end = TASK_END_RE.search(message)
        done = NODE_DONE_RE.search(message)
        match = start or end or done
        if match is None:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            warnings.append(f"{path}:{row.line}: 任务记录 JSON 无法解析")
            continue
        if not isinstance(data, dict):
            continue
        tid = int(done.group(2)) if done else data.get("task_id")
        if type(tid) is not int:
            continue
        if args.task_id is not None and tid != args.task_id or args.pid and row.pid != args.pid:
            continue
        key = (row.pid, tid)
        item = tasks.setdefault(key, {
            "source": str(path), "pid": row.pid, "task_id": tid, "entry": None,
            "start": None, "end": None, "framework_ret": None,
            "node_done_count": 0, "source_lines": [], "source_lines_truncated": False,
            "first_ts": None, "last_ts": None,
        })
        if len(item["source_lines"]) < 20:
            item["source_lines"].append(row.line)
        else:
            item["source_lines"][-1] = row.line
            item["source_lines_truncated"] = True
        stamp = _stamp(row.ts)
        item["first_ts"] = item["first_ts"] or stamp
        item["last_ts"] = stamp or item["last_ts"]
        if start:
            if item["start"] is not None:
                warnings.append(f"{path}:{row.line}: pid/task_id 重复开始，保留首 start 和末 end；用 source_lines 复核")
            item["entry"] = data.get("entry")
            item["start"] = item["start"] or stamp
        elif end:
            item["entry"] = data.get("entry") or item["entry"]
            item["end"] = stamp
            item["framework_ret"] = {"true": True, "false": False}.get(end.group(2))
        else:
            item["node_done_count"] += 1
    result = []
    for item in tasks.values():
        lo = datetime.fromisoformat(item["first_ts"]) if item["first_ts"] else None
        hi = datetime.fromisoformat(item["last_ts"]) if item["last_ts"] else None
        if args.t_from and (hi is None or hi < args.t_from):
            continue
        if args.t_to and (lo is None or lo > args.t_to):
            continue
        result.append(item)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """输出 schema_version=1 JSON；参数/路径/读文件错误由 argparse 返回 2。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="日志文件或递归查找 *.log 的目录，重叠路径去重")
    parser.add_argument("--view", choices=("inventory", "tasks", "search"), default="inventory")
    parser.add_argument("--task-id", type=_nonnegative)
    parser.add_argument("--pid", type=_pid)
    parser.add_argument("--node", help="精确节点 token，不做子串匹配")
    parser.add_argument("--contains", help="大小写敏感的字面子串")
    parser.add_argument("--level", action="append", type=str.upper)
    parser.add_argument("--from", dest="t_from", type=_time)
    parser.add_argument("--to", dest="t_to", type=_time)
    parser.add_argument("--limit", type=_positive, default=50)
    parser.add_argument("--offset", type=_nonnegative, default=0)
    parser.add_argument("--context", type=_nonnegative, default=0)
    parser.add_argument("--max-chars", type=_positive, default=2000)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    explicit = {text.split("=", 1)[0] for text in arguments if text.startswith("--")}
    unsupported = {"--contains", "--node", "--level", "--context", "--max-chars"}
    if args.view == "inventory":
        unsupported |= {"--task-id", "--pid", "--from", "--to"}
    if args.view != "search" and explicit & unsupported:
        parser.error(f"{args.view} 视图不支持：" + ", ".join(sorted(explicit & unsupported)))
    if args.t_from and args.t_to and args.t_from > args.t_to:
        parser.error("--from 不得晚于 --to")
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    search_total = 0
    try:
        files = _files(args.paths)
        for path in files:
            rows = _lines(path, warnings)
            if args.view == "inventory":
                items.append(_inventory(path, rows))
            elif args.view == "tasks":
                items.extend(_tasks(path, rows, args, warnings))
            else:
                for index, row in enumerate(rows):
                    if not _matches(row, args):
                        continue
                    position = search_total
                    search_total += 1
                    if not args.offset <= position < args.offset + args.limit:
                        continue
                    item = {"path": str(path), "ts": _stamp(row.ts), "level": row.level,
                            "pid": row.pid, **_excerpt(row, args.max_chars)}
                    item["context"] = [
                        _excerpt(r, args.max_chars)
                        for r in rows[max(0, index - args.context):index + 1 + args.context]
                        if r.line != row.line
                    ]
                    items.append(item)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    total = search_total if args.view == "search" else len(items)
    page = items if args.view == "search" else items[args.offset:args.offset + args.limit]
    output = {"schema_version": 1, "view": args.view, "total": total, "offset": args.offset,
              "limit": args.limit, "has_more": args.offset + args.limit < total,
              "items": page, "warnings": warnings}
    # Preserve test StringIO streams; configure real Windows redirected output as UTF-8.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
