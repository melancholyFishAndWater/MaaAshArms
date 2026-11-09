"""pre-push 触发的 Sentry issue 自动关闭。

放 tools/ 根：与 install.py / configure.py 同属发布链路（见 validation 的 README 开头）。
由 .git/hooks/pre-push 调用，stdin 是 git 给的
    <local ref> <local oid> <remote ref> <remote oid>
只处理**新建**的 refs/tags/v*（remote oid 全零），取 <上一个 v* tag>..<新 tag>
的提交正文，找 Fixes/Closes/Resolves + <短 ID>，再逐个把 Sentry issue 置 resolved。

边界（githooks(5) / git-push(1)）：pre-push 在推送**之前**执行，推送失败也会跑完；
--no-verify 可绕过；只在本机生效。因此本脚本在 hook 模式**恒 exit 0**，
任何异常只告警、绝不阻断 push，且所有写操作幂等（重复 resolve 仍返回 200）。

凭据只从本机取，不进仓库 Secrets：SENTRY_AUTH_TOKEN 环境变量 > `sentry auth token`
> 本机 cli.db 的 auth 表。REST 用标准库 urllib，不装第三方包。

用法见 --help；--selftest 是纯函数自检，零网络、零凭据。
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAME = Path(__file__).name
CONFIG_FILE = REPO_ROOT / "tools" / "sentry" / "config.json"
HOOK_MARKER = "# managed-by: sentry_issue_resolve"
DEFAULT_BASE_URL = "https://sentry.io"
DEFAULT_ISSUE_PREFIX = "PYTHON-"

ZERO_RE = re.compile(r"^0+$")
TAG_REF_RE = re.compile(r"^refs/tags/(v.+)$")
KEYWORD_LINE_RE = re.compile(
    r"^\s*(?:fix(?:es)?|closes?|resolves?)\b\s*:?\s*(.*)$", re.IGNORECASE
)

for _stream in (sys.stdout, sys.stderr):
    if isinstance(_stream, io.TextIOWrapper):
        try:
            _stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            continue


def warn(message: str) -> None:
    """hook 的 stdout 会被 git-send-pack 转发到对端，用户未必看得到；消息一律走 stderr。"""
    print(f"[sentry-issues] {message}", file=sys.stderr)


def note(message: str) -> None:
    warn(message)


# ---------------------------------------------------------------- 纯函数（可自检）


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
    """扫描结果 + --issue 追加，去重且保持顺序。"""
    out = list(scanned)
    for item in extra:
        short_id = item.upper()
        if short_id not in out:
            out.append(short_id)
    return out


def choose_previous_tag(sorted_tags: list[str], current: str) -> str | None:
    """git tag --sort=-creatordate 的回退路径：取列表里 current 之后那个（更旧的）。"""
    for index, tag in enumerate(sorted_tags):
        if tag == current and index + 1 < len(sorted_tags):
            return sorted_tags[index + 1]
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
    script = Path(__file__).resolve().as_posix()
    python = Path(sys.executable).resolve().as_posix()
    flag = " --apply" if apply else ""
    return "\n".join(
        [
            "#!/bin/sh",
            HOOK_MARKER,
            "# 由 " + SCRIPT_NAME + " --install 生成；本 hook 恒 exit 0，不阻断 push。",
            "# 删掉本文件即停用；去掉命令行的 apply 开关就只列清单、不写远端。",
            "py='" + python + "'",
            'if [ ! -x "$py" ]; then py=python; fi',
            '"$py" \'' + script + "'" + flag + ' "$@"',
            "exit 0",
            "",
        ]
    )


# ---------------------------------------------------------------- git / 配置


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
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        org, project = str(data["target"]).split("/", 1)
        return org, project
    except (OSError, ValueError, KeyError) as exc:
        warn(f"读不到 {CONFIG_FILE.name} 的 target：{exc}")
        return None


def previous_v_tag(tag: str, oid: str) -> str | None:
    # annotated tag 的 oid 是 tag object，先剥到 commit；lightweight tag 原样返回
    peeled = run_git(["rev-parse", f"{oid}^{{commit}}"])
    commit = peeled.stdout.strip().splitlines()[0] if peeled.returncode == 0 and peeled.stdout.strip() else oid
    result = run_git(describe_args(commit))
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    listed = run_git(["tag", "--sort=-creatordate", "--list", "v*"])
    return choose_previous_tag(listed.stdout.split(), tag)


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
        note(f"{previous}..{tag}: {', '.join(found) or '（无）'}")
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


# ---------------------------------------------------------------- Sentry REST


def resolve_token() -> tuple[str, str]:
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
        row = connection.execute("SELECT token FROM auth ORDER BY id DESC LIMIT 1").fetchone()
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


def api_request(method: str, url: str, token: str, payload: dict[str, str] | None = None) -> dict[str, object]:
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
    done = 0
    for short_id in ids:
        try:
            info = api_request("GET", shortids_url(base, org, short_id), token)
            group_id = info.get("groupId")
            if not isinstance(group_id, str) or not group_id:
                warn(f"{short_id} 没解析到 groupId，跳过")
                continue
            api_request("PUT", issue_url(base, group_id), token, {"status": "resolved"})
            note(f"resolved {short_id} (group {group_id})")
            done += 1
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            warn(f"{short_id} 处理失败，跳过：{exc}")
    return done


# ---------------------------------------------------------------- 动作


def read_ref_lines(args: argparse.Namespace) -> list[str]:
    if args.ref:
        return list(args.ref)
    if args.stdin_file:
        return Path(args.stdin_file).read_text(encoding="utf-8").splitlines()
    if sys.stdin.isatty():
        return []
    return sys.stdin.read().splitlines()


def hook_mode(args: argparse.Namespace) -> int:
    tags = parse_ref_lines(read_ref_lines(args))
    if not tags:
        note("没有新建的 v* tag，跳过")
        return 0
    note(f"新 tag: {', '.join(name for name, _oid in tags)}")
    # --only：只用 --issue 给出的 ID，不扫提交范围。用于验证 REST 分支而不碰真实 issue。
    scanned = [] if args.only else collect_issue_ids(tags, args.issue_prefix)
    ids = merge_ids(scanned, args.issue)
    note(f"待关闭: {', '.join(ids) or '（无）'}")
    if not args.apply:
        note("list-only（未写远端）；hook 加 --apply 才实际关闭")
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
    note(f"token 来源: {source}; org={org}; base={args.base_url}")
    count = resolve_issues(ids, org, args.base_url.rstrip("/"), token)
    note(f"本次处理 {count}/{len(ids)}")
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
            warn(f"{path} 已存在且不是本脚本生成的，未覆盖；确认后加 --force")
            return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_hook_script(apply).encode("utf-8"))
    print(f"[sentry-issues] 已写入 {path}")
    print(f"[sentry-issues] 模式: {'apply（会写 Sentry）' if apply else 'list-only（只列清单）'}")
    print("[sentry-issues] 卸载：删除该文件，或跑 " + SCRIPT_NAME + " --uninstall")
    return 0


def uninstall_hook() -> int:
    path = git_hook_path()
    if path is None or not path.exists():
        print("[sentry-issues] 没有已安装的 hook")
        return 0
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(f"读不了 hook：{exc}")
        return 1
    if HOOK_MARKER not in content:
        warn(f"{path} 不是本脚本生成的，未删除")
        return 1
    path.unlink()
    print(f"[sentry-issues] 已删除 {path}")
    return 0


def print_check(args: argparse.Namespace) -> int:
    target = config_target()
    path = git_hook_path()
    mode = "未安装"
    if path and path.exists():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            mode = "apply" if "--apply" in text else "list-only"
            if HOOK_MARKER not in text:
                mode = "外部 hook（不是本脚本生成）"
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


def run_selftest() -> int:
    checks = 0

    def expect(condition: bool, name: str) -> None:
        nonlocal checks
        if not condition:
            raise AssertionError(name)
        checks += 1

    zero40 = "0" * 40
    zero64 = "0" * 64
    sha = "a" * 40
    lines = [
        f"refs/tags/v1.0.0-beta.71 {sha} refs/tags/v1.0.0-beta.71 {zero40}",
        f"refs/tags/v2.0.0 {'b' * 64} refs/tags/v2.0.0 {zero64}",
        f"refs/heads/main {sha} refs/heads/main {'c' * 40}",
        f"refs/tags/beta.1 {sha} refs/tags/beta.1 {zero40}",
        f"refs/tags/v1.0.0-beta.70 {zero40} refs/tags/v1.0.0-beta.70 {sha}",
        f"refs/tags/v1.0.0-beta.70 {sha} refs/tags/v1.0.0-beta.70 {sha}",
        "坏行",
    ]
    expect(
        parse_ref_lines(lines)
        == [("v1.0.0-beta.71", sha), ("v2.0.0", "b" * 64)],
        "parse_ref_lines",
    )
    expect(parse_ref_lines([]) == [], "parse_ref_lines empty")

    body = "\n".join(
        [
            "fix: 某修复",
            "",
            "Fixes PYTHON-32",
            "fixes: python-34, PYTHON-35",
            "Closes #123",
            "Resolves PYTHON-2",
            "这里提了一句 PYTHON-999 不算",
            "fixes PYTHON-32",
        ]
    )
    expect(
        extract_issue_ids(body) == ["PYTHON-32", "PYTHON-34", "PYTHON-35", "PYTHON-2"],
        "extract_issue_ids",
    )
    expect(extract_issue_ids("Fixes MAA-1", "MAA-") == ["MAA-1"], "extract prefix override")
    expect(extract_issue_ids("Fixes #123") == [], "extract ignores GitHub issue")
    expect(merge_ids(["PYTHON-32"], ["PYTHON-34", "python-32"]) == ["PYTHON-32", "PYTHON-34"], "merge_ids")
    expect(merge_ids([], []) == [], "merge_ids empty")
    expect(merge_ids([], ["python-34"]) == ["PYTHON-34"], "merge_ids only extra")

    tags = ["v1.0.0-beta.71", "v1.0.0-beta.70", "v1.0.0-beta.69"]
    expect(choose_previous_tag(tags, "v1.0.0-beta.71") == "v1.0.0-beta.70", "choose_previous_tag")
    expect(choose_previous_tag(tags, "v1.0.0-beta.69") is None, "choose_previous_tag oldest")
    expect(choose_previous_tag(tags, "v9.9.9") is None, "choose_previous_tag absent")
    expect(describe_args(sha)[-1] == f"{sha}^", "describe_args")
    expect(log_args("v1", sha) == ["log", f"v1..{sha}", "--format=%B"], "log_args")
    expect(
        shortids_url("https://sentry.io", "org", "PYTHON-32")
        == "https://sentry.io/api/0/organizations/org/shortids/PYTHON-32/",
        "shortids_url",
    )
    expect(issue_url("https://sentry.io", "42") == "https://sentry.io/api/0/issues/42/", "issue_url")
    expect("--apply" in build_hook_script(True), "hook script apply")
    expect("--apply" not in build_hook_script(False), "hook script list-only")
    expect(build_hook_script(True).splitlines()[-2].endswith(' "$@"'), "hook script forwards args")
    expect(build_hook_script(False).splitlines()[-1] == "exit 0", "hook script never blocks push")
    expect(HOOK_MARKER in build_hook_script(False), "hook script marker")
    print(f"selftest: {checks} checks passed")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="pre-push 触发：按提交正文的 Fixes/Closes/Resolves 关闭 Sentry issue",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--selftest", action="store_true", help="纯函数自检，零网络零凭据")
    action.add_argument("--install", action="store_true", help="写入 .git/hooks/pre-push")
    action.add_argument("--uninstall", action="store_true", help="删除本脚本生成的 hook")
    action.add_argument("--check", action="store_true", help="打印配置、hook 模式与 token 来源")
    parser.add_argument("--apply", action="store_true", help="真正写 Sentry（默认只列清单）")
    parser.add_argument("--force", action="store_true", help="--install 时覆盖已有 hook")
    parser.add_argument("--ref", action="append", default=[], metavar="LINE", help="模拟一条 pre-push stdin 行，可重复")
    parser.add_argument("--stdin-file", metavar="PATH", help="从文件读 ref 行（测试用）")
    parser.add_argument("--issue", action="append", default=[], metavar="SHORT-ID", help="额外要处理的 issue，可重复")
    parser.add_argument("--only", action="store_true", help="只用 --issue 给出的 ID，不扫提交范围（验证 REST 用）")
    parser.add_argument("--org", help="organization slug（默认 env / config.json target）")
    parser.add_argument("--base-url", default=os.environ.get("SENTRY_URL", DEFAULT_BASE_URL))
    parser.add_argument("--issue-prefix", default=os.environ.get("SENTRY_ISSUE_PREFIX", DEFAULT_ISSUE_PREFIX))
    parser.add_argument("remote", nargs="?", help="git 给的远端名（hook 调用时自动传入）")
    parser.add_argument("url", nargs="?", help="git 给的远端 URL（hook 调用时自动传入）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.selftest:
        try:
            return run_selftest()
        except AssertionError as exc:
            warn(f"selftest FAILED: {exc}")
            return 1
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


if __name__ == "__main__":
    sys.exit(main())
