"""像素绘制：节点框、连线、角标、注记行、失败现场图贴图。

输入是 `layout` 给的坐标与 `media` 给的图片，输出一张 PIL Image；不读日志、
不做判定。版面细节（间距、槽位、折叠角标）都在这一个函数里，改版面只动这里。"""

from __future__ import annotations


import logging
import os
import zipfile
import zlib
from dataclasses import dataclass

from .layout import ColumnSpan, anchor_of, build_rounds, choose_cut_set, classify, compose_columns, overlay_repeats, parent_groups, roles, round_steps

from .media import MediaResolver, OnErrorIndex, VisionIndex, backfill_failure_thumbs, member_clock

from .paint import NOTE_COLORS, PALETTE, PARENT_COLORS, _note_lines, load_font

from .stale import stale_label

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .media import MediaEntry
    from .model import ParsedLog, Step
    from .render import FlowOptions

log = logging.getLogger("tools.flow_from_log")


@dataclass
class DrawResult:
    """一次画图的产出（`draw_flow()` 的返回值）。

    带上折叠后的轮次与列范围，是为了让 mermaid 也用同一套合并结果——在 `render_flow()` 里
    重新算一遍轮次的话，`--overlay` 对 .mmd 就静默失效了。
    """

    width: int
    height: int
    steps: int  # 步数（仅事件模式下按识别记录条数算）
    boxes: int
    shown: int
    rounds: int  # 原始轮数（折叠前）
    columns: list[list[Step]]  # 折叠后的列（含未命中/异常框）
    spans: list[ColumnSpan]
    rounds_shown: list[list[Step]]  # 折叠后的"命中步骤"轮（mermaid 用）

    @property
    def overlay_groups(self) -> int:
        return sum(1 for span in self.spans if span.merged)

    @property
    def column_count(self) -> int:
        return len(self.columns)


