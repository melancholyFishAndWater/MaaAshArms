"""按提交正文关闭 Sentry issue：`pre-push` 钩子。

    python -m tools.sentry.cli issue-resolve            # 手动：按最新的本地 v* tag 列清单
    python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.71   # 指定 tag
    python -m tools.sentry.cli issue-resolve --apply    # 真的把 issue 置 resolved
    python -m tools.sentry.cli issue-resolve --status   # 额外联网核对 Sentry 上的状态
    python -m tools.sentry.cli issue-resolve --install  # 写 .git/hooks/pre-push（默认 list-only）
    python -m tools.sentry.cli issue-resolve --check    # 看配置 / hook 模式 / token 来源

只处理**新建**的 `refs/tags/v*`：`pre-push` 的 stdin 每行是
`<local ref> <local oid> <remote ref> <remote oid>`，remote oid 全零才算新建 tag。
用 local oid（不是 tag 名，annotated tag 的 oid 是 tag object）算
`<上一个 v* tag>..<新 tag>` 的提交正文，只认行首 `Fixes` / `Closes` / `Resolves` 后面跟的
`PYTHON-<ID>`，再逐个把 issue 置 resolved。

**默认不联网**（联网一次就够麻烦的了）：清单里的「已关闭」来自两个本地来源——本工具自己
关过的（`RESOLVE_LOG`：`--apply` 成功、或 `--status` 观察后写回）与本地遥测库的卡片；两者都
按时刻取最新。tag 推送之后这些 issue 通常已经 `resolved`（hook 装成 `--apply` 时 push 就关过
一遍，Sentry 的提交关联也会按 `Fixes` 关），只按提交正文列「待关闭」会让人以为没关（实测
`v1.0.0-beta.71` 的 PYTHON-2T / 2P / 34 / 32）。
本地没有记录时如实说"没有本地状态记录"，并提示 `--status`——**不猜**已关闭。
`--status` 走一次 shortids GET（响应 `group` 段同时带 `groupId` 与 `status`；少数部署不带
`group` 时才再 `GET /api/0/issues/<groupId>/`），核对完把结果写回本地记录。
`--only`（验证 REST 分支）不看任何状态——它要按显式给的 ID 原样处理。

**手动运行**（stdin 一个 ref 行都没有，`pre-push` 不会这样）默认按**最新的本地 `v*` tag**
处理：人刚建的 tag 就是最新那个，先跑一遍看清单，确认了再加 `--apply`；想指定别的 tag 用
`--tag NAME`。`--ref` / `--stdin-file` 仍然只模拟 pre-push 输入。

## 为什么是本地 hook 而不是 CI

CI 侧（`install.yml` 的 tag 触发）改 Sentry 状态必须把 token 放进仓库 Secrets；本地
`pre-push` 让凭据完全留在本机（`SENTRY_AUTH_TOKEN` > `SENTRY_CONFIG_DIR/cli.db` >
`sentry auth token`），零 Secret。代价是只在本机、只在手动 push 时生效、`--no-verify`
可绕过，而且 `pre-push` 在**推送之前**执行——推送失败时脚本已经跑过了。所以：

- `--install` 默认写 **list-only** 的 hook（只列清单，不写远端），加 `--apply` 才真的 PUT；
- hook 里**恒 exit 0**：任何异常都只告警、绝不阻断 push；
- 写操作幂等（重复 resolve 仍返回 200），推送是否成功当作未知。

## 两步 REST

`GET /api/0/organizations/<org>/shortids/<SHORT-ID>/` 取 `groupId`；响应的 `group` 段
同时带 `status`（少数部署不带时再 `GET /api/0/issues/<groupId>/`），`PUT
/api/0/issues/<groupId>/` 带 `{"status": "resolved"}` 才写。直接把 short ID 塞
`/api/0/issues/<ID>/` 会 404。用标准库 `urllib`，不装第三方包。

## 验证

`--check` 打印仓库 / 配置 / org / hook 模式 / token 来源；`--only --issue <ID>` 只用给定 ID、
不扫提交范围、也不看状态（验证 REST 分支时别把扫到的真实 issue 一起关掉）；`--status` 额外
联网核对并把结果写回本地记录；纯函数自检在
`python -m tools.sentry.cli selftest`（`lib/selftests/issue_resolve.py`）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .config import CONFIG

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMAND = "python -m tools.sentry.cli issue-resolve"
HOOK_MARKER = "# managed-by: sentry_issue_resolve"
DEFAULT_BASE_URL = "https://sentry.io"
DEFAULT_ISSUE_PREFIX = "PYTHON-"
# 这两种状态都算"已经处理过"：list-only 不再列进待关闭
CLOSED_STATUSES = frozenset({"resolved", "ignored"})
# 状态核对是只读 GET，但它跑在 pre-push 里；网络明显不通时连查两次就收手，别拖住 push
STATUS_TIMEOUT = 5.0
STATUS_MAX_FAILURES = 2
# 本地关闭记录：这个工具自己关掉过哪些 issue（`--apply` 成功后写，`--status` 观察也会写回）。
# 放在 `temp/` 下（不入库），离线跑时靠它回答"是不是已经关过了"——不用联网、不用 token。
RESOLVE_LOG = "temp/sentry/issue-resolve.json"
LOG_SCHEMA = 1

ZERO_RE = re.compile(r"^0+$")
TAG_REF_RE = re.compile(r"^refs/tags/(v.+)$")
KEYWORD_LINE_RE = re.compile(
    r"^\s*(?:fix(?:es)?|closes?|resolves?)\b\s*:?\s*(.*)$", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# 输出：hook 的 stdout 会被 git-send-pack 转发到对端，消息一律走 stderr
# --------------------------------------------------------------------------- #


def say(message: str) -> None:
    print(f"[sentry-issues] {message}", file=sys.stderr)


def warn(message: str) -> None:
    say(f"警告：{message}")


# --------------------------------------------------------------------------- #
# 纯函数（自检用，零网络零凭据）
# --------------------------------------------------------------------------- #


def parse_ref_lines(lines: list[str]) -> list[tuple[str, str]]:
    """从 pre-push 的 stdin 行里挑出"新建的 v* tag"（名字, local oid）。

    用 oid 而不是 tag 名算提交范围：新建的 tag 还没到远端，annotated tag 的 oid
    还是 tag object，只有 stdin 里的 local oid 是 git 明确给出的被推送对象。
    """
    tags: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_oid, remote_oid = parts[0], parts[1], parts[3]
        match = TAG_REF_RE.match(local_ref)
        if not match:
            continue
        if ZERO_RE.match(local_oid):  # 删除 tag
            continue
        if not ZERO_RE.match(remote_oid):  # 更新已有 tag
            continue
        if all(name != match.group(1) for name, _oid in tags):
            tags.append((match.group(1), local_oid))
    return tags


def issue_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(r"\b(" + re.escape(prefix) + r"[0-9A-Z]+)\b", re.IGNORECASE)


def extract_issue_ids(bodies: str, prefix: str = DEFAULT_ISSUE_PREFIX) -> list[str]:
    """只认以 Fixes/Closes/Resolves 开头的行，避免把正文里提到的编号误关。"""
    pattern = issue_pattern(prefix)
    ids: list[str] = []
    for line in bodies.splitlines():
        match = KEYWORD_LINE_RE.match(line)
        if not match:
            continue
        for found in pattern.findall(match.group(1)):
            short_id = found.upper()
            if short_id not in ids:
                ids.append(short_id)
    return ids


def merge_ids(scanned: list[str], extra: list[str]) -> list[str]:
    """扫描结果 + `--issue` 追加，去重且保持顺序。"""
    out = list(scanned)
    for item in extra:
        short_id = item.upper()
        if short_id not in out:
            out.append(short_id)
    return out


# --------------------------------------------------------------------------- #
# 离线状态：本地关闭记录 + 本地遥测库
# --------------------------------------------------------------------------- #


def resolve_log_path() -> Path:
    return REPO_ROOT / RESOLVE_LOG


def read_resolve_log() -> dict[str, dict[str, Any]]:
    """本地关闭记录 `{short_id: {"status", "at", "by"}}`；没有 / 读不出就是空。"""
    try:
        data = json.loads(resolve_log_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, dict):
        return {}
    return {str(key): value for key, value in issues.items() if isinstance(value, dict)}


def write_resolve_log(entries: dict[str, dict[str, Any]]) -> None:
    path = resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {"schema": LOG_SCHEMA, "issues": entries},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
    except OSError as exc:
        warn(f"写本地关闭记录失败（不影响远端）：{exc}")


def remember_states(
    states: dict[str, str], *, by: str, groups: dict[str, str] | None = None
) -> None:
    """把已知状态写进本地记录：`--apply` 关成功后、`--status` 联网观察后都走这里。

    `groups` 给了就把 `groupId` 一起缓存——`groupId` 只跟 issue 走，缓存住之后离线 `--apply`
    省掉每条一次 shortids GET（联网麻烦的时候少一半请求）。
    """
    if not states:
        return
    entries = read_resolve_log()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    touched = False
    for short_id, status in states.items():
        text = status.strip().lower()
        if not text:
            continue
        entry: dict[str, Any] = {"status": text, "at": stamp, "by": by}
        group = (groups or {}).get(short_id)
        if group:
            entry["group"] = group
        elif isinstance(entries.get(short_id), dict) and entries[short_id].get("group"):
            entry["group"] = entries[short_id]["group"]  # 保留旧的 groupId
        entries[short_id] = entry
        touched = True
    if touched:
        write_resolve_log(entries)


def local_group_ids(ids: Iterable[str]) -> dict[str, str]:
    """本地记录里缓存的 `short_id → groupId`（没有就没有，调用方再联网解析）。"""
    entries = read_resolve_log()
    out: dict[str, str] = {}
    for short_id in ids:
        group = str((entries.get(short_id) or {}).get("group") or "")
        if group:
            out[short_id] = group
    return out


def telemetry_state(short_id: str) -> tuple[str, float] | None:
    """本地遥测库里这条的状态与写入时刻（没有卡片返回 None）。

    这个库默认只留**未解决**的 issue（`telemetry_index.ISSUE_OPEN_STATUSES`，刷新时清掉
    resolved），所以"在库里"≈"上次刷新时还没关"；不在库里既可能是已解决，也可能只是超出
    `--telemetry-days` 或从没上报过——不能当"已关闭"的证据。
    """
    try:
        from . import telemetry_index
    except ImportError:  # 单独拷走这个模块时不该整条命令失败
        return None
    card = telemetry_index.read_card(short_id)
    if card is None:
        return None
    try:
        when = telemetry_index.card_path(short_id).stat().st_mtime
    except OSError:
        when = 0.0
    return (str(card.status or ""), when)


def local_evidence(ids: Iterable[str]) -> dict[str, list[tuple[float, str, str]]]:
    """离线证据：每条 issue 的 `(时刻, 状态, 来源说明)` 候选，交给 `newest_state` 取最新。

    两个来源都会给：工具自己关过的（`RESOLVE_LOG`）与本地遥测库的卡片。谁的时刻更新算谁
    ——push 之后本地库还没刷新时，卡片说 unresolved、关闭记录说 resolved，取关闭记录。
    """
    entries = read_resolve_log()
    evidence: dict[str, list[tuple[float, str, str]]] = {}
    for short_id in ids:
        items: list[tuple[float, str, str]] = []
        entry = entries.get(short_id)
        if entry:
            status = str(entry.get("status") or "")
            at = str(entry.get("at") or "")
            by = str(entry.get("by") or "本工具")
            items.append((_iso_epoch(at), status, f"{by}（{at or '?'}）"))
        card = telemetry_state(short_id)
        if card is not None:
            status, when = card
            stamp = time.strftime("%m-%d %H:%M", time.localtime(when)) if when else "?"
            items.append((when, status, f"本地遥测库 {stamp} 记为 {status or '未知'}"))
        evidence[short_id] = items
    return evidence


def _iso_epoch(text: str) -> float:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def newest_state(entries: Iterable[tuple[float, str, str]]) -> tuple[str, float, str]:
    """时刻最新的那条 `(status, when, source)`；没有就返回 `("", 0.0, "")`。"""
    best: tuple[float, str, str] | None = None
    for entry in entries:
        if best is None or entry[0] >= best[0]:
            best = entry
    if best is None:
        return ("", 0.0, "")
    return (best[1], best[0], best[2])


def classify_states(
    ids: Iterable[str], evidence: dict[str, list[tuple[float, str, str]]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """→ `(待关闭, [(已关闭的编号, 来源说明)])`。状态未知的留在待关闭。"""
    pending: list[str] = []
    closed: list[tuple[str, str]] = []
    for short_id in ids:
        status, _when, source = newest_state(evidence.get(short_id, []))
        if status.strip().lower() in CLOSED_STATUSES:
            closed.append((short_id, source))
        else:
            pending.append(short_id)
    return pending, closed


def hook_mode_label() -> str:
    """已装 hook 的模式（`apply` / `list-only` / 未安装 / 不可读 / 外部 hook）。"""
    path = git_hook_path()
    if path is None or not path.exists():
        return "未安装"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "不可读"
    if HOOK_MARKER not in text:
        return "外部 hook（不是本工具生成的）"
    return "apply" if "--apply" in text else "list-only"


def choose_previous_tag(sorted_tags: list[str], current: str) -> str | None:
    """`git tag --sort=-creatordate` 的回退路径：取列表里 current 之后那个（更旧的）。"""
    for index, tag in enumerate(sorted_tags):
        if tag == current and index + 1 < len(sorted_tags):
            return sorted_tags[index + 1]
    return None


def pick_latest_tag(names: Sequence[str]) -> str | None:
    """`git tag --sort=-creatordate --list v*` 的输出里第一个 `v*` tag（就是最新的）。

    只认 `v` 开头：`--list v*` 理论上不会回别的名字，多一层判断免得把
    `git tag` 的回显/颜色码当 tag。
    """
    for name in names:
        if name.startswith("v") and len(name) > 1:
            return name
    return None


def describe_args(oid: str) -> list[str]:
    return ["describe", "--tags", "--match", "v*", "--abbrev=0", f"{oid}^"]


def log_args(previous: str, oid: str) -> list[str]:
    return ["log", f"{previous}..{oid}", "--format=%B"]


def shortids_url(base: str, org: str, short_id: str) -> str:
    return f"{base}/api/0/organizations/{org}/shortids/{short_id}/"


def issue_url(base: str, group_id: str) -> str:
    return f"{base}/api/0/issues/{group_id}/"


def build_hook_script(apply: bool) -> str:
    """`--install` 写进 `.git/hooks/pre-push` 的内容。

    先 `cd` 到仓库根再 `python -m tools.sentry.cli issue-resolve`：包导入要求 cwd 在仓库根，
    而 git 只保证 hook 的 cwd 是工作树根（`core.hooksPath` / `GIT_DIR` 另指时不一定）。
    解释器用装这个脚本的那个（`sys.executable`），找不到时退回 PATH 上的 `python`。
    """
    python = Path(sys.executable).resolve().as_posix()
    root = REPO_ROOT.as_posix()
    flag = " --apply" if apply else ""
    return "\n".join(
        [
            "#!/bin/sh",
            HOOK_MARKER,
            f"# 由 {COMMAND} --install 生成；本 hook 恒 exit 0，不阻断 push。",
            "# 删掉本文件即停用；脚本换位置后要重跑 --install（旧 hook 指向旧路径）。",
            f"py='{python}'",
            'if [ ! -x "$py" ]; then py=python; fi',
            f"cd '{root}' || exit 0",
            f'"$py" -m tools.sentry.cli issue-resolve{flag} "$@"',
            "exit 0",
            "",
        ]
    )


# --------------------------------------------------------------------------- #
# git / 配置
# --------------------------------------------------------------------------- #


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"git {' '.join(args)} 调用失败：{exc}")
        return subprocess.CompletedProcess(["git", *args], 1, "", "")


def config_target() -> tuple[str, str] | None:
    """`tools/sentry/config.json` 的 `target`（`<org>/<project>`）。"""
    try:
        org, project = CONFIG.target.split("/", 1)
    except ValueError:
        warn(f"config.json 的 target 不是 <org>/<project>：{CONFIG.target!r}")
        return None
    return org, project


def previous_v_tag(tag: str, oid: str) -> str | None:
    # annotated tag 的 oid 是 tag object，先剥到 commit；lightweight tag 原样返回
    commit = tag_oid(oid) or oid
    result = run_git(describe_args(commit))
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    listed = run_git(["tag", "--sort=-creatordate", "--list", "v*"])
    return choose_previous_tag(listed.stdout.split(), tag)


def tag_oid(name: str) -> str | None:
    """tag 名（或任意对象名）→ commit oid；annotated tag 剥到 commit，读不到返回 None。"""
    result = run_git(["rev-parse", f"{name}^{{commit}}"])
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        warn(f"读不到 {name} 这个对象（tag 不在本地？先 git fetch --tags）")
        return None
    return text.splitlines()[0]


def latest_local_tag() -> str | None:
    """本地最新的 `v*` tag（`--sort=-creatordate`）；一个都没有返回 None。"""
    listed = run_git(["tag", "--sort=-creatordate", "--list", "v*"])
    if listed.returncode != 0:
        return None
    return pick_latest_tag(listed.stdout.split())


def collect_issue_ids(tags: list[tuple[str, str]], prefix: str) -> list[str]:
    ids: list[str] = []
    for tag, oid in tags:
        previous = previous_v_tag(tag, oid)
        if previous is None:
            warn(f"{tag} 找不到上一个 v* tag，跳过（不扫全历史）")
            continue
        result = run_git(log_args(previous, oid))
        if result.returncode != 0:
            warn(f"git log {previous}..{oid} 失败：{result.stderr.strip()}")
            continue
        found = extract_issue_ids(result.stdout, prefix)
        say(f"{previous}..{tag}: {', '.join(found) or '（无）'}")
        for short_id in found:
            if short_id not in ids:
                ids.append(short_id)
    return ids


def git_hook_path() -> Path | None:
    result = run_git(["rev-parse", "--git-path", "hooks/pre-push"])
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


# --------------------------------------------------------------------------- #
# Sentry REST
# --------------------------------------------------------------------------- #


def resolve_token() -> tuple[str, str]:
    """凭据只从本机取：环境变量 > cli.db 的 auth 表 > 已装的 sentry CLI。"""
    from_env = os.environ.get("SENTRY_AUTH_TOKEN", "").strip()
    if from_env:
        return from_env, "SENTRY_AUTH_TOKEN"
    # 本机 OAuth 凭据在 cli.db 的 auth 表；SENTRY_CONFIG_DIR 可指向它的副本
    # （工作区外只读环境下用 `copy cli.db temp/` + 指过去）。
    config_dir = os.environ.get("SENTRY_CONFIG_DIR")
    db = (
        Path(config_dir) / "cli.db"
        if config_dir
        else Path.home() / ".config" / "sentry" / "cli.db"
    )
    try:
        connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT token FROM auth ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connection.close()
        if row and row[0]:
            return str(row[0]), str(db)
    except (sqlite3.Error, OSError) as exc:
        warn(f"读 {db} 失败：{exc}")
    # 最后再试装好的 CLI（Windows 上是 .cmd，subprocess 不会自动补后缀）
    cli = shutil.which("sentry.cmd") or shutil.which("sentry")
    if cli:
        try:
            result = subprocess.run(
                [cli, "auth", "token"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            lines = (result.stdout or "").strip().splitlines()
            if result.returncode == 0 and lines and lines[0].strip():
                return lines[0].strip(), f"{cli} auth token"
        except (OSError, subprocess.SubprocessError) as exc:
            warn(f"`sentry auth token` 不可用：{exc}")
    return "", ""


def api_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    parsed = json.loads(raw) if raw.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def shortids_info(
    short_id: str, org: str, base: str, token: str, *, with_status: bool = True
) -> tuple[str | None, str]:
    """一次 GET 同时拿 `groupId` 与 `status`：shortids 响应的 `group` 段就带 status。

    直接把 short ID 塞 `/issues/<ID>/` 会 404，所以 groupId 还得从 shortids 取。少数部署
    的 shortids 不带 `group` 段时才退回 `GET /api/0/issues/<groupId>/` 读 status
    （`with_status=False` 时不做这一步——`resolve_issues` 只需要 groupId）。
    """
    info = api_request(
        "GET", shortids_url(base, org, short_id), token, timeout=STATUS_TIMEOUT
    )
    raw_id = info.get("groupId")
    if isinstance(raw_id, str) and raw_id:
        group_id: str | None = raw_id
    elif isinstance(raw_id, int) and raw_id > 0:
        group_id = str(raw_id)
    else:
        group_id = None
    group = info.get("group")
    status = group.get("status") if isinstance(group, dict) else None
    if not isinstance(status, str) and with_status and group_id:
        detail = api_request(
            "GET", issue_url(base, group_id), token, timeout=STATUS_TIMEOUT
        )
        fallback = detail.get("status")
        status = fallback if isinstance(fallback, str) else None
    return group_id, status if isinstance(status, str) else ""


def group_id_of(short_id: str, org: str, base: str, token: str) -> str | None:
    """short ID → groupId（`resolve_issues` 用，不需要状态）。"""
    return shortids_info(short_id, org, base, token, with_status=False)[0]


def fetch_issue_states(
    ids: list[str], org: str, base: str, token: str
) -> tuple[dict[str, str], dict[str, str]]:
    """只读核对远端状态，返回 `(short_id → status, short_id → groupId)`。

    `groupId` 一并返回给 `--apply` 复用，省掉一次 shortids GET；单个失败只告警，那个 ID
    的 status 留空（`partition_issue_states` 会把它当"待关闭"）。连续两次失败就收手：
    网络明显不通时不该为每条 issue 都等一次超时，hook 还在 push 的关键路径上。
    """
    states: dict[str, str] = {}
    group_ids: dict[str, str] = {}
    failures = 0
    for short_id in ids:
        if failures >= STATUS_MAX_FAILURES:
            warn("状态核对连续失败，剩下的不再查（按待关闭处理）")
            break
        try:
            group_id, status = shortids_info(short_id, org, base, token)
            if group_id is None:
                warn(f"{short_id} 没解析到 groupId，状态未知")
                failures += 1
                continue
            group_ids[short_id] = group_id
            if status:
                states[short_id] = status
            failures = 0
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            failures += 1
            warn(f"{short_id} 状态核对失败，按待关闭处理：{exc}")
    return states, group_ids


def resolve_issues(
    ids: list[str],
    org: str,
    base: str,
    token: str,
    group_ids: dict[str, str] | None = None,
) -> list[str]:
    """逐个把 short ID 置 resolved，返回**成功**的那些；单个失败只告警，不中断后面的。"""
    done: list[str] = []
    known = group_ids or {}
    for short_id in ids:
        try:
            group_id = known.get(short_id) or group_id_of(short_id, org, base, token)
            if not group_id:
                warn(f"{short_id} 没解析到 groupId，跳过")
                continue
            api_request("PUT", issue_url(base, group_id), token, {"status": "resolved"})
            say(f"resolved {short_id} (group {group_id})")
            done.append(short_id)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            warn(f"{short_id} 处理失败，跳过：{exc}")
    return done


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #


def read_ref_lines(args: argparse.Namespace) -> list[str]:
    if args.ref:
        return list(args.ref)
    if args.stdin_file:
        return Path(args.stdin_file).read_text(encoding="utf-8").splitlines()
    if sys.stdin.isatty():
        return []
    return sys.stdin.read().splitlines()


def hook_mode(args: argparse.Namespace) -> int:
    """`pre-push` 调用时的主路径；**恒返回 0**，不阻断 push。

    三条取 tag 的路，按优先级：

    1. `--tag NAME`：直接处理这个本地 tag（不读 stdin）；
    2. stdin 的 pre-push 行：只认 remote oid 全零的**新建** `v*` tag；
    3. stdin 一个 ref 行都没有（`pre-push` 不会这样，只会是人手动跑）→ 处理**最新的本地
       `v*` tag**。先出清单，确认了再加 `--apply`。

    第 3 条只在"完全没有输入"时触发：stdin 里**有行但没有新建 tag**时仍是原来的跳过——
    推送分支、更新已有 tag 都不该把本地最新 tag 的 issue 再关一遍。
    """
    if args.tag:
        tags: list[tuple[str, str]] = []
        for name in args.tag:
            oid = tag_oid(name)
            if oid is not None:
                tags.append((name, oid))
        if not tags:
            return 0
    else:
        lines = read_ref_lines(args)
        tags = parse_ref_lines(lines)
        if not tags and not lines:
            latest = latest_local_tag()
            if latest is None:
                say("本地没有 v* tag，跳过")
                return 0
            oid = tag_oid(latest)
            if oid is None:
                return 0
            say(f"没有 pre-push 输入（手动运行）：按最新的本地 tag {latest} 处理")
            tags = [(latest, oid)]
    if not tags:
        say("没有新建的 v* tag，跳过")
        return 0
    say(f"新 tag: {', '.join(name for name, _oid in tags)}")
    # --only：只用 --issue 给出的 ID，不扫提交范围。用于验证 REST 分支而不碰真实 issue。
    scanned = [] if args.only else collect_issue_ids(tags, args.issue_prefix)
    ids = merge_ids(scanned, args.issue)
    target = config_target()
    org = args.org or os.environ.get("SENTRY_ORG") or (target[0] if target else "")
    base = args.base_url.rstrip("/")

    # 状态来源按"代价从低到高"：本地关闭记录 + 本地遥测库（零网络零 token）→ `--status`
    # 联网核对。默认不联网：hook 已经关过的东西本地记着，没必要每次去 Sentry 问一遍。
    evidence = {} if args.only else local_evidence(ids)
    token, token_source = "", ""
    group_ids: dict[str, str] = {}
    if ids and args.status:
        token, token_source = resolve_token()
        if org and token:
            network_states, group_ids = fetch_issue_states(ids, org, base, token)
            stamp = time.strftime("%m-%d %H:%M")
            for short_id, status in network_states.items():
                evidence.setdefault(short_id, []).append(
                    (time.time(), status, f"联网核对 {stamp}：{status}")
                )
            # 顺手把观察到的状态与 groupId 记下来，下次离线也能答、也少一次 GET
            remember_states(network_states, by="联网核对", groups=group_ids)
        else:
            say("--status 需要 org 与 token，这次只按本地记录判断")

    pending, closed = classify_states(ids, evidence)
    grouped: dict[str, list[str]] = {}
    for short_id, source in closed:
        grouped.setdefault(source, []).append(short_id)
    for source, sids in grouped.items():
        say(f"已关闭（{source}）：{', '.join(sids)}")
    say(f"待关闭: {', '.join(pending) or '（无）'}")
    if not args.status and not args.only and ids:
        unknown = [short_id for short_id in pending if not evidence.get(short_id)]
        if unknown:
            say(
                f"提示：{', '.join(unknown)} 没有本地状态记录（只有这个工具关过的才留记录）；"
                "hook 是 --apply 时 push 可能已经关过，加 --status 可联网核对"
            )

    if not args.apply:
        if not pending:
            say(f"list-only（本次未写远端）；没有需要处理的 issue（已关闭 {len(closed)} 条）")
        elif hook_mode_label() == "apply":
            say(
                "list-only（本次未写远端）；hook 已是 --apply 模式，push 新 tag 时会自动关闭"
                "上面「待关闭」的 issue；要现在就关加 --apply"
            )
        else:
            say(
                "list-only（本次未写远端）；要现在就关加 --apply，"
                "或 --install --apply 让 hook 自动关"
            )
        return 0
    # 写哪些：联网核对过的按核对结果（已关闭的不再重复 PUT）；纯离线时只跳过 ignored
    # （那是有意"不修"的状态，不能被 resolved 覆盖），其余照旧幂等 PUT。
    if args.status:
        targets = list(pending)
    else:
        targets = [
            short_id
            for short_id in ids
            if newest_state(evidence.get(short_id, []))[0].strip().lower() != "ignored"
        ]
    if not targets:
        say("没有需要写远端的 issue，跳过写远端")
        return 0
    if not org:
        warn("没有 organization slug（--org / SENTRY_ORG / config.json target），跳过写远端")
        return 0
    if not token:
        token, token_source = resolve_token()
    if not token:
        warn("拿不到 Sentry token（env / sentry auth token / cli.db 都没有），跳过写远端")
        return 0
    if not args.status:
        # 离线 apply 只跳过 ignored（有意"不修"的状态不能被 resolved 覆盖），已 resolved 的仍
        # 幂等重写一次：issue 可能被 Sentry 自动 reopened，跳过就会漏关。
        stale = [
            short_id
            for short_id in targets
            if newest_state(evidence.get(short_id, []))[0].strip().lower() in CLOSED_STATUSES
        ]
        if stale:
            say(f"本地记录说这些已关闭，仍幂等重写一次：{', '.join(stale)}")
    say(f"本次写入: {', '.join(targets)}")
    say(f"token 来源: {token_source}; org={org}; base={args.base_url}")
    known_groups = dict(local_group_ids(targets))
    known_groups.update(group_ids)
    done = resolve_issues(targets, org, base, token, known_groups)
    if done:
        remember_states(
            {short_id: "resolved" for short_id in done},
            by="issue-resolve " + ", ".join(name for name, _oid in tags),
            groups=known_groups,
        )
    say(f"本次处理 {len(done)}/{len(targets)}")
    return 0


def install_hook(apply: bool, force: bool) -> int:
    path = git_hook_path()
    if path is None:
        warn("git rev-parse --git-path 失败，不在 git 仓库里？")
        return 1
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn(f"读不了现有 hook：{exc}")
            return 1
        if HOOK_MARKER not in existing and not force:
            warn(f"{path} 已存在且不是本工具生成的，未覆盖；确认后加 --force")
            return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_hook_script(apply).encode("utf-8"))
    try:
        path.chmod(0o755)  # Unix 上 pre-push 必须可执行；Windows 上是空操作
    except OSError:
        pass
    print(f"[sentry-issues] 已写入 {path}", file=sys.stderr)
    print(
        f"[sentry-issues] 模式: {'apply（会写 Sentry）' if apply else 'list-only（只列清单）'}",
        file=sys.stderr,
    )
    print(f"[sentry-issues] 卸载：删除该文件，或跑 {COMMAND} --uninstall", file=sys.stderr)
    return 0


def uninstall_hook() -> int:
    path = git_hook_path()
    if path is None or not path.exists():
        print("[sentry-issues] 没有已安装的 hook", file=sys.stderr)
        return 0
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(f"读不了 hook：{exc}")
        return 1
    if HOOK_MARKER not in content:
        warn(f"{path} 不是本工具生成的，未删除")
        return 1
    path.unlink()
    print(f"[sentry-issues] 已删除 {path}", file=sys.stderr)
    return 0


def print_check(args: argparse.Namespace) -> int:
    """打印配置、hook 模式与 token 来源；不写任何东西。"""
    target = config_target()
    path = git_hook_path()
    mode = hook_mode_label()
    token, source = resolve_token()
    org = args.org or os.environ.get("SENTRY_ORG") or (target[0] if target else "")
    print(f"[sentry-issues] repo       = {REPO_ROOT}")
    print(f"[sentry-issues] config     = {target}")
    print(f"[sentry-issues] org        = {org}")
    print(f"[sentry-issues] base_url   = {args.base_url}")
    print(f"[sentry-issues] prefix     = {args.issue_prefix}")
    print(f"[sentry-issues] hook       = {path} ({mode})")
    print(f"[sentry-issues] token      = {source or '未找到'} ({'有' if token else '无'})")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or COMMAND,
        description="pre-push 触发：按提交正文的 Fixes/Closes/Resolves 关闭 Sentry issue",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help="写入 .git/hooks/pre-push")
    action.add_argument("--uninstall", action="store_true", help="删除本工具生成的 hook")
    action.add_argument("--check", action="store_true", help="打印配置、hook 模式与 token 来源")
    parser.add_argument("--apply", action="store_true", help="真正写 Sentry（默认只列清单）")
    parser.add_argument("--force", action="store_true", help="--install 时覆盖已有 hook")
    parser.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="LINE",
        help="模拟一条 pre-push stdin 行，可重复",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="NAME",
        help="直接处理这个本地 v* tag（可重复），不读 pre-push stdin；"
        "不传且 stdin 一个 ref 行都没有时，按最新的本地 v* tag 处理",
    )
    parser.add_argument("--stdin-file", metavar="PATH", help="从文件读 ref 行（测试用）")
    parser.add_argument(
        "--issue",
        action="append",
        default=[],
        metavar="SHORT-ID",
        help="额外要处理的 issue，可重复（追加到扫描结果）",
    )
    parser.add_argument(
        "--only", action="store_true", help="只用 --issue 给出的 ID，不扫提交范围（验证 REST 用）"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="联网核对 Sentry 上的 issue 状态（默认只看本地：这个工具关过的记录 + 本地遥测库）。"
        "核对过就把观察到的状态记进本地记录，之后离线也能答；已 resolved 的不再重复写远端",
    )
    parser.add_argument("--org", help="organization slug（默认 env / config.json target）")
    parser.add_argument("--base-url", default=os.environ.get("SENTRY_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--issue-prefix", default=os.environ.get("SENTRY_ISSUE_PREFIX", DEFAULT_ISSUE_PREFIX)
    )
    parser.set_defaults(status=False)
    parser.add_argument("remote", nargs="?", help="git 给的远端名（hook 调用时自动传入）")
    parser.add_argument("url", nargs="?", help="git 给的远端 URL（hook 调用时自动传入）")
    return parser


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                continue

    args = create_argument_parser(prog).parse_args(argv)
    if args.install:
        return install_hook(args.apply, args.force)
    if args.uninstall:
        return uninstall_hook()
    if args.check:
        return print_check(args)
    try:
        return hook_mode(args)
    # pre-push 恒 exit 0：任何异常都不得阻断推送
    except Exception as exc:  # noqa: BLE001
        warn(f"hook 内部异常，已忽略：{exc}")
        return 0
