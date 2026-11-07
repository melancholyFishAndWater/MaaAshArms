"""--self-test 的前半段用例：解析原语、A1–A15 的合成节点树。
只构造内存里的 Ctx，不读仓库文件；返回 checks 列表，顺序即输出顺序。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .jsonc import strip_jsonc_comments
from .model import (
    ANNOTATION_RE,
    Ctx,
    DOC_VERSION_RE,
    NAMING_SPEC_VERSION,
    Node,
    SPEC_STRUCTURE,
    STRUCT_SPEC_VERSION,
    always_hit,
    complement_of,
    parse_ref,
    version_finding,
    version_tuple,
    walk_key,
)
from .rules_misc import annotations_of
from .rules_pipeline_a import (
    check_a13_naming,
    check_a13_references,
    check_a14_loop_fallback,
    check_a15_error_branch,
    check_a7_jumpback,
)
from .rules_pipeline_b import desc_tags

def synth_ctx(nodes: dict[str, dict], files: tuple[str, ...] = ("p.json",)) -> Ctx:
    ctx = Ctx(repo_root=Path("."))
    ctx.nodes = {n: Node(n, d, Path("p.json")) for n, d in nodes.items()}
    ctx.pipeline_files = [Path(f) for f in files]
    return ctx

def _checks_core() -> list[tuple]:
    """前半段用例：解析原语、A1–A15 等，返回 checks 列表。"""
    checks = [
        (
            parse_ref("[JumpBack]Foo")[:2] == ("Foo", True),
            "parse_ref 字符串 + JumpBack",
        ),
        (
            desc_tags("[once][jp] 每日一次") == ["once", "jp"],
            "desc 前缀标记逐个解析",
        ),
        (
            desc_tags("点击[确认]按钮") == [],
            "正文里的方括号不算标记",
        ),
        (
            parse_ref({"name": "Bar", "max_hit": 1})[:2] == ("Bar", False),
            "parse_ref 对象",
        ),
        (parse_ref({"recognition": "OCR"})[0] is None, "parse_ref 内联定义"),
        (
            always_hit({}) and always_hit({"recognition": "DirectHit"}),
            "always_hit DirectHit",
        ),
        (not always_hit({"recognition": "OCR"}), "always_hit 非 DirectHit"),
        (
            complement_of({"recognition": "And", "inverse": True, "all_of": ["Z"]})
            == "Z",
            "complement_of",
        ),
        (
            complement_of({"recognition": "And", "all_of": ["Z"]}) is None,
            "complement_of 非取反",
        ),
        (
            strip_jsonc_comments('{"a": 1, // x\n "b": "// not comment"}')
            == '{"a": 1, \n "b": "// not comment"}',
            "strip_jsonc_comments",
        ),
        (
            json.loads(strip_jsonc_comments('{"a": [1,], // x\n}')) == {"a": [1]},
            "末尾逗号按运行时口径丢掉（meojson 接受）",
        ),
        (
            json.loads(strip_jsonc_comments('{"a": [", ]"]}')) == {"a": [", ]"]},
            "字符串里的 ,] 不算末尾逗号",
        ),
        (
            [v for _p, v in walk_key({"x": {"template": ["a.png"]}}, "template")]
            == [["a.png"]],
            "walk_key 嵌套",
        ),
        (
            re.fullmatch(r"[A-Z][A-Za-z0-9]*", "BattleButton") is not None,
            "CamelCase 判定",
        ),
        (version_tuple("v1.10") > version_tuple("v1.3"), "版本号按数字比大小"),
        (
            ANNOTATION_RE.search("// INFO 使用命名规范版本 v1.2 校验年月日 26/10/13").group(2) == "1.2",  # type: ignore[union-attr]
            "头部注解版本号（旧写法）",
        ),
        (
            [
                spec.label
                for spec, _v, _d in annotations_of(
                    "使用节点命名与分类 v1.5 校验年月日 26/10/16\n使用任务链结构 v1.3 校验年月日 26/10/16"
                )
            ]
            == ["节点命名与分类", "任务链结构"],
            "头部注解（新写法，一行一份）两份都认",
        ),
        (
            [
                (spec.label, date)
                for spec, _v, date in annotations_of(
                    '"$comment": "使用命名规范 v1.6; 使用任务链结构 v1.4"'
                )
            ]
            == [("节点命名与分类", None), ("任务链结构", None)],
            "写法模板那种没有校验日期的简写也认",
        ),
        (DOC_VERSION_RE.search("版本号: v1.3").group(1) == "1.3", "文档版本号"),  # type: ignore[union-attr]
    ]

    # 新增的护栏规则：合成一棵最小的节点树，确认真的会响（也为"别误报"留反例）

    ref_ctx = synth_ctx(
        {
            "TaskX": {"next": ["XEnterHome"]},
            "XEnterHome": {"action": "Click", "next": ["XEnsureHome"]},
            "XEnsureHome": {"next": ["XLoop"]},
            "XLoop": {"next": ["XEnsureHome"]},
        }
    )
    check_a13_references(ref_ctx)
    ref_ids = {f.id for f in ref_ctx.findings}
    checks += [
        (
            "A13-ensure-jumpback" not in ref_ids,
            "A13 不再按 Ensure 名前缀判（v3.1 停用）",
        ),
        (
            "A13-ensure-loop-next" not in ref_ids,
            "Ensure 节点的 next 里放 Loop 也不再报 A13",
        ),
    ]

    naming_ctx = synth_ctx(
        {
            "TaskY": {"next": ["YProbeHome", "YClickYes", "YLoop", "YEnterHome"]},
            "YProbeHome": {"next": ["YEnterHome"]},
            "YClickYes": {"action": "Click"},
            "YLoop": {"action": "Click"},
            "YEnterHome": {"action": "Click", "custom_action_param": {"badKey": 1}},
        },
        files=("BadName.json",),
    )
    check_a13_naming(naming_ctx)
    check_a13_references(naming_ctx)
    naming_ids = {f.id for f in naming_ctx.findings}
    checks += [
        ("A13-probe-next" in naming_ids, "探测节点带 next 会被拦"),
        ("A13-click-synonym" in naming_ids, "ClickYes 这类同义写法会被拦"),
        ("A13-loop-next" in naming_ids, "工作循环没有 next 会被拦"),
        ("A13-key-case" in naming_ids, "参数键非 snake_case 会被拦"),
        ("A13-file-case" in naming_ids, "pipeline 片段文件名不符会被拦"),
    ]

    # 分支 Branch：合规写法不报；带 action / 无识别 / 无 next 三条都报
    branch_ok = synth_ctx(
        {
            "XProbeHome": {"recognition": "OCR"},
            "XBranchHome": {
                "recognition": "And",
                "all_of": ["XProbeHome"],
                "next": ["XProbeHome"],
            },
        }
    )
    check_a13_naming(branch_ok)
    branch_bad = synth_ctx(
        {
            "XBranchBad": {"action": "Click"},
        }
    )
    check_a13_naming(branch_bad)
    bad_ids = {f.id for f in branch_bad.findings}
    checks += [
        (
            not any(f.id.startswith("A13-branch") for f in branch_ok.findings),
            "合规的分支节点不报 A13-branch-*",
        ),
        (
            {"A13-branch-action", "A13-branch-recognition", "A13-branch-next"}
            <= bad_ids,
            "分支带 action / 无识别 / 无 next 都会被拦",
        ),
    ]

    # 本轮新增规则：子 Loop 串联允许（只 info）、指向祖先 Loop 仍报 error。
    # 合成节点名必须以 `Loop` 结尾，否则走不到这条判据（`XLoopMain` 这种写法本身
    # 就违反 §3 的"以 Loop 结尾"；判据按 `endswith("Loop")` 识别目标类别）。
    jb_ok = synth_ctx(
        {
            "XEnterHome": {"action": "Click", "next": ["XLoopMainLoop"]},
            "XLoopMainLoop": {
                "recognition": "And",
                "all_of": ["XProbeHome"],
                "next": ["[JumpBack]XLoopSubLoop"],
            },
            "XLoopSubLoop": {"recognition": "And", "all_of": ["XProbeHome"]},
            "XProbeHome": {"recognition": "TemplateMatch"},
        }
    )
    check_a7_jumpback(jb_ok)
    checks.append(
        (
            any(
                f.id == "A7-jumpback-target"
                and f.severity == "info"
                and "XLoopSubLoop" in f.message
                for f in jb_ok.findings
            ),
            "指向子 Loop 的 [JumpBack] 只报 info",
        )
    )

    jb_ancestor = synth_ctx(
        {
            "XLoopMainLoop": {"recognition": "And", "next": ["XLoopChildLoop"]},
            "XLoopChildLoop": {
                "recognition": "And",
                "next": ["[JumpBack]XLoopMainLoop"],
            },
        }
    )
    check_a7_jumpback(jb_ancestor)
    checks.append(
        (
            any(
                f.id == "A7-jumpback-target" and f.severity == "error"
                for f in jb_ancestor.findings
            ),
            "指向祖先 Loop 的 [JumpBack] 仍报 error",
        )
    )

    # A14：循环末项兜底取反守卫与循环守卫同探针 → warn；不同探针 → 不报
    fallback_ctx = synth_ctx(
        {
            "XProbeWork": {"recognition": "OCR"},
            "XProbeElse": {"recognition": "OCR"},
            "XEnsureWork": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "inverse": True,
            },
            "XEnsureElse": {
                "recognition": "And",
                "all_of": ["XProbeElse"],
                "inverse": True,
            },
            "XLoopWork": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "next": ["[JumpBack]XEnsureWork"],
            },
            "XLoopElse": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "next": ["[JumpBack]XEnsureElse"],
            },
            "XPlainWork": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
            },
            "XLoopPlain": {
                "recognition": "And",
                "all_of": ["XProbeWork"],
                "next": ["[JumpBack]XPlainWork"],
            },
        }
    )
    check_a14_loop_fallback(fallback_ctx)
    checks += [
        (
            any(
                f.id == "A14-fallback-same-probe"
                and f.severity == "warn"
                and "XLoopWork" in f.message
                for f in fallback_ctx.findings
            ),
            "兜底取反守卫与循环守卫同探针 → 报 A14（warn，结构文档 §3 是硬规则）",
        ),
        (
            not any("XLoopElse" in f.message for f in fallback_ctx.findings),
            "兜底取反守卫换一个探针 → 不报 A14",
        ),
        (
            not any("XLoopPlain" in f.message for f in fallback_ctx.findings),
            "末项不是取反守卫（没有 inverse）→ 不报 A14",
        ),
    ]

    # 动词表新增 Enable：pipeline 内的开关型动作不再报 A13-verb
    enable_ctx = synth_ctx({"XEnableReward": {"action": "Custom"}})
    check_a13_naming(enable_ctx)
    checks.append(
        (
            not any(
                f.id == "A13-verb" and "XEnableReward" in f.message
                for f in enable_ctx.findings
            ),
            "Enable 进动词表：XEnableReward 不报 A13-verb",
        )
    )

    # 规范版本：文档/模板与脚本不一致就要报，一致时不报（自检不写文件，只试纯函数）
    def vf_id(found: str | None) -> str:
        verdict = version_finding(found)
        return verdict[0] if verdict else ""

    checks += [
        (vf_id("9.9") == "E2-spec-version", "文档版本更高 → 报 E2"),
        (vf_id("1.2") == "E2-spec-version", "文档版本更低 → 也报 E2"),
        (vf_id(NAMING_SPEC_VERSION.lstrip("v")) == "", "版本一致 → 不报"),
        (vf_id(None) == "E1-spec-missing", "找不到版本号 → E1"),
        (
            version_tuple("v1.2") < version_tuple(NAMING_SPEC_VERSION),
            "旧注解比当前版本低",
        ),
        (
            "任务链结构"
            in (
                version_finding("1.2", STRUCT_SPEC_VERSION, SPEC_STRUCTURE) or ("", "")
            )[1],
            "E2 说清是哪一份规范的版本不一致",
        ),
        (
            version_finding(
                STRUCT_SPEC_VERSION.lstrip("v"), STRUCT_SPEC_VERSION, SPEC_STRUCTURE
            )
            is None,
            "结构文档版本一致 → 不报 E2",
        ),
    ]

    # A15：通用错误分支的引用写法与位次（pipeline-structure.md §9）
    a15_ctx = synth_ctx(
        {
            "BaseBranchError": {
                "recognition": "Or",
                "any_of": ["ProbeConfirmX"],
                "next": ["ProbeHomeX"],
            },
            "ProbeConfirmX": {"recognition": "OCR", "expected": "确认"},
            "ProbeHomeX": {"recognition": "OCR", "expected": "主页"},
            "ClickConfirmX": {
                "recognition": "OCR",
                "expected": "确认",
                "action": "Click",
            },
            "EnsureX": {
                "recognition": "And",
                "all_of": ["ProbeHomeX"],
                "inverse": True,
                "next": ["ProbeHomeX"],
            },
            "StopAllBusyX": {
                "recognition": "Or",
                "any_of": ["ProbeHomeX"],
                "inverse": True,
            },
            "GuardInverseX": {
                "recognition": "OCR",
                "inverse": True,
                "next": ["ProbeHomeX"],
            },
            "EnsureNoInverseX": {
                "recognition": "OCR",
                "next": ["ProbeHomeX"],
            },
            "TaskOnlyError": {"next": ["[JumpBack]BaseBranchError"]},
            "TaskOk": {"next": ["[JumpBack]BaseBranchError", "EnsureX"]},
            "TaskNoJump": {"next": ["BaseBranchError"]},
            "TaskShadowed": {"next": ["EnsureX", "[JumpBack]BaseBranchError"]},
            "TaskInverseEnd": {"next": ["StopAllBusyX", "[JumpBack]BaseBranchError"]},
            "TaskShadowedByGuard": {
                "next": ["GuardInverseX", "[JumpBack]BaseBranchError"]
            },
            "TaskEnsureNoInverse": {
                "next": ["EnsureNoInverseX", "[JumpBack]BaseBranchError"]
            },
            "TaskConflict": {"next": ["[JumpBack]BaseBranchError", "ClickConfirmX"]},
            "TaskClaimFirst": {"next": ["ClickConfirmX", "[JumpBack]BaseBranchError"]},
        }
    )
    check_a15_error_branch(a15_ctx)

    def a15_ids(node_name: str) -> set[str]:
        return {f.id for f in a15_ctx.findings if f.message.startswith(node_name + ".")}

    checks += [
        (not a15_ids("TaskOnlyError"), "只挂 [JumpBack]BaseBranchError → 不报 A15"),
        (not a15_ids("TaskOk"), "错误分支排在取反守卫之前 → 不报 A15"),
        (
            "A15-error-branch-jumpback" in a15_ids("TaskNoJump"),
            "引用错误分支不带 [JumpBack] → 报",
        ),
        (
            "A15-error-branch-shadowed" in a15_ids("TaskShadowed"),
            "错误分支排在顶层 inverse 的节点之后 → 报 shadowed",
        ),
        (
            "A15-error-branch-shadowed" in a15_ids("TaskInverseEnd"),
            "顶层 inverse 的终点探针也算取反守卫 → 报 shadowed",
        ),
        (
            "A15-error-branch-shadowed" in a15_ids("TaskShadowedByGuard"),
            "不叫 Ensure、但顶层 inverse 的节点也算取反守卫 → 报 shadowed",
        ),
        (
            not a15_ids("TaskEnsureNoInverse"),
            "叫 Ensure、但没有 inverse 的节点不算取反守卫 → 不报 shadowed",
        ),
        (
            any(
                f.id == "A15-error-branch-conflict" and f.severity == "error"
                for f in a15_ctx.findings
                if f.message.startswith("TaskConflict.")
            ),
            "错误分支排在自己的确认之前 → 报 conflict（v3.2 起 error）",
        ),
        (
            not a15_ids("TaskClaimFirst"),
            "自己的确认排在错误分支之前 → 不报 conflict",
        ),
    ]

    return checks
