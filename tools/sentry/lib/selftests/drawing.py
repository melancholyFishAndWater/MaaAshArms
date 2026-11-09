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

    out.append(
        "  回跳重排 / 仅事件模式（4 条识别合成步骤）/ 派发链模式（只剩 Node.NextList）/ "
        "别的 task 的日志不硬画 / 截断行 / 三类空窗口 / 严格 task 过滤 / 任务区间 各自有说法"
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
        manifest["cases"]["PYTHON-Z.png"] = {"fingerprint": dict(fp_a, last_seen="OTHER")}
        taken = overview_filename("PYTHON-Z", tmp, manifest, fp_a)
        _eq(
            taken != "PYTHON-Z.png"
            and taken.startswith("PYTHON-Z_")
            and taken.endswith(".png"),
            True,
            "被别指纹占用时追加哈希",
        )
        manifest["cases"]["PYTHON-Z.png"]["fingerprint"] = fp_a
        _eq(
            overview_filename("PYTHON-Z", tmp, manifest, fp_a),
            "PYTHON-Z.png",
            "同指纹不算占用（幂等跳过）",
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
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return ["概览卡渲染（PNG + .mmd）/ 文件名碰撞 / 来源标记 / 批量前清理旧产物 各自有断言"]


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
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "本地 release 按内容认运行 / 同时间同 task 不同内容不算 / 两份都对得上不猜 各有断言"
    ]
