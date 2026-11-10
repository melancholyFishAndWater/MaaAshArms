"""`issue-resolve` 用例：pre-push ref 解析、提交正文提取、状态分流、REST URL 与 hook 内容。

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
        classify_states,
        describe_args,
        extract_issue_ids,
        fixed_in_unreleased,
        hook_mode_label,
        issue_url,
        log_args,
        merge_ids,
        newest_state,
        parse_ref_lines,
        pick_latest_tag,
        pick_manual_range,
        rev_list_args,
        shortids_url,
    )
    import tools.sentry.lib.issue_resolve as ir

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

    # 状态分流：hook 装成 --apply 后 push 时已经关过，手动再跑不能把它们列成「待关闭」。
    # 证据带时刻：push 之后本地遥测库还没刷新（卡片说 unresolved），关闭记录更新 → 取记录。
    ids = ["PYTHON-2T", "PYTHON-2P", "PYTHON-34", "PYTHON-32"]
    evidence = {
        "PYTHON-2T": [(1.0, "resolved", "本地记录")],
        "PYTHON-2P": [(1.0, "IGNORED", "本地遥测库")],
        "PYTHON-34": [(2.0, "unresolved", "本地遥测库")],
    }
    pending, closed = classify_states(ids, evidence)
    _eq(pending, ["PYTHON-34", "PYTHON-32"], "未关闭与状态未知的留在待关闭")
    _eq(
        [short_id for short_id, _source in closed],
        ["PYTHON-2T", "PYTHON-2P"],
        "已 resolved/ignored 的分到已关闭",
    )
    _eq(
        [source for _short_id, source in closed],
        ["本地记录", "本地遥测库"],
        "已关闭的那条带上来源说明",
    )
    _eq(
        newest_state([(1.0, "unresolved", "本地遥测库"), (2.0, "resolved", "本地记录")])[0],
        "resolved",
        "取时刻最新的那条（新记录压过旧卡片）",
    )
    _eq(
        newest_state([(2.0, "resolved", "本地记录"), (3.0, "unresolved", "本地遥测库")])[0],
        "unresolved",
        "反过来也按时刻取（库更新时以库为准）",
    )
    _eq(newest_state([]), ("", 0.0, ""), "没有任何证据时状态为空")
    _eq(classify_states(ids, {})[0], ids, "一个状态都没有时全部按待关闭处理")
    _eq(classify_states([], {})[0], [], "空清单")
    _eq(
        hook_mode_label()
        in {"apply", "list-only", "未安装", "不可读", "外部 hook（不是本工具生成的）"},
        True,
        "hook 模式标签落在约定的几种取值里（不依赖本机装没装）",
    )

    # shortids 的 group 段就带 status：一次 GET 够用；不带 group 时才退回第二次 GET。
    # 用假 api_request 顶掉网络，只验解析与请求次数（自检不联网）。
    original_api = ir.api_request
    calls: list[str] = []

    def fake_api(method, url, token, payload=None, timeout=10.0):
        calls.append(url)
        if "shortids/PYTHON-32" in url:
            return {"groupId": "7835286269", "group": {"status": "resolved"}}
        if "shortids/PYTHON-34" in url:
            return {"groupId": "7835314781"}  # 模拟不带 group 段的部署
        return {"status": "unresolved"}  # GET /issues/<gid>/ 的兜底

    ir.api_request = fake_api
    try:
        calls.clear()
        with_group = ir.shortids_info("PYTHON-32", "org", "https://sentry.io", "t")
        calls_with_group = len(calls)
        calls.clear()
        no_group = ir.shortids_info("PYTHON-34", "org", "https://sentry.io", "t")
        calls_no_group = len(calls)
    finally:
        ir.api_request = original_api
    _eq(with_group, ("7835286269", "resolved"), "shortids 一次 GET 同时取 groupId 与 status")
    _eq(calls_with_group, 1, "带 group.status 时只发一次 GET")
    _eq(no_group, ("7835314781", "unresolved"), "不带 group 段时退回第二次 GET 读 status")
    _eq(calls_no_group, 2, "退回路径确实只多一次 GET")

    # 手动运行用哪段提交：最新 tag 之后有新提交就算 tag..HEAD（要发的版本还没打 tag），
    # HEAD 停在 tag 上时退回"最新 tag 之前、直到次新 tag"那一段。
    _eq(
        pick_manual_range("v1.0.0-beta.71", "v1.0.0-beta.70", 8),
        ("v1.0.0-beta.71", "HEAD"),
        "tag 之后有新提交 → 算 tag..HEAD",
    )
    _eq(
        pick_manual_range("v1.0.0-beta.71", "v1.0.0-beta.70", 0),
        ("v1.0.0-beta.70", "v1.0.0-beta.71"),
        "tag 之后没有提交 → 算次新 tag..最新 tag",
    )
    _eq(
        pick_manual_range("v1.0.0-beta.71", None, 0),
        None,
        "没有次新 tag 且没有新提交 → 不扫全历史",
    )
    _eq(
        pick_manual_range("v1.0.0-beta.71", None, 3),
        ("v1.0.0-beta.71", "HEAD"),
        "没有次新 tag 但有新提交 → 照常算 tag..HEAD",
    )
    _eq(
        rev_list_args("v1.0.0-beta.71", "HEAD"),
        ["rev-list", "--count", "v1.0.0-beta.71..HEAD"],
        "数提交数用 rev-list --count",
    )

    # 画图侧复用这段范围给产物名加「已修复待发版」：必须与手动运行同一段，且**一句都不打**
    # （否则每画一张图都会冒出 issue-resolve 的提示）。用假 run_git 顶掉真实仓库。
    original_git = ir.run_git

    def fake_git(args):
        joined = " ".join(args)
        if joined.startswith("tag --sort"):
            return subprocess.CompletedProcess(args, 0, "v1.0.0-beta.71\nv1.0.0-beta.70\n", "")
        if joined.startswith("rev-parse"):
            # tag 名与 commit oid 都当同一个提交，省掉"annotated tag 剥壳"那段
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if joined.startswith("describe"):
            return subprocess.CompletedProcess(args, 0, "v1.0.0-beta.70\n", "")
        if joined.startswith("rev-list --count"):
            return subprocess.CompletedProcess(args, 0, "3\n", "")
        if joined.startswith("log "):
            body = "fix: 修 A\n\nFixes PYTHON-3D\n\nfix: 修 B\n\nCloses: PYTHON-M\n"
            return subprocess.CompletedProcess(args, 0, body, "")
        return subprocess.CompletedProcess(args, 1, "", "")

    import contextlib as _contextlib
    import io as _io
    import subprocess

    ir.run_git = fake_git
    captured = _io.StringIO()
    try:
        with _contextlib.redirect_stderr(captured):
            label, marked = fixed_in_unreleased("PYTHON-")
    finally:
        ir.run_git = original_git
    _eq(label, "v1.0.0-beta.71..HEAD", "复用手动运行的那段提交")
    _eq(marked, ["PYTHON-3D", "PYTHON-M"], "扫出提交里 Fixes/Closes 的编号")
    _eq(captured.getvalue(), "", "静默：一个字都不往 stderr 打")

    tags = ["v1.0.0-beta.71", "v1.0.0-beta.70", "v1.0.0-beta.69"]
    _eq(
        choose_previous_tag(tags, "v1.0.0-beta.71"),
        "v1.0.0-beta.70",
        "回退路径取列表里的下一个",
    )
    _eq(choose_previous_tag(tags, "v1.0.0-beta.69"), None, "最早的 tag 没有上一个")
    _eq(choose_previous_tag(tags, "v9.9.9"), None, "不在列表里的 tag")

    # 手动运行的范围：`git tag --sort=-creatordate` 的输出第一个就是最新 tag
    _eq(
        pick_latest_tag(["v1.0.0-beta.71", "v1.0.0-beta.70", "v1.0.0-beta.69"]),
        "v1.0.0-beta.71",
        "最新 tag 取列表第一个",
    )
    _eq(pick_latest_tag(["v1.0.0"]), "v1.0.0", "只有一个 tag")
    _eq(pick_latest_tag([]), None, "没有 tag")
    _eq(pick_latest_tag(["beta.1", "v1.0.0"]), "v1.0.0", "忽略非 v 开头的名字")

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
        "新建 tag 判定 / 关键字提取 / 状态分流（本地证据取最新，已关闭不列待关闭）/ "
        "手动范围（tag 后 / tag 前那段）+ 画图侧静默复用 / shortids 一次 GET 取状态 / "
        "两次 REST URL / hook 入口与 exit 0 各有断言"
    ]
