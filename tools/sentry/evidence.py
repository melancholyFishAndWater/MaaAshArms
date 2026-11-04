"""把某个失败的证据拉到本地：现场图（附件）+ **那次运行**的日志，并打印能直接跑的绘图命令。

    python -m tools.sentry.cli evidence --list                # 只报告有没有证据
    python -m tools.sentry.cli evidence --issue PYTHON-14     # 拉一个 issue
    python tools/sentry/evidence.py --period 7d               # 等价，直接跑文件

产出（`temp/`，不入库）刻意照真机 `debug/` 那棵树，`flow --case` 与 `flow_from_log` 两条路都认：

    temp/evidence/<SHORT-ID>/
      debug/maafw.log                          ← `sentry trace logs` 拼出来的那次运行日志
      debug/on_error/<run_id>/<时间戳>_<节点名>.png  ← **按失败事件分目录**：一个 issue 常有多次失败，
                                                  每次一份 `mfa-task-failure-<run_id>-<task_id>-screenshots.zip`
      evidence.json                            ← 账目：两道闸门的状态、失败字段、重复行数、写了什么、每个事件的图

## 现场图按失败事件逐个取（不是只取一个事件）

`sentry issue view` 只回**一个**事件，只查它就会永远只有那一份现场图（实测同一个 issue 有 3 份，
其中同一任务 `200000003` 在 10-31 与 11-02 两次运行里都失败）。这里枚举最近 `--max-events` 个事件，
逐个查附件列表，有图的下到 `debug/on_error/<run_id>/`；已下过的 run 跳过（增量，不重下、不覆盖）。
日志仍只取 `issue view` 那个事件（`issue events` 精简视图拿不到 trace id）。只画某一次失败就
`--on-error temp/evidence/<issue>/debug/on_error/<run_id>`。

## 两道 Sentry 侧的闸门（先看再下，不然白等）

1. `attachment.status`：`attached` 才有现场图。实测 2026-10-13~15 的 12 个 issue 里只有 2 个是
   `attached`，其余 `no_evidence`（附件根本没有）。这个字段在 `sentry issue view <ID> --json` 的
   `event.context` 里，键名是**扁平的** `attachment.status`，不是嵌套对象。
2. `logs.status`：`captured` 就有这次运行的日志。**没有附件不等于没得画**——实测两次 `BaseTaskStopError`
   的 issue 都是 `no_evidence`，但日志拿得到（同一条 trace 11787 行、34 条 `PipelineTask node done`），
   照样能画"节点循环"，只是没有画面格。

## 三个把路子堵住的 CLI 事实（都实测过）

- `sentry api` **打不出二进制**（下载端点加 `--silent` 也是空 stdout）→ 必须自己带 Bearer
  `GET https://sentry.io/api/0/projects/<org>/<project>/events/<event_id>/attachments/<id>/?download=1`。
  token 用 `sentry auth token --json` 拿（**裸 JSON 字符串**），不必去读 `cli.db` 的 `auth` 表。
- `sentry trace logs` 的 `--limit` **上限 1000 且没有 `--cursor`**：长运行一次拿不全。失败发生在运行
  末尾，所以默认 `--sort newest`（只认 `newest` / `oldest`；`-timestamp` 在 Windows 上会被当成参数别名报错）。
  拿到的条数、是否还有更多（`hasMore`）都写进清单。
- `sentry trace logs` 返回的批次之间有**逐字节相同的行**（实测 5951 行里 1366 行重复，其中 737 行是
  maafw 原生行：`node done` 24 条里有 6 条是重复的）。来源是 MFA 续写日志时带的
  `LogPreludeBytes = 64 KB` 前缀重叠（见 `.agents/evidence/project.md`）。不去重的话图上
  `--miss-min` 聚合框的次数会被灌水——正是"按行计数翻倍"那个老坑。所以默认去重并记账，`--keep-duplicates` 可关。

**为什么还要写一份 `maafw.log`**：附件 zip 里只有 `debug/on_error/*.png`，而 `flow_cases.load_case()`
对目录要求根下有 `maafw.log`/`maa.log`——直接把解出来的目录喂 `--case` 会报"找不到 maafw.log"。
工具把 `trace logs` 写进 `debug/maafw.log`，这条目录才成为完整的证据包。

**清单说的是目录里有什么，不是本次下了什么**（`written` / `events` / `hints` 都按这个口径）：工具不删旧文件，
所以 `--force` 配 `--no-log` / `--no-images` 只重下一半时，`collect()` 会先扫目录
（`existing_artifacts()`）再把磁盘上还在的文件补进清单（`reconcile_written()`）——否则清单写着
"没有日志"，而 `debug/maafw.log` 就在旁边放着，照清单走的人会以为画不了图。

**复用旧包时回显它属于哪个事件**：以前只打一句"已存在"，照旧包出图却不知道画的是哪一次；
现在打 `event.id` / `received` / `release`。事件对不上时不复用日志，会重新取。

**单份附件失败不废整包**：SSL 中断之类只跳过那一份、记进 `written.attachment_failed` 并提示，
日志和其它事件的现场图照常处理（以前一个附件失败会把已经能拿到的日志一起带没）。

**不做**：不翻页取全历史事件（`issue events` 没有游标，只取最近 `--max-events` 个）、不把
newest+oldest 拼起来（中间有洞比截断更误导）、不下载 vision 帧（附件里没有）、不自动画图
（只打印命令）、不打印也不落盘 token。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
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
    from .config import CONFIG
except ImportError:
    import report_common  # type: ignore[no-redef]
    from config import CONFIG  # type: ignore[no-redef]

from tools.drawing.flow_cases import LOG_NAMES
from tools.sentry.explore_csv import sort_lines

EVIDENCE_SCHEMA = 1
EVIDENCE_KIND = "sentry-evidence"

DEFAULT_OUTDIR = "temp/evidence"
DEFAULT_PERIOD = "7d"
DEFAULT_LIMIT = 20
DEFAULT_LOG_LIMIT = 1000
# 现场图按**失败事件**逐个取：一个 issue 常有多次失败、各一份 on_error 包，默认看最近 10 个事件。
# `sentry issue events` 没有批量接口、每次要单独查附件列表，所以上限必须有（不能无限翻）。
DEFAULT_MAX_EVENTS = 10
# Sentry CLI 自己限死的：`--limit` 只能 1~1000，且 trace logs 没有游标
MAX_LOG_LIMIT = 1000
LOG_SORTS = ("newest", "oldest")
HTTP_TIMEOUT_SECONDS = 60
SENTRY_HOST = "https://sentry.io"

# 附件名形如 mfa-task-failure-<run.id>-<task.id>-screenshots.zip：
# `run.id` 是 MFA 的一次运行（= 一个 Sentry 事件对应的那趟 trace），`task.id` 是失败的任务。
# 两个都要：run.id 是"哪次运行的现场图"，也是按失败事件分目录落盘的键（同一事件只有一份图），
# task.id 用来拼 `--task-id`。
ATTACHMENT_NAME_RE = re.compile(
    r"^mfa-task-failure-([0-9a-f]+)-(\d+)-screenshots\.zip$", re.IGNORECASE
)

# 失败标记：把 context 里的 node_id 换成人能读的名字时用（与 flow_from_log 的判据同一批）
MARKER_RES = (
    re.compile(r"Task timeout \[pretask\.name=([^\]]+)\]"),
    re.compile(r"invalid node id, handle error \[node\.name=([^\]]+)\]"),
    re.compile(r"node not completed, handle error \[node\.name=([^\]]+)\]"),
)


# --------------------------------------------------------------------------- #
# Sentry CLI
# --------------------------------------------------------------------------- #


def issue_context(event: dict[str, Any]) -> dict[str, Any]:
    """`event.context` 里那几个扁平键取出来，缺的补 None。

    键名的点号是**字符串的一部分**（`attachment.status`），不是层级——写成 `ctx["attachment"]["status"]`
    会永远取到 None。
    """
    ctx = event.get("context") or {}
    return {
        "attachment_status": ctx.get("attachment.status"),
        "attachment_image_count": ctx.get("attachment.image_count"),
        "attachment_bundle_bytes": ctx.get("attachment.bundle_bytes"),
        "attachment_selected_raw_bytes": ctx.get("attachment.selected_raw_bytes"),
        "logs_status": ctx.get("logs.status"),
        "logs_count": ctx.get("logs.count"),
        "logs_selected_raw_bytes": ctx.get("logs.selected_raw_bytes"),
        "logs_truncated": ctx.get("logs.truncated"),
        "failure_task_id": ctx.get("failure.task_id"),
        "failure_node_id": ctx.get("failure.node_id"),
        "failure_message": ctx.get("failure.message"),
        "failure_duration_ms": ctx.get("failure.duration_ms"),
        "instance_task_count": ctx.get("instance.task_count"),
    }


def release_version(event: dict[str, Any]) -> str:
    """`event.release` 归一成版本字符串。

    Sentry 的 `event.release` 是**对象**（`{"id":…, "version":"MFA@v…+MaaAshArms@v1.0.0-beta.55", …}`），
    不是字符串。直接 `str()` 会把 Python repr 写进 `evidence.json`（`"{'id': …}"`），
    下游 `version_label()` 虽然碰巧能从中抠出 `vX.Y.Z`，但那是脏数据。这里统一取 `version`。
    """
    rel = event.get("release")
    if isinstance(rel, dict):
        return str(rel.get("version") or "")
    return str(rel or "")


def wants_images(evidence: dict[str, Any]) -> bool:
    return evidence.get("attachment_status") == "attached"


def wants_log(evidence: dict[str, Any]) -> bool:
    return evidence.get("logs_status") == "captured"


@dataclass
class Issue:
    """一次 survey 的结论：这个 issue 值不值得下、下什么。"""

    short_id: str
    title: str
    count: Any
    last_seen: str
    event_id: str
    trace_id: str
    # Sentry 事件顶层字段 `release`（如 `MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38`），
    # 画图时它是「疑似过时」最可靠的版本来源——原生 maafw.log 没有「资源版本」那行。
    release: str = ""
    # 事件时间（`dateCreated`/`timestamp`）：复用旧包时要能说出"这是哪一次失败"
    received: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def images(self) -> bool:
        return wants_images(self.evidence)

    @property
    def logs(self) -> bool:
        return wants_log(self.evidence)

    @property
    def has_anything(self) -> bool:
        return self.images or self.logs


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
        # --event 直接给事件 id（issue 的历史事件走这条），先按 issue list 的顺序找它的归属
        wanted_ids = {e.strip().lower() for e in events if e.strip()}
        found = [issue for issue in found if issue.event_id.lower() in wanted_ids]
    return found


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


# --------------------------------------------------------------------------- #
# 纯函数（自检直接测这些，不写文件、不联网）
# --------------------------------------------------------------------------- #


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


def build_hints(
    outdir: Path,
    issue: Issue,
    *,
    log_rel: str | None,
    has_images: bool,
    runs: Sequence[str] = (),
) -> list[str]:
    """可复制的下一步命令（相对仓库根，命令都在仓库根跑）。

    `log_rel` / `has_images` 说的是**磁盘上有没有**（`existing_artifacts()` 的结论），不是本次下没下：
    清单里写着日志就一定能画，别再让"本次没下"把可用的命令藏掉。文件名也照磁盘上那个来
    （`LOG_NAMES` 里可能是 `maa.log`），别硬写 `maafw.log` 指到不存在的文件。

    `runs` 非空且不止一个时，多给一条"只画某一次失败"的写法：现场图按 run_id 分了目录，
    `--on-error <case>/on_error/<run_id>` 就能只贴那一次的现场图。
    """
    if not log_rel:
        return []
    root = report_common.repo_root()
    try:
        shown = outdir.relative_to(root).as_posix()
    except ValueError:
        shown = outdir.as_posix()
    case_dir = f"{shown}/debug"
    hints: list[str] = []
    task = issue.evidence.get("failure_task_id")
    task_arg = f" --task-id {task}" if task else ""
    hints.append(f"python -m tools.sentry.cli flow --case {case_dir}{task_arg}")
    if has_images:
        hints.append(
            f"python tools/drawing/flow_from_log.py {shown}/{log_rel} "
            f"--on-error {case_dir}/on_error{task_arg}"
        )
    else:
        hints.append(
            f"python tools/drawing/flow_from_log.py {shown}/{log_rel}{task_arg} --no-thumbs"
        )
    ordered = sorted(runs)
    if len(ordered) > 1:
        shown_runs = "、".join(ordered[:6]) + ("…" if len(ordered) > 6 else "")
        hints.append(
            f"只贴某一次失败的现场图：--on-error {case_dir}/on_error/<run_id>"
            f"（共 {len(ordered)} 次：{shown_runs}）"
        )
    return hints


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


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #


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


@dataclass
class Evidence:
    """一个 issue 的产出。"""

    directory: Path
    inventory: dict[str, Any]
    hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def collect(
    sentry: str,
    issue: Issue,
    *,
    outdir: Path,
    token: str | None,
    do_images: bool,
    do_log: bool,
    log_limit: int,
    log_sort: str,
    force: bool,
    keep_duplicates: bool,
    verbose: bool,
    timeout: float,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> Evidence:
    """把一个 issue 的证据落盘；已经下过就补缺、不重下（`--force` 重新下）。

    清单里 `written` 说的是**这个证据目录里有什么**，不是"本次下了什么"：本次没下但磁盘上还在的
    文件（`--force` 配 `--no-log` / `--no-images`）照样记进去，`written` 与 `hints` 才对得上磁盘。

    现场图按**失败事件**逐个取（见 `collect_image_attachments`）：一个 issue 常有多次失败、各一份
    on_error 包，只取"issue view 那一个事件"就永远只有那一份（实测 3 份不同运行的图）。这里枚举
    最近 `max_events` 个事件、逐个下到 `debug/on_error/<run_id>/`，已下过的 run 跳过（增量：
    不重下，也不覆盖别的事件）。
    """
    directory = outdir / issue.short_id
    manifest_path = directory / "evidence.json"
    recorded: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                recorded = loaded
        except (OSError, json.JSONDecodeError):
            recorded = {}
    reused = bool(recorded) and recorded.get("event", {}).get("id") == issue.event_id

    written: dict[str, Any] = {
        "log": None,
        "log_bytes": 0,
        "images": [],
        "zip_bytes": 0,
    }
    trace_meta: dict[str, Any] = {}
    notes: list[str] = []
    # 上一趟留下的文件（`--force` 只重下一半时它们仍在原地）：先记下来，免得清单与磁盘脱节
    existing_log, existing_images = existing_artifacts(directory)
    have_runs = existing_run_ids(existing_images)

    jobs: list[dict] = []
    downloaded_images = 0
    if do_images:
        jobs = collect_image_attachments(
            sentry, issue, verbose=verbose, timeout=timeout, max_events=max_events
        )
        wanted = jobs if force else [j for j in jobs if j["run_id"] not in have_runs]
        failed: list[str] = []
        for job in wanted:
            if token is None:
                token = sentry_token(sentry, verbose=verbose, timeout=timeout)
            # 单份附件失败不能把整包废掉：原来一个 SSL 中断就抛出去了，连这次已经能拿到的
            # 日志和别的失败的现场图一起没了（ISSUES 里记的那条）。这里跳过它、继续下一份。
            try:
                blob = download_attachment(
                    CONFIG.target, str(job["event_id"]), str(job["attachment_id"]), token
                )
            except (RuntimeError, OSError) as exc:
                failed.append(f"{job['run_id']}（{exc}）")
                continue
            if blob[:4] != b"PK\x03\x04":
                notes.append(
                    f"附件 {job['name']} 不是 zip（前 4 字节 {blob[:4]!r}），跳过"
                )
                continue
            written["zip_bytes"] += len(blob)
            written["images"].extend(
                write_event_images(blob, directory, str(job["run_id"]))
            )
            have_runs.add(str(job["run_id"]))
            downloaded_images += 1
        if failed:
            written["attachment_failed"] = failed
            notes.append(
                f"{len(failed)} 份现场图没下成，已跳过（日志和其它事件的现场图照常）："
                + "；".join(failed)
            )
        if not jobs:
            notes.append(
                f"没有现场图（最近 {max_events} 个事件里都没有 "
                "`mfa-task-failure-*.zip` 附件）"
            )
        elif not wanted:
            notes.append(
                f"现场图共 {len(jobs)} 份、都是上一趟下过的（增量只补新的失败事件）"
            )

    got_log = False
    # 日志仍取 `issue view` 那个事件的 trace（`issue events` 精简视图拿不到 trace id）。
    # 已下过且没 --force 就不重下，由 reconcile 把磁盘上那份记进清单。
    if do_log and issue.logs and issue.trace_id and (force or not existing_log):
        lines, has_more = fetch_log_lines(
            sentry,
            CONFIG.target,
            issue.trace_id,
            limit=log_limit,
            sort=log_sort,
            verbose=verbose,
            timeout=timeout,
        )
        total = len(lines)
        kept, dropped = dedupe_lines(lines) if not keep_duplicates else (list(lines), 0)
        ordered, out_of_order = sort_lines(kept)
        log_path = directory / "debug" / LOG_NAMES[0]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(ordered) + ("\n" if ordered else "")
        log_path.write_text(text, encoding="utf-8", newline="\n")
        written["log"] = f"debug/{LOG_NAMES[0]}"
        written["log_bytes"] = len(text.encode("utf-8"))
        trace_meta = {
            "id": issue.trace_id,
            "items_limit": log_limit,
            "sort": log_sort,
            "has_more": has_more,
            "lines": total,
            "duplicates_dropped": dropped,
            "out_of_order": out_of_order,
            "markers": marker_lines(ordered),
        }
        if has_more:
            notes.append(
                f"日志超过 {log_limit} 条上限（Sentry CLI 没有游标）：只含 {log_sort} 的那一段"
            )
        if dropped:
            notes.append(
                f"去掉 {dropped} 行完全重复的行（MFA 续写日志的 64 KB 前缀重叠）"
            )
        if not marker_lines(ordered):
            notes.append(
                "日志里没有看到 Task timeout / invalid node id / node not completed"
            )
        # 日志是"整个运行"的：一次运行常排多个任务，标记可能来自同一运行里的别的任务。
        # 同一条 trace 会同时挂多个 issue（实测 PYTHON-14 与 PYTHON-16 就是这样），
        # 所以上面那几条标记不能默认都算在这个 issue 头上——用 --task-id 定到 failure.task_id 才准。
        tasks_run = issue.evidence.get("instance_task_count")
        try:
            task_count = int(tasks_run) if tasks_run is not None else 0
        except (TypeError, ValueError):
            task_count = 0
        if task_count > 1:
            notes.append(
                f"这次运行共 {task_count} 个任务，日志是整段的：标记可能来自别的任务，"
                f"按 --task-id {issue.evidence.get('failure_task_id')} 才只看本次失败的那个"
            )
        got_log = True
    elif do_log and not existing_log:
        notes.append(
            f"没有日志（logs.status={issue.evidence.get('logs_status') or '未知'}）"
        )
    if not got_log and isinstance(recorded.get("trace"), dict):
        # 复用时保留上一趟的 trace 账（行数/去重/标记），别让清单里那几项变空
        trace_meta = dict(recorded["trace"])

    # 清单记的是"目录里有什么"：本次没下但磁盘上还在的（`--force` 配 `--no-log`/`--no-images`）也补进来
    reconcile_written(directory, written, existing_log, existing_images, notes)

    runs = sorted(existing_run_ids(written["images"]))
    has_log = written["log"] is not None
    hints = build_hints(
        directory,
        issue,
        log_rel=str(written["log"]) if has_log else None,
        has_images=bool(written["images"]),
        runs=runs,
    )
    if not hints:
        # hints 为空 = 没有日志（`build_hints` 第一条就要求 log_rel）；现场图再多也画不了流程图
        if written["images"]:
            notes.append(
                f"没有日志：画不出流程图（现场图有 {len(written['images'])} 张，"
                "但图上的步骤来自日志）"
            )
        else:
            notes.append("既没有日志也没有现场图：画不出流程图（画图必须有日志）")
    if reused and downloaded_images == 0 and not got_log:
        # 复用旧包时必须回显它属于哪个事件：否则照着旧包出图却不知道画的是哪一次（踩过的坑）
        old = recorded.get("event", {}) or {}
        bits = []
        if old.get("id"):
            bits.append(f"event.id={old['id']}")
        if old.get("received"):
            bits.append(f"received={old['received']}")
        if old.get("release"):
            bits.append(f"release={old['release']}")
        detail = f"（{'，'.join(bits)}）" if bits else ""
        notes.append(f"已存在（--force 重新下载）{detail}")

    inventory = {
        "schema": EVIDENCE_SCHEMA,
        "kind": EVIDENCE_KIND,
        "generated_at": report_common.utc_now_iso(),
        "issue": {
            "short_id": issue.short_id,
            "title": issue.title,
            "count": issue.count,
            "last_seen": issue.last_seen,
        },
        "event": {
            "id": issue.event_id,
            "received": issue.received,
            **issue.evidence,
            "release": issue.release,
        },
        # 每个失败事件一份现场图（`debug/on_error/<run_id>/`）：哪份图属哪次运行、哪个 task
        "events": event_index(written["images"], jobs),
        "trace": trace_meta,
        "written": written,
        "hints": hints,
        "notes": notes,
    }
    directory.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(inventory, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return Evidence(directory=directory, inventory=inventory, hints=hints, notes=notes)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def format_survey(issues: Sequence[Issue], output: TextIO) -> None:
    """survey 结果表：一眼看出哪些有图、哪些只有日志。"""
    rows = []
    for issue in issues:
        if not issue.has_anything:
            mark = "无证据"
        elif issue.images:
            mark = "图 + 日志" if issue.logs else "只有图"
        else:
            mark = "只有日志"
        rows.append(
            (
                issue.short_id,
                str(issue.count),
                str(issue.evidence.get("attachment_status") or "-"),
                str(issue.evidence.get("logs_status") or "-"),
                mark,
                issue.title[:38],
            )
        )
    report_common.write_console_table(
        ("issue", "次数", "附件", "日志", "可拉到的", "标题"),
        rows,
        output,
        right_aligned={1},
    )


def format_result(evidence: Evidence, output: TextIO, stderr: TextIO) -> None:
    inventory = evidence.inventory
    written = inventory.get("written", {})
    trace = inventory.get("trace", {})
    print(f"\n{evidence.directory.as_posix()}", file=output)
    print(
        f"  现场图: {len(written.get('images') or [])} 个"
        f"（附件 {written.get('zip_bytes', 0)} 字节）",
        file=output,
    )
    events = inventory.get("events") or []
    if events:
        # 一个失败事件一份现场图：列出「run_id（task）几张」，
        # 现场图目录是 `debug/on_error/<run_id>/`，要只画某一次就 --on-error 指过去
        rows = []
        for item in events:
            task = item.get("task_id")
            count = len(item.get("images") or [])
            rows.append(f"{item['run_id']}（task {task or '?'}，{count} 张）")
        print(f"  失败事件: {len(events)} 个 —— {'；'.join(rows)}", file=output)
    failed = written.get("attachment_failed") or []
    if failed:
        print(
            f"  现场图未下成: {len(failed)} 份（已跳过，不影响日志）—— {'；'.join(failed)}",
            file=output,
        )
    if written.get("log"):
        print(
            f"  日志:   {written['log']}（{written['log_bytes']} 字节"
            f"；拿到 {trace.get('lines')} 行，去掉重复 {trace.get('duplicates_dropped')} 行"
            f"{'，还有更多' if trace.get('has_more') else ''}）",
            file=output,
        )
    else:
        print("  日志:   无", file=output)
    rel = inventory.get("event", {}).get("release")
    if rel:
        label = report_common.version_label(rel)
        tag = f"（资源版本 {label}）" if label else ""
        print(f"  release: {rel}{tag}", file=output)
    for marker in trace.get("markers") or []:
        print(f"  失败标记: {marker}", file=output)
    for hint in evidence.hints:
        print(f"  下一步: {hint}", file=output)
    for note in evidence.notes:
        print(f"  提示: {note}", file=stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=report_common.module_summary(__doc__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"扫哪段时间的 issue（默认 {DEFAULT_PERIOD}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"最多看几个 issue（默认 {DEFAULT_LIMIT}）",
    )
    parser.add_argument(
        "--issue", action="append", help="只处理这个 issue（如 PYTHON-14，可重复）"
    )
    parser.add_argument(
        "--event",
        action="append",
        help="只处理这个事件 id（可重复；issue 的历史事件用这条）",
    )
    parser.add_argument(
        "--outdir", default=DEFAULT_OUTDIR, help=f"产出目录（默认 {DEFAULT_OUTDIR}）"
    )
    parser.add_argument("--list", action="store_true", help="只报告证据状态，不下载")
    parser.add_argument(
        "--force",
        action="store_true",
        help="已经下过也重新下（只补本次允许下的那些；旧文件不删，仍会记进清单）",
    )
    parser.add_argument("--no-images", action="store_true", help="不下载现场图")
    parser.add_argument("--no-log", action="store_true", help="不取日志")
    parser.add_argument(
        "--log-limit",
        type=int,
        default=DEFAULT_LOG_LIMIT,
        help=f"取多少条日志（上限 {MAX_LOG_LIMIT}，失败在末尾所以默认取最新那段）",
    )
    parser.add_argument(
        "--sort",
        choices=LOG_SORTS,
        default=LOG_SORTS[0],
        help=f"日志取哪一段：{LOG_SORTS[0]}（默认，失败在末尾）/ {LOG_SORTS[1]}",
    )
    parser.add_argument(
        "--keep-duplicates", action="store_true", help="保留完全重复的行（默认去重）"
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=DEFAULT_MAX_EVENTS,
        help=f"现场图按失败事件逐个取，最多看最近几个事件（默认 {DEFAULT_MAX_EVENTS}）。"
        "一个 issue 常有多次失败、各一份 on_error 包，只取 issue view 那一个事件会只有最旧的那份",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="单次 Sentry 调用超时秒数（默认 180）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 sentry 查询命令")
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = create_argument_parser(prog).parse_args(argv)
    if arguments.no_images and arguments.no_log:
        raise ValueError("--no-images 与 --no-log 一起给就没有任何东西可拉。")

    sentry = report_common.resolve_sentry_command()
    issues = survey(
        sentry,
        CONFIG.target,
        period=arguments.period,
        limit=arguments.limit,
        issues=arguments.issue,
        events=arguments.event,
        verbose=arguments.verbose,
        timeout=arguments.timeout,
    )
    if not issues:
        print("没有匹配的 issue。", file=sys.stderr)
        return 0

    if arguments.list:
        format_survey(issues, sys.stdout)
        return 0

    outdir = Path(arguments.outdir)
    outdir = outdir if outdir.is_absolute() else report_common.repo_root() / outdir
    format_survey(issues, sys.stdout)

    token: str | None = None
    collected: list[Evidence] = []
    for issue in issues:
        if not issue.has_anything:
            print(
                f"\n跳过 {issue.short_id}：attachment.status="
                f"{issue.evidence.get('attachment_status') or '未知'}、logs.status="
                f"{issue.evidence.get('logs_status') or '未知'}",
                file=sys.stderr,
            )
            continue
        if issue.images and token is None and not arguments.no_images:
            # token 只取一次；不打印、不落盘、不进清单
            token = sentry_token(
                sentry, verbose=arguments.verbose, timeout=arguments.timeout
            )
        evidence = collect(
            sentry,
            issue,
            outdir=outdir,
            token=token,
            do_images=not arguments.no_images,
            do_log=not arguments.no_log,
            log_limit=arguments.log_limit,
            log_sort=arguments.sort,
            force=arguments.force,
            keep_duplicates=arguments.keep_duplicates,
            verbose=arguments.verbose,
            timeout=arguments.timeout,
            max_events=arguments.max_events,
        )
        collected.append(evidence)
        format_result(evidence, sys.stdout, sys.stderr)

    if collected:
        print(
            f"\n共 {len(collected)} 个证据目录落到 {outdir.as_posix()}", file=sys.stdout
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
