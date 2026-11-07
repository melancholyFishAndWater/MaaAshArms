"""trace logs → maafw.log：取行、去重、按失败标记提节点名、从附件名解析 run/task id。

纯数据处理，不发请求（`fetch_log_lines` 通过 `report_common` 调 CLI 的那一步除外，由调用方触发）。
"""

from __future__ import annotations

import re

from .constants import ATTACHMENT_NAME_RE
from .constants import MARKER_RES
from .constants import MAX_LOG_LIMIT
from collections.abc import Sequence
from .. import report_common

def fetch_log_lines(
    sentry: str,
    target: str,
    trace_id: str,
    *,
    limit: int,
    sort: str,
    verbose: bool,
    timeout: float,
) -> tuple[list[str], bool]:
    """取这次运行的日志行；返回 (行, 是否还有更多)。

    `--limit` 上限 1000 且没有游标：超过就只能拿一段。默认 `--sort newest`——失败发生在运行末尾，
    取最新那段才有失败现场；取 `oldest` 会拿到运行开头，失败窗口直接落在截断之外。
    """
    if not 1 <= limit <= MAX_LOG_LIMIT:
        raise ValueError(
            f"--log-limit 必须在 1~{MAX_LOG_LIMIT} 之间（Sentry CLI 自己限死的）。"
        )
    payload = report_common.run_sentry_json_value(
        sentry,
        [
            "trace",
            "logs",
            f"{target}/{trace_id}",
            "--json",
            "--limit",
            str(limit),
            "--sort",
            sort,
        ],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("sentry trace logs 返回的 JSON 里没有 data 列表。")
    lines: list[str] = []
    for row in rows:
        message = str((row or {}).get("message") or "")
        lines.extend(message.splitlines())
    return lines, bool(payload.get("has_more"))


def safe_members(names: Sequence[str]) -> list[str]:
    """挑出可以安全解包的成员名：丢掉绝对路径与含 `..` 的（zip slip）。"""
    safe: list[str] = []
    for raw in names:
        name = raw.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            continue
        if any(part == ".." for part in name.split("/")):
            continue
        safe.append(name)
    return safe


def parse_task_id(attachment_name: str) -> int | None:
    """从附件名里抠出 task_id（`mfa-task-failure-<run>-<task>-screenshots.zip`）。"""
    match = ATTACHMENT_NAME_RE.match(attachment_name.strip())
    return int(match.group(2)) if match else None


def parse_run_id(attachment_name: str) -> str | None:
    """从附件名里抠出 run_id（`mfa-task-failure-<run>-<task>-screenshots.zip`）。

    run_id 是"哪次运行"的现场图。一个 issue 常有多次失败、各一份图，按 run_id 分目录落盘，
    多次失败的现场图才不会互相覆盖，也不会只剩最旧的那一份。
    """
    match = ATTACHMENT_NAME_RE.match(attachment_name.strip())
    return match.group(1).lower() if match else None


def dedupe_lines(lines: Sequence[str]) -> tuple[list[str], int]:
    """去掉逐字节相同的行，返回 (去重后的行, 丢掉几条)。

    重复来自 MFA 续写日志时的 64 KB 前缀重叠，不是两次真的事件；不去重会把图上的未命中次数灌水。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)
    return kept, len(lines) - len(kept)


def marker_lines(lines: Sequence[str], limit: int = 8) -> list[str]:
    """日志里的失败标记（超时节点名、invalid node id 的节点名…），给报告用。"""
    found: list[str] = []
    for line in lines:
        for pattern in MARKER_RES:
            match = pattern.search(line)
            if match:
                stamp = line[1:24] if line.startswith("[") else ""
                found.append(f"{stamp} {match.group(1)}".strip())
                break
        if len(found) >= limit:
            break
    return found
