"""日志解析与模型层用例：`parse_log` / `parse_nextlist` / `Step` 的解析口径（事件行切分、按 task 归属、空窗口诊断）。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations



from .asserts import CheckFailure, _eq
def check_flow_parser() -> list[str]:
    """流程图解析器：同一份日志，给文件路径与给文本必须解析出**完全相同**的结果。

    这是遥测侧唯一用得上流程图的地方——远程没有本地 `maafw.log` 文件，只有
    Sentry 取回的日志正文。不依赖 Pillow（只解析、不画图），所以 CI 也能跑。
    """
    import io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.drawing.lib.flow_from_log import _first_log_ts, _open_log_lines, parse_log

    sample = "\n".join(
        [
            "[2026-10-14 00:30:00.050][INF][Px1][Tx1][Utils/EventDispatcher.hpp][L65][A::notify] "
            "!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.Starting] "
            '[details={"focus":null,"list":[{"anchor":false,"jump_back":false,"name":"BattleProbeNow"},'
            '{"anchor":false,"jump_back":true,"name":"BattleEnter"}],"name":"BattleLoop","task_id":200000118}] ',
            "[2026-10-14 00:30:00.100][INF][Px1][Tx1][Recognizer.cpp][L1][A::b] "
            "recognize_list [cur_node_=BattleProbeNow] [list=[{name=BattleProbeNow}]]",
            "[2026-10-14 00:30:00.200][INF][Px1][Tx1][Recognizer.cpp][L2][A::c] "
            'reco [result={"name":"BattleProbeNow","algorithm":"OCR","box":[1,2,3,4],"reco_id":11,"detail":{"text":"接管作战"}}]',
            "[2026-10-14 00:30:00.300][INF][Px1][Tx1][TaskBase.cpp][L3][A::d] "
            'action [i=0] [data.repeat=1] [result={"action":"Click","success":true,"action_id":21}]',
            "[2026-10-14 00:30:00.400][INF][Px1][Tx1][PipelineTask.cpp][L4][A::e] "
            'PipelineTask node done [result={"name":"BattleProbeNow","node_id":5,"reco_id":11,"action_id":21,'
            '"completed":true}] [task_id_=200000118]',
            # 不是日志行，必须被跳过且不影响结果
            "Total 0 files, 0 bytes",
        ]
    )

    from_text = parse_log(sample, None, None, None, False)
    from_stream = parse_log(io.StringIO(sample), None, None, None, False)
    _eq(len(from_text.steps), 1, "文本输入的步数")
    _eq([s.name for s in from_text.steps], ["BattleProbeNow"], "文本输入解析出的节点")
    _eq([s.name for s in from_stream.steps], ["BattleProbeNow"], "文本流输入的节点")
    _eq(from_text.steps[0].texts, ["接管作战"], "文本输入抓到的 OCR 文本")
    _eq(from_text.steps[0].action, "Click", "文本输入抓到的动作")
    # 同一份日志里的 NextList 事件要认出来（小写 + 行尾那个 `]` 都实测踩过）
    _eq(from_text.stats.nextlist_seen, 1, "NextList 事件计数")
    _eq(from_text.steps[0].parent, "BattleLoop", "父节点从 NextList 的 name 来")
    _eq(
        from_text.steps[0].candidates,
        ["BattleProbeNow", "BattleEnter"],
        "候选表按 list 顺序",
    )
    _eq(from_text.steps[0].jumps, ["BattleEnter"], "带 [JumpBack] 的候选")
    _eq(from_text.steps[0].parent_source, "nextlist", "父节点来源")
    # `_first_log_ts` 可能返回 None（不是 maafw.log 的文本），直接 .strftime 会抛
    # AttributeError 而不是可读的断言失败，所以先判 None。
    first_ts = _first_log_ts(sample)
    if first_ts is None:
        raise CheckFailure("_first_log_ts 没解析出首行时间戳")
    _eq(
        first_ts.strftime("%H:%M:%S.%f")[:-3],
        "00:30:00.050",
        "_first_log_ts 取首行时间戳",
    )

    # 路径输入路径仍要可用（本地工具的主用法）
    lines = list(_open_log_lines(sample))
    _eq(len(lines), 6, "文本按行切分")
    return [
        f"  文本 / 文本流 / 路径 三种入参一致（{len(from_text.steps)} 步，"
        f"父节点={from_text.steps[0].parent}）"
    ]
