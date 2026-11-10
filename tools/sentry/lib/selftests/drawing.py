"""流程图渲染用例：没有节点记录时的诊断、概览卡（概览卡只给文字/现场图）版面、证据包的版本门控与失败 task 定位。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import sys


from .asserts import CheckFailure, _eq, _contains, _note_text
def check_flow_diagnostics() -> list[str]:
    """流程图解析的诊断行为：每种"画不出来/画得不全"的输入都要有自己的说法。

    这几条都是真实踩过的：Explore 导出的 CSV 时间戳会回跳（原来的提前 break 会把窗口内
    的记录整段丢掉）、Sentry 导出的日志只有识别事件没有 `PipelineTask node done`、
    导出按字节裁剪会把行尾切掉一半、路径写错与"窗口内没有记录"原来报同一句话。
    只解析不画图，不依赖 Pillow。
    """
    import json as json_module
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_from_log import (
        FlowOptions,
        build_notes,
        describe_empty_window,
        parse_log,
        round_steps,
    )
    from tools.drawing.lib.flowlog.stale import check_stale_nodes

    def raw(ts: str, msg: str, px: str = "Px1") -> str:
        return (
            f"[2026-10-14 {ts}][INF][{px}][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"
        )

    def done(ts: str, name: str, tid: int = 100, px: str = "Px1") -> str:
        return raw(
            ts,
            f'PipelineTask node done [result={{"name":"{name}","node_id":1,'
            f'"reco_id":1,"completed":true}}] [task_id_={tid}]',
            px=px,
        )

    def reco(ts: str, name: str, box: str = "[1,2,3,4]") -> str:
        return raw(
            ts,
            f'reco [result={{"name":"{name}","algorithm":"OCR","box":{box},'
            f'"reco_id":1,"detail":{{}}}}]',
        )

    out: list[str] = []

    # 1) 时间戳回跳：行要按时间重排，两条都要在
    shuffled = "\n".join(
        [done("00:30:02.000", "Second"), done("00:30:01.000", "First")]
    )
    parsed = parse_log(shuffled, None, None, None, False)
    _eq(parsed.stats.out_of_order, 1, "回跳行数")
    _eq([s.name for s in parsed.steps], ["First", "Second"], "回跳后按时间重排的顺序")
    _contains(_note_text(build_notes(parsed, FlowOptions())), "已按时间重排", "回跳注记")

    # 2) 只有识别事件：用识别记录合成步骤，不再抛"没有解析到任何节点执行记录"
    only_events = "\n".join(
        [reco("00:31:01.000", "EntrustChooseLevel", box="null")] * 4
    )
    parsed = parse_log(only_events, None, None, None, False)
    _eq(parsed.derived_from, "attempts", "仅事件模式的来源")
    _eq(len(parsed.steps), 4, "合成出的步骤数")
    _eq(len(round_steps(parsed, FlowOptions(collapse=3))), 1, "--collapse 3 折成一格")
    _eq(len(round_steps(parsed, FlowOptions(collapse=1))), 4, "--collapse 1 每次一格")
    _contains(
        _note_text(build_notes(parsed, FlowOptions())), "由识别记录还原", "仅事件模式注记"
    )

    # 3) 行尾被截断的 Task timeout：仍要出现，并标明数字不全
    truncated = raw(
        "00:32:00.000",
        "Task timeout [pretask.name=EntrustMoveToFind] " "[duration_since(start_",
    )
    parsed = parse_log(truncated, None, None, None, False)
    _eq(
        [e.name for e in parsed.events],
        ["Task timeout EntrustMoveToFind"],
        "截断超时事件",
    )
    _contains(parsed.events[0].action, "行尾被截断", "截断超时的说明")
    _eq(parsed.stats.truncated_kept, 1, "截断但保留的条数")

    # 4) 行尾被截断的 reco：读不出就计数，不能抛
    parsed = parse_log(
        raw("00:33:00.000", 'reco [result={"name":"A","box":null'),
        None,
        None,
        None,
        False,
    )
    _eq(parsed.stats.truncated_skipped, 1, "截断被跳过的条数")

    # 5) 路径写错：必须是"没有这个文件"，不能混进"窗口内没有记录"
    opts = FlowOptions()
    missing = "no/such/dir/vision"
    message = describe_empty_window(
        parse_log(missing, None, None, None, False), opts, missing
    )
    _contains(message, "磁盘上没有这个文件", "路径写错的说法")
    _contains(message, "日志正文", "路径写错的说法")

    # 6) 窗口落在日志时间范围外：报出日志自己的时间范围
    elsewhere = done("00:34:00.000", "Only")
    from datetime import datetime as _dt

    opts = FlowOptions(t_from=_dt(2030, 1, 1), t_to=_dt(2030, 1, 2))
    message = describe_empty_window(
        parse_log(elsewhere, opts.t_from, opts.t_to, None, False), opts, elsewhere
    )
    _contains(message, "2026-10-14 00:34:00.000", "空窗口时的日志时间范围")

    # 7) --task-id 严格过滤：别的任务的失败收尾不能进图，框架级事件要留
    mixed = "\n".join(
        [
            done("00:35:00.000", "Mine", tid=100),
            raw(
                "00:35:01.000",
                'task end: [cb_detail={"entry":"Other","task_id":999}] [ret=false]',
            ),
            raw("00:35:02.000", "invalid node id, handle error [node.name=Mine]"),
        ]
    )
    parsed = parse_log(mixed, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events], ["invalid node id"], "别的任务的失败收尾被排掉"
    )
    _eq(parsed.stats.events_dropped, 1, "被排掉的失败事件数")
    _contains(
        _note_text(build_notes(parsed, FlowOptions(task_id=100))),
        "已排除其他 task",
        "排掉事件的注记",
    )

    # 8) 窗口内全是别的 task：说法要指向 task_id，而不是"窗口内没有记录"
    other = done("00:36:00.000", "Theirs", tid=999)
    opts = FlowOptions(task_id=100)
    message = describe_empty_window(
        parse_log(other, None, None, 100, False), opts, other
    )
    _contains(message, "其他 task_id", "全是别人的 task 时的说法")

    # 9) 任务判定窗口：框架级事件没有 task_id，"下一个别的任务的第一步"之外的那些要排掉
    #    （实测：一份装了多次运行的导出把 23:59 那趟训练的 node not completed 画进了 13:11 的图上）
    span_log = "\n".join(
        [
            done("00:40:00.000", "Mine", tid=100),
            done("00:40:05.000", "Mine", tid=100),
            raw(
                "00:40:06.000", "invalid node id, handle error [node.name=Mine]"
            ),  # 自己的失败标记：留
            done("01:10:00.000", "Theirs", tid=200),  # 别的任务开始了：右界
            raw(
                "01:10:05.000", "node not completed, handle error [node.name=Theirs]"
            ),  # 别的运行的：排掉
        ]
    )
    parsed = parse_log(span_log, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events],
        ["invalid node id"],
        "自己的失败标记留、别的运行的排掉",
    )
    _eq(parsed.stats.events_dropped, 1, "窗口外被排掉的事件数")
    _eq(parsed.span is not None, True, "选中任务的判定窗口")
    _eq(len(parsed.steps), 2, "窗口只影响事件与未命中聚合，不动步骤")

    # 10) 有 task end 时右端就是它自己：别的任务连步骤都没有也不能漏进来
    #     （实测 200000054 的图里混进过同一运行里 200000062 的 Task timeout 与 invalid node id）
    ended_log = "\n".join(
        [
            done("00:50:00.000", "Mine", tid=100),
            raw(
                "00:50:09.000",
                'task end: [cb_detail={"entry":"TaskMine","task_id":100}] [ret=false]',
            ),
            raw("00:50:37.000", "invalid node id, handle error [node.name=Later]"),
        ]
    )
    parsed = parse_log(ended_log, None, None, 100, False)
    _eq(
        [e.name for e in parsed.events],
        ["任务结束 TaskMine 失败"],
        "自己的 task end 之外的框架事件被排掉",
    )
    _eq(parsed.stats.events_dropped, 1, "task end 之外被排掉的事件数")

    # 11) 连 reco 行都没有、只剩事件总线时：按该 task 的 Node.NextList 派发记录还原成一条链
    #     （实测 PYTHON-31 的包在失败的那 95 秒里只有 `Node.*` 事件）
    def nextlist(ts: str, kind: str, name: str, candidates: list[str], tid: int = 100) -> str:
        details = {
            "focus": {"name": name},
            "list": [{"name": c, "jump_back": False} for c in candidates],
            "name": name,
            "task_id": tid,
        }
        return raw(f"{ts}", f"[msg=Node.NextList.{kind}] [details={json_module.dumps(details)}]")

    chain_log = "\n".join(
        [
            nextlist("00:41:00.000", "Starting", "TaskMine", ["TaskMine"]),
            nextlist("00:41:00.100", "Succeeded", "TaskMine", ["TaskMine"]),
            nextlist("00:41:01.000", "Starting", "MineLoop", ["MineStep", "BaseBranchError"]),
            nextlist("00:41:01.100", "Failed", "MineLoop", ["MineStep", "BaseBranchError"]),
        ]
    )
    parsed = parse_log(chain_log, None, None, 100, False)
    _eq(parsed.derived_from, "nextlist", "派发链模式的来源")
    _eq([s.name for s in parsed.steps], ["TaskMine", "MineLoop"], "一个派发者一步")
    _eq(parsed.steps[1].dispatcher, "TaskMine", "父节点按链上前一步")
    _eq(parsed.steps[1].parent_source, "chain", "父节点来源 = chain")
    _eq(parsed.stats.parent_derived, 1, "chain 来源单独记账")
    _eq(parsed.steps[1].completed, False, "候选表一次都没成功 → 未完成")
    _eq(parsed.steps[1].kind, "miss", "未完成的派发者画成未命中框")
    _contains(
        _note_text(build_notes(parsed, FlowOptions(task_id=100))),
        "由 NextList 派发记录还原",
        "派发链模式的注记",
    )

    # 12) 日志里只有别的 task 的记录（`Node.NextList` 带 task_id 却不是它）：
    #     不能把整段识别记录当成这个 task 的步骤，否则同一次运行的每个 issue 都画出同一张混合图
    #     （实测 PYTHON-33/35/36 的包只留了 MFA 面板行）
    foreign_log = "\n".join(
        [
            nextlist("00:42:00.000", "Succeeded", "TaskOther", ["TaskOther"], tid=200),
            reco("00:42:01.000", "SomeNode"),
            reco("00:42:02.000", "SomeNode"),
        ]
    )
    parsed = parse_log(foreign_log, None, None, 100, False)
    _eq(parsed.steps, [], "只有别的 task 时不合成步骤")
    _eq(parsed.derived_from, "", "不标成仅事件模式")

    # 13) 运行期覆写建出来的节点：`BaseErrorNextReco` 会 override 出 `<父节点>ErrorNext`，
    #     它不在 pipeline JSON 里，「疑似过时」不能报成"已改名/删除"（实测 PYTHON-3C：
    #     `EscortEnterAcceptErrorNext` 既是失败节点，也被记进 missing）
    override_log = "\n".join(
        [
            done("00:43:00.000", "EscortEnterAccept", 300),
            done("00:43:01.000", "EscortEnterAcceptErrorNext", 300),
            raw(
                "00:43:00.500",
                'handle_context_override_pipeline [req={"_ContextOverridePipelineReverseRequest":1,'
                '"pipeline_override":{"EscortEnterAccept":{"next":["EscortEnterAcceptErrorNext"]},'
                '"EscortEnterAcceptErrorNext":{"action":"Custom",'
                '"custom_action":"BaseTaskStopErrorAct","recognition":"DirectHit"}}}] ',
            ),
            # 内部识别/动作是 MFA 自己建的，不是 pipeline 节点：不该进这份名字集合
            raw(
                "00:43:00.600",
                'Context::override_pipeline [pipeline_override={'
                '"recognition/OCR/0dd7369e-4f37-4128-b174-58468dac622b":{"recognition":"OCR"},'
                '"action/Swipe/bf1bbf9e-1dbd-4c81-8e71-a49d9f779661":{"action":"Swipe"}}] ',
            ),
        ]
    )
    parsed = parse_log(override_log, None, None, 300, False)
    _eq(
        parsed.override_nodes,
        {"EscortEnterAccept", "EscortEnterAcceptErrorNext"},
        "覆写建出来的节点名进 override_nodes（内部识别/动作不算）",
    )
    check = check_stale_nodes(
        root,
        {s.name for s in parsed.steps},
        "v1.0.0-beta.71",
        "cli",
        override_nodes=parsed.override_nodes,
    )
    _eq(check.synthesized, ["EscortEnterAcceptErrorNext"], "覆写节点记成「合成」")
    _eq(check.missing, [], "覆写节点不再被报成已找不到")
    _eq([n.name for n in check.nodes], [], "覆写节点不标疑似过时")
    _contains(
        _note_text(build_notes(parsed, FlowOptions(task_id=300, stale=check))),
        "由运行期覆写合成",
        "合成节点有独立注记",
    )

    out.append(
        "  回跳重排 / 仅事件模式（4 条识别合成步骤）/ 派发链模式（只剩 Node.NextList）/ "
        "别的 task 的日志不硬画 / 截断行 / 三类空窗口 / 严格 task 过滤 / 任务区间 / "
        "运行期覆写合成的节点不算已删除 各自有说法"
    )
    return out

def check_overview_render() -> list[str]:
    """遥测 issue 的概览卡渲染 + 文件名碰撞（`--telemetry-bulk` 没命中日志包时的降级产物）。"""
    import os as _os
    import shutil as _shutil
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_from_log import Note, render_overview
    from tools.drawing.lib.flow_cases import (
        clear_artifacts,
        overview_filename,
        prune_artifacts,
        save_manifest,
    )
    from tools.sentry.lib import telemetry_index as ti

    tmp = _os.path.join(str(root), "temp", "flow", "_selftest_overview")
    _shutil.rmtree(tmp, ignore_errors=True)
    _os.makedirs(tmp, exist_ok=True)
    try:
        # 1) 文件名：空闲 → 基础名；被不同指纹占用 → 追加哈希；同指纹 → 不追加
        manifest: dict = {"schema": 1, "cases": {}}
        fp_a = {
            "short_id": "PYTHON-Z",
            "last_seen": "2026-11-03T00:50:57Z",
            "release": "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55",
            "images": 0,
            "renderer": 20,
        }
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a),
            "PYTHON-Z.png",
            "空闲文件名",
        )
        # 被**别的产物**占着（单包流程图的记录没有 short_id）→ 让路，追加哈希
        manifest["cases"]["PYTHON-Z.png"] = {"fingerprint": {"log": "deadbeef"}}
        taken = overview_filename("PYTHON-Z", tmp, manifest, fp_a)
        _eq(
            taken != "PYTHON-Z.png"
            and taken.startswith("PYTHON-Z_")
            and taken.endswith(".png"),
            True,
            "被别的产物占用时追加哈希",
        )
        # 同一个 issue 的旧记录不算占用：last_seen 变了也沿用原名（否则每来一次新事件就换名）
        manifest["cases"]["PYTHON-Z.png"] = {"fingerprint": dict(fp_a, last_seen="OTHER")}
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a),
            "PYTHON-Z.png",
            "同一 issue 的旧记录复用原名（增量清理不来回改名）",
        )
        manifest["cases"]["PYTHON-Z.png"]["fingerprint"] = fp_a
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a),
            "PYTHON-Z.png",
            "同指纹不算占用（幂等跳过）",
        )
        # 记录丢了但磁盘上还有同名文件（改名 / 旧版本留下的）：名字是本工具的批量形状就
        # 复用它（覆盖旧图），不再追加哈希——否则旧卡永远占着名字、新卡只能叫哈希名
        manifest["cases"].pop("PYTHON-Z.png", None)
        with open(_os.path.join(tmp, "PYTHON-Z.png"), "wb") as handle:
            handle.write(b"stale")
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a),
            "PYTHON-Z.png",
            "没有记录的同名旧图直接覆盖，不写哈希名",
        )
        # 问题名进名字：`PYTHON-3B_Reward.png`（读图的人不用回查标题）
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a, label="Reward"),
            "PYTHON-Z_Reward.png",
            "问题名跟在问题号后面",
        )

        # 2) 渲染：合成卡片 → 一张非空 PNG（+ --mermaid 时的 .mmd），来源标记为 overview
        card = ti.IssueCard(
            "PYTHON-Z",
            "Maa task failed: Entrust (EntrustBranchChapter/recognition)",
            count=12,
            status="unresolved",
            last_seen="2026-11-03T00:50:57Z",
            received="2026-11-03T00:50:57Z",
            release="MFA@v2.16.1+MaaAshArms@v1.0.0-beta.55",
            failure_task_id=200001359,
            failure_node_id=1001,
            permalink="https://example.sentry.io/PYTHON-Z",
        )
        notes = [Note(t, k) for t, k in ti.card_notes(card, [])]
        out = _os.path.join(tmp, "PYTHON-Z.png")
        res = render_overview(
            out,
            title="遥测问题 PYTHON-Z",
            subtitle="12 次 · unresolved",
            fields=[("问题号", "PYTHON-Z"), ("标题", card.title)],
            images=[],
            notes=notes,
            mermaid=True,
        )
        _eq(
            _os.path.isfile(res.path) and _os.path.getsize(res.path) > 0,
            True,
            "概览卡 PNG 产出且非空",
        )
        _eq(
            res.mermaid is not None and _os.path.isfile(res.mermaid),
            True,
            "--mermaid 产出 .mmd",
        )
        _eq(res.derived_from, "overview", "概览卡来源标记")
        _contains(notes[0].text, "beta.55", "注记带资源版本")

        # 3) 批量出图前的清理：删 png/mmd/manifest，不动 explore-csv 的 .log 与子目录
        save_manifest(tmp, manifest)
        keep_log = _os.path.join(tmp, "Explore_2026.log")
        with open(keep_log, "w", encoding="utf-8") as handle:
            handle.write("x\n")
        keep_dir = _os.path.join(tmp, "_sub")
        _os.makedirs(keep_dir, exist_ok=True)
        _eq(clear_artifacts(tmp), 3, "清掉 png + mmd + manifest")
        _eq(_os.path.isfile(keep_log), True, "清理不动 .log")
        _eq(_os.path.isdir(keep_dir), True, "清理不动子目录")
        _eq(clear_artifacts(tmp), 0, "再清一次没有可删的")

        # 4) 增量清理：只动 manifest 里记着的产物，按 issue 归属与"本次是否保留"判定
        pdir = _os.path.join(tmp, "prune")
        _os.makedirs(pdir, exist_ok=True)

        def add(record_name: str, **extra) -> None:
            for suffix in (".png", ".mmd"):
                with open(
                    _os.path.join(pdir, _os.path.splitext(record_name)[0] + suffix), "wb"
                ) as handle:
                    handle.write(b"x")
            prune_manifest["cases"][record_name] = {
                "path": _os.path.join(pdir, record_name),
                **extra,
            }

        prune_manifest: dict = {"schema": 2, "cases": {}}
        kept = {"PYTHON-FF.png"}
        add("PYTHON-FF.png", issue="PYTHON-FF", origin="bulk")  # 本次保留
        add("PYTHON-AA.png", issue="PYTHON-AA", origin="bulk")  # 在集合里但被取代
        add("PYTHON-BB.png", origin="bulk")  # 归属靠文件名前缀，不在集合里
        add("PYTHON-CC_task1.png", origin="case")  # 单包画的，issue 在集合里
        add("PYTHON-DD.png", issue="PYTHON-DD", origin="bulk")  # 本次画失败
        add("Entrust_20261001.png", origin="case")  # 认不出问题号
        prune_manifest["cases"]["PYTHON-EE.png"] = {"issue": "PYTHON-EE"}  # 文件已不在
        prune_manifest["cases"]["..\\evil.png"] = {"issue": "PYTHON-ZZ"}  # 坏 manifest
        removed = prune_artifacts(
            pdir,
            prune_manifest,
            kept,
            issues={"PYTHON-AA", "PYTHON-CC", "PYTHON-FF"},
            failed_issues={"PYTHON-DD"},
        )
        _eq(removed, 2, "清理：集合外一个 + 被取代一个")
        for gone, name in (
            ("PYTHON-AA.png", "被本次重画取代的批量产物"),
            ("PYTHON-AA.mmd", "同干的 .mmd"),
            ("PYTHON-BB.png", "不在本次 issue 集合里的产物"),
        ):
            _eq(_os.path.isfile(_os.path.join(pdir, gone)), False, f"{name} 删掉")
        for stays, name in (
            ("PYTHON-CC_task1.png", "issue 仍在集合里的单包产物"),
            ("PYTHON-DD.png", "本次画失败的 issue 的旧图"),
            ("PYTHON-FF.png", "本次保留的产物"),
            ("Entrust_20261001.png", "认不出问题号的产物"),
        ):
            _eq(_os.path.isfile(_os.path.join(pdir, stays)), True, f"{name} 保留")
        _eq("PYTHON-EE.png" in prune_manifest["cases"], False, "文件已不在的记录被清掉")
        _eq(
            "..\\evil.png" in prune_manifest["cases"],
            True,
            "文件名带路径分隔符的记录不动",
        )

        # 4b) 没有 manifest 记录、但名字是本工具批量形状的旧产物：也按归属清
        for unrecorded in (
            "PYTHON-GG.png",
            "PYTHON-GG.mmd",
            "PYTHON-HH.png",
            "PYTHON-II.png",
            "Entrust_20261001_old.png",
        ):
            with open(_os.path.join(pdir, unrecorded), "wb") as handle:
                handle.write(b"x")
        # HH 本次是画成 `_task1` 的：规范名上的无记录旧图属于"被本次取代"
        removed_unrecorded = prune_artifacts(
            pdir,
            prune_manifest,
            {"PYTHON-FF.png", "PYTHON-HH_task1.png"},
            issues={"PYTHON-AA", "PYTHON-CC", "PYTHON-FF", "PYTHON-HH", "PYTHON-II"},
            failed_issues={"PYTHON-DD"},
        )
        _eq(removed_unrecorded, 2, "无记录旧产物：集合外一个 + 被取代一个")
        _eq(
            _os.path.isfile(_os.path.join(pdir, "PYTHON-GG.png")),
            False,
            "无记录、不在本次集合 → 删",
        )
        _eq(
            _os.path.isfile(_os.path.join(pdir, "PYTHON-GG.mmd")),
            False,
            "同干 .mmd 跟着删",
        )
        _eq(
            _os.path.isfile(_os.path.join(pdir, "PYTHON-HH.png")),
            False,
            "无记录、本次换了名字画 → 删（否则旧图永远占着规范名）",
        )
        _eq(
            _os.path.isfile(_os.path.join(pdir, "PYTHON-II.png")),
            True,
            "无记录、本次该画但没画出来 → 留着",
        )
        _eq(
            _os.path.isfile(_os.path.join(pdir, "Entrust_20261001_old.png")),
            True,
            "无记录、认不出问题号 → 不碰",
        )

        # 4c) 名字里带问题名（`PYTHON-JJ_Reward.png`）：认得出就照同一套规则清
        ndir = _os.path.join(tmp, "named")
        _os.makedirs(ndir, exist_ok=True)
        for unrecorded in (
            "PYTHON-JJ_Reward.png",
            "PYTHON-JJ_Reward_已修复待发版_疑似过时.png",
            "PYTHON-KK_Other.png",
        ):
            with open(_os.path.join(ndir, unrecorded), "wb") as handle:
                handle.write(b"x")
        removed_named = prune_artifacts(
            ndir,
            {"schema": 2, "cases": {}},
            set(),
            issues={"PYTHON-II"},
            failed_issues=set(),
            labels={"PYTHON-JJ": "Reward", "PYTHON-KK": "Reward"},
        )
        _eq(
            removed_named,
            2,
            "带问题名的旧产物：label 对得上的删、对不上的不碰",
        )
        _eq(
            _os.path.isfile(_os.path.join(ndir, "PYTHON-JJ_Reward.png")),
            False,
            "问题名 + 集合外 → 删",
        )
        _eq(
            _os.path.isfile(
                _os.path.join(ndir, "PYTHON-JJ_Reward_已修复待发版_疑似过时.png")
            ),
            False,
            "问题名 + 标记 + 疑似过时也认得出来",
        )
        _eq(
            _os.path.isfile(_os.path.join(ndir, "PYTHON-KK_Other.png")),
            True,
            "问题名对不上 → 当成别的来路，不碰",
        )

        # 5) 「已修复待发版」只是改名：后缀、去标记、改名搬文件、清理认得它
        from tools.drawing.lib.flow_cases import (
            RELEASE_MARK,
            artifact_issue,
            mark_suffix,
            rename_artifact,
            unmarked_name,
        )

        marked = frozenset({"PYTHON-3D"})
        _eq(mark_suffix("PYTHON-3D", marked), f"_{RELEASE_MARK}", "命中的 issue 加后缀")
        _eq(mark_suffix("PYTHON-3A", marked), "", "没命中的不加")
        _eq(mark_suffix("", marked), "", "没有 issue 身份时不加")
        _eq(
            mark_suffix("python-3d", marked),
            f"_{RELEASE_MARK}",
            "大小写归一（Sentry 编号可能小写）",
        )
        _eq(
            unmarked_name(f"PYTHON-3D_{RELEASE_MARK}.png"),
            "PYTHON-3D.png",
            "去掉标记",
        )
        _eq(
            unmarked_name(f"PYTHON-3D_{RELEASE_MARK}_疑似过时.png"),
            "PYTHON-3D_疑似过时.png",
            "去掉标记时保留「疑似过时」",
        )
        _eq(unmarked_name("PYTHON-3D.png"), "PYTHON-3D.png", "没有标记就原样返回")
        _eq(
            artifact_issue(f"PYTHON-3D_{RELEASE_MARK}.png"),
            ("PYTHON-3D", RELEASE_MARK),
            "归属仍按第一个下划线切，带标记不影响 issue 身份",
        )

        rdir = _os.path.join(tmp, "rename")
        _os.makedirs(rdir, exist_ok=True)
        rmap: dict = {"schema": 2, "cases": {}}
        old = "PYTHON-3D_疑似过时.png"
        new = f"PYTHON-3D_{RELEASE_MARK}_疑似过时.png"
        for suffix in (".png", ".mmd"):
            with open(_os.path.join(rdir, _os.path.splitext(old)[0] + suffix), "wb") as fh:
                fh.write(b"x")
        rmap["cases"][old] = {"issue": "PYTHON-3D", "path": _os.path.join(rdir, old)}
        _eq(rename_artifact(rdir, rmap, old, new), True, "改名成功")
        _eq(_os.path.isfile(_os.path.join(rdir, new)), True, "png 落到新名字")
        _eq(
            _os.path.isfile(_os.path.join(rdir, _os.path.splitext(new)[0] + ".mmd")),
            True,
            "同干的 .mmd 跟着改",
        )
        _eq(_os.path.isfile(_os.path.join(rdir, old)), False, "旧名字不再占着")
        _eq(new in rmap["cases"] and old not in rmap["cases"], True, "manifest 记录搬到新名字")
        _eq(
            rename_artifact(rdir, rmap, "PYTHON-ZZ.png", "PYTHON-ZZ_new.png"),
            False,
            "旧文件不在时返回 False（让调用方照常重画）",
        )
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "概览卡渲染（PNG + .mmd）/ 文件名占用判据（同 issue 稳定）/ 来源标记 / "
        "整目录清理 / 按 issue 归属的增量清理 / 「已修复待发版」只改名不重画 各自有断言"
    ]


def check_evidence_pack_gate() -> list[str]:
    """证据包的版本门控与失败 task 定位（`--telemetry-version` 下老证据包怎么处理）。

    两条都来自实测：
    - 老包的 `evidence.json` 没有 `event.release`，版本只能从日志的「资源版本：」行取
      （PYTHON-2P 的包是 beta.61，筛的是 >=beta.68，不能拿它画流程）；
    - Sentry 只留日志尾部时，失败 task 的 `node done` 行会被截掉，`scan_tasks()` 列不出它，
      但清单里的 `failure_task_id` 仍要用来 `--task-id` 定范围（PYTHON-2P 的包只剩 5 个
      task、清单写的是 200000058）。
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.cases.version_filter import VersionFilter
    from tools.drawing.lib.flow_cases import (
        _evidence_task_id,
        _pack_version,
        _usable_evidence_case,
        load_case,
    )
    from tools.sentry.lib.telemetry_index import IssueCard

    tmp = root / "temp" / "flow" / "_selftest_pack"
    _shutil.rmtree(tmp, ignore_errors=True)
    pack = tmp / "PYTHON-ZZ"
    (pack / "debug").mkdir(parents=True)

    def raw(ts: str, msg: str) -> str:
        return f"[2026-11-08 {ts}][INF][Px1][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"

    def done(ts: str, name: str, tid: int) -> str:
        return raw(
            ts,
            f'PipelineTask node done [result={{"name":"{name}","node_id":1,'
            f'"reco_id":1,"completed":true}}] [task_id_={tid}]',
        )

    def nextlist(ts: str, name: str, tid: int) -> str:
        details = {
            "focus": {"name": name},
            "list": [{"name": name}],
            "name": name,
            "task_id": tid,
        }
        return raw(ts, f"[msg=Node.NextList.Starting] [details={_json.dumps(details)}]")

    def reco(ts: str, name: str) -> str:
        return raw(
            ts,
            f'reco [result={{"name":"{name}","algorithm":"OCR","box":null,'
            f'"reco_id":1,"detail":{{}}}}]',
        )

    log_lines = [
        raw("08:00:00.000", "资源版本：v1.0.0-beta.60"),
        done("08:00:01.000", "TaskMine", 200000001),
        done("08:00:02.000", "TaskOther", 200000002),
    ]
    (pack / "debug" / "maafw.log").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema": 1,
        "kind": "sentry-evidence",
        "event": {
            "id": "evt-selftest",
            # 老包没有 release：版本要从日志的「资源版本」行取
            "release": "",
            # 这个 task 不在日志的任务列表里（尾部被截断），仍要用来 --task-id 定范围
            "failure_task_id": 200000099,
        },
    }
    (pack / "evidence.json").write_text(
        _json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    try:
        case = load_case([str(pack / "debug")])
        card = IssueCard("PYTHON-ZZ")
        _eq(
            _pack_version(case),
            "v1.0.0-beta.60",
            "清单没 release 时取日志的「资源版本」行",
        )
        _eq(
            _evidence_task_id(case, card),
            (200000099, 2),
            "清单的 failure_task_id 不在任务列表里也要用来定范围",
        )
        _eq(
            _usable_evidence_case(
                card, case, VersionFilter.parse([">=v1.0.0-beta.68"]), allow_fetch=False
            ),
            None,
            "版本不符且不许重拉时不用这个包",
        )
        _eq(
            _usable_evidence_case(
                card, case, VersionFilter.parse(["<=v1.0.0-beta.61"]), allow_fetch=False
            ),
            case,
            "版本相符时照用",
        )
        # 清单里有 release 时优先用它（比日志的「资源版本」更贴那次事件）
        manifest["event"]["release"] = "MFA@v2.16.1+MaaAshArms@v1.0.0-beta.70"
        (pack / "evidence.json").write_text(
            _json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        _eq(_pack_version(case), "v1.0.0-beta.70", "清单的 event.release 优先")
        _eq(
            _usable_evidence_case(
                card, case, VersionFilter.parse([">=v1.0.0-beta.68"]), allow_fetch=False
            ),
            case,
            "清单 release 落入范围时照用",
        )

        # 任务级失败（标题没有 `(节点/阶段)`，如 `Maa task failed: Replays`）：靠 `Task<label>`
        # 认任务；同一入口在日志里跑过多遍时，要挑清单里那次失败时刻附近的那一遍
        from datetime import timedelta

        from tools.drawing.lib.cases.pack import _local_time

        plain = IssueCard("PYTHON-ZY", "Maa task failed: Replays")
        _eq(plain.label, "Replays", "任务级失败的 label")
        _eq(plain.node_name, "", "任务级失败没有节点名")

        base = _local_time("2026-11-08T00:00:05Z")
        assert base is not None

        def stamp(offset_seconds: int) -> str:
            return (base + timedelta(seconds=offset_seconds)).strftime("%H:%M:%S.%f")[:-3]

        zy = tmp / "PYTHON-ZY"
        (zy / "debug").mkdir(parents=True)
        (zy / "debug" / "maafw.log").write_text(
            "\n".join(
                [
                    nextlist(stamp(-15), "TaskReplays", 200000098),
                    reco(stamp(-5), "ReplaysBranchFormation"),
                    nextlist(stamp(2), "ReplaysBranchFormation", 200000098),
                    # 同一入口 10 分钟后又跑了一遍（重开一局）：不该被当成这次失败
                    nextlist(stamp(600), "TaskReplays", 200000097),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (zy / "evidence.json").write_text(
            _json.dumps(
                {
                    "schema": 1,
                    "kind": "sentry-evidence",
                    "event": {"id": "evt-zy", "received": "2026-11-08T00:00:05Z"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _eq(
            _evidence_task_id(load_case([str(zy / "debug")]), plain),
            (200000098, 2),
            "任务级失败按 Task<label> 认任务，且挑失败时刻附近那次",
        )
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "老包版本取日志「资源版本」/ 清单 release 优先 / 版本不符退包 / 截断包的失败 task 靠清单定位 / "
        "任务级失败按 Task<label> 与失败时刻认任务 各有断言"
    ]


def check_local_release_assist() -> list[str]:
    """本地 release 辅助只按**内容**认同一次运行（不按时间、不按文件名）。

    实测的坑：本地轮转日志的文件名是"轮转时刻"不是内容区间，同一小时里两份日志都可能盖住
    同一个时刻——按时间挑就会挑错。这里把"同一时刻、同 task 号、不同内容"的两份日志摆在一起，
    断言只会挑内容逐字对得上的那份；两份都对得上时认不出就不猜（返回 None）。
    """
    import os as _os
    import shutil as _shutil
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.cases.local_release import LocalRelease
    from tools.drawing.lib.cases.pack import load_case

    tmp = root / "temp" / "flow" / "_selftest_local"
    _shutil.rmtree(tmp, ignore_errors=True)
    pack_dir = tmp / "pack" / "PYTHON-ZZ" / "debug"
    release_debug = tmp / "release" / "debug"
    pack_dir.mkdir(parents=True)
    release_debug.mkdir(parents=True)
    (release_debug / "vision").mkdir()
    (release_debug / "on_error").mkdir()

    def raw(ts: str, msg: str, px: str = "Px1") -> str:
        return f"[2026-11-09 {ts}][INF][{px}][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"

    def done(ts: str, name: str, tid: int, px: str = "Px1") -> str:
        return raw(
            ts,
            f'PipelineTask node done [result={{"name":"{name}","node_id":1,'
            f'"reco_id":1,"completed":true}}] [task_id_={tid}]',
            px=px,
        )

    def timeout(ts: str, node: str) -> str:
        return raw(ts, f"Task timeout [pretask.name={node}] [duration_since(start_clock)=21140ms]")

    pack_lines = [
        done("10:00:10.000", "TaskMine", 200000001),
        done("10:00:20.000", "MineStep", 200000001),
        timeout("10:00:30.000", "NodeX"),
    ]
    (pack_dir / "maafw.log").write_text("\n".join(pack_lines) + "\n", encoding="utf-8")
    # 同一时刻、同 task 号，但内容不是这次运行：名字/内容都不同
    (release_debug / "maafw.log").write_text(
        "\n".join(
            [
                done("10:00:10.000", "TaskOther", 200000001),
                done("10:00:20.000", "OtherStep", 200000001),
                timeout("10:00:30.000", "NodeY"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # 内容逐字相同的那份：文件名看起来更旧，也必须是它
    true_log = release_debug / "maafw.bak.2026.11.09-11.00.00.000.log"
    true_log.write_text(
        "\n".join(pack_lines + [done("10:00:40.000", "MineEnd", 200000001)]) + "\n",
        encoding="utf-8",
    )
    # 本机面板日志：只有产生这个事件的那台机器会打这一行
    event_id = "0123456789abcdef0123456789abcdef"
    panel = tmp / "release" / "logs"
    panel.mkdir()
    (panel / "log-20261109.log").write_text(
        "[2026-11-09 10:00:30.000][INF] [cfg=Default][inst=配置 1/default][src=Worker]"
        f" [op=ExecuteTaskQueue] [Telemetry] 失败事件已加入发送队列：event_id={event_id}\n",
        encoding="utf-8",
    )
    try:
        local = LocalRelease.discover(str(tmp / "release"))
        _eq(local is not None, True, "本地 release 可发现")
        if local is None:
            raise CheckFailure("本地 release 没被发现")
        _eq(
            local.panel_event(event_id) is not None,
            True,
            "面板日志能按 event_id 查到这条失败",
        )
        pack = load_case([str(pack_dir)])
        run = local.find(pack, 200000001)
        _eq(run is not None, True, "内容对得上就认出来")
        if run is not None:
            _eq(
                _os.path.basename(run.log_path),
                true_log.name,
                "挑内容逐字相同的那份，不按文件名/时间",
            )
            _eq(bool(run.vision_dir), True, "同目录的 vision 一并带上")

        # 给了本机事件号也照样按内容认：不因为事件号就跳过对账
        with_event = local.find(pack, 200000001, event_id=event_id)
        _eq(
            with_event is not None and _os.path.basename(with_event.log_path) == true_log.name,
            True,
            "事件号 + 内容都对上 → 认同一份",
        )

        # 只有时间与 task 号相同、内容不同的包：不许认（另一个用户同一时刻的运行）
        other_pack = tmp / "pack2" / "PYTHON-YY" / "debug"
        other_pack.mkdir(parents=True)
        (other_pack / "maafw.log").write_text(
            "\n".join(
                [
                    done("10:00:11.000", "TaskNope", 200000001),
                    done("10:00:21.000", "NopeStep", 200000001),
                    timeout("10:00:31.000", "NodeNope"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _eq(
            local.find(load_case([str(other_pack)]), 200000001),
            None,
            "时间和 task 号相同、行对不上 → 不参与",
        )
        _eq(
            local.find(load_case([str(other_pack)]), 200000001, event_id=event_id),
            None,
            "事件号在本机但行对不上 → 也不参与",
        )

        # 两份本地日志都与包里逐字相同 → 认不出是哪一次，返回 None（不猜）
        (release_debug / "maafw.bak.2026.11.09-12.00.00.000.log").write_text(
            "\n".join(pack_lines) + "\n", encoding="utf-8"
        )
        ambiguous = LocalRelease.discover(str(tmp / "release"))
        assert ambiguous is not None
        _eq(ambiguous.find(pack, 200000001), None, "两份都对得上时不猜")

        # 共享 trace：包的失败行属于另一个任务，目标任务只在更早轮转掉的那份里
        shared_pack = tmp / "shared" / "PYTHON-SH" / "debug"
        shared_pack.mkdir(parents=True)
        other_task_lines = [
            done("10:10:10.000", "TaskA", 200000002),
            done("10:10:20.000", "AStep", 200000002),
            timeout("10:10:30.000", "NodeA"),
        ]
        (shared_pack / "maafw.log").write_text(
            "\n".join(other_task_lines) + "\n", encoding="utf-8"
        )
        release2 = tmp / "release2" / "debug"
        release2.mkdir(parents=True)
        (release2 / "maafw.log").write_text(
            "\n".join(other_task_lines) + "\n", encoding="utf-8"
        )
        rotated = release2 / "maafw.bak.2026.11.09-10.20.00.000.log"
        rotated.write_text(
            "\n".join(
                [other_task_lines[0], other_task_lines[1], done("10:10:40.000", "BStep", 200000099)]
            )
            + "\n",
            encoding="utf-8",
        )
        local2 = LocalRelease.discover(str(tmp / "release2"))
        assert local2 is not None
        shared_case = load_case([str(shared_pack)])
        by_task = local2.find(shared_case, 200000099)
        _eq(by_task is not None, True, "给了 task 就只认含这个 task 的日志")
        if by_task is not None:
            _eq(
                _os.path.basename(by_task.log_path),
                rotated.name,
                "失败行属于别的任务时按 task 归属挑，不按失败行锚定",
            )
        whole_pack = local2.find(shared_case)
        _eq(
            whole_pack is not None
            and _os.path.basename(whole_pack.log_path) == "maafw.log",
            True,
            "没给 task 时仍按失败行锚定（旧行为不变）",
        )
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "本地 release 按内容认运行 / 同时间同 task 不同内容不算 / 两份都对得上不猜 / "
        "共享 trace 时按 task 归属而不是失败行锚定 各有断言"
    ]

def check_incremental_trace() -> list[str]:
    """增量绘制的分步摘要：代码摘要只认代码、数据步只认数据、理由要点到是哪一步。

    判据来自实测需求：改绘图脚本时"哪些图要重画"不能靠人判。这里只验纯逻辑
    （不渲染、不联网、不读真实 temp）：

    - 代码摘要按规范化 AST 算，改注释 / docstring 不算改代码；
    - 时间窗、注记要进 `options` 摘要（旧的 manifest 指纹把这两项漏掉了，改了不重画）；
    - 实现步变了不算"数据变了"，注记变了才算，且理由点到那一步。
    """
    import os as _os
    import shutil as _shutil
    from datetime import datetime as _datetime
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_cases import (
        module_digest,
        parse_signature,
        raster_signature,
    )
    from tools.drawing.lib.flow_from_log import FlowOptions, Note
    from tools.drawing.lib.flowlog.trace import (
        STAGE_ORDER,
        canonical,
        notes_payload,
        options_payload,
        reason_of,
        same_data,
        stage_digest,
        trace_of,
    )

    tmp = _os.path.join(str(root), "temp", "flow", "_selftest_trace")
    _shutil.rmtree(tmp, ignore_errors=True)
    _os.makedirs(tmp, exist_ok=True)
    try:

        def write(name: str, text: str) -> str:
            path = _os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return path

        plain = write("plain.py", "def f(x):\n    return x + 1\n")
        documented = write(
            "documented.py",
            '"""模块说明。"""\n\n\ndef f(x):\n    """函数说明。"""\n    return x + 1  # 尾注释\n',
        )
        changed = write("changed.py", "def f(x):\n    return x + 2\n")
        _eq(
            module_digest(plain) == module_digest(documented),
            True,
            "只差注释/docstring 的源码摘要相同",
        )
        _eq(module_digest(plain) != module_digest(changed), True, "改一个字面量摘要就变")
        broken = write("broken.py", "def f(:\n")
        _eq(
            module_digest(broken) == module_digest(broken),
            True,
            "语法不完整的文件不抛异常、摘要稳定",
        )
        _eq(parse_signature() != raster_signature(), True, "读取实现与绘制实现分别记账")

        base = FlowOptions(task_id=200000001)
        window = FlowOptions(task_id=200000001, t_from=_datetime(2026, 11, 1, 8, 0, 0))
        annotated = FlowOptions(task_id=200000001, extra_notes=[("提示", "info")])
        _eq(
            stage_digest(options_payload(base))
            != stage_digest(options_payload(window)),
            True,
            "时间窗变化改 options 摘要",
        )
        _eq(
            stage_digest(options_payload(base))
            != stage_digest(options_payload(annotated)),
            True,
            "注记变化改 options 摘要",
        )
        _eq(notes_payload([Note("a", "fail")]), [["a", "fail"]], "注记载荷是 (文本, 颜色)")
        _eq(
            canonical({"b": 1, "a": (2, [3])}),
            {"a": [2, [3]], "b": 1},
            "规范化：键排序、元组转列表",
        )

        full = {name: f"v-{name}" for name in STAGE_ORDER}
        _eq(
            same_data(full, dict(full, parse_impl="x", raster_impl="y")),
            True,
            "实现步变了不算数据变",
        )
        _eq(same_data(full, dict(full, notes="x")), False, "注记变了算数据变")
        _eq(
            same_data(full, dict(full, repo="x")),
            True,
            "仓库状态变了不算数据变（只多读一遍日志核对标记）",
        )
        _eq(reason_of(full, dict(full, notes="x")), "注记变了", "理由点到那一步")
        _eq(
            reason_of(full, dict(full, repo="x")),
            "仓库状态变了（只影响「疑似过时」标记）",
            "只改了仓库状态时理由说清是它",
        )
        _eq(
            reason_of(full, dict(full, raster_impl="x")),
            "绘制实现变了",
            "绘制实现变了优先报",
        )
        _eq(reason_of({}, full), "没有旧记录", "没有旧记录")
        _eq(trace_of(full) == trace_of(dict(full)), True, "合成 sha 稳定")
        _eq(
            trace_of(full) != trace_of(dict(full, notes="x")),
            True,
            "任一阶段变了合成 sha 就变",
        )
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "代码摘要去注释 / 时间窗与注记进 options / 数据步判定 / 理由与合成 sha 各自有断言"
    ]


def check_scene_images() -> list[str]:
    """现场图导出：名字带问题名、日志包来源加后缀、按失败时刻挑图、清理只动自己的记录。

    需求是"遥测拉下来的现场图别散在 `temp/evidence/<ID>/debug/on_error/<run_id>/` 里，
    在 flow 产出目录留一份稳定名字的副本；遥测没图时用补充日志包里的命中图兜底"。
    这里只用合成目录（不读真实 `temp/evidence`、不渲染、不联网）。
    """
    import os as _os
    import shutil as _shutil
    import time as _time
    from datetime import datetime as _datetime
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.cases.scene import (
        export_scene,
        prune_scene,
        scene_dir,
        scene_entries,
        scene_filename,
        select_scene,
    )

    tmp = root / "temp" / "flow" / "_selftest_scene"
    _shutil.rmtree(tmp, ignore_errors=True)
    on_error = tmp / "pack" / "PYTHON-ZZ" / "debug" / "on_error"
    on_error.mkdir(parents=True)
    # 同一个 issue 的两次失败：10:00:05 那次两个节点同秒抓图，11:00:00 另一次
    for name, blob in (
        ("2026.11.09-10.00.05.100_NodeA.png", b"a" * 16),
        ("2026.11.09-10.00.05.900_NodeB.png", b"b" * 16),
        ("2026.11.09-11.00.00.000_NodeC.png", b"c" * 16),
    ):
        (on_error / name).write_bytes(blob)

    _eq(scene_filename("PYTHON-3B", "Reward"), "PYTHON-3B_Reward.png", "遥测来的图名")
    _eq(
        scene_filename("PYTHON-3B", "Reward", from_pack=True),
        "PYTHON-3B_Reward_日志包.png",
        "日志包来的图加 _日志包",
    )
    _eq(
        scene_filename("PYTHON-1E", "Gift", from_pack=True, clock="20261109-173719"),
        "PYTHON-1E_Gift_日志包_20261109-173719.png",
        "多张时追加抓图时刻",
    )

    entries = scene_entries(str(on_error))
    _eq([name for _ts, name, _entry in entries], ["NodeA", "NodeB", "NodeC"], "按时刻枚举")
    when = _datetime(2026, 11, 9, 10, 0, 6)
    picked = select_scene(entries, when, "NodeB")
    _eq(
        [entry.member.rsplit("_", 1)[-1] for entry in picked],
        ["NodeB.png", "NodeA.png"],
        "同批两张都取、失败节点排第一；另一小时的失败不跟进来",
    )
    _eq(
        len(select_scene(entries, _datetime(2026, 11, 9, 10, 0, 0))),
        2,
        "用失败时刻夹住批次",
    )
    _eq(
        select_scene(entries, None)[0].member.endswith("NodeC.png"),
        True,
        "不知道失败时刻时只取最新那张",
    )

    manifest: dict = {}
    written = export_scene(
        str(tmp),
        manifest,
        issue="PYTHON-ZZ",
        label="Mine",
        entries=[(entry, True) for entry in picked],
    )
    _eq(
        written,
        [
            "PYTHON-ZZ_Mine_日志包_20261109-100005.png",
            "PYTHON-ZZ_Mine_日志包_20261109-100005-2.png",
        ],
        "同秒两张按序号区分，节点名优先的那张拿不带序号的名字",
    )
    _eq(
        (manifest["images"][written[0]]["from"] or "").endswith("NodeB.png"),
        True,
        "记录里的 from 是来源成员名",
    )
    _eq(
        _os.path.isfile(_os.path.join(scene_dir(str(tmp)), written[0])),
        True,
        "图真的落到 images/ 下",
    )
    first = _os.path.join(scene_dir(str(tmp)), written[0])
    before = _os.path.getmtime(first)
    _time.sleep(0.01)
    again = export_scene(
        str(tmp),
        manifest,
        issue="PYTHON-ZZ",
        label="Mine",
        entries=[(entry, True) for entry in picked],
    )
    _eq(again, written, "重导得到同一组名字")
    _eq(_os.path.getmtime(first) == before, True, "内容一样就不重写（mtime 不动）")
    _eq(
        {rec["source"] for rec in manifest["images"].values()},
        {"pack"},
        "记录里标了来源是日志包",
    )

    _eq(
        prune_scene(str(tmp), manifest, set(), issues={"PYTHON-ZZ"}, keep_issues=set()),
        0,
        "这一轮没导出图就保住上一轮的那几张",
    )
    _eq(
        prune_scene(str(tmp), manifest, set(again), issues=set(), keep_issues=set()),
        len(again),
        "issue 不在本次集合就删",
    )
    _eq(_os.listdir(scene_dir(str(tmp))), [], "删干净")
    _eq(manifest["images"], {}, "记录一起清掉")
    _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "名字带问题名 / 日志包来源加后缀 / 按失败时刻挑同批图 / 同秒加序号 / "
        "内容没变不重写 / 清理按归属且保住没导出的那几张 各有断言"
    ]

