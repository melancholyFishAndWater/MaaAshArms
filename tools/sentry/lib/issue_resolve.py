"""按提交正文关闭 Sentry issue：`pre-push` 钩子。

    python -m tools.sentry.cli issue-resolve            # 手动：按最新的本地 v* tag 列清单
    python -m tools.sentry.cli issue-resolve --tag v1.0.0-beta.71   # 指定 tag
    python -m tools.sentry.cli issue-resolve --apply    # 真的把 issue 置 resolved
    python -m tools.sentry.cli issue-resolve --install  # 写 .git/hooks/pre-push（默认 list-only）
    python -m tools.sentry.cli issue-resolve --check    # 看配置 / hook 模式 / token 来源

只处理**新建**的 `refs/tags/v*`：`pre-push` 的 stdin 每行是
`<local ref> <local oid> <remote ref> <remote oid>`，remote oid 全零才算新建 tag。
用 local oid（不是 tag 名，annotated tag 的 oid 是 tag object）算
`<上一个 v* tag>..<新 tag>` 的提交正文，只认行首 `Fixes` / `Closes` / `Resolves` 后面跟的
`PYTHON-<ID>`，再逐个把 issue 置 resolved。

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

`GET /api/0/organizations/<org>/shortids/<SHORT-ID>/` 取 `groupId`，再
`PUT /api/0/issues/<groupId>/` 带 `{"status": "resolved"}`。直接把 short ID 塞
`/api/0/issues/<ID>/` 会 404。用标准库 `urllib`，不装第三方包。

## 验证

`--check` 打印仓库 / 配置 / org / hook 模式 / token 来源；`--only --issue <ID>` 只用给定 ID、
不扫提交范围（验证 REST 分支时别把扫到的真实 issue 一起关掉）；纯函数自检在
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
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from .config import CONFIG

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMAND = "python -m tools.sentry.cli issue-resolve"
HOOK_MARKER = "# managed-by: sentry_issue_resolve"
DEFAULT_BASE_URL = "https://sentry.io"
DEFAULT_ISSUE_PREFIX = "PYTHON-"

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
    method: str, url: str, token: str, payload: dict[str, str] | None = None
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
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read().decode("utf-8", "replace")
    parsed = json.loads(raw) if raw.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_issues(ids: list[str], org: str, base: str, token: str) -> int:
    """逐个把 short ID 置 resolved；单个失败只告警，不中断后面的。"""
    done = 0
    for short_id in ids:
        try:
            info = api_request("GET", shortids_url(base, org, short_id), token)
            group_id = info.get("groupId")
            if not isinstance(group_id, str) or not group_id:
                warn(f"{short_id} 没解析到 groupId，跳过")
                continue
            api_request("PUT", issue_url(base, group_id), token, {"status": "resolved"})
            say(f"resolved {short_id} (group {group_id})")
            done += 1
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
    say(f"待关闭: {', '.join(ids) or '（无）'}")
    if not args.apply:
        say("list-only（未写远端）；hook 加 --apply 才实际关闭")
        return 0
    if not ids:
        return 0
    target = config_target()
    org = args.org or os.environ.get("SENTRY_ORG") or (target[0] if target else "")
    if not org:
        warn("没有 organization slug（--org / SENTRY_ORG / config.json target），跳过写远端")
        return 0
    token, source = resolve_token()
    if not token:
        warn("拿不到 Sentry token（env / sentry auth token / cli.db 都没有），跳过写远端")
        return 0
    say(f"token 来源: {source}; org={org}; base={args.base_url}")
    count = resolve_issues(ids, org, args.base_url.rstrip("/"), token)
    say(f"本次处理 {count}/{len(ids)}")
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
    mode = "未安装"
    if path and path.exists():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mode = "apply" if "--apply" in text else "list-only"
            if HOOK_MARKER not in text:
                mode = "外部 hook（不是本工具生成的）"
        except OSError:
            mode = "不可读"
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
    parser.add_argument("--org", help="organization slug（默认 env / config.json target）")
    parser.add_argument("--base-url", default=os.environ.get("SENTRY_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--issue-prefix", default=os.environ.get("SENTRY_ISSUE_PREFIX", DEFAULT_ISSUE_PREFIX)
    )
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
