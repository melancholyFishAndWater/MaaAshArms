"""流程图版面用例：父节点归属（派发者是 `Node.NextList` 的 name）与重复轮次合并（overlay）的判据。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import sys

from typing import Any

from .asserts import CheckFailure, _eq, _contains, _note_text
def check_flow_parents() -> list[str]:
    """父节点（派发者）的归属规则：候选命中、自循环、跨 task 隔离、几种退回。

    `Node.NextList.*` 是父子链的唯一来源（`[cur_node_=X]` 只知道派发者、不知道候选表）。
    全用合成日志，不联网、不写文件；真实日志上那条链另有验收（`.agents/project.md` 记了结论）。
    """
    import sys
    from datetime import datetime
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_from_log import (
        FlowOptions,
        build_notes,
        parent_groups,
        parse_log,
        parse_nextlist,
        resolve_parent,
        stall_notes,
    )

    def line(ts: str, msg: str, px: str = "Px1") -> str:
        return f"[2026-10-14 {ts}][INF][{px}][Tx1][Unit.cpp][L1][MaaNS::T] {msg}"

    def nextlist(
        ts: str,
        name: str,
        names: list[str],
        tid: int,
        jumps: tuple[str, ...] = (),
        px: str = "Px1",
        kind: str = "Starting",
    ) -> str:
        entries = ",".join(
            '{"anchor":false,"jump_back":%s,"name":"%s"}'
            % ("true" if n in jumps else "false", n)
            for n in names
        )
        return line(
            ts,
            f"!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.{kind}] "
            f'[details={{"focus":null,"list":[{entries}],"name":"{name}","task_id":{tid}}}] ',
            px,
        )

    def done(ts: str, name: str, tid: int, px: str = "Px1") -> str:
        return line(
            ts,
            'PipelineTask node done [result={"name":"%s","node_id":1,"reco_id":1,'
            '"action_id":1,"completed":true}] [task_id_=%d]' % (name, tid),
            px,
        )

    # 1) 候选表里同时有本步与别的候选：父节点 = 派发者，候选表与 [JumpBack] 照抄
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000",
                "TrainingLoop",
                ["TrainingGetStatus", "TrainingEnterOne"],
                100,
            ),
            done("00:00:02.000", "TrainingEnterOne", 100),
        ]
    )
    # 局部变量叫 `st` 而不是 `step`：下面第 9 项会 `def step(...)`，同名会让类型检查把前面的
    # 赋值当成 `step` 的声明，后面每次 `step(...)` 调用都报"Step 不可调用"（Pylance 口径实测 5 条）。
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(st.parent, "TrainingLoop", "父节点 = 派发者")
    _eq(st.candidates, ["TrainingGetStatus", "TrainingEnterOne"], "候选表原样带上")
    _eq(
        st.parent_detail(),
        "候选：TrainingGetStatus ・ √TrainingEnterOne",
        "框里那一行（派发者名字在分组框角标里，这一行只写候选）",
    )

    # 2) 自循环 / 入口合成轮：list 里含自己 → 不画成父子，写成"自派发/自循环"
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000", "BattleWin", ["BaseClickRewardTap", "BattleWin"], 100
            ),
            done("00:00:02.000", "BattleWin", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(st.dispatched, True, "自己派发自己")
    _eq(st.parent_detail(), "父 BattleWin（自派发/自循环）", "自循环的说法")

    # 3) 候选多于 PARENT_CHOICE_LIMIT 项：框里折成"另 N 项"；--list 用 full=True 给全
    text = "\n".join(
        [
            nextlist("00:00:01.000", "Hub", ["A", "B", "C", "D", "E"], 100),
            done("00:00:02.000", "D", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _contains(st.parent_detail(), "另 3 项", "框里折起来的候选数")
    _eq(st.parent_detail().count("√"), 0, "被折掉的那个不在框里")
    _contains(st.parent_detail(full=True), "√D", "全量时标出本次跑的那个")
    _eq(st.parent_detail(full=True).count("・"), 4, "全量时五个候选都在")

    # 4) 跨 task 隔离：同名公共节点出现在别的任务的候选表里，不能认成它的父节点
    text = "\n".join(
        [
            nextlist(
                "00:00:01.000", "EntrustLoop", ["EntrustTouchDown", "BaseMoveUp"], 200
            ),
            nextlist(
                "00:00:01.500", "BattleLoop", ["BattleProbeNow", "BaseMoveUp"], 100
            ),
            done("00:00:02.000", "BaseMoveUp", 100),
        ]
    )
    _eq(
        parse_log(text, None, None, 100, False).steps[0].parent,
        "BattleLoop",
        "只认同 task 的派发记录",
    )

    # 5) 退回顺序：没有候选记录时用 [cur_node_=X]；连它也没有时留空（图上写"未记录"）
    text = "\n".join(
        [
            line(
                "00:00:01.000",
                "recognize_list [cur_node_=BattleLoop] [list=[{name=BattleProbeNow}]]",
            ),
            done("00:00:02.000", "BattleProbeNow", 100),
        ]
    )
    st = parse_log(text, None, None, 100, False).steps[0]
    _eq(
        (st.parent, st.parent_source, st.candidates),
        ("BattleLoop", "walk", []),
        "退回 cur_node_",
    )
    st = parse_log(
        done("00:00:02.000", "BattleProbeNow", 100), None, None, 100, False
    ).steps[0]
    _eq(
        (st.parent, st.parent_source, st.parent_detail()),
        ("", "none", "父 未记录"),
        "取不到就留空",
    )

    # 6) agent 进程那种形态（`details_json`）**不能**用来判父节点：它的载荷只有 focus + list，
    #    没有 name / task_id（实测本机日志 16 条全是这样）→ 必须返回 None，不能猜。
    agent_line = (
        "[2026-10-14 00:00:01.000][TRC][Px2][Tx1][AgentClient.cpp][L1]"
        "[MaaNS::A::ctx_event_sink] [handle=true] [message=Node.NextList.Starting] "
        '[details_json={"focus":null,"list":[{"anchor":false,"jump_back":false,"name":"X"}]}] '
    )
    _eq(
        parse_nextlist(agent_line, datetime(2026, 10, 14, 0, 0, 1), "Px2"),
        None,
        "没有 name 的 details_json 不参与父节点归属",
    )
    # 但行尾那个 `]` 与大小写要认：同一个正则也得吃下 details_json 里带 name 的写法
    named = agent_line.replace('"focus":null', '"focus":null,"name":"Hub","task_id":7')
    record = parse_nextlist(named, datetime(2026, 10, 14, 0, 0, 1), "Px2")
    if record is None:
        raise CheckFailure("带 name 的 details_json 形态没解析出来")
    _eq(
        (record.name, record.task_id, record.candidates),
        ("Hub", 7, ("X",)),
        "details_json 形态",
    )

    # 7) 时间上限：几十分钟前那条派发记录不再算这一步的父节点
    stale = parse_nextlist(
        nextlist("00:00:01.000", "Hub", ["A"], 100),
        datetime(2026, 10, 14, 0, 0, 1),
        "Px1",
    )
    if stale is None:
        raise CheckFailure("合成的 NextList 行没解析出来")
    _eq(
        resolve_parent([stale], 100, "A", datetime(2026, 10, 14, 1, 0, 1))[0],
        None,
        "超过 NEXTLIST_MAX_GAP_SECONDS 的派发记录不认",
    )

    # 8) 账按**进图的步骤**算，不按解析过的所有记录（`--task-id` 会滤掉别的任务的步骤）
    only_done = parse_log(
        done("00:00:02.000", "BattleProbeNow", 100), None, None, 100, False
    )
    _eq(
        (only_done.stats.parent_unknown, only_done.stats.parent_resolved),
        (1, 0),
        "未归属计数",
    )
    _contains(
        _note_text(build_notes(only_done, FlowOptions(task_id=100))),
        "父节点只能留空",
        "没有 NextList 事件时的注记",
    )

    # 9) 分组（图上套框 + 左上角标）：连续同父才算一组，父节点自己 / 未记录 / 任务起止都不入组
    def step(
        name: str, parent: str, kind: str = "hit", candidates: tuple[str, ...] = ("x",)
    ) -> Any:
        from tools.drawing.lib.flow_from_log import Step as _Step

        return _Step(
            ts=datetime(2026, 10, 14, 0, 0, 1),
            name=name,
            completed=True,
            kind=kind,
            dispatcher=parent,
            candidates=list(candidates) if parent != name else [],
        )

    column = [
        step("TaskA", "TaskA", kind="task"),  # 任务起始：不入组
        step("A1", "Hub"),
        step("A2", "Hub"),  # 同一个父节点连着两步 → 一组
        step("Hub", "Hub"),  # 自派发（父就是自己）→ 不入组，自己隔开
        step("B1", "A2"),  # 父换了 → 新组
        step("none1", ""),  # 父未记录 → 不入组
        step("C1", "A2"),  # 与 B1 不连续 → 另起一组（同父不同组）
    ]
    _eq(
        parent_groups(column),
        [("Hub", 1, 2), ("A2", 4, 4), ("A2", 6, 6)],
        "连续同父才成组；自派发/未记录/任务起止不入组",
    )
    # 10) 候选表评估账 + "卡在派发层"的注记：连续 Failed 才是证据（单次 Failed 是循环推进的正常现象）
    text = "\n".join(
        [
            nextlist("00:00:01.000", "TaskLoop", ["Entry"], 100, kind="Succeeded"),
            nextlist(
                "00:00:02.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            nextlist(
                "00:00:03.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            nextlist(
                "00:00:04.000",
                "TaskLoop",
                ["ReplaysStart", "ReplaysEnd"],
                100,
                kind="Failed",
            ),
            done("00:00:05.000", "TaskLoop", 100),
        ]
    )
    parsed = parse_log(text, None, None, 100, False)
    stat = parsed.nextlist_stats[(100, "TaskLoop")]
    _eq(
        (stat.starting, stat.succeeded, stat.failed),
        (0, 1, 3),
        "候选表评估账按事件类型分记",
    )
    _eq(
        stat.real_candidates,
        ("ReplaysStart", "ReplaysEnd"),
        "真实候选表取全未命中那轮的（入口轮是 [自己]，不算）",
    )
    notes = stall_notes(parsed, 100, {"TaskLoop"})
    _eq(len(notes), 1, "只报一个派发者")
    _contains(notes[0].text, "3 轮全未命中", "注记给出未命中轮数")
    _contains(notes[0].text, "一个候选都没跑起来", "候选全没跑起来时的说法")
    _eq(
        stall_notes(parsed, 100, {"TaskLoop", "ReplaysStart"})[0].text.count(
            "跑起来的候选"
        ),
        1,
        "有候选跑起来时改说跑起来的是哪个",
    )
    _eq(stall_notes(parsed, 999, set()), [], "别的 task 不串台")
    return [
        "候选命中 / 跨 task 隔离 / 自循环 / 折行 / 三种退回 / 时间上限 / 计数口径 / 分组 / "
        "候选表评估账 各自有断言"
    ]


def check_flow_overlay() -> list[str]:
    """重复轮次合并（`overlay_repeats`）：只有连续、同形、整轮无图、无失败框才折。"""
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_from_log import (
        ColumnSpan,
        MediaEntry,
        Step,
        backfill_failure_thumbs,
        column_signature,
        member_clock,
        overlay_repeats,
        window_failure_images,
    )

    def step(
        name: str, kind: str = "hit", run_len: int = 1, completed: bool = True
    ) -> Step:
        return Step(
            ts=_dt(2026, 10, 21, 0, 0, 1),
            name=name,
            completed=completed,
            kind=kind,
            run_len=run_len,
        )

    def col(*names: str) -> list[Step]:
        return [step(n) for n in names]

    def no_thumb(_index: int) -> bool:
        """这几轮都没贴到图。"""
        return False

    def all_thumb(_index: int) -> bool:
        """每一轮都有图（用来验证"有图就不折"）。"""
        return True

    # 1) 三段同形 → 一列 ×3；用首轮的步骤列表（时间戳真实，不是复制出来的第 2/3 轮）
    columns = [col("A", "B"), col("A", "B"), col("A", "B")]
    merged = overlay_repeats(columns, columns, no_thumb, min_span=2)
    _eq(len(merged), 1, "三段同形应折成一列")
    _eq(merged[0][1], ColumnSpan(0, 2), "合并范围")
    _eq(merged[0][0], columns[0], "合并列用首轮的步骤（同一对象，不是副本）")

    # 2) 有图就不折：整组无图才合并，中间任何一轮有图都断开
    thumbs = {1}
    merged = overlay_repeats(columns, columns, lambda i: i in thumbs, min_span=2)
    _eq(
        [m[1] for m in merged],
        [ColumnSpan(0, 0), ColumnSpan(1, 1), ColumnSpan(2, 2)],
        "有图的那一轮把重复段切断",
    )

    # 3) 失败框不折：`classify()` 判 error 的框（未完成的 event）不能被 ×N 藏起来
    bad = col("A", "B") + [step("node not completed", kind="event", completed=False)]
    merged = overlay_repeats(
        [col("A", "B"), bad, col("A", "B")], [bad] * 3, no_thumb, min_span=2
    )
    _eq(len(merged), 3, "带失败框的轮不折")

    # 4) 未命中聚合框进签名：`A,B` 与 `A,B,未命中×6` 形状不同
    with_miss = col("A", "B") + [step("B", kind="miss", run_len=6)]
    _eq(
        column_signature(col("A", "B")) == column_signature(with_miss),
        False,
        "未命中聚合框要与命中步骤区分开",
    )
    merged = overlay_repeats(
        [col("A", "B"), with_miss], [col("A", "B"), with_miss], no_thumb, min_span=2
    )
    _eq(len(merged), 2, "多一个未命中框就不是同形")

    # 5) 折叠次数进签名：`×3` 的一格与三格不等价
    _eq(
        column_signature([step("A", run_len=3)])
        == column_signature(col("A", "A", "A")),
        False,
        "折叠后的 ×N 与逐次记录不等价",
    )

    # 6) 从列首对齐、不做子序列匹配：`A,B,C,D,E` 与 `B,C,D,E` 不合并
    long, short = col("A", "B", "C", "D", "E"), col("B", "C", "D", "E")
    merged = overlay_repeats(
        [long, short, short], [long, short, short], no_thumb, min_span=2
    )
    _eq(
        [m[1] for m in merged],
        [ColumnSpan(0, 0), ColumnSpan(1, 2)],
        "前缀差一步不算重复（后两轮才合并）",
    )

    # 7) 阈值：min_span=2 时两轮就折；min_span=3 时两轮不折
    two = [col("A"), col("A")]
    _eq(len(overlay_repeats(two, two, no_thumb, min_span=2)), 1, "两轮同形即折")
    _eq(len(overlay_repeats(two, two, no_thumb, min_span=3)), 2, "阈值 3 时两轮不折")
    _eq(len(overlay_repeats(two, two, all_thumb, min_span=2)), 2, "有图不折")

    # 8) 中间夹一轮不同形状 → 断成两段，各自按阈值判
    a, b = col("A"), col("B")
    mixed = [a, a, b, a, a]
    _eq(
        [m[1] for m in overlay_repeats(mixed, mixed, no_thumb, min_span=2)],
        [ColumnSpan(0, 1), ColumnSpan(2, 2), ColumnSpan(3, 4)],
        "不连续的重复各自成段",
    )

    # 9) 失败现场图回贴：只贴"时间紧挨着"的那一步、一格只留最近那张、不覆盖自己的精确帧。
    #    踩过的坑：同名 `BaseTaskStopError` 一天失败 4 次，没有时间上限时 45 分钟后那张会赢，
    #    本次失败框就贴着别的跑次的图，读图的人分不出哪张是真的。
    from datetime import timedelta as _td

    base = _dt(2026, 11, 3, 8, 52, 0)

    def at(sec: float, name: str) -> Step:
        return Step(ts=base + _td(seconds=sec), name=name, completed=True, kind="hit")

    hits = [[at(54.330, "BaseTaskStopError")]]
    own = MediaEntry("vision/own.jpg")
    near = MediaEntry("on_error/2026.11.03-08.52.54.334_BaseTaskStopError.png")
    far = MediaEntry("on_error/2026.11.03-09.03.47.104_BaseTaskStopError.png")
    items = [
        (base + _td(minutes=11, seconds=47.104), "BaseTaskStopError", far),
        (base + _td(seconds=54.334), "BaseTaskStopError", near),
    ]

    exact_slot = {(0, 0): (own, True, "")}
    _eq(
        backfill_failure_thumbs(hits, items, exact_slot),
        [],
        "已有自己的精确识别帧时不覆盖",
    )
    _eq(exact_slot[(0, 0)][0], own, "精确帧保住了")

    empty_slot: dict = {}
    _eq(
        backfill_failure_thumbs(hits, items, empty_slot),
        [near.member],
        "只取时间最近、且在时间上限内的那张（45 分钟后那张不贴）",
    )
    _eq(empty_slot[(0, 0)][2], "失败现场", "角标是失败现场")

    far_only: dict = {}
    _eq(
        backfill_failure_thumbs(hits, [items[0]], far_only),
        [],
        "隔了十几分钟的另一次失败不算这一步的现场图",
    )

    # 该 task 的判定窗口：相邻 task 首尾相接（实测差 51 ms）时，光靠 ±8 s 会把上一个
    # task 的现场图算进来；左端不放宽才是对的。
    windowed: dict = {}
    _eq(
        backfill_failure_thumbs(
            hits, items, windowed, window=(base, base + _td(seconds=60))
        ),
        [near.member],
        "落在本 task 窗口里的现场图才贴",
    )
    outside: dict = {}
    _eq(
        backfill_failure_thumbs(
            hits,
            items,
            outside,
            window=(base + _td(seconds=60), base + _td(seconds=120)),
        ),
        [],
        "窗口外的现场图不贴",
    )

    # 10) 文件名 → 时刻；本窗口内的现场图才点名（顶部注记用）
    _eq(member_clock(near), "08:52:54.334", "现场图文件名里的时刻")
    _eq(member_clock(far), "09:03:47.104", "另一张现场图的时刻")
    _eq(member_clock(MediaEntry("vision/x.jpg")), "", "不是现场图就没有时刻")
    _eq(
        window_failure_images(
            items, base + _td(seconds=7), base + _td(seconds=56)
        ),
        ["2026.11.03-08.52.54.334_BaseTaskStopError.png"],
        "只有落在窗口内的现场图才点名",
    )

    return [
        "同形合并 / 有图断开 / 失败断开 / 未命中进签名 / 前缀不等价 / 阈值 / 不连续 各自有断言；"
        "失败现场图回贴按时间上限 + 最近优先 + 不覆盖精确帧"
    ]
