"""`issue-resolve` 用例：pre-push ref 解析、提交正文里的 issue 提取、REST URL 与 hook 内容。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .asserts import _contains, _eq


def check_issue_resolve() -> list[str]:
    """`issue-resolve` 的纯逻辑：只认新建 v* tag、只认行首关键字、两步 REST URL、hook 恒 exit 0。

    hook 的内容断言不只是"能生成"：它指向的入口（`-m tools.sentry.cli issue-resolve`）、
    `cd` 到仓库根、`exit 0` 三件事任一写错，都会让 pre-push 静默失效或误阻断 push。
    """
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib.issue_resolve import (
        build_hook_script,
        choose_previous_tag,
        describe_args,
        extract_issue_ids,
        issue_url,
        log_args,
        merge_ids,
        parse_ref_lines,
        shortids_url,
    )

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
    _eq(
        parse_ref_lines(lines),
        [("v1.0.0-beta.71", sha), ("v2.0.0", "b" * 64)],
        "只收 remote oid 全零的新建 v* tag",
    )
    _eq(parse_ref_lines([]), [], "没有输入时没有 tag")

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
    _eq(
        extract_issue_ids(body),
        ["PYTHON-32", "PYTHON-34", "PYTHON-35", "PYTHON-2"],
        "只认行首关键字、大小写归一、去重",
    )
    _eq(extract_issue_ids("Fixes MAA-1", "MAA-"), ["MAA-1"], "前缀可换")
    _eq(extract_issue_ids("Fixes #123"), [], "GitHub 编号不算")
    _eq(
        merge_ids(["PYTHON-32"], ["PYTHON-34", "python-32"]),
        ["PYTHON-32", "PYTHON-34"],
        "--issue 追加且去重",
    )
    _eq(merge_ids([], []), [], "两边都空")
    _eq(merge_ids([], ["python-34"]), ["PYTHON-34"], "只给 --issue")

    tags = ["v1.0.0-beta.71", "v1.0.0-beta.70", "v1.0.0-beta.69"]
    _eq(
        choose_previous_tag(tags, "v1.0.0-beta.71"),
        "v1.0.0-beta.70",
        "回退路径取列表里的下一个",
    )
    _eq(choose_previous_tag(tags, "v1.0.0-beta.69"), None, "最早的 tag 没有上一个")
    _eq(choose_previous_tag(tags, "v9.9.9"), None, "不在列表里的 tag")

    _eq(describe_args(sha)[-1], f"{sha}^", "describe 从父提交往回找 tag")
    _eq(log_args("v1", sha), ["log", f"v1..{sha}", "--format=%B"], "提交范围与格式")
    _eq(
        shortids_url("https://sentry.io", "org", "PYTHON-32"),
        "https://sentry.io/api/0/organizations/org/shortids/PYTHON-32/",
        "short ID → groupId 的 URL",
    )
    _eq(
        issue_url("https://sentry.io", "42"),
        "https://sentry.io/api/0/issues/42/",
        "改状态的 URL",
    )

    hook = build_hook_script(True)
    _contains(hook, "-m tools.sentry.cli issue-resolve --apply", "hook 调 CLI 入口")
    _contains(hook, "cd '", "hook 先进仓库根")
    _eq(hook.splitlines()[-1], "exit 0", "hook 最后一行恒 exit 0")
    _eq(hook.splitlines()[-2].endswith(' "$@"'), True, "hook 把远端参数转发给脚本")
    _contains(hook, "managed-by: sentry_issue_resolve", "hook 带可识别的标记")
    _eq("--apply" in build_hook_script(False), False, "默认 list-only 不带 --apply")
    return [
        "新建 tag 判定 / 关键字提取 / 两次 REST URL / hook 入口与 exit 0 各有断言"
    ]
