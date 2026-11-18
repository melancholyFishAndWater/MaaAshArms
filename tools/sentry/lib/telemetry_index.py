"""本地遥测库：把 Sentry issue 的详情**增量**存到 `temp/`，供画图离线查"这个包对应哪个问题"。

## 为什么需要它

用户发来的是**本地日志包**，而画图是离线的（`flow` 不联网）。两边唯一对得上的键是日志里的
失败信息：`Node.PipelineNode.Failed` 的 `name` / `node_id` / `task_id`，以及 `Task timeout` /
`node not completed` 的节点名。Sentry 事件 context 里正好有 `failure.task_id` / `failure.node_id`
（实测 30 个真实包里 25 个能靠这两项精确对上）。

所以做法是：联网时把 issue 详情**增量**落一份本地库（只补新增或 `lastSeen` 变了的），
画图时在库里查——查询零网络、可复现。

## 产出

    temp/telemetry/issues/<SHORT-ID>.json   ← 每个 issue 一份（issue view 的解析结果）
    temp/telemetry/index.json               ← {schema, issues: {<SHORT>: {status, lastSeen, fetched_at}}}

## 清理（用户明确要求：过时 / 已解决的数据不能留）

- `status` 是 resolved/ignored/archived → 默认**删**（`--keep-resolved` 才留）；
- `lastSeen` 早于 `--telemetry-days`（默认 14 天）→ 删；
- 不在最近一次 `issue list` 返回里 → 删（服务器上已消失）；
- `index.json` 的 `schema` 变了 → 整库重建，不做迁移。

**不做**：不缓存 spans/聚合数据（那是 `fetch` 的活）、不自动删别的工具写的 `temp/` 文件、
不猜测"哪个 issue 一定对"——匹配永远给候选和级别，不硬猜（实测一次运行常对应多个 issue）。
"""

from __future__ import annotations

