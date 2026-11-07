"""CSV 与速度报表用例：`explore-csv` 的归一化、speed 报表的排序与门槛。

只放用例：断言助手在 `asserts.py`，固定输入在 `fixtures.py`，入口在 `cli.py`。
"""

from __future__ import annotations

import sys

from pathlib import Path

from .asserts import CheckFailure, _eq, _contains, _raises
def check_speed_report() -> list[str]:
    """速度报告：版本排序、截断、样本量门槛、重构对比判定、系统伪任务排除。"""
    import io as _io

    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib.speed_report import (
        DEFAULT_COMPARE_MIN_COUNT,
        DEFAULT_LAST,
        build_speed_report,
        format_ms,
        write_console,
        write_json,
        write_markdown,
    )

    def row(task: str, release: str, p50: float, count: int) -> dict:
        return {
            "description": task,
            "release": release,
            "_count": count,
            "p50(span_duration)": p50,
            "p95(span_duration)": p50 * 2,
            "avg(span_duration)": p50,
        }

    def rel(tag: str) -> str:
        return f"MaaAshArms@v1.0.0-{tag}"

    rows = [
        # 同一任务跨 5 个版本；beta.2 只有 n=1（样本不足）
        row("Training", rel("beta.1"), 100.0, 6),
        row("Training", rel("beta.2"), 5.0, 1),
        row("Training", rel("beta.10"), 200.0, 7),
        row("Training", rel("beta.9"), 120.0, 8),
        # 框架伪任务：应被排除
        row("连接", rel("beta.1"), 4.8, 50),
        row("开始前脚本", rel("beta.1"), 9.7, 50),
        # mfa. 前缀同样是系统伪任务
        row("mfa.task_run", rel("beta.1"), 44385.0, 50),
    ]

    report = build_speed_report(rows, last=0, min_count=2)

    # 1) 版本按语义排序：beta.9 必须在 beta.10 之前（字典序会排错，这是实测踩过的坑）
    _eq(
        [v for v in report.versions],
        ["v1.0.0-beta.1", "v1.0.0-beta.2", "v1.0.0-beta.9", "v1.0.0-beta.10"],
        "版本按版本号排序而非字典序（RELEASE_PATTERN 带包名前缀，喂短标签会全失配）",
    )

    # 2) 系统伪任务被排除
    _eq(
        report.tasks,
        ("Training",),
        "mfa.* 与 config.system_labels 里的伪任务不进任务表",
    )

    # 3) 样本量门槛：n=1 的格子不算够样本
    #    cell() 对没有样本的版本返回 None，那种格子一律算"不够样本"（enough() 入参是 SpeedCell，
    #    None 要在这里先滤掉）；有格子的版本照样参与。
    enough: dict[str, bool] = {}
    for v in report.versions:
        cell = report.cell("Training", v)
        enough[v] = report.enough(cell) if cell is not None else False
    _eq(
        enough,
        {
            "v1.0.0-beta.1": True,
            "v1.0.0-beta.2": False,
            "v1.0.0-beta.9": True,
            "v1.0.0-beta.10": True,
        },
        "n=1 的版本不达门槛",
    )

    # 4) 截断：只留最近 N 个版本
    short = build_speed_report(rows, last=2, min_count=2)
    _eq(
        short.versions,
        ("v1.0.0-beta.9", "v1.0.0-beta.10"),
        f"--last 2 只留最近两个版本（默认 {DEFAULT_LAST}）",
    )

    # 5) 重构对比：beta.1(100ms) → beta.10(200ms) 是变慢
    slow = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.10"
    )
    _eq(len(slow.compares), 1, "只有两版都有数据的任务才进对比")
    _eq(slow.compares[0].verdict, "regressed", "p50 翻倍判为变慢")
    _eq(round(slow.compares[0].ratio, 3), 2.0, "比值 = 新版/基线")

    # 6) 噪声内：需要一对比值落在 [1/1.15, 1.15] ≈ [0.87, 1.15] 的组合。
    #    beta.1=100 → beta.9=120 是 1.20（超出，判变慢）；beta.10=200 → beta.9=120 是 0.60（超出，判改善）。
    #    所以专门加一对接近的：beta.3=100 → beta.4=105，比值 1.05。
    noise_rows = rows + [
        row("Training", rel("beta.3"), 100.0, 6),
        row("Training", rel("beta.4"), 105.0, 6),
    ]
    near = build_speed_report(
        noise_rows, last=0, min_count=2, compare="v1.0.0-beta.3..v1.0.0-beta.4"
    )
    _eq(near.compares[0].verdict, "noise", "比值 1.05 落在噪声带内，不硬给方向")
    over = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.9"
    )
    _eq(over.compares[0].verdict, "regressed", "比值 1.20 超出阈值 1.15 判变慢")

    # 6b) 改善：beta.10(200ms) → beta.1(100ms) 是 0.50x，明显改善
    better = build_speed_report(
        rows, last=0, min_count=2, compare="v1.0.0-beta.10..v1.0.0-beta.1"
    )
    _eq(better.compares[0].verdict, "improved", "比值 0.50 低于 1/1.15 判改善")

    # 7) 样本不足的两版 → 证据不足（不是"改善"）
    thin_rows = [
        row("Gift", rel("beta.1"), 128400.0, 2),  # n=2 < compare 门槛 3
        row("Gift", rel("beta.2"), 801.1, 4),
    ]
    thin = build_speed_report(
        thin_rows, last=0, min_count=2, compare="v1.0.0-beta.1..v1.0.0-beta.2"
    )
    _eq(thin.compares[0].verdict, "insufficient", "一侧样本不足时给证据不足而不是方向")
    _eq(
        (thin.compares[0].base_count, thin.compares[0].new_count),
        (2, 4),
        "判定里带两侧样本数",
    )
    _eq(DEFAULT_COMPARE_MIN_COUNT, 3, "对比门槛比展示门槛更严")

    # 8) --compare 的版本必须在窗口内
    _raises(
        lambda: build_speed_report(
            rows, last=1, compare="v1.0.0-beta.1..v1.0.0-beta.10"
        ),
        ValueError,
        "--compare 的版本不在窗口内",
    )
    _raises(
        lambda: build_speed_report(rows, last=0, compare="beta1"),
        ValueError,
        "--compare 缺 A..B 形式",
    )

    # 9) 输出：console 表要有样本密度提示，markdown/json 也要能出
    buffer = _io.StringIO()
    write_console(report, buffer)
    text = buffer.getvalue()
    _contains(text, "样本密度", "表格底部报样本密度")
    _contains(text, "n=1", "样本不足的格子标 n=N")

    buffer = _io.StringIO()
    write_markdown(report, buffer)
    _contains(buffer.getvalue(), "| 任务 |", "markdown 表头")

    buffer = _io.StringIO()
    write_json(report, buffer)
    _contains(buffer.getvalue(), '"min_count"', "json 报告带门槛参数")

    # 10) 毫秒/秒的人读格式
    _eq(format_ms(801.1), "801.1ms", "亚秒级用 ms")
    _eq(format_ms(246134.0), "246.1s", "超秒级换算成 s")

    return [
        "版本排序 / 伪任务排除 / 样本门槛 / 截断 / 对比判定 / 证据不足 / 三种输出 / 毫秒格式 各自有断言"
    ]


