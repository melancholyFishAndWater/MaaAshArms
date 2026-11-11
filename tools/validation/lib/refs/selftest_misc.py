"""--self-test 的后半段用例：A17–A30、A23 临时文件、B7、C6/C7、A2 解析失败守卫。
接收前半段的 checks 列表原地追加；临时目录用完自删。
"""

from __future__ import annotations

from pathlib import Path

from .model import Ctx, Node, Option, Reg
from .rules_misc import check_agent, check_option_shape, template_calls_in_source
from .rules_pipeline_a import (
    _is_inverse_guard,
    check_a17_guard_in_next,
    check_a18_next_length,
    check_a2_custom,
    check_a4_shadowed,
)
from .rules_pipeline_b import (
    check_a19_desc_markers,
    check_a20_jp_self_loop,
    check_a21_self_loop_error_next,
    check_a22_error_next_first,
    check_a23_section_comment,
    check_a25_ensure_banned,
    check_a26_single_consumer_probe,
    check_a27_click_ok,
    check_a28_next_list_cap,
    check_a29_and_box_index,
    check_a30_and_inline_sub,
    check_a31_unreferenced_node,
)
from .selftest_pipeline import synth_ctx

def _checks_rest(checks: list[tuple]) -> None:
    # A17：守卫探针在 next 里重复（pipeline-structure.md §10）
    """后半段用例：A17–A30、A23 临时文件、B7、C6/C7、A2-parse 守卫，追加到 checks。"""
    guard_ctx = synth_ctx(
        {
            "XProbeHome": {"recognition": "OCR"},
            "XProbeOther": {"recognition": "OCR"},
            "XEnsureDup": {
                "recognition": "And",
                "all_of": ["XProbeHome"],
                "inverse": True,
                "next": ["XProbeHome", "XProbeOther"],
            },
            "XEnsureNoDup": {
                "recognition": "And",
                "all_of": ["XProbeHome"],
                "inverse": True,
                "next": ["XProbeOther"],
            },
        }
    )
    check_a17_guard_in_next(guard_ctx)
    checks += [
        (
            any(
                f.id == "A17-guard-in-next" and "XEnsureDup" in f.message
                for f in guard_ctx.findings
            ),
            "next 里重复守卫探针 → 报 A17（info）",
        ),
        (
            not any("XEnsureNoDup" in f.message for f in guard_ctx.findings),
            "next 里没有守卫探针 → 不报 A17",
        ),
    ]

    # A4：必然命中挡住后面；`*Init` 与带 `max_hit` 的例外（v3.4 / v3.7）
    shadow_ctx = synth_ctx(
        {
            "XPlainShadow": {"action": "Custom"},
            "XDoInit": {"action": "Custom", "max_hit": 1},
            "XMaxShadow": {"action": "Custom", "max_hit": 3},
            "XTarget": {"recognition": "OCR"},
            "XBranchList": {
                "recognition": "OCR",
                "next": ["XPlainShadow", "XTarget"],
            },
            "XBranchInit": {"recognition": "OCR", "next": ["XDoInit", "XTarget"]},
            "XBranchMax": {"recognition": "OCR", "next": ["XMaxShadow", "XTarget"]},
        }
    )
    check_a4_shadowed(shadow_ctx)
    shadow_hits = {
        f.message.split(".")[0]: f.severity for f in shadow_ctx.findings
    }
    checks += [
        (
            shadow_hits.get("XBranchList") == "error",
            "必然命中且没有 max_hit、挡住后面 → 报 A4（error）",
        ),
        (
            "XBranchInit" not in shadow_hits,
            "`*Init` 必然命中 → 不报 A4（v3.4：单次初始化）",
        ),
        (
            "XBranchMax" not in shadow_hits,
            "必然命中但带 max_hit → 不报 A4（v3.7：用尽即让位）",
        ),
    ]

    # A18：next 列表长度上限（pipeline-structure.md §10）
    length_ctx = synth_ctx(
        {
            "XProbeA": {"recognition": "OCR"},
            "XProbeB": {"recognition": "OCR"},
            "XProbeC": {"recognition": "OCR"},
            "XProbeD": {"recognition": "OCR"},
            "XProbeE": {"recognition": "OCR"},
            "XProbeF": {"recognition": "OCR"},
            "TaskShort": {"next": ["XProbeA", "XProbeB", "XProbeC", "XProbeD"]},
            "TaskLong": {
                "next": ["XProbeA", "XProbeB", "XProbeC", "XProbeD", "XProbeE"]
            },
            "XFiveLoop": {
                "recognition": "And",
                "all_of": ["XProbeA"],
                "next": [
                    "XProbeA",
                    "XProbeB",
                    "XProbeC",
                    "XProbeD",
                    "XProbeE",
                ],
            },
            "XSixLoop": {
                "recognition": "And",
                "all_of": ["XProbeA"],
                "next": [
                    "XProbeA",
                    "XProbeB",
                    "XProbeC",
                    "XProbeD",
                    "XProbeE",
                    "XProbeF",
                ],
            },
            "XEnsureLong": {
                "next": ["XProbeA", "XProbeB", "XProbeC", "XProbeD", "XProbeE"]
            },
        }
    )
    check_a18_next_length(length_ctx)
    checks += [
        (
            not any("TaskLong" in f.message for f in length_ctx.findings),
            "`Task*` 入口 5 项 → 不报 A18（v3.8：入口完全不判）",
        ),
        (
            not any("TaskShort" in f.message for f in length_ctx.findings),
            "Task 4 项 → 不报 A18",
        ),
        (
            not any("XFiveLoop" in f.message for f in length_ctx.findings),
            "Loop 5 项 → 不报 A18（Loop 上限 5）",
        ),
        (
            any(
                f.id == "A18-next-length" and "XSixLoop" in f.message
                for f in length_ctx.findings
            ),
            "Loop 6 项 → 报 A18",
        ),
        (
            not any("XEnsureLong" in f.message for f in length_ctx.findings),
            "Ensure 不再按 A18 判（v3.1 停用，存量由 A25 管）",
        ),
    ]

    # A28：非任务入口 next ≤ 5 项、不计 BaseBranchError / BaseErrorNext（v3.3 设 7 项，v3.10 降到 5）
    cap_ctx = synth_ctx(
        {
            "XFive": {
                "recognition": "OCR",
                "next": ["P1", "P2", "P3", "P4", "P5"],
            },
            "XSix": {
                "recognition": "OCR",
                "next": ["P1", "P2", "P3", "P4", "P5", "P6"],
            },
            "XFiveFallback": {
                "recognition": "OCR",
                "next": [
                    "P1",
                    "P2",
                    "P3",
                    "P4",
                    "P5",
                    "BaseErrorNext",
                    "[JumpBack]BaseBranchError",
                ],
            },
            "TaskNine": {"next": [f"P{i}" for i in range(1, 10)]},
        }
    )
    check_a28_next_list_cap(cap_ctx)
    cap_hit = {f.message.split(".")[0] for f in cap_ctx.findings}
    checks += [
        ("XFive" not in cap_hit, "非入口 5 项 → 不报 A28"),
        ("XSix" in cap_hit, "非入口 6 项 → 报 A28"),
        (
            "XFiveFallback" not in cap_hit,
            "5 项 + BaseErrorNext + BaseBranchError → 不计入、不报 A28",
        ),
        ("TaskNine" not in cap_hit, "任务入口不报 A28（由 A18 管）"),
    ]

    # A29：And 组合多个子识别要写 box_index（v3.5；v3.6 起只管有 action 的节点）
    box_ctx = synth_ctx(
        {
            "XAndTwo": {
                "recognition": "And",
                "all_of": ["XOcrA", "XOcrB"],
                "action": "Click",
            },
            "XAndTwoBox": {
                "recognition": "And",
                "all_of": ["XOcrA", "XOcrB"],
                "action": "Click",
                "box_index": 1,
            },
            "XAndOne": {
                "recognition": "And",
                "all_of": ["XOcrA"],
                "action": "Click",
            },
            "XAndNoAct": {"recognition": "And", "all_of": ["XOcrA", "XOcrB"]},
            "XOcrA": {"recognition": "OCR"},
            "XOcrB": {"recognition": "OCR"},
        }
    )
    check_a29_and_box_index(box_ctx)
    box_hit = {f.message.split(" ")[0] for f in box_ctx.findings}
    checks += [
        ("XAndTwo" in box_hit, "And 组合 2 个子识别、有 action、没写 box_index → 报 A29"),
        ("XAndTwoBox" not in box_hit, "And 写了 box_index → 不报 A29"),
        ("XAndOne" not in box_hit, "And 只有 1 个子识别 → 不报 A29"),
        ("XAndNoAct" not in box_hit, "And 组合多项但没有 action → 不报 A29（v3.6）"),
    ]

    # A30：多条件 And 的子识别写成内联对象（v3.3）
    sub_ctx = synth_ctx(
        {
            "XAndInline": {
                "recognition": "And",
                "all_of": ["XOcrA", {"recognition": "OCR", "expected": "x"}],
            },
            "XAndNamed": {"recognition": "And", "all_of": ["XOcrA", "XOcrB"]},
            "XAndSingleInline": {
                "recognition": "And",
                "all_of": [{"recognition": "OCR", "expected": "y"}],
            },
            "XOcrA": {"recognition": "OCR"},
            "XOcrB": {"recognition": "OCR"},
        }
    )
    check_a30_and_inline_sub(sub_ctx)
    sub_hit = {f.message.split(" ")[0] for f in sub_ctx.findings}
    checks += [
        ("XAndInline" in sub_hit, "多条件 And 里写内联子识别 → 报 A30"),
        ("XAndNamed" not in sub_hit, "多条件 And 全用命名引用 → 不报 A30"),
        ("XAndSingleInline" not in sub_hit, "单探针包装内联（all_of 一项）→ 不报 A30"),
    ]

    # A19：desc 的方括号标记。左边是四条该报的，右边是三条合规的不该报
    marker_ctx = synth_ctx(
        {
            "XTagUnknown": {"desc": "[ends] 写错的标记"},
            "XTagMax": {"desc": "[max] 限次点击", "action": "Click"},
            "XTagOnce": {"desc": "[once] 每日一次", "max_hit": 2},
            "XTagWork": {"desc": "[Loop] 标在工作节点上", "action": "Click"},
            "XTagEndLoose": {"desc": "[end] 其实还要往下", "next": ["XTagMax"]},
            "XTagMaxOk": {"desc": "[max] 限次点击", "max_hit": 3, "action": "Click"},
            "XTagOnceOk": {"desc": "[once] 每日一次", "max_hit": 1},
            "XTagWorkLoop": {"desc": "[Loop] 工作循环", "next": ["XTagMaxOk"]},
            "XTagEndOk": {"desc": "[end] 收尾", "next": ["BaseTaskStop"]},
            "XTagPlain": {"desc": "没有标记", "action": "Click"},
            "XTagProse": {"desc": "点击[确认]按钮", "action": "Click"},
        }
    )
    check_a19_desc_markers(marker_ctx)
    marker_ids = {f.id for f in marker_ctx.findings}
    checks += [
        ("A19-desc-marker-unknown" in marker_ids, "[ends] 这种自造标记会被拦"),
        (
            any(
                f.id == "A19-desc-marker-mismatch" and "XTagMax" in f.message
                for f in marker_ctx.findings
            ),
            "[max] 没有 max_hit 会被拦",
        ),
        (
            any(
                f.id == "A19-desc-marker-mismatch" and "XTagOnce" in f.message
                for f in marker_ctx.findings
            ),
            "[once] 的 max_hit 不是 1 会被拦",
        ),
        (
            any(
                f.id == "A19-desc-marker-mismatch" and "XTagWork" in f.message
                for f in marker_ctx.findings
            ),
            "[Loop] 标在名字不含 Loop 的节点上会被拦",
        ),
        (
            any(
                f.id == "A19-desc-marker-end" and "XTagEndLoose" in f.message
                for f in marker_ctx.findings
            ),
            "[end] 带非终点 next 会被拦",
        ),
        (
            not any(
                f.id.startswith("A19") and tag in f.message
                for f in marker_ctx.findings
                for tag in (
                    "XTagMaxOk",
                    "XTagOnceOk",
                    "XTagWorkLoop",
                    "XTagEndOk",
                    "XTagPlain",
                    "XTagProse",
                )
            ),
            "合规的标记（含 [end] 收在 BaseTaskStop 上）不报 A19",
        ),
    ]

    # A20 / A21 / A22：自环动作与 BaseErrorNext 的三条（结构文档 §0）
    loop_ctx = synth_ctx(
        {
            "XJpLoop": {"desc": "[jp] 补一刀", "action": "Click", "next": ["XJpLoop"]},
            "XSelfNoErr": {"desc": "自环", "action": "Click", "next": ["XSelfNoErr"]},
            "XSelfOk": {
                "desc": "自环带兜底",
                "action": "Click",
                "next": ["BaseErrorNext", "XSelfOk"],
            },
            "XErrNotFirst": {
                "desc": "兜底排后面",
                "action": "Click",
                "next": ["XOther", "BaseErrorNext"],
            },
            "XOther": {"desc": "[end] 别的", "action": "Click"},
            "XJpNoLoop": {"desc": "[jp] 只补一次", "action": "Click"},
            "BaseErrorNext": {
                "desc": "[end] 连续未命中兜底",
                "recognition": "Custom",
                "next": ["BaseTaskStopError"],
            },
        }
    )
    check_a20_jp_self_loop(loop_ctx)
    check_a21_self_loop_error_next(loop_ctx)
    check_a22_error_next_first(loop_ctx)
    loop_ids = {(f.id, f.message.split(".")[0]) for f in loop_ctx.findings}
    checks += [
        (("A20-jp-self-loop", "XJpLoop") in loop_ids, "[jp] 自环会被拦（A20，error）"),
        (
            ("A21-self-loop-without-error-next", "XSelfNoErr") in loop_ids,
            "自环缺 BaseErrorNext 会被拦（A21）",
        ),
        (
            ("A22-error-next-not-first", "XErrNotFirst") in loop_ids,
            "BaseErrorNext 不在 next[0] 会被拦（A22）",
        ),
        (
            not any(n in ("XSelfOk", "XJpNoLoop", "XOther") for _i, n in loop_ids),
            "带 BaseErrorNext 的自环、[jp] 不自环、没用 BaseErrorNext 的都不报 A20–A22",
        ),
    ]

    # A25：Ensure 存量节点只报 warn（v3.1 类别停用，节点待删）
    banned_ctx = synth_ctx(
        {
            "XEnsureHome": {"next": ["XProbeHome"]},
            "XProbeHome": {"recognition": "OCR"},
            "XEnterHome": {"action": "Click"},
        }
    )
    check_a25_ensure_banned(banned_ctx)
    checks += [
        (
            any(
                f.id == "A25-ensure-banned"
                and f.severity == "warn"
                and "XEnsureHome" in f.message
                for f in banned_ctx.findings
            ),
            "Ensure 节点报 A25（warn，迁移期不能用 error）",
        ),
        (
            not any(
                "XProbeHome" in f.message or "XEnterHome" in f.message
                for f in banned_ctx.findings
            ),
            "非 Ensure 节点不报 A25",
        ),
    ]

    # _is_inverse_guard：按结构判，不再看 Ensure 名字
    checks += [
        (_is_inverse_guard({"inverse": True}), "顶层 inverse → 取反守卫"),
        (
            _is_inverse_guard({"recognition": {"type": "And", "inverse": True}}),
            "识别配置里带 inverse → 取反守卫",
        ),
        (
            not _is_inverse_guard({"recognition": "And", "all_of": ["Z"]}),
            "没有 inverse 的节点不算取反守卫（不再看 Ensure 名字）",
        ),
    ]

    # A26：只被一处引用的探针（node-naming.md v3.1「单探针内联」）
    probe_ctx = synth_ctx(
        {
            "XProbeWrap": {"recognition": "OCR", "expected": "主页"},
            "XBranchWrap": {
                "recognition": "And",
                "all_of": ["XProbeWrap"],
                "next": ["XClickDo"],
            },
            "XProbeMulti": {"recognition": "OCR"},
            "XClickMultiA": {"action": "Click", "next": ["XProbeMulti"]},
            "XClickMultiB": {"action": "Click", "next": ["XProbeMulti"]},
            "XProbeCustom": {
                "recognition": "Custom",
                "custom_recognition": "XProbeCustomReco",
            },
            "XBranchCustom": {
                "recognition": "And",
                "all_of": ["XProbeCustom"],
                "next": ["XClickDo"],
            },
            "XProbeJump": {"recognition": "OCR"},
            "XClickJump": {"next": ["[JumpBack]XProbeJump"]},
            "XProbeEnsure": {"recognition": "OCR"},
            "XEnsureWrap": {
                "recognition": "And",
                "all_of": ["XProbeEnsure"],
                "next": ["XClickDo"],
            },
            "XProbeRoi": {"recognition": "OCR"},
            "XClickRoi": {
                "action": "Click",
                "roi": "XProbeRoi",
                "next": ["XClickDo"],
            },
            "XProbeExternal": {"recognition": "OCR"},
            "XClickExternal": {"next": ["XProbeExternal"]},
            "XClickDo": {"action": "Click"},
        }
    )
    probe_ctx.interface = {"next": ["XProbeExternal"]}
    check_a26_single_consumer_probe(probe_ctx)
    probe_ids = {(f.id, f.message.split(" ")[0]) for f in probe_ctx.findings}
    checks += [
        (
            ("A26-single-consumer-probe", "XProbeWrap") in probe_ids,
            "只被一处 And all_of 包装引用的探针 → 报 A26（info）",
        ),
        (
            not any(
                name
                in (
                    "XProbeMulti",
                    "XProbeCustom",
                    "XProbeJump",
                    "XProbeEnsure",
                    "XProbeRoi",
                    "XProbeExternal",
                )
                for _id, name in probe_ids
            ),
            "多消费者 / 自定义 / JumpBack / Ensure 引用方 / roi 引用 / 外部引用都不报 A26",
        ),
    ]

    # A27：确认按钮统一 *Confirm、禁 ClickOk（v3.2）
    click_ctx = synth_ctx(
        {
            "XClickOk": {"action": "Click"},
            "XClickConfirm": {"action": "Click"},
        }
    )
    check_a27_click_ok(click_ctx)
    checks += [
        (
            any(
                f.id == "A27-click-ok-banned" and "XClickOk" in f.message
                for f in click_ctx.findings
            ),
            "节点名以 Ok 结尾 → 报 A27（error）",
        ),
        (
            not any("XClickConfirm" in f.message for f in click_ctx.findings),
            "Confirm 结尾的节点不报 A27",
        ),
    ]

    # A23：分节注释下面不许紧跟非通用 Probe（要读文件内容，所以写一个临时文件）
    # 不用 tempfile.TemporaryDirectory：它在建目录后会 chmod 0o700，受限沙箱下报
    # PermissionError（本机实测），而这份自检要在本机跑得通。手工建/删即可。
    tmp_dir = Path(__file__).resolve().parents[4] / "temp" / "selftest-a23"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        section = tmp_dir / "sec.json"
        section.write_text(
            "{\n"
            "    /* -------------------- 探测 -------------------- */\n"
            '    "XProbeTop": {"desc": "[end] 顶在分节开头", "recognition": "OCR"},\n'
            "    /* -------------------- 通用探测 -------------------- */\n"
            '    "XProbeGeneric": {"desc": "[end] 通用探针", "recognition": "OCR"},\n'
            "    /* -------------------- 工作 -------------------- */\n"
            '    "XClickWork": {"desc": "干活", "action": "Click"},\n'
            "    // 纯辅助节点：没有 next，不要求注释\n"
            '    "XHelper": {"desc": "[jp] 补一刀", "action": "Click"},\n'
            "    // 主干节点没注释（下面这个）\n"
            '    "XTrunkNoComment": {"desc": "分支", "recognition": "OCR", "next": ["XClickWork"]},\n'
            '    "XTrunkNoComment2": {"desc": "分支2", "recognition": "OCR", "next": ["XClickWork"]}\n'
            "}\n",
            encoding="utf-8",
        )
        sec_ctx = synth_ctx(
            {
                "XProbeTop": {"desc": "[end] 顶在分节开头", "recognition": "OCR"},
                "XProbeGeneric": {"desc": "[end] 通用探针", "recognition": "OCR"},
                "XClickWork": {"desc": "干活", "action": "Click"},
                "XHelper": {"desc": "[jp] 补一刀", "action": "Click"},
                "XTrunkNoComment": {
                    "desc": "分支",
                    "recognition": "OCR",
                    "next": ["XClickWork"],
                },
                "XTrunkNoComment2": {
                    "desc": "分支2",
                    "recognition": "OCR",
                    "next": ["XClickWork"],
                },
            },
            files=("sec.json",),
        )
        sec_ctx.pipeline_files = [section]
        check_a23_section_comment(sec_ctx)
        sec_msgs = [f.message for f in sec_ctx.findings]
        checks += [
            (
                any("XProbeTop" in m for m in sec_msgs),
                "分节注释下紧跟非通用 Probe 会被拦（A23）",
            ),
            (
                not any("XProbeGeneric" in m for m in sec_msgs),
                "「通用探测」段下面放 Probe 不报 A23",
            ),
            (
                not any("XClickWork" in m for m in sec_msgs),
                "分节注释下面是动作节点不报 A23",
            ),
            (
                any("XTrunkNoComment2" in m for m in sec_msgs),
                "主干节点（有 next）没注释会被拦（A24）",
            ),
            (
                not any("XHelper" in m for m in sec_msgs),
                "纯辅助节点（没有 next）不要求注释，不报 A24",
            ),
            (
                not any("XTrunkNoComment（" in m for m in sec_msgs),
                "注释紧挨着的主干节点不报 A24",
            ),
        ]
    finally:
        section.unlink(missing_ok=True)
        tmp_dir.rmdir()

    # B7：checkbox 的 default_case 是数组，不应再按标量比较误报
    opt_ok = Ctx(repo_root=Path("."))
    opt_ok.options = {
        "reward": Option(
            "reward",
            {
                "type": "checkbox",
                "default_case": ["siku_reward_switch"],
                "cases": [
                    {"name": "siku_reward_switch", "label": "司库部奖励"},
                    {"name": "shop", "label": "采购中心"},
                ],
            },
            Path("assets/resource/tasks/option/reward.json"),
        )
    }
    check_option_shape(opt_ok)
    checks += [
        (
            not any(
                f.id == "B7-default-case" and "reward" in f.message
                for f in opt_ok.findings
            ),
            "checkbox 的数组 default_case 含 cases[0] → 不报 B7-default-case",
        ),
    ]

    opt_bad = Ctx(repo_root=Path("."))
    opt_bad.options = {
        "reward_bad": Option(
            "reward_bad",
            {
                "type": "checkbox",
                "default_case": ["other"],
                "cases": [{"name": "siku_reward_switch", "label": "司库部奖励"}],
            },
            Path("p.json"),
        )
    }
    check_option_shape(opt_bad)
    checks += [
        (
            any(f.id == "B7-default-case" for f in opt_bad.findings),
            "checkbox 的 default_case 不含 cases[0] → 仍报 B7-default-case",
        ),
    ]

    # C6/C7：agent 里 JTemplateMatch 的模板实参（字面量才查图片；变量 / f-string 只提示）
    agent_src = (
        "class XReco(CustomRecognition):\n"
        "    def analyze(self, context, argv):\n"
        "        context.run_recognition_direct(\n"
        "            JRecognitionType.TemplateMatch,\n"
        '            JTemplateMatch(["Train/ArrowDart.png", "Train/ArrowLocked.png"]),\n'
        "            argv.image,\n"
        "        )\n"
        "        probe = JTemplateMatch(template=[name], roi=(0, 0, 1, 1))\n"
        '        ignored = JOCR(["Train/NotATemplate.png"])\n'
    )
    template_calls = template_calls_in_source(agent_src)
    template_values_list = [values for _s, _e, _o, values in template_calls]
    checks += [
        (len(template_calls) == 2, "agent 模板调用只认 JTemplateMatch"),
        (
            ["Train/ArrowDart.png", "Train/ArrowLocked.png"] in template_values_list,
            "agent 模板字面量逐个取出",
        ),
        ([None] in template_values_list, "agent 模板变量记 None"),
        (
            any(owner == "XReco.analyze" for _s, _e, owner, _v in template_calls),
            "agent 模板归属到方法",
        ),
    ]

    # D1：只有带 `@AgentServer.custom_*` 的模块才要求被 main 导入
    # （实测 `agent/shared/direct.py` 只是还没用上的工具模块，没有装饰器，报 D1 是假红）
    d1_dir = Path(__file__).resolve().parents[4] / "temp" / "selftest-d1" / "agent"
    d1_shared = d1_dir / "shared"
    d1_shared.mkdir(parents=True, exist_ok=True)
    try:
        (d1_dir / "main.py").write_text("import shared.used\n", encoding="utf-8")
        (d1_shared / "used.py").write_text("VALUE = 1\n", encoding="utf-8")
        (d1_shared / "plain.py").write_text("VALUE = 2\n", encoding="utf-8")
        (d1_shared / "registered.py").write_text(
            "from maa.agent.agent_server import AgentServer\n"
            "\n"
            '@AgentServer.custom_action("XLostAct")\n'
            "class XLostAct:\n"
            "    pass\n",
            encoding="utf-8",
        )
        d1_ctx = Ctx(repo_root=Path("."))
        d1_ctx.agent_files = [
            d1_dir / "main.py",
            d1_shared / "used.py",
            d1_shared / "plain.py",
            d1_shared / "registered.py",
        ]
        check_agent(d1_ctx)
        checks += [
            (
                any(
                    f.id == "D1-unimported-module"
                    and "registered.py" in str(f.file)
                    for f in d1_ctx.findings
                ),
                "没被 main 导入、但带 @AgentServer.custom_* 的模块报 D1",
            ),
            (
                not any(
                    "plain.py" in str(f.file) or "used.py" in str(f.file)
                    for f in d1_ctx.findings
                ),
                "没有装饰器的工具模块不报 D1（还没用上不算错）",
            ),
        ]
    finally:
        for path in sorted(d1_shared.glob("*.py")) + [d1_dir / "main.py"]:
            path.unlink(missing_ok=True)
        for folder in (d1_shared, d1_dir, d1_dir.parent):
            folder.rmdir() if folder.exists() else None
    broken_ctx = Ctx(repo_root=Path("."))
    broken_ctx.regs = {"XReco": Reg("XReco", "recognition", "XReco", Path("x.py"), 1)}
    broken_ctx.add("A0-parse-error", "error", Path("p.json"), None, "坏文件")
    check_a2_custom(broken_ctx)
    checks += [
        (
            not any(f.id == "A2-unused-registration" for f in broken_ctx.findings),
            "pipeline 解析失败时不报 A2-unused",
        )
    ]

    # A31：没有任何引用、也没被 interface / agent 提到的节点
    #   非 base.json：探针 warn、动作/分支 error；base.json 一律 warn；自引用不算引用
    a31_ctx = Ctx(repo_root=Path("."))
    a31_base = Path("base.json")
    a31_misc = Path("gift.json")

    def a31_node(name: str, data: dict, path: Path = a31_misc) -> None:
        a31_ctx.nodes[name] = Node(name, data, path)

    a31_node(
        "XClickDead",
        {"action": "Click", "target": [1, 2]},
    )
    a31_node(
        "XBranchDead",
        {"recognition": "OCR", "next": ["BaseErrorNext"]},
    )
    a31_node("XProbeDead", {"recognition": "OCR", "expected": "x"})
    a31_node(
        "XClickUsed",
        {"action": "Click"},
    )
    a31_node(
        "XSelfOnly",
        {"action": "Click", "next": ["XSelfOnly", "BaseErrorNext"]},
    )
    a31_node("XBaseDead", {"action": "Click"}, a31_base)
    a31_node(
        "TaskGiftEntry",
        {"recognition": "OCR", "next": ["XClickUsed"]},
    )
    check_a31_unreferenced_node(a31_ctx)
    a31 = {(f.message.split(" ")[0], f.severity) for f in a31_ctx.findings if f.id == "A31-unreferenced-node"}
    checks += [
        (("XClickDead", "error") in a31, "A31：没人调用的动作节点报 error"),
        (("XBranchDead", "error") in a31, "A31：没人调用的分支节点报 error"),
        (("XProbeDead", "warn") in a31, "A31：没人引用的探针只报 warn"),
        (("XBaseDead", "warn") in a31, "A31：base.json 里无引用只报 warn"),
        (("XSelfOnly", "error") in a31, "A31：只被自己 next 引用的同样算没人用"),
        (
            not any(name in {"XClickUsed", "TaskGiftEntry"} for name, _lv in a31),
            "A31：被别的节点 next 引用、或自己是入口时不报",
        ),
    ]