import json
import copy
import shutil
import tempfile
import time
import uuid
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
for _path in (_HERE, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from tools._logging import configure_tool_logging

log = logging.getLogger("tools.telemetry_index")

try:
    from . import report_common
    from .config import CONFIG
    from .evidence import issue_context, release_version
    from .telemetry_candidates import FailureCandidate, extract_candidates
except ImportError:  # 直接跑文件时
    from lib import report_common  # type: ignore[no-redef]
    from lib.config import CONFIG  # type: ignore[no-redef]
    from lib.evidence import issue_context, release_version  # type: ignore[no-redef]
    from lib.telemetry_candidates import FailureCandidate, extract_candidates  # type: ignore[no-redef]

SCHEMA = 1
STORE_DIR = "temp/telemetry/issues"
INDEX_NAME = "index.json"
DEFAULT_DAYS = 14
DEFAULT_GAP_DAYS = 3
# `--telemetry-max-age` 的默认值：这么多秒内刷新过就直接读本地库，不再起 sentry 子进程。
# 5 分钟足够"调一张图连跑几条命令"的场景，又不会让人对着隔夜的库当最新数据用。
DEFAULT_MAX_AGE = 300.0
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 8.0
RETRY_DELAY = 0.2
# 匹配级别：越小越强。排序与展示都用它
TIER_RANK = {"high": 0, "mid": 1, "low": 2}
TIER_LABEL = {"high": "高", "mid": "中", "low": "低"}

# issue title：`Maa task failed: Entrust (EntrustBranchChapter/recognition)`
TITLE_RE = re.compile(r"^Maa task failed:\s*(.*?)\s*\(([^/]+)/([^/)]+)\)\s*$")
# 任务级失败没有 `(节点/阶段)` 后缀：`Maa task failed: Replays`——整段就是任务名，
# 不能当成"没有任务名"（画图要靠它找 `TaskReplays` 那个 task）
TITLE_PLAIN_RE = re.compile(r"^Maa task failed:\s*(.+?)\s*$")
# 日志里的行首时间（与 maafw 的 LINE_RE 同格式，本地自带一份避免反向依赖绘图模块）
# `Node.PipelineNode.Failed` 两种写法：带 details 的（有 node_id/task_id），和只有 [name=] 的
# 其它失败标记（与 evidence.MARKER_RES 同一批判据）

ISSUE_OPEN_STATUSES = {"", "open", "unresolved", "archived"}
ISSUE_CLOSED_STATUSES = {"resolved", "ignored", "muted", "deleted"}


# --------------------------------------------------------------------------- #
# 卡片与候选
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class IssueCard:
    """一个 issue 的详情（从 `sentry issue view --json` 解析）。"""

    short_id: str
    title: str = ""
    count: Any = None
    status: str = ""
    last_seen: str = ""
    received: str = ""
    release: str = ""  # 归一成版本字符串（MFA release 可能是对象）
    failure_task_id: int | None = None
    failure_node_id: int | None = None
    failure_message: Any = None
    attachment_status: Any = None
    logs_status: Any = None
    event_id: str = ""
    permalink: str = ""

    @property
    def label(self) -> str:
        """title 里的任务名（`Maa task failed: Entrust (…)` → `Entrust`）。

        任务级失败没有 `(节点/阶段)` 后缀（`Maa task failed: Replays`），整段就是任务名：
        画图要靠它找 `TaskReplays` 那个 task，返回空就等于"不知道在画哪个任务"。
        """
        match = TITLE_RE.match(self.title)
        if match:
            return match.group(1).strip()
        plain = TITLE_PLAIN_RE.match(self.title)
        return plain.group(1).strip() if plain else ""

    @property
    def node_name(self) -> str:
        match = TITLE_RE.match(self.title)
        return match.group(2) if match else ""

    @property
    def stage(self) -> str:
        match = TITLE_RE.match(self.title)
        return match.group(3) if match else ""

    def to_json(self) -> dict:
        return {
            "short_id": self.short_id,
            "title": self.title,
            "count": self.count,
            "status": self.status,
            "last_seen": self.last_seen,
            "received": self.received,
            "release": self.release,
            "failure_task_id": self.failure_task_id,
            "failure_node_id": self.failure_node_id,
            "failure_message": self.failure_message,
            "attachment_status": self.attachment_status,
            "logs_status": self.logs_status,
            "event_id": self.event_id,
            "permalink": self.permalink,
        }


@dataclass(frozen=True)
class Match:
    card: IssueCard
    tier: str  # high | mid | low
    detail: str
    gap_seconds: float | None = None


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #


def _parse_time(text: str) -> datetime | None:
    """Sentry 的时刻（UTC）：ISO `...Z` 或 `YYYY/MM/DD HH:MM:SS`。解析不出返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _utc_offset() -> timedelta:
    """本机时区偏移：本地日志是 naive 本地时间，Sentry 是 UTC，比较前要换算。"""
    offset = datetime.now().astimezone().utcoffset()
    return offset or timedelta(0)


def _local_as_utc(ts: datetime) -> datetime:
    return (ts - _utc_offset()).replace(tzinfo=timezone.utc)


def _fmt_time(value: datetime) -> str:
    return value.strftime("%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 本地库
# --------------------------------------------------------------------------- #


def store_dir() -> Path:
    return report_common.repo_root() / STORE_DIR


def index_path() -> Path:
    return store_dir() / INDEX_NAME


def card_path(short_id: str, index: dict | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", short_id):
        raise RuntimeError(f"非法 issue ID：{short_id!r}")
    selected = load_index() if index is None else index
    snapshot = selected.get("snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot):
            raise RuntimeError("本地遥测库 snapshot 非法，拒绝读取")
        return store_dir() / "_snapshots" / snapshot / f"{short_id}.json"
    return store_dir() / f"{short_id}.json"


def load_index(*, strict: bool = False) -> dict:
    path = index_path()
    if not path.is_file():
        return {"schema": SCHEMA, "issues": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("issues"), dict):
            raise ValueError("索引必须含 issues 对象")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in data["issues"].items()):
            raise ValueError("issues 条目必须为对象")
        snapshot = data.get("snapshot")
        if snapshot is not None and (not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot)):
            raise ValueError("snapshot 必须为生成代 ID")
        return data
    except (OSError, ValueError) as error:
        if strict:
            raise RuntimeError(f"本地遥测索引不可读：{path}: {error}") from error
        return {"schema": SCHEMA, "issues": {}}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_index(index: dict) -> None:
    """固定 index.json 是快照发布的唯一提交点，同目录原子替换。"""
    _atomic_json(index_path(), index)


def read_card(short_id: str, index: dict | None = None) -> IssueCard | None:
    try:
        path = card_path(short_id, index)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("short_id") != short_id:
            return None
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            return None
        return IssueCard(**{k: v for k, v in data.items() if k in IssueCard.__dataclass_fields__})
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def _drop(
    short_id: str, notes: list[str], index: dict | None = None, reason: str = ""
) -> None:
    """把一个 issue 从本地库清掉：卡片文件 + `index["issues"]` 里的条目。

    只删卡片文件不够——索引项还在的话，"库里有几个 issue"（索引）和"实际能画几个"
    （卡片）会对不上，`load_cards` 也会静默跳过（实测 13 vs 11）。

    只在**确实清掉了东西**时才记一条：服务端列表里有、但本来就没入库的 issue（例如超过
    `--telemetry-days`、每轮都会被扫到）不该每轮刷一条"清掉"（实测 PYTHON-Q 每跑一次刷新
    就报一次，看着像库在反复增删）。`reason` 说明为什么清，跟着这一条一起打出来。
    """
    path = card_path(short_id)
    removed = False
    if path.is_file():
        try:
            path.unlink()
            removed = True
        except OSError as exc:
            notes.append(f"{short_id} 的卡片删不掉（{exc}）")
            return
    if index is not None:
        dropped = index.get("issues", {}).pop(short_id, None) is not None
        removed = removed or dropped
    if removed:
        notes.append(
            f"{short_id} 从本地遥测库清掉（{reason}）"
            if reason
            else f"{short_id} 从本地遥测库清掉"
        )


def write_card(card: IssueCard) -> None:
    """兼容单卡写入 API；刷新批次不调用它，以免改动当前快照。"""
    _atomic_json(card_path(card.short_id), card.to_json())


# --------------------------------------------------------------------------- #
# 联网取数（增量）
# --------------------------------------------------------------------------- #


def _unsupported_query(error: BaseException) -> bool:
    message = str(error).lower()
    return "query" in message and any(token in message for token in (
        "unknown option", "unrecognized option", "unrecognised option", "unexpected argument",
    ))


def _request(sentry: str, arguments: list[str], *, verbose: bool, timeout: float, validate):
    operation = " ".join(arguments[:3])
    for attempt in range(1, DEFAULT_ATTEMPTS + 1):
        log.info("遥测请求 %s attempt %d/%d", operation, attempt, DEFAULT_ATTEMPTS)
        try:
            payload = report_common.run_sentry_json_value(
                sentry, arguments, verbose=verbose, timeout_seconds=timeout,
            )
            return validate(payload)
        except (RuntimeError, OSError) as error:
            if "--query" in arguments and _unsupported_query(error):
                raise
            if attempt == DEFAULT_ATTEMPTS:
                raise RuntimeError(f"遥测请求 {operation} 在 {DEFAULT_ATTEMPTS} 次尝试后失败：{error}") from error
            log.warning("遥测请求 %s attempt %d/%d 失败：%s", operation, attempt, DEFAULT_ATTEMPTS, error)
            time.sleep(RETRY_DELAY)
    raise AssertionError("unreachable")


def list_issues(sentry: str, target: str, *, period: str, limit: int,
                verbose: bool, timeout: float, query: str | None = None) -> list[dict]:
    arguments = ["issue", "list", target, "--period", period, "--limit", str(limit), "--json"]
    if query:
        arguments += ["--query", query]

    def validate(payload):
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise RuntimeError("issue list 响应必须是数组或含 data 数组的对象")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("shortId"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", row["shortId"]):
                raise RuntimeError("issue list 含缺少有效 shortId 的记录")
            short = row["shortId"]
            if short in seen:
                raise RuntimeError(f"issue list 含重复 shortId：{short}")
            seen.add(short)
        return rows

    return _request(sentry, arguments, verbose=verbose, timeout=timeout, validate=validate)


def issue_view(sentry: str, short_id: str, *, verbose: bool, timeout: float) -> dict:
    def validate(raw):
        if not isinstance(raw, dict) or raw.get("shortId") != short_id:
            raise RuntimeError(f"issue view {short_id} 身份缺失或不一致")
        if not isinstance(raw.get("title"), str) or not raw["title"].strip():
            raise RuntimeError(f"issue view {short_id} 缺少 title")
        event = raw.get("event")
        if not isinstance(event, dict) or not event:
            raise RuntimeError(f"issue view {short_id} 缺少 event 对象")
        event_id = event.get("id") or event.get("eventID") or event.get("eventId")
        if not isinstance(event_id, str) or not event_id.strip():
            raise RuntimeError(f"issue view {short_id} 缺少事件 ID")
        return raw

    return _request(sentry, ["issue", "view", short_id, "--json"],
                    verbose=verbose, timeout=timeout, validate=validate)


def parse_issue_view(raw: dict, short_id: str) -> IssueCard:
    """`issue view --json` → IssueCard。

    `event.release` 在 Sentry 里是**对象**（含 `version`），不是字符串——直接 `str()` 会写成
    Python repr（上一版就这么错了）。这里统一取 `version`。
    """
    event = raw.get("event") or {}
    ctx = issue_context(event)
    return IssueCard(
        short_id=str(raw.get("shortId") or short_id),
        title=str(raw.get("title") or ""),
        count=raw.get("count"),
        status=str(raw.get("status") or event.get("status") or ""),
        last_seen=str(raw.get("lastSeen") or raw.get("lastEvent") or ""),
        received=str(event.get("dateCreated") or event.get("timestamp") or ""),
        release=release_version(event),
        failure_task_id=_as_int(ctx.get("failure_task_id")),
        failure_node_id=_as_int(ctx.get("failure_node_id")),
        failure_message=ctx.get("failure_message"),
        attachment_status=ctx.get("attachment_status"),
        logs_status=ctx.get("logs_status"),
        event_id=str(event.get("id") or event.get("eventID") or event.get("eventId") or ""),
        permalink=str(raw.get("permalink") or ""),
    )


def refresh(
    sentry: str, target: str, *, verbose: bool = False, timeout: float = DEFAULT_TIMEOUT,
    days: int = DEFAULT_DAYS, keep_resolved: bool = False, period: str = "30d",
    limit: int = 200, full_details: bool = False,
) -> tuple[dict, list[str]]:
    """所有请求及校验成功后发布完整代；失败不改当前索引或任何旧卡片。"""
    if limit <= 0 or timeout <= 0:
        raise RuntimeError("遥测 limit 和 timeout 必须大于 0")
    previous = load_index(strict=True)
    notes: list[str] = []
    if not keep_resolved:
        try:
            rows = list_issues(sentry, target, period=period, limit=limit, verbose=verbose,
                               timeout=timeout, query="is:unresolved")
        except RuntimeError as error:
            if not _unsupported_query(error):
                raise
            notes.append("sentry CLI 明确不支持 --query，兼容使用未过滤列表")
            rows = list_issues(sentry, target, period=period, limit=limit, verbose=verbose, timeout=timeout)
    else:
        rows = list_issues(sentry, target, period=period, limit=limit, verbose=verbose, timeout=timeout)
    if len(rows) >= limit:
        raise RuntimeError(f"issue list 返回 {len(rows)} 条达到 --limit {limit}，可能截断；拒绝发布不完整快照")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    index: dict[str, Any] = copy.deepcopy(previous) if previous.get("schema") == SCHEMA else {"schema": SCHEMA}
    index["issues"] = {}
    cards: dict[str, IssueCard] = {}
    fetched: list[str] = []
    for row in rows:
        short = row["shortId"]
        status = str(row.get("status") or "").lower()
        last_seen_raw = str(row.get("lastSeen") or row.get("lastEvent") or "")
        last_seen = _parse_time(last_seen_raw)
        if (status in ISSUE_CLOSED_STATUSES and not keep_resolved) or (last_seen is not None and last_seen < cutoff):
            continue
        meta = {"status": status, "lastSeen": last_seen_raw}
        cached = previous.get("issues", {}).get(short) or {}
        card = None
        if not full_details and previous.get("schema") == SCHEMA and all(cached.get(k) == v for k, v in meta.items()):
            card = read_card(short, previous)
        if card is None:
            card = parse_issue_view(issue_view(sentry, short, verbose=verbose, timeout=timeout), short)
            fetched.append(short)
            cached = {"fetched_at": now.isoformat()}
        cards[short] = card
        index["issues"][short] = {**meta, "fetched_at": cached.get("fetched_at", now.isoformat())}
    removed = set(previous.get("issues", {})) - set(index["issues"])
    if removed:
        notes.append("本地遥测库移出当前快照：" + "、".join(sorted(removed)))
    generation = uuid.uuid4().hex
    stage = store_dir() / "_snapshots" / generation
    try:
        stage.mkdir(parents=True, exist_ok=False)
        for short, card in cards.items():
            _atomic_json(stage / f"{short}.json", card.to_json())
        index["snapshot"] = generation
        index["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        save_index(index)
    except (OSError, RuntimeError, ValueError, TypeError):
        # 只删本次未发布目录；旧代保留，已持有旧 index 的读者仍可一致读取。
        if stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError as cleanup_error:
                log.warning("未发布快照清理失败 %s：%s", stage, cleanup_error)
        raise
    notes.append(f"本地遥测库完整快照 {generation} 已发布：{len(cards)} 个 issue，重取详情 {len(fetched)} 个")
    return index, notes


def _snapshot_intact(index: dict) -> bool:
    try:
        return index.get("schema") == SCHEMA and all(read_card(short, index) is not None for short in index.get("issues", {}))
    except RuntimeError:
        return False


def index_age() -> float | None:
    """本地库距上次刷新过了多少秒；没有 `refreshed_at`（旧库）或读不出时返回 None。"""
    stamp = load_index().get("refreshed_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def load_or_refresh(
    *,
    offline: bool = False,
    max_age: float = DEFAULT_MAX_AGE,
    force: bool = False,
    **kwargs: Any,
) -> tuple[dict, list[str]]:
    """读本地库；只有过期（超过 `max_age` 秒）或 `force=True` 时才联网刷新。

    `flow` / `cases` 默认每次都跑 `sentry issue list` + 逐 issue `issue view`，本机实测 5.7 s；
    调图时一分钟跑一条命令，这笔等待全是为了拿一份没变的数据。`max_age=0` 等于每次都刷。
    """
    if offline:
        return load_index(), []
    selected = load_index(strict=True)
    age = index_age()
    if not force and max_age > 0 and age is not None and age <= max_age and _snapshot_intact(selected):
        return selected, [
            f"本地遥测库 {int(age)} 秒前刷新过（--telemetry-max-age {max_age:g} 内直接复用；"
            "要最新数据加 --telemetry-refresh）"
        ]
    kwargs["full_details"] = force or bool(kwargs.get("full_details", False))
    return refresh_default(**kwargs)


def load_cards(index: dict, *, short_ids: list[str] | None = None) -> list[IssueCard]:
    """读出卡片；`short_ids` 为空时读全部。读不了的跳过（不抛）。

    索引里有、卡片缺失或读不出时打一条 warning：不然"库里有几个 issue"（索引）和
    "实际能画几个"（卡片）会对不上，批量出图会静默少画（实测 13 vs 11）。
    """
    keys = short_ids if short_ids is not None else list(index.get("issues", {}))
    out: list[IssueCard] = []
    missing: list[str] = []
    for short in keys:
        card = read_card(short, index)
        if card is not None:
            out.append(card)
        else:
            missing.append(short)
    if missing and short_ids is None:
        shown = "、".join(missing[:8])
        more = f" 等 {len(missing)} 个" if len(missing) > 8 else ""
        log.warning("索引里有、卡片缺失或读不出，已跳过：%s%s", shown, more)
    return out


# --------------------------------------------------------------------------- #
# 本地日志 → 失败候选
# --------------------------------------------------------------------------- #






# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


def _gap_seconds(card: IssueCard, cand: FailureCandidate) -> float | None:
    """卡片时刻（UTC）与候选本地时刻的差（秒）；卡片没时刻返回 None。"""
    card_ts = _parse_time(card.received) or _parse_time(card.last_seen)
    if card_ts is None:
        return None
    return abs((card_ts - _local_as_utc(cand.ts)).total_seconds())


def match(
    candidates: list[FailureCandidate],
    cards: list[IssueCard],
    *,
    gap_days: int = DEFAULT_GAP_DAYS,
) -> list[Match]:
    """把候选和卡片对上，给级别。级别定义见 README：

    - 高：`task_id` 与 `node_id` 都相等（实测 25/30 的真实包能中）；
    - 中：节点名相同，且卡片时刻与失败时刻差 ≤ `gap_days`；
    - 低：只有节点名相同。

    一张卡只保留最强的那条命中；结果按级别、时间差排序。
    """
    limit = gap_days * 86400
    out: list[Match] = []
    for card in cards:
        best: Match | None = None
        for cand in candidates:
            tier = ""
            detail = ""
            gap: float | None = None
            if (
                cand.task_id is not None
                and cand.node_id is not None
                and cand.task_id == card.failure_task_id
                and cand.node_id == card.failure_node_id
            ):
                tier, detail = "high", "task_id + node_id 全等"
                # 同一级别里用时间差排序：同一次运行可能有多个失败都精确对上
                gap = _gap_seconds(card, cand)
            elif card.node_name and cand.name == card.node_name:
                # 通用兜底节点（BaseTaskStopError / BaseErrorNext / BaseProbeConfirm）被多个
                # 任务共用：只有节点名对得上不够，失败任务的入口名也要跟标题里的任务名一致
                # （`TaskEntrust` ↔ 标题 `Entrust`）。两边缺一头就不强判，保留原匹配。
                if cand.entry and card.label:
                    norm = (
                        cand.entry[4:]
                        if cand.entry.startswith("Task")
                        else cand.entry
                    )
                    if norm != card.label:
                        continue
                gap = _gap_seconds(card, cand)
                if gap is not None and gap <= limit:
                    tier = "mid"
                    detail = f"同节点名，时间差 {_fmt_gap(gap)}"
                else:
                    tier, detail = "low", "只有节点名相同"
            if not tier:
                continue
            current = Match(card=card, tier=tier, detail=detail, gap_seconds=gap)
            if best is None or TIER_RANK[tier] < TIER_RANK[best.tier]:
                best = current
            elif tier == best.tier and gap is not None:
                if best.gap_seconds is None or gap < best.gap_seconds:
                    best = current
        if best is not None:
            out.append(best)
    # 排序：级别 → 时间差 → 事件新旧（**不用 count**：热门问题会压过真正的那次）。
    def _recency(m: Match) -> float:
        stamp = _parse_time(m.card.last_seen) or _parse_time(m.card.received)
        return -stamp.timestamp() if stamp else 0.0

    out.sort(
        key=lambda m: (
            TIER_RANK[m.tier],
            m.gap_seconds if m.gap_seconds is not None else float("inf"),
            _recency(m),
        )
    )
    return out


def _fmt_gap(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}min"
    return f"{seconds / 3600:.1f}h"


# --------------------------------------------------------------------------- #
# 注记（纯函数，画图侧直接用）
# --------------------------------------------------------------------------- #


def card_notes(
    card: IssueCard | None,
    matches: list[Match],
    *,
    chosen: str = "",
) -> list[tuple[str, str]]:
    """把遥测关联写成图上注记（`(文本, kind)`，与 `flow_from_log.Note` 同口径）。"""
    notes: list[tuple[str, str]] = []
    if card is not None:
        rel = ""
        if card.release:
            try:
                label = report_common.version_label(card.release)
            except Exception:  # noqa: BLE001 - release 字段类型不定，解析失败就退回空标签
                label = None
            rel = f" · {label}" if label else ""
        when = _fmt_seen(card.last_seen)
        notes.append(
            (
                f"遥测问题 {card.short_id}：{card.count} 次"
                + (f" · 最后 {when}" if when else "")
                + rel,
                "version",
            )
        )
        if card.title:
            notes.append((f"遥测失败：{card.title}", "fail"))
    if matches:
        ids = "、".join(m.card.short_id for m in matches[:4])
        more = f" 等 {len(matches)} 个" if len(matches) > 4 else ""
        head = f"（取 {chosen}）" if chosen else ""
        notes.append(
            (f"匹配到 {len(matches)} 个遥测问题{head}：{ids}{more}", "info")
        )
    return notes


def _fmt_seen(text: str) -> str:
    """Sentry 的时刻是 UTC，注记里要显示**本地时间**（日志是本地时间，读者对的是日志）。"""
    parsed = _parse_time(text)
    if parsed is None:
        return ""
    return _fmt_time((parsed + _utc_offset()).replace(tzinfo=None))


# --------------------------------------------------------------------------- #
# CLI（可单独跑：刷新 / 只查）
# --------------------------------------------------------------------------- #


def refresh_default(**kwargs: Any) -> tuple[dict, list[str]]:
    """刷新本地库，自己解析 sentry 命令与 target（画图侧不想 import 这些）。"""
    sentry = report_common.resolve_sentry_command()
    return refresh(sentry, CONFIG.target, **kwargs)


@dataclass
class Annotation:
    """一次"本地包 → 遥测问题"的结论。"""

    card: IssueCard | None = None
    matches: list[Match] = field(default_factory=list)
    chosen: str = ""
    version: str = ""  # 归一成 `vX.Y.Z`（给「疑似过时」当基线）
    notes: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0


def usable_match(picked: "Match", issue: str | None = None) -> bool:
    """这个匹配能不能拿来定「这次在哪个版本」——即命名产物、`release` 基线、失败注记。

    只有**高**置信（task_id + node_id 全等）才算；`--telemetry-issue` 显式点名时另说。
    中/低置信只说明"节点名撞上了"，可能是一次别的任务、甚至另一个用户在同名节点上的失败
    （实测 `PYTHON-2Y`：3 天前 beta.68 的低置信匹配被当成基线，把当次 beta.73 的 3 个节点
    标成「疑似过时」，还挂上不相干的「遥测失败 GiftEnterHome」）。
    """
    return picked.tier == "high" or bool(issue)


def annotate(
    log: Any,
    *,
    index: dict | None = None,
    offline: bool = False,
    issue: str | None = None,
    task_id: int | None = None,
    days: int = DEFAULT_DAYS,
    gap: int = DEFAULT_GAP_DAYS,
    keep_resolved: bool = False,
    verbose: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_age: float = DEFAULT_MAX_AGE,
    force: bool = False,
) -> Annotation:
    """拿本地日志在本地遥测库里查对应的 issue；返回注记 + 版本基线。

    `index` 给了就不再刷新（调用方已刷过一次）；`offline=True` 只读本地库、不联网。
    `max_age` 秒内刷新过就直接用本地库（`force=True` 强制刷新）——每次 `flow` 都跑一遍
    `sentry issue list` 要 5.7 s，调图时一分钟跑一条命令全是白等。
    刷新失败直接抛错；调用方不得回退旧库并报告绘图成功。
    """
    out = Annotation()
    if index is None:
        if offline:
            index = load_index()
        else:
            index, notes = load_or_refresh(
                max_age=max_age, force=force, verbose=verbose, timeout=timeout,
                days=days, keep_resolved=keep_resolved,
            )
            out.warnings.extend(notes)

    cards = load_cards(index, short_ids=[issue] if issue else None)
    if not cards:
        out.warnings.append(
            "本地遥测库是空的：先跑一次 `python -m tools.sentry.cli telemetry --refresh`（需联网）"
        )
        return out

    # 只读一遍日志：完整的 maafw.log 有几十 MB，读两次纯属浪费
    candidates = extract_candidates(log)
    if task_id is not None:
        # 指定了 task 就只认**它自己**的失败：一份包常含多个 task 的失败，
        # 用整份日志去查会把别的 task 的问题算到这张图上（`--all-tasks` 尤其明显）。
        owned = [c for c in candidates if c.task_id == task_id]
        if owned:
            candidates = owned
    out.candidates = len(candidates)
    if not candidates:
        out.warnings.append("日志里没找到失败标记（PipelineNode.Failed / Task timeout 等），无法反查")
        return out

    out.matches = match(candidates, cards, gap_days=gap)
    if not out.matches:
        out.warnings.append(
            "本地遥测库里没有对得上的问题（可能还没上报，或已被清理）"
        )
        return out

    highs = [m for m in out.matches if m.tier == "high"]
    if issue:
        picked = next((m for m in out.matches if m.card.short_id == issue), None)
    elif len(highs) == 1:
        picked = highs[0]
    else:
        picked = out.matches[0]

    if picked is not None:
        out.card = picked.card
        # 只有「高」置信才用它命名产物；中/低置信只进注记（显式 --telemetry-issue 例外）。
        # 通用兜底节点会让中置信的问题号挂到别的任务上，写进文件名就是把问题张冠李戴。
        # 同理也**不拿它的 release 当基线**：拿错版本会把当次运行的动作全标成「疑似过时」。
        usable = usable_match(picked, issue)
        out.chosen = picked.card.short_id if usable else ""
        if usable and picked.card.release:
            try:
                out.version = report_common.version_label(picked.card.release) or ""
            except Exception:  # noqa: BLE001 - release 字段类型不定，解析失败就留空
                out.version = ""
        if len(highs) > 1:
            out.warnings.append(
                f"同一次运行里有 {len(highs)} 个失败都精确对上（"
                + "、".join(m.card.short_id for m in highs[:4])
                + f"），先按时间取了 {picked.card.short_id}；用 --telemetry-issue 指定"
            )
        elif picked.tier != "high":
            out.warnings.append(
                f"{picked.card.short_id} 只是「{TIER_LABEL[picked.tier]}」置信（{picked.detail}），"
                "别把它当唯一答案；不以它命名产物、不拿它的 release 当「疑似过时」基线"
            )
    # 低置信连卡片注记都不挂：那会造成"这次真的是这个失败"的错觉
    out.notes = card_notes(
        out.card if (picked is not None and usable_match(picked, issue)) else None,
        out.matches,
        chosen=out.chosen,
    )
    return out


def format_matches(matches: list[Match], output: TextIO) -> None:
    if not matches:
        print("没有匹配的遥测问题。", file=output)
        return
    print(f"\n匹配到 {len(matches)} 个遥测问题：", file=output)
    for m in matches:
        card = m.card
        print(
            f"  [{TIER_LABEL[m.tier]}] {card.short_id:<12} "
            f"{card.title[:44]:<46} {card.count or 0:>3} 次  {m.detail}",
            file=output,
        )


def create_argument_parser(prog: str | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog,
        description="本地遥测库：增量下载 issue 详情，并按本地日志反查对应的问题",
    )
    parser.add_argument("--refresh", action="store_true", help="只刷新本地遥测库，不查")
    parser.add_argument("--case", default=None, help="本地日志/证据包，反查它对应哪个 issue")
    parser.add_argument("--issue", default=None, help="只报告这个 issue（不反查）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"保留最近 N 天（默认 {DEFAULT_DAYS}）")
    parser.add_argument("--gap", type=int, default=DEFAULT_GAP_DAYS, help=f"匹配的时间窗（默认 {DEFAULT_GAP_DAYS} 天）")
    parser.add_argument("--keep-resolved", action="store_true", help="保留已解决/已忽略的 issue")
    parser.add_argument("--period", default="30d", help="列表窗口（默认 30d）")
    parser.add_argument("--limit", type=int, default=200, help="列表上限（默认 200）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次调用超时秒数（默认 8，最多尝试 3 次）")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="日志文件路径（默认按运行时间戳建 temp/logs/telemetry_<时间戳>_<pid>.log）；"
        "传 none / 空 只走控制台",
    )
    return parser


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    # `cli.py` 统一按 `(argv, prog)` 调各子命令的 main，签名要对齐
    args = create_argument_parser(prog).parse_args(argv)
    _, log_path = configure_tool_logging(
        "telemetry", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)
    try:
        sentry = report_common.resolve_sentry_command()
        index, notes = refresh(
            sentry,
            CONFIG.target,
            verbose=args.verbose,
            timeout=args.timeout,
            days=args.days,
            keep_resolved=args.keep_resolved,
            period=args.period,
            limit=args.limit,
            full_details=args.refresh,
        )
    except (RuntimeError, OSError) as exc:
        log.error(
            "刷新本地遥测库失败：%s；若报「unable to open database file」，"
            "把 SENTRY_CONFIG_DIR 指到含可写 cli.db 的目录",
            exc,
        )
        return 1
    for note in notes:
        log.info("提示：%s", note)

    if args.refresh:
        print(f"本地遥测库：{len(index['issues'])} 个 issue")
        return 0

    if args.issue:
        card = read_card(args.issue, index)
        if card is None:
            log.error("%s 不在本地遥测库里。", args.issue)
            return 1
        for text, kind in card_notes(card, []):
            print(text)
        return 0

    if not args.case:
        log.error("要给 --case <日志/证据包> 才会反查。")
        return 1

    candidates = extract_candidates(args.case)
    matches = match(candidates, load_cards(index), gap_days=args.gap)
    format_matches(matches, sys.stdout)
    return 0