def check_explore_csv() -> list[str]:
    """Explore CSV → 日志正文 + 清单：块边界接续、五分类、任务可画性、截断计数、入口报错。

    合成一份最小 CSV（用 `csv.writer` 写，保证引号与内嵌换行是真的 CSV），覆盖实测形状：
    每格首行是半截行、有一处能接回来的跨格断行、一条被裁断的 reco、一条 MFA 面板行。
    全程不写文件（`read_cells` 接受正文），所以受限沙箱下也能跑。
    """
    import csv as _csv
    import io as _io
    import sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.sentry.lib import explore_csv

    def maafw(ts: str, msg: str, px: str = "Px1") -> str:
        return (
            f"[2026-10-14 {ts}][INF][{px}][Tx1][PipelineTask.cpp][L1][MaaNS::T] {msg}"
        )

    node_a = maafw(
        "00:01:00.000",
        'PipelineTask node done [result={"name":"TaskA","node_id":1,'
        '"reco_id":11,"completed":true}] [task_id_=111]',
    )
    node_b = maafw(
        "00:05:00.000",
        'PipelineTask node done [result={"name":"TaskB","node_id":2,'
        '"reco_id":21,"completed":true}] [task_id_=222]',
    )
    head = "[2026-10-14 00:04:00.000][INF][Px1][Tx1"  # 第一格末行：断头（下一格接着）
    tail = "][Utils/EventDispatcher.hpp][L65][MaaNS::EventDispatcher::notify] !!!OnEventNotify!!!"
    dangling = "[2026-10-14 00:06:00.000][INF][Px1][Tx1"  # 第二格末行：没人接的断头
    cell_one = "\n".join(
        [
            'ls={"focus":null,"list":[]}',  # 碎片（没有时间戳）
            node_a,
            maafw(
                "00:03:00.000", 'reco [result={"name":"A","algorithm":"OCR","box":null'
            ),  # 被裁断
            head,
        ]
    )
    cell_two = "\n".join(
        [
            tail,  # 接上第一格的断头
            node_b,
            "[2026-10-14 00:00:50.306][WRN] [cfg=Default] 实时画面为空：截图方式=EmulatorExtras",
            dangling,
        ]
    )
    buffer = _io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["timestamp", "message"])
    writer.writerow(["2026-10-14T00:06:07+00:00", cell_one])
    writer.writerow(["2026-10-14T00:06:07+00:00", cell_two])
    text = buffer.getvalue()

    result = explore_csv.extract(text)
    recovered, parse = result.inventory["recovered"], result.inventory["parse"]
    _eq(result.inventory["source"]["csv_rows"], 2, "数据行数")
    _eq(recovered["boundaries_joined"], 1, "接回的跨格断行数")
    _eq(
        recovered["lines_complete"],
        4,
        "结构完整的行（两条 node done + 接回的那条 + 被裁断的 reco）",
    )
    _eq(recovered["lines_panel"], 1, "面板行数")
    _eq(recovered["lines_head_cut"], 1, "断头行数（没人接的那条）")
    _eq(recovered["lines_fragment"], 1, "碎片行数")
    _contains(result.log_text, tail, "接回来的那行进了正文")
    _eq(parse["steps"], 2, "两条 node done")
    _eq(parse["truncated_skipped"], 1, "被裁断的记录数")
    _eq(parse["truncated_by_head"], {"reco [result=": 1}, "截断记录按类型拆")
    _eq([task["task_id"] for task in result.tasks], [111, 222], "清单里的任务")
    _eq([task["drawable"] for task in result.tasks], [True, True], "两个任务都可画")
    _eq(result.inventory["panel_lines"]["count"], 1, "面板行计数")
    _contains(
        result.inventory["panel_lines"]["samples"][0]["text"],
        "实时画面为空",
        "面板行样例",
    )
    _eq(result.inventory["images"], False, "导出不含图片")

    # 卡在派发层的任务：只有 1 步 node done，但窗口里有 6 次连续未命中 —— 清单要能点名
    # （`reco` 行没有 task_id，窗口靠该 task 的 Node.NextList 事件撑开，所以这里必须带一条）
    def nextlist(ts: str, name: str, names: list[str], tid: int) -> str:
        entries = ",".join(
            '{"anchor":false,"jump_back":false,"name":"%s"}' % n for n in names
        )
        return maafw(
            ts,
            "!!!OnEventNotify!!! [handle=true] [msg=Node.NextList.Failed] "
            f'[details={{"focus":null,"list":[{entries}],"name":"{name}","task_id":{tid}}}] ',
        )

    stall_lines = [
        nextlist("00:07:00.000", "TaskC", ["CX", "CY"], 333),
        *[
            maafw(
                f"00:07:0{i}.000",
                'reco [result={"name":"CX","algorithm":"OCR","box":null,'
                f'"reco_id":3{i}}}]',
            )
            for i in range(1, 7)
        ],
        maafw(
            "00:07:07.000",
            'PipelineTask node done [result={"name":"TaskC","node_id":9,'
            '"reco_id":39,"completed":true}] [task_id_=333]',
        ),
    ]
    stall_buffer = _io.StringIO()
    stall_writer = _csv.writer(stall_buffer, lineterminator="\r\n")
    stall_writer.writerow(["timestamp", "message"])
    stall_writer.writerow(["2026-10-14T00:08:00+00:00", "\n".join(stall_lines)])
    stall = explore_csv.extract(stall_buffer.getvalue())
    stall_task = next(task for task in stall.tasks if task["task_id"] == 333)
    _eq(stall_task["node_done"], 1, "空转任务只有 1 步")
    _eq(stall_task["misses"], 6, "窗口内的未命中次数")
    _eq(stall_task["stalled_misses"], 6, "最长的一段连续未命中")
    _eq(stall_task["stalled_at"], "2026-10-14T00:07:01.000", "空转起点（第一条未命中）")
    _eq(
        stall_task["window"][0],
        "2026-10-14T00:07:00.000",
        "窗口起点取 NextList 记录（早于第一步）",
    )
    stall_notes = " ".join(stall.inventory["notes"])
    _contains(stall_notes, "task 333", "清单点名空转任务")
    _contains(stall_notes, "连续 6 次识别未命中", "清单说明空转形态")

    # 列不对 / 路径不对要报清楚，不能混成"解析出 0 行"
    try:
        explore_csv.extract("trace,span.op\n1,db")
    except ValueError as error:
        _contains(str(error), "没有 message 列", "缺 message 列的报错")
    else:
        raise CheckFailure("缺 message 列时没有报错")
    try:
        explore_csv.extract("no/such/explore.csv")
    except FileNotFoundError as error:
        _contains(str(error), "没有这个文件", "路径写错的报错")
    else:
        raise CheckFailure("路径写错时没有报错")

    return [
        f"  2 格 CSV → {recovered['lines']} 行（完整 {recovered['lines_complete']} / 面板 "
        f"{recovered['lines_panel']}），接回 {recovered['boundaries_joined']} 处断行，"
        f"{len(result.tasks)} 个任务可画"
    ]
