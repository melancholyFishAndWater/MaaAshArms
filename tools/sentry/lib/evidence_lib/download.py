"""Sentry CLI 与 HTTP 侧：列 issue/事件、查附件、下载 zip、取 trace 行。

所有联网都在这里；调用方负责决定"要不要下"和"落到哪"。失败按单份跳过/记录，不中断整包。
"""

from __future__ import annotations

import urllib.error
import urllib.request

from ..config import CONFIG
from .constants import DEFAULT_MAX_EVENTS
from .constants import HTTP_TIMEOUT_SECONDS
from .model import Issue
from .constants import SENTRY_HOST
from collections.abc import Sequence
from .model import issue_context
from .traces import parse_run_id
from .traces import parse_task_id
from .model import release_version
from .. import report_common

def sentry_token(sentry: str, *, verbose: bool, timeout: float) -> str:
    """`sentry auth token --json` 输出的是**裸 JSON 字符串**（不是对象），所以不能用 run_sentry_json。"""
    value = report_common.run_sentry_json_value(
        sentry, ["auth", "token", "--json"], verbose=verbose, timeout_seconds=timeout
    )
    token = value if isinstance(value, str) else (value or {}).get("token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError(
            "拿不到 Sentry token（sentry auth token 返回空）。先执行一次 `sentry auth login`，"
            "令牌过期时执行 `sentry auth refresh`。"
        )
    return token.strip()