def draw_flow(parsed: ParsedLog, options: FlowOptions, out_path: str) -> DrawResult:
    from PIL import Image, ImageDraw

    f_name = load_font(max(11, int(options.font_size * options.scale)), bold=True)
    f_det = load_font(max(9, int((options.font_size - 3) * options.scale)))
    # 父节点那一行比明细再小一档：实测最长的组合（`父 BattleWinSuccess：√TrainingBattleWin`）
    # 在 13px 下要 255px，而带图槽的框只有 216px 可写；11px 下 219px，刚好放得下。
    f_par = load_font(max(9, int((options.font_size - 5) * options.scale)))
    f_title = load_font(int((options.font_size + 8) * options.scale), bold=True)
    f_leg = load_font(max(10, int((options.font_size - 2) * options.scale)))

    raw = round_steps(parsed, options)
    if not raw:
        raise SystemExit("窗口内没有解析到任何节点执行记录，检查 --from/--to/--task-id")

    # 父节点 = 派发过子节点的节点（日志里出现 [cur_node_=X] 的那些），它们拥有 next 表
    # 重要节点 = 全局重复出现的节点
    parents, important = roles(raw, options.repeat_threshold)
    if options.cut_on:
        cut = {n.strip() for n in options.cut_on.split(",") if n.strip()}
    else:
        cut = choose_cut_set(raw, parents, options.min_round_len)
    hard_cut = {n for n in cut if n not in parents}
    rounds = build_rounds(raw, options.min_round_len, cut, hard_cut)
    if len(rounds) > options.max_rounds:
        rounds = rounds[: options.max_rounds]
    anchor = options.anchor or anchor_of(raw, rounds)

    # 每轮末尾追加该轮内"连续未命中"的聚合框；窗口右端延伸到下一轮开始（末轮延伸到最后一个动作/事件）
    columns = compose_columns(
        parsed, rounds, options.miss_min, options.collapse, options.miss_max
    )
    steps = [s for r in rounds for s in r]
    # 仅事件模式下 raw 已经折过：图上报的步数要按识别记录条数算，与"折叠后 N 个框"对齐
    total_steps = len(parsed.steps) if parsed.derived_from else len(steps)

    # ---- 先把每个框要贴的图解析出来（决定框高：有无图）----
    box_w = int(options.box_width * options.scale)
    pad = int(10 * options.scale)
    slot_w = int(box_w * 0.46)  # 右侧图槽
    slot_h = int(options.thumb_height * options.scale) if options.thumbs else 0
    # 候选那一行要占高度：本图一条候选都没有（日志没有 NextList 事件）时整行不加——那种情况下每个框
    # 都写"父 未记录"只是刷屏，同一件事由图上第二行的注记说一次（`build_notes()`）。
    any_candidates = (
        any(st.candidates for col in columns for st in col)
        if options.parents
        else False
    )

    def parent_details(st: Step) -> str:
        if not options.parents or not any_candidates:
            return ""
        if st.candidates:
            return st.parent_detail()
        return "" if st.parent else "父 未记录"

    parent_h = (
        int(options.font_size * options.scale) + int(6 * options.scale)
        if any_candidates
        else 0
    )
    box_h = (
        max(
            int(options.box_height * options.scale) + parent_h,
            slot_h + int(18 * options.scale),
        )
        if slot_h
        else int(options.box_height * options.scale) + parent_h
    )
    event_h = int(options.box_height * options.scale) + parent_h
    gap_y = int(options.gap_y * options.scale)
    gap_err = int(options.gap_error * options.scale)
    gap_x = int(options.gap_x * options.scale)
    margin = int(34 * options.scale)
    header_h = int(34 * options.scale)
    cols_per_band = (
        max(1, options.cols_per_band) if options.cols_per_band else len(columns)
    )

    vision = VisionIndex(options.vision) if options.vision else None
    on_err = OnErrorIndex(options.on_error) if options.on_error else None
    # (列, 行) -> (证据条目, 是否该节点自己的识别图, 角标文字)
    thumb_of: dict[tuple[int, int], tuple[MediaEntry, bool, str]] = {}
    # 贴图失败的记录与打印上限：读图失败要能看见，但不能刷满屏幕
    thumb_errors: list[str] = []
    WARN_THUMB_LIMIT = 5
    if slot_h:
        for ci, col in enumerate(columns):
            for ri, st in enumerate(col):
                path, exact, note = None, False, ""
                if st.kind in ("hit", "miss") and vision:
                    path, exact = vision.find(st)
                    note = "" if exact else "邻近帧"
                elif st.kind == "event":
                    if on_err:
                        path, exact, note = on_err.find(st), False, "失败现场"
                    if not path and vision:
                        path, exact = vision.find(st)
                        note = "邻近帧"
                if path:
                    thumb_of[(ci, ri)] = (path, exact, note)
        # 只有 on_error 图时（没给 --vision 或该节点本来就没存图）：把现场图贴回"被记录的那个节点"
        if on_err and on_err.items:
            # 只看选中任务自己的判定窗口：同一份滚动日志里，相邻 task 会首尾相接（实测差 51 ms）
            backfill_failure_thumbs(columns, on_err.items, thumb_of, window=parsed.span)

    # ---- 重复且无图的轮次合并成一列（必须在 thumb_of 算完之后：判定要用真实贴图结果）----
    round_count = len(rounds)
    round_steps_all = rounds  # 折叠前的原始轮次：列头要用末轮的时间
    spans: list[ColumnSpan] = [ColumnSpan(i, i) for i in range(len(columns))]
    if options.overlay and options.overlay_min >= 2:
        merged = overlay_repeats(
            columns,
            rounds,
            has_thumb=lambda ci: any(
                (ci, ri) in thumb_of for ri in range(len(columns[ci]))
            ),
            min_span=options.overlay_min,
        )
        if len(merged) != len(columns):
            # thumb_of 的键是旧列索引，折叠后必须重排：合并列按定义整轮无图，所以只留
            # 单列（未合并）那些轮自己的贴图，其余键全部丢弃——否则 --no-thumbs / on_error
            # 回贴那条路径会带着旧索引把图贴到别的列上。
            old_of_new = [span.first for _c, span in merged]
            thumb_of = {
                (new_ci, ri): value
                for new_ci, old_ci in enumerate(old_of_new)
                for (ci, ri), value in thumb_of.items()
                if ci == old_ci
            }
            spans = [span for _c, span in merged]
            columns = [col for col, _s in merged]
            rounds = [rounds[span.first] for span in spans]
    overlay_groups = sum(1 for span in spans if span.merged)

    bands: list[list[int]] = [
        list(range(i, min(i + cols_per_band, len(columns))))
        for i in range(0, len(columns), cols_per_band)
    ]
    # 父节点分组：每列的 [(父, 首, 末)]，套框与角标都用它
    groups_of: dict[int, list[tuple[str, int, int]]] = {
        ci: parent_groups(col) for ci, col in enumerate(columns)
    }
    group_badge_h = int(20 * options.scale)  # 角标高度（实心矩形 + 白字）
    group_pad = int(6 * options.scale)  # 分组框四边的内边距
    # 组内相邻两步贴紧一点：默认 36px 的间隔叠上分组框的上下内边距会显得很空
    group_gap = int(12 * options.scale)
    # 相邻两个分组框之间留的空：上一组的框底还带 group_pad 的内边距，只留 group_gap 会让两个框贴到一起
    group_gap_between = int(26 * options.scale)
    # 每个分组在列里要额外占的高度。角标画在"第一格上边界的上方"（badge_h + pad），而上一格与本格之间
    # 只有 group_gap 的空档——所以必须把整个角标走廊 + 上下内边距都预留出来，否则角标会压住上一格的文字。
    group_extra_one = group_badge_h + group_pad * 2 + group_gap

    # 画布宽度只由列数与版式决定，先算出来：诊断注记要在定标题高度之前折行
    width = margin * 2 + max(len(b) for b in bands) * (box_w + gap_x)
    width = max(width, int(900 * options.scale))
    note_lines = _note_lines(options.notes, f_leg, width - margin * 2)
    title_h = int(64 * options.scale) + len(note_lines) * int(20 * options.scale)

    def box_height_for(ci: int, ri: int) -> int:
        return box_h if (ci, ri) in thumb_of else event_h

    # 每列的高度：框高 + 间隔（未命中/异常相邻处用更大的间隔，别挤在一起）+ 分组框的净增高。
    # 不预先撑开这些空隙的话，后画的浅色分组框会糊住上一个框的文字；相邻两组的框之间也要留够
    # （上一组的框底带 group_pad 内边距，只留 group_gap 会让两个框贴到一起）。
    def column_geo(
        ci: int, x: int, y0: int
    ) -> tuple[list[tuple[int, int, int, int]], int]:
        span: dict[int, tuple[int, int]] = {}
        for _name, first, last in (groups_of[ci] if options.parents else []):
            for ri in range(first, last + 1):
                span[ri] = (first, last)
        y = y0 + header_h
        boxes: list[tuple[int, int, int, int]] = []
        # 合并列的列头（第 a–b 轮 · ×N）是这一列唯一的身份信息，不能被角标压住：
        # 首格如果开着一组，就补一段角标走廊。**只对合并列**这么做——非合并列维持原样，
        # 这样 --no-overlay 的产物与改动前逐字节一致（那条是"能退回旧图"的判据）。
        if spans[ci].merged and 0 in span and span[0][0] == 0:
            y += group_extra_one
        for ri, st in enumerate(columns[ci]):
            if ri:
                prev_st = columns[ci][ri - 1]
                noisy = prev_st.kind in ("miss", "event") or st.kind in (
                    "miss",
                    "event",
                )
                if ri in span and span[ri][0] == ri:
                    # 新的一组的首格：让出上一组的框底内边距 + 角标走廊
                    y += group_extra_one + (
                        group_gap_between - group_gap if span.get(ri - 1) else 0
                    )
                elif noisy:
                    y += gap_err
                elif span.get(ri) and span.get(ri) == span.get(ri - 1):
                    y += group_gap  # 同一组内的相邻两步
                else:
                    y += gap_y
            boxes.append((x, y, box_w, box_height_for(ci, ri)))
            y += boxes[-1][3]
        return boxes, y

    # 先按"同一行"的纵坐标依次算一遍，再定画布高度
    col_x: dict[int, int] = {}
    probe: dict[int, int] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            col_x[ci] = margin + (ci - band[0]) * (box_w + gap_x)
            _boxes, probe[ci] = column_geo(ci, col_x[ci], y_band)
        y_band = max(probe[ci] for ci in band) + int(96 * options.scale)

    height = y_band + int(64 * options.scale)

    img = Image.new("RGB", (width, height), (250, 251, 253))
    d = ImageDraw.Draw(img)

    # ---- 标题 ----
    t0 = rounds[0][0].ts if rounds and rounds[0] else None
    # 末轮时间取**折叠前**的最后一轮：合并列的步骤表来自首轮，用它会把结束时间读早
    t1 = round_steps_all[-1][-1].ts if round_steps_all and round_steps_all[-1] else None
    title = options.title or f"执行流程  ·  {os.path.basename(options.log)}"
    sub = f"{t0.strftime('%Y-%m-%d %H:%M:%S') if t0 else '?'} → {t1.strftime('%H:%M:%S') if t1 else '?'}"
    sub += f"   |   {total_steps} 步 / {round_count} 轮"
    if overlay_groups:
        sub += f"（{overlay_groups} 段重复已合并 → {len(rounds)} 列）"
    if anchor:
        sub += f"   |   循环锚点 {anchor}"
    if options.task_id:
        sub += f"   |   task_id={options.task_id}"
    if options.truncated_start:
        # 日志只剩尾巴时必须印在图上，否则读图的人会以为任务从这里开始。
        # 只用中文字符：⚠ 这类符号在 msyh 里没有字形，会画成方框。
        sub += "   |   注意：日志仅含尾部"
    d.text((margin, int(14 * options.scale)), title, font=f_title, fill=(20, 26, 40))
    d.text((margin, int(40 * options.scale)), sub, font=f_leg, fill=(96, 104, 122))
    # 诊断注记单独一行：由识别记录还原、时间戳已重排、多少条被裁断——这些是读图前提，
    # 挤在副标题里会被一起读过去（也可能超出画布右边被裁掉）
    for i, (note, kind) in enumerate(note_lines):
        d.text(
            (margin, int(58 * options.scale) + i * int(20 * options.scale)),
            note,
            font=f_leg,
            fill=NOTE_COLORS.get(kind, NOTE_COLORS["info"]),
        )

    # 三种字体共用的截断（画父节点角标时就要用，所以定义在这里，不放在"框"那一段）
    def fit_text(s: str, font, maxw: int) -> str:
        if d.textlength(s, font=font) <= maxw:
            return s
        out = s
        while out and d.textlength(out + "…", font=font) > maxw:
            out = out[:-1]
        return out + "…"

    # ---- 每列的框位置 + 列底色/表头 ----
    pos: dict[int, list[tuple[int, int, int, int]]] = {}
    y_band = title_h + margin
    for band in bands:
        for ci in band:
            boxes, bottom = column_geo(ci, col_x[ci], y_band)
            pos[ci] = boxes
            x = col_x[ci]
            if ci % 2 == 1:
                d.rounded_rectangle(
                    [
                        x - int(14 * options.scale),
                        y_band,
                        x + box_w + int(14 * options.scale),
                        bottom + int(12 * options.scale),
                    ],
                    radius=int(12 * options.scale),
                    fill=(244, 246, 250),
                )
            r = rounds[ci]
            span = spans[ci]
            if span.merged:
                # 合并列：轮号写成区间 + 次数，并把**末轮**的起止时间补回来（首轮的步骤表是这一列
                # 唯一的一批时间戳，不给末轮时间就没法知道这段重复持续了多久）。
                last_round = round_steps_all[span.last]
                label = (
                    f"第 {span.first + 1}–{span.last + 1} 轮 · ×{span.count} 重复 · "
                    f"{r[0].ts.strftime('%H:%M:%S')}–{last_round[-1].ts.strftime('%H:%M:%S')}"
                )
            else:
                # 非合并列的轮号取 span.first：折叠会把后面的列往后挪，用 ci + 1 会写成"第 3 轮"
                label = (
                    f"第 {span.first + 1} 轮 · {r[0].ts.strftime('%H:%M:%S')}–"
                    f"{r[-1].ts.strftime('%H:%M:%S')} · {len(r)} 步"
                )
            d.rounded_rectangle(
                [x, y_band, x + box_w, y_band + int(24 * options.scale)],
                radius=int(6 * options.scale),
                fill=(252, 226, 200) if span.merged else (226, 230, 238),
                outline=(206, 140, 70) if span.merged else None,
                width=2 if span.merged else 1,
            )
            d.text(
                (x + int(8 * options.scale), y_band + int(4 * options.scale)),
                fit_text(label, f_leg, box_w - int(16 * options.scale)),
                font=f_leg,
                fill=(140, 88, 24) if span.merged else (58, 66, 84),
            )
            if span.merged:
                # 右侧一根橙条贯穿整列：远看就能认出"这一列是压出来的"，不用去读列头小字
                d.rectangle(
                    [
                        x + box_w - int(4 * options.scale),
                        y_band,
                        x + box_w,
                        bottom + int(12 * options.scale),
                    ],
                    fill=(214, 140, 60),
                )
        y_band = max(pos[ci][-1][1] + pos[ci][-1][3] for ci in band) + int(
            96 * options.scale
        )

    # ---- 父节点分组框（在框与连线之前画，只当背景）----
    # 浅蓝底 + 同色系边框套住"同一个派发者连着派发的那几步"，左上角压一个深色实心角标写父节点名。
    # 角标压在第一格左上、与它重叠：重叠必须能读出来，所以不透明 + 白字，不给连线留穿透的余地
    # （连线在下一步才画，会绕不过角标——那一格的上边框端口因此改从角标右边出）。
    group_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    badge_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = (
        {}
    )  # (ci, 末行) -> 角标矩形
    if options.parents:
        inset = int(7 * options.scale)
        # 同名父节点用同一套颜色（一眼看出"又是它派发的"），不同父节点按**首次出现顺序**轮换配色
        color_of: dict[
            str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]
        ] = {}
        for cols in groups_of.values():
            for name, _first, _last in cols:
                if name not in color_of:
                    color_of[name] = PARENT_COLORS[len(color_of) % len(PARENT_COLORS)]
        for ci, cols in groups_of.items():
            for name, first, last in cols:
                fill, edge, badge_color = color_of[name]
                fx, fy, _fw, _fh = pos[ci][first]
                lx, ly, _lw, lh = pos[ci][last]
                left, top = fx - inset, fy - group_badge_h - inset
                right = fx + box_w + inset
                bottom = ly + lh + inset + group_badge_h
                d.rounded_rectangle(
                    [left, top, right, bottom],
                    radius=int(9 * options.scale),
                    fill=fill,
                    outline=edge,
                    width=2,
                )
                # 角标按文字宽度收紧（不铺满整格宽，否则读起来像标题栏而不是标签）
                text_w_badge = int(d.textlength(name, font=f_par))
                bw = min(box_w - inset, text_w_badge + int(16 * options.scale))
                d.rounded_rectangle(
                    [left, top, left + bw, top + group_badge_h],
                    radius=int(6 * options.scale),
                    fill=badge_color,
                )
                d.text(
                    (left + int(7 * options.scale), top + int(3 * options.scale)),
                    fit_text(name, f_par, bw - int(12 * options.scale)),
                    font=f_par,
                    fill=(255, 255, 255),
                )
                group_boxes[(ci, first)] = (left, top, right, bottom)
                badge_boxes[(ci, last)] = (left, top, left + bw, top + group_badge_h)

    # ---- 端口与连线（先画线，再画框，线不会压住文字）----
    # 端口规则（--ports pos，默认）：
    #   同列相邻（竖直方向） —— 上入下出：出口 = 下中，入口 = 上中
    #   跨轮相邻（水平方向） —— 左入右出：出口 = 右中，入口 = 左中
    # --ports role 是旧规则：重要节点左下出/右上进，父节点左下出/右下进。
    pad_port = int(12 * options.scale)
    lane_gap = int(34 * options.scale)

    def role_of(name: str) -> str:
        if name in parents and name in important:
            return "parent+repeat"
        if name in parents:
            return "parent"
        if name in important:
            return "repeat"
        return "plain"

    def out_port(ci: int, ri: int, horizontal: bool = False) -> tuple[int, int]:
        x, y, w, h = pos[ci][ri]
        if options.ports == "pos":
            return (x + w, y + h // 2) if horizontal else (x + w // 2, y + h)
        if role_of(columns[ci][ri].name) == "plain":
            return (x + w // 2, y + h)
        return (x + pad_port, y + h)

    def in_port(
        ci: int, ri: int, cross_round: bool = False
    ) -> tuple[tuple[int, int], str]:
        """返回 (接点坐标, 'top'|'left'|'right')，'left'/'right' 表示从左右边缘水平进入。

        这一格如果顶着父节点分组框的角标（角标压在第一格左上），入口就挪到角标**右边**：
        角标是不透明的，从正中间上去会被它盖住，箭头看着像断的。
        """
        x, y, w, h = pos[ci][ri]
        badge = badge_boxes.get((ci, ri))
        entry_x = x + w // 2
        if badge is not None and badge[2] > entry_x:
            # 角标压住了中点：改从"角标右缘 ~ 框右缘"的中点上去
            entry_x = x + (w + badge[2]) // 2
        if options.ports == "pos":
            if cross_round:
                return (x, y + h // 2), "left"
            return (entry_x, y), "top"
        r = role_of(columns[ci][ri].name)
        if cross_round or r == "repeat":
            return (x + w - pad_port, y), "top"
        if r in ("parent", "parent+repeat"):
            return (x + w, y + h - pad_port), "right"
        return (x + w // 2, y), "top"

    def arrow(points: list[tuple[int, int]], color, width: int = 2):
        d.line(points, fill=color, width=width, joint="curve")
        (x0, y0), (x1, y1) = points[-2], points[-1]
        ah = int(9 * options.scale)
        if abs(x1 - x0) >= abs(y1 - y0):  # 水平进入
            s = 1 if x1 > x0 else -1
            d.polygon(
                [(x1, y1), (x1 - s * ah, y1 - ah // 2), (x1 - s * ah, y1 + ah // 2)],
                fill=color,
            )
        else:  # 垂直进入
            s = 1 if y1 > y0 else -1
            d.polygon(
                [(x1, y1), (x1 - ah // 2, y1 - s * ah), (x1 + ah // 2, y1 - s * ah)],
                fill=color,
            )

    def route(
        a: tuple[int, int], b: tuple[int, int], kind: str, bx: int, bw: int
    ) -> None:
        if kind == "top":  # 竖直方向：上入下出
            y_mid = a[1] + max(int(14 * options.scale), (b[1] - a[1]) // 2)
            arrow([a, (a[0], y_mid), (b[0], y_mid), b], (150, 158, 174))
        elif kind == "left":  # 水平方向：左入右出（竖线走两列之间的空档）
            x_lane = (a[0] + bx) // 2
            arrow([a, (x_lane, a[1]), (x_lane, b[1]), b], (120, 130, 150), width=3)
        else:  # 旧的"右下进"：从右边缘进
            x_lane = bx + bw + lane_gap
            arrow(
                [
                    a,
                    (a[0], a[1] + int(14 * options.scale)),
                    (x_lane, a[1] + int(14 * options.scale)),
                    (x_lane, b[1]),
                    b,
                ],
                (150, 158, 174),
            )

    band_of = {ci: bi for bi, band in enumerate(bands) for ci in band}
    over = int(26 * options.scale)
    for ci, boxes in enumerate(columns):
        for ri in range(len(boxes) - 1):  # 列内：竖直相邻
            a = out_port(ci, ri)
            b, kind = in_port(ci, ri + 1)
            bx, _by, bw, _bh = pos[ci][ri + 1]
            route(a, b, kind, bx, bw)
        nxt = ci + 1
        if nxt >= len(columns) or not boxes:
            continue
        a = out_port(ci, len(boxes) - 1, horizontal=True)  # 跨轮：水平相邻
        bx, _by, bw, _bh = pos[nxt][0]
        b, kind = in_port(nxt, 0, True)
        if band_of[ci] == band_of[nxt] and kind == "left":
            route(a, b, kind, bx, bw)
        else:  # 换行时改从上边进，避免竖线穿过下一排的框
            b2 = (bx + bw // 2, pos[nxt][0][1])
            corridor = pos[ci][-1][1] + pos[ci][-1][3] + over
            x_lane = (a[0] + b2[0]) // 2
            arrow(
                [a, (x_lane, a[1]), (x_lane, corridor), (b2[0], corridor), b2],
                (120, 130, 150),
                width=3,
            )

    # ---- 框 ----
    from PIL import ImageOps

    def draw_thumb(
        st: Step,
        entry: MediaEntry,
        exact: bool,
        note: str,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> None:
        im = MediaResolver.open_image(entry)
        frame = im.crop((0, 0, min(1280, im.width), min(720, im.height)))
        if exact and st.box and st.box[2] > 0 and st.box[3] > 0:
            bx, by, bw, bh = st.box
            p = int(26 * options.scale)
            crop = frame.crop(
                (
                    max(0, bx - p),
                    max(0, by - p),
                    min(frame.width, bx + bw + p),
                    min(frame.height, by + bh + p),
                )
            )
        else:
            crop = frame
        sx = x + w - pad - slot_w
        sy = y + (h - slot_h) // 2
        # 失败现场图给红框：它和"识别帧/邻近帧"必须一眼分开——同一次失败里十几张识别帧
        # 是同一个卡住画面的截图，只有失败那一刻的 on_error 图才是"最后怎么死的"。
        is_failure = note == "失败现场"
        d.rectangle(
            [sx - 1, sy - 1, sx + slot_w + 1, sy + slot_h + 1],
            fill=(255, 255, 255),
            outline=(190, 40, 40) if is_failure else (198, 204, 216),
            width=max(1, int(2 * options.scale)) if is_failure else 1,
        )
        thumb = ImageOps.contain(crop, (slot_w - 2, slot_h - 2))
        img.paste(
            thumb, (sx + (slot_w - thumb.width) // 2, sy + (slot_h - thumb.height) // 2)
        )
        if note:
            if is_failure:
                # 实心红底白字角标压在右下角：不靠"小字颜色"区分，缩略图一样也能一眼认出。
                # 角标里带**这张图自己的时刻**——同名节点一天失败好几次时，只有它能对上
                # `debug/on_error/<文件名>`（框体那行会被 fit_text 截掉，装不下完整时间）。
                clock = member_clock(entry)
                badge = f"失败现场 {clock}" if clock else note
                tw = int(d.textlength(badge, font=f_det)) + int(10 * options.scale)
                th = int(16 * options.scale)
                bx, by = sx + slot_w - tw, sy + slot_h - th
                d.rectangle([bx, by, sx + slot_w, sy + slot_h], fill=(190, 40, 40))
                d.text(
                    (bx + int(5 * options.scale), by + int(1 * options.scale)),
                    badge,
                    font=f_det,
                    fill=(255, 255, 255),
                )
            else:
                d.text(
                    (sx + 2, sy + slot_h - int(13 * options.scale)),
                    note,
                    font=f_det,
                    fill=(168, 120, 40),
                )

    for ci, col in enumerate(columns):
        for ri, (x, y, w, h) in enumerate(pos[ci]):
            st = col[ri]
            cls = classify(st)
            fill, edge, ink = PALETTE[cls]
            d.rounded_rectangle(
                [x, y, x + w, y + h],
                radius=int(10 * options.scale),
                fill=fill,
                outline=edge,
                width=2,
            )
            got = thumb_of.get((ci, ri))
            text_w = (w - pad * 2) if not got else (w - slot_w - pad * 3)
            # 父节点那一行的右边界单独算：有图时**从图槽左边**过字（`draw_thumb` 只占右槽，
            # 字压在缩略图上是原来就有的行为），只按 `text_w` 量会把够宽的一行也截掉。
            row_w = (w - pad * 2) if not got else (w - slot_w - pad * 2)
            d.text(
                (x + pad, y + int(6 * options.scale)),
                st.ts.strftime("%H:%M:%S.%f")[:-3],
                font=f_det,
                fill=(126, 132, 148),
            )
            label = st.name if st.run_len <= 1 else f"{st.name}  ×{st.run_len}"
            d.text(
                (x + pad, y + int(22 * options.scale)),
                fit_text(stale_label(label), f_name, text_w),
                font=f_name,
                fill=ink,
            )
            det = st.detail
            if det:
                d.text(
                    (
                        x + pad,
                        y
                        + int(22 * options.scale)
                        + int(options.font_size * options.scale)
                        + int(6 * options.scale),
                    ),
                    fit_text(det, f_det, text_w),
                    font=f_det,
                    fill=(88, 94, 110),
                )
            detail_line = parent_details(st)
            if detail_line:
                # 父节点那部分已经进了分组框角标，这一行写的是"候选"（或"未记录"）
                d.text(
                    (x + pad, y + h - parent_h + int(1 * options.scale)),
                    fit_text(detail_line, f_par, row_w),
                    font=f_par,
                    fill=(120, 104, 148),
                )
            if got:
                try:
                    draw_thumb(st, got[0], got[1], got[2], x, y, w, h)
                except (
                    OSError,
                    ValueError,
                    zipfile.BadZipFile,
                    EOFError,
                    zlib.error,
                ) as exc:
                    # 不吞异常：图贴不上必须能看出来（遥测的证据包可能是被截断的分卷、
                    # 也可能图片已被 log_dir 的 7 天清理删掉，两种都表现为读图失败）。
                    # 但一张坏图不该掀掉整张图，这一类"字节是坏的"异常都要接住：
                    #   * `PIL.UnidentifiedImageError` 与图片截断都是 `OSError` 子类
                    #     （Pillow 12.2 实测 MRO）；
                    #   * 归档条目坏 CRC 抛 `BadZipFile`，deflate 流坏抛 `zlib.error`，
                    #     压缩流比记录的短抛 `EOFError`（`zipfile.ZipExtFile._read2`），
                    #     偏移为负抛 `ValueError`——四个都不是 OSError。
                    # 有界——一次几百张图读不了时不能刷满屏幕。
                    thumb_errors.append(f"{got[0].member}: {type(exc).__name__}: {exc}")
                    if len(thumb_errors) <= WARN_THUMB_LIMIT:
                        log.warning("[警告] 贴图失败 %s", thumb_errors[-1])

    # ---- 图例 ----
    if len(thumb_errors) > WARN_THUMB_LIMIT:
        log.warning(
            "[警告] 另有 %s 张贴图失败（已省略）",
            len(thumb_errors) - WARN_THUMB_LIMIT,
        )
    lx = margin
    ly = height - int(46 * options.scale)
    d.text((lx, ly), "图例：", font=f_leg, fill=(60, 66, 80))
    lx += int(52 * options.scale)
    for key, text in (
        ("hit", "命中并执行"),
        ("custom", "自定义识别/动作"),
        ("direct", "DirectHit"),
        ("miss", "未命中（聚合）"),
        ("error", "超时/失败"),
        ("task", "任务起止"),
    ):
        fill, edge, _ = PALETTE[key]
        d.rounded_rectangle(
            [
                lx,
                ly - int(2 * options.scale),
                lx + int(16 * options.scale),
                ly + int(14 * options.scale),
            ],
            radius=3,
            fill=fill,
            outline=edge,
        )
        d.text((lx + int(22 * options.scale), ly), text, font=f_leg, fill=(70, 76, 92))
        lx += (
            int(22 * options.scale)
            + int(d.textlength(text, font=f_leg))
            + int(22 * options.scale)
        )
    if overlay_groups:
        d.rounded_rectangle(
            [
                lx,
                ly - int(2 * options.scale),
                lx + int(16 * options.scale),
                ly + int(14 * options.scale),
            ],
            radius=3,
            fill=(252, 226, 200),
            outline=(206, 140, 70),
        )
        text = f"合并列（连续重复且无截图，共 {overlay_groups} 段）"
        d.text((lx + int(22 * options.scale), ly), text, font=f_leg, fill=(140, 88, 24))

    img.save(out_path)
    # 框数按折叠后的列算；轮数与列数分开报（调用方要能说出"合了几列"）
    shown = sum(1 for c in columns for s in c if s.kind == "hit")
    boxes = sum(len(c) for c in columns)
    return DrawResult(
        width=img.width,
        height=img.height,
        steps=total_steps,
        boxes=boxes,
        shown=shown,
        rounds=round_count,
        columns=columns,
        spans=spans,
        rounds_shown=rounds,
    )
