"""流程图渲染用例：没有节点记录时的诊断、概览卡（概览卡只给文字/现场图）版面、证据包的版本门控与失败 task 定位。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import sys


from .asserts import _eq, _contains, _note_text
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

    out.append(
        "  回跳重排 / 仅事件模式（4 条识别合成步骤）/ 派发链模式（只剩 Node.NextList）/ 截断行 / "
        "三类空窗口 / 严格 task 过滤 / 任务区间 各自有说法"
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
    from tools.drawing.lib.flow_cases import overview_filename
    from tools.sentry.lib import telemetry_index as ti

    tmp = _os.path.join(str(root), "temp", "flow", "_selftest_overview")
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
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return ["概览卡渲染（PNG + .mmd）/ 文件名碰撞 / 来源标记 各自有断言"]


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
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)

    return [
        "老包版本取日志「资源版本」/ 清单 release 优先 / 版本不符退包 / 截断包的失败 task 靠清单定位 各有断言"
    ]