def list_attachments(
    sentry: str, target: str, event_id: str, *, verbose: bool, timeout: float
) -> list[dict]:
    """列事件附件。返回体是 {"status":200,"body":[…]}，附件在 body 里。"""
    payload = report_common.run_sentry_json_value(
        sentry,
        ["api", f"projects/{target}/events/{event_id}/attachments/", "--json"],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    body = payload.get("body") if isinstance(payload, dict) else None
    return [item for item in body or [] if isinstance(item, dict)]


def list_events(
    sentry: str, short_id: str, *, verbose: bool, timeout: float, limit: int
) -> list[dict]:
    """`sentry issue events <short-id> --json` → 最近若干事件，只留 id 与收到时间（新的在前）。

    `issue view` 只给**一个**事件，所以只取它就永远只有那一份现场图（实测一个 issue 有 3 份
    不同运行的 on_error 包，而只会拿到最旧的那份）。要拿齐一个 issue 的多次失败，就得多看几个事件。
    这个视图是精简的（拿不到 `attachment.*` / `trace`），但有 event id——够用：拿 id 再逐个查附件列表。

    枚举不了时返回 `[]`（子命令变了、没登录、网络失败），调用方退回"只查 issue view 那一个事件"
    的旧行为，而不是整条命令报错。
    """
    try:
        raw = report_common.run_sentry_json_value(
            sentry,
            ["issue", "events", short_id, "--json"],
            verbose=verbose,
            timeout_seconds=timeout,
        )
    except RuntimeError:
        return []
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id") or row.get("eventID") or "")
        if not eid:
            continue
        out.append(
            {
                "id": eid,
                "received": str(
                    row.get("received") or row.get("dateCreated") or ""
                ),
            }
        )
    # 按收到时间倒序（拿不到时间就保持原序，排在时间已知的后面）
    out.sort(key=lambda e: e.get("received") or "", reverse=True)
    return out[: max(0, limit)]


def collect_image_attachments(
    sentry: str,
    issue: Issue,
    *,
    verbose: bool,
    timeout: float,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> list[dict]:
    """枚举该 issue 最近若干失败事件，取每个事件挂着现场图的附件。

    返回 `[{event_id, received, run_id, task_id, attachment_id, name}]`。`run_id` 从附件名取
    （`mfa-task-failure-<run_id>-<task_id>-screenshots.zip`），就是"哪次运行"的现场图。

    为什么逐个事件查：一个 issue 的多次失败各有一份 on_error 包，而 `issue view` 只回一个事件，
    只查它就永远只有那一份（实测 3 份、其中同一任务 `200000003` 在不同运行里失败了两次）。
    代表事件（`issue.event_id`）无论如何都查一次，这样 `issue events` 不可用时也能拿到它的图。
    """
    jobs: list[dict] = []
    if issue.event_only and issue.event_id:
        # `--event` 指定了单个历史事件：只取这一次的现场图，别把最近几次的图也塞进来。
        ids = [issue.event_id]
        received_of = {issue.event_id: issue.received}
    else:
        events = list_events(
            sentry, issue.short_id, verbose=verbose, timeout=timeout, limit=max_events
        )
        ids = [str(e["id"]) for e in events]
        received_of = {str(e["id"]): str(e.get("received") or "") for e in events}
        if issue.event_id and issue.event_id not in ids:
            ids.insert(0, issue.event_id)
    for event_id in ids:
        try:
            attachments = list_attachments(
                sentry, CONFIG.target, event_id, verbose=verbose, timeout=timeout
            )
        except RuntimeError:
            continue
        for item in attachments:
            aid = str(item.get("id") or "")
            name = str(item.get("name") or f"attachment-{aid}.zip")
            run_id = parse_run_id(name)
            if not aid or run_id is None:
                continue
            jobs.append(
                {
                    "event_id": event_id,
                    "received": received_of.get(event_id, ""),
                    "run_id": run_id,
                    "task_id": parse_task_id(name),
                    "attachment_id": aid,
                    "name": name,
                }
            )
    return jobs


def download_attachment(
    target: str, event_id: str, attachment_id: str, token: str
) -> bytes:
    """带 Bearer 下载附件。`sentry api` 打不出二进制，只能自己发请求。"""
    url = f"{SENTRY_HOST}/api/0/projects/{target}/events/{event_id}/attachments/{attachment_id}/?download=1"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError(
                f"下载附件被拒（HTTP {error.code}）：token 可能已过期，执行 `sentry auth refresh`；"
                "或该账号没有这个项目的读取权限。"
            ) from error
        if error.code == 404:
            raise RuntimeError(
                "附件已不存在（HTTP 404）：Sentry 的保留策略可能已经清掉它，重新跑一次复现再取。"
            ) from error
        raise RuntimeError(f"下载附件失败：HTTP {error.code} {error.reason}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"下载附件失败（网络）：{error.reason}") from error


def survey_issue(sentry: str, short_id: str, *, verbose: bool, timeout: float) -> Issue:
    """看一个 issue 的最近一次事件：有没有附件、有没有日志、失败字段是什么。"""
    raw = report_common.run_sentry_json_value(
        sentry,
        ["issue", "view", short_id, "--json"],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"sentry issue view {short_id} 返回了非对象 JSON。")
    event = raw.get("event") or {}
    trace = raw.get("trace") or {}
    return Issue(
        short_id=str(raw.get("shortId") or short_id),
        title=str(raw.get("title") or ""),
        count=raw.get("count"),
        last_seen=str(raw.get("lastSeen") or ""),
        event_id=str(event.get("id") or ""),
        trace_id=str(trace.get("traceId") or ""),
        release=release_version(event),
        received=str(
            event.get("dateCreated") or event.get("timestamp") or ""
        ),
        evidence=issue_context(event),
    )


def event_issue(
    sentry: str, short_id: str, event_id: str, *, verbose: bool, timeout: float
) -> Issue:
    """按**指定事件**建 Issue（历史事件走这条）：trace / release / context 都取自该事件。

    `issue view` 只给**最新**事件，历史事件只能靠 `event view <id>`。同一份事件里
    `contexts.trace.trace_id` 与扁平 `context`（`attachment.status` 等点号键）都在，
    所以日志（按 trace 取）和失败字段都对得上这一次运行。
    """
    raw = report_common.run_sentry_json_value(
        sentry,
        ["event", "view", event_id, "--json"],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"sentry event view {event_id} 返回了非对象 JSON。")
    meta = survey_issue(sentry, short_id, verbose=verbose, timeout=timeout)
    trace = (raw.get("contexts") or {}).get("trace") or {}
    trace_id = str(
        trace.get("trace_id") or (raw.get("trace") or {}).get("trace_id") or ""
    )
    return Issue(
        short_id=meta.short_id,
        title=meta.title or str(raw.get("title") or raw.get("message") or ""),
        count=meta.count,
        last_seen=meta.last_seen,
        event_id=str(raw.get("id") or raw.get("eventID") or event_id),
        trace_id=trace_id,
        release=release_version(raw),
        received=str(raw.get("dateCreated") or raw.get("timestamp") or ""),
        evidence=issue_context(raw),
        event_only=True,
    )


def survey(
    sentry: str,
    target: str,
    *,
    period: str,
    limit: int,
    issues: Sequence[str] | None,
    events: Sequence[str] | None,
    verbose: bool,
    timeout: float,
    progress: bool = True,
) -> list[Issue]:
    """列出窗口里的 issue 并逐个看证据状态。

    没有批量接口（`sentry issue events --json` 是精简视图、没有 context），所以只能一个 issue 一次
    `issue view`——实测一次 ~7 s，12 个 issue 就是 ~90 s，进度必须打出来。
    """
    if issues:
        wanted = [i.strip().upper() for i in issues if i.strip()]
        event_ids = [e.strip() for e in (events or []) if e.strip()]
        if event_ids:
            # --event：按指定历史事件取（一个 issue × 一个事件 一条 Issue）。
            # 原来这里提前 return，把 --event 整个丢了（ISSUES 第 2 条）。
            if progress:
                report_common.show_progress(
                    f"检查 {len(wanted)} 个 issue × {len(event_ids)} 个指定事件…",
                    quiet=False,
                )
            out: list[Issue] = []
            for short in wanted:
                for eid in event_ids:
                    out.append(
                        event_issue(sentry, short, eid, verbose=verbose, timeout=timeout)
                    )
            return out
        if progress:
            report_common.show_progress(
                f"检查 {len(wanted)} 个指定 issue…", quiet=False
            )
        return [
            survey_issue(sentry, short, verbose=verbose, timeout=timeout)
            for short in wanted
        ]

    listing = report_common.run_sentry_json_value(
        sentry,
        [
            "issue",
            "list",
            target,
            "--period",
            period,
            "--limit",
            str(limit),
            "--json",
            "--fields",
            "shortId,title,count,lastSeen",
        ],
        verbose=verbose,
        timeout_seconds=timeout,
    )
    rows = listing.get("data") if isinstance(listing, dict) else listing
    if not isinstance(rows, list):
        raise RuntimeError(
            f"sentry issue list 返回的 JSON 里没有 data 列表：{type(listing).__name__}"
        )

    found: list[Issue] = []
    for index, row in enumerate(rows, 1):
        short = str(row.get("shortId") or "")
        if not short:
            continue
        if progress:
            report_common.show_progress(f"[{index}/{len(rows)}] {short} …", quiet=False)
        found.append(survey_issue(sentry, short, verbose=verbose, timeout=timeout))

    if events:
        # 没给 --issue 时无法把事件 id 映射到 short_id（历史事件只能按 issue + event 指定）
        raise ValueError("--event 需要同时给 --issue（例：--issue PYTHON-2H --event <id>）。")
    return found
