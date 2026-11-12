"""对外渲染入口：`render_flow` / `render_overview`、`FlowOptions` / `FlowResult`，
以及入参归一化（路径 / 正文 / 文本流 / zip 与父目录）。

把 `logparse` → `layout` → `draw` 串起来、写产物并回填 `FlowResult`；
`flow_cases` 与 `sentry` 侧都从这里调用。"""

from __future__ import annotations


import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .constants import LINE_RE, ON_ERROR_BACKFILL_SECONDS

from .draw import draw_flow

from .layout import anchor_of, build_rounds, choose_cut_set, compose_columns, roles, round_steps

from .logparse import _open_log_lines, parse_log

from .media import MediaResolver, OnErrorIndex, _is_zip, _pad_hi, member_clock, window_failure_images

from .model import Note, parse_ts

from .notes import build_notes, describe_empty_window

from .paint import NOTE_COLORS, load_font

from .paths import DEFAULT_OUTDIR, REPO_ROOT

from .stale import VERSION_SOURCE_TELEMETRY, _apply_stale, _log_version_of, check_stale_nodes, stale_label

from .trace import notes_payload, options_payload, parsed_payload, reason_of, same_data, stage_digest, stale_payload, trace_of

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stale import StaleCheck

log = logging.getLogger("tools.flow_from_log")


@dataclass
class FlowOptions:
    """画图参数。默认值与命令行默认值一致，调用方只改需要的项。"""

    # 解析/分轮
    t_from: datetime | None = None
    t_to: datetime | None = None
    task_id: int | None = None
    # 进程号（`Px30992` 形态）：task_id 在各进程内复用，多进程日志要配它才能唯一定位一个任务
    px: str | None = None
    keep_internal: bool = False
    anchor: str | None = None
    cut_on: str | None = None
    collapse: int = 3
    repeat_threshold: int = 2
    min_round_len: int = 2
    miss_min: int = 5
    miss_max: int = 8
    max_rounds: int = 40
    ports: str = "pos"
    cols_per_band: int = 0
    # 连续、同形、整轮无截图且无失败框的轮次合并成一列（`overlay_min` 是"连续几轮才合并"，
    # 0/1 等于关闭）。判定要用真实贴图结果，所以发生在 draw_flow 算完 thumb_of 之后。
    overlay: bool = True
    overlay_min: int = 2
    # 框里写"父 X：候选表"（父节点 = 本轮的派发者，来自 Node.NextList 事件）
    parents: bool = True
    # 证据来源：目录 / zip / zip 列表 / MediaResolver / None
    vision: Any = None
    on_error: Any = None
    # 日志名，只用于默认标题；由 render_flow 按入参填好，调用方不用管
    log: str = ""
    # 日志只剩尾部（第一行晚于请求的 --from）：会印在图的副标题上，别省成"完整日志"
    truncated_start: bool = False
    # 诊断注记（由 render_flow 填好并印在图上的第二行）：由识别记录还原、时间戳回跳已重排、
    # 有多少记录被导出裁断……读图的人必须先知道这些，否则会把图当作完整过程。
    # 每条带 `kind`，画的时候按类型上色。
    notes: list[Note] = field(default_factory=list)
    # 节点是否"疑似过时"：日志所属版本之后，该节点所在 pipeline 模块有没有提交。
    # 默认开——这条信息决定"这张图能不能用来判断当前代码"。关掉要显式 --no-stale-check。
    stale_check: bool = True
    # 上面的检查结果（由 render_flow 填好）：图上给节点名加后缀，注记里写清楚口径
    stale: StaleCheck | None = None
    # 资源版本基线（「疑似过时」比对用）。默认 None = 自动从日志 / 同装 MFA 日志 /
    # 遥测证据包 evidence.json 取；用 --version 或 flow_cases 从证据包带下来时一并带 version_source 标来源。
    version: str | None = None
    version_source: str = ""
    # 遥测关联：命中的问题号（进产物文件名）与额外注记 `(文本, kind)`。
    # 只由调用方填——渲染器不认识 Sentry，也不联网。
    issue: str = ""
    extra_notes: list[tuple[str, str]] = field(default_factory=list)
    # 版面
    thumbs: bool = True
    thumb_height: int = 132
    box_width: int = 470
    box_height: int = 66
    gap_y: int = 36
    gap_error: int = 64
    gap_x: int = 120
    font_size: int = 16
    scale: float = 1.0
    title: str | None = None

    def summary(self) -> dict[str, Any]:
        """可序列化的选项摘要，用于 manifest 判断"同一份输入 + 同一套参数"。"""
        # notes 由 render_flow 按输入填写，同一份 options 会在多个 case 之间复用，
        # 留在摘要里会把上一个 case 的注记带进下一个 case 的指纹。
        # stale 同理：它是这次跑出来的结论，不是参数。
        skip = {
            "vision",
            "on_error",
            "t_from",
            "t_to",
            "notes",
            "stale",
            "extra_notes",
        }
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in self.__dict__.items()
            if k not in skip
        }


@dataclass
class FlowResult:
    """一次渲染的产出。"""

    path: str
    mermaid: str | None
    width: int
    height: int
    steps: int
    boxes: int
    rounds: int  # 原始轮数（折叠前）
    columns: int = 0  # 图上实际列数（= rounds - 被合并掉的轮数）
    overlay_groups: int = 0  # 合并了多少段（0 = 没有重复可合）
    truncated_start: bool = False  # 日志只剩尾巴：第一行晚于请求的 --from
    notes: list[Note] = field(default_factory=list)  # 与图上第二行相同的诊断注记（带 kind）
    derived_from: str = ""  # 非空 = 步骤由识别记录合成（仅事件模式）
    out_of_order: int = 0  # 时间戳回跳的行数（>0 表示已按时间重排）
    stale: StaleCheck | None = None  # 疑似过时的节点（None = 没做这项检查）
    # 增量绘制的分步摘要（`flowlog/trace.py`）：`trace_stages` 各步各一个，`trace` 是它们的合成。
    # `cases` 把这组值写进 manifest，下一轮据此判断"这张图要不要重画"。
    trace: str = ""
    trace_stages: dict[str, str] = field(default_factory=dict)
    # 按轨迹跳过：文件没动、也没设置宽高与统计（调用方据此把这一轮算成"跳过"）
    skipped: bool = False
    skip_reason: str = ""
    # 没跳过时：这次为什么要重画（`代码签名变了` / `注记变了` / `没有旧记录` …）
    redraw_reason: str = ""


def _remove_quiet(path: str) -> None:
    """删临时文件，失败不报（真正的错误由调用方继续抛，别让清理掩盖它）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _skipped_result(target: str, reason: str, stages: dict[str, str]) -> FlowResult:
    """按轨迹跳过时的返回值：只带路径与摘要，没有像素产出。"""
    return FlowResult(
        path=target,
        mermaid=None,
        width=0,
        height=0,
        steps=0,
        boxes=0,
        rounds=0,
        trace_stages=stages,
        skipped=True,
        skip_reason=reason,
    )


def _log_basename(log: Any) -> str:
    """给输出文件取名用的日志名；文本输入没有文件名时退回 'maafw'。"""
    if isinstance(log, str) and "\n" not in log and "\r" not in log:
        return os.path.splitext(os.path.basename(log))[0]
    # 先取到局部变量再判类型：`name` 存在但不是 str 时（例如包装文件描述符的对象
    # 给的是 int）直接 `basename(log.name)` 会抛 TypeError。
    name = getattr(log, "name", None)
    if isinstance(name, str) and name:
        return os.path.splitext(os.path.basename(name))[0]
    return "maafw"


def _norm_source(source: Any) -> Any:
    """把 --vision/--on-error 允许的入参归一化。

    接受：目录、zip（可给 bash 展开后的多个文件，用列表传）、父目录（例如 debug/）、
    已经是 MediaResolver 的对象、内存字节。给的是父目录时自动往下找同名子目录。
    """
    if source is None or isinstance(source, MediaResolver):
        return source
    if isinstance(source, (list, tuple)):
        return MediaResolver(source)
    if hasattr(source, "read"):
        return MediaResolver(source)
    text = str(source)
    if os.path.isdir(text):
        return MediaResolver(text)
    return source


def _pick_subdir(path: Any, name: str) -> Any:
    """`--vision debug/` 这种父目录入参：往下找 `debug/vision`。支持列表与 zip。"""
    if path is None:
        return None
    items = path if isinstance(path, (list, tuple)) else [path]
    out: list[str] = []
    for item in items:
        item = str(item)
        if os.path.isdir(os.path.join(item, name)):
            out.append(os.path.join(item, name))
        elif os.path.isdir(item) or _is_zip(item):
            out.append(item)
    if not out:
        return None
    return out[0] if len(out) == 1 else out


def _first_log_ts(log: Any) -> datetime | None:
    """日志里第一条能解析出时间戳的行。

    用于判断"这份日志是不是只剩尾巴"：遥测附件只带 `maafw.log` 的 512 KB 尾部，
    也可能用户手动截断过。判据是**日志第一行**晚于请求的 `--from`，而不是
    "第一个节点执行晚于 --from"——后者在窗口开头本来就没有动作时也会成立（误报）。
    """
    try:
        for raw in _open_log_lines(log):
            m = LINE_RE.match(raw)
            if m:
                return parse_ts(m.group("ts"))
            if raw.strip():
                return None  # 第一行有内容但不是日志行，说明这不是 maafw.log
    except (OSError, TypeError):
        return None
    return None




def render_flow(
    log,
    *,
    vision: Any = None,
    on_error: Any = None,
    out: str | None = None,
    outdir: str | None = None,
    options: FlowOptions | None = None,
    mermaid: bool = False,
    list_only: bool = False,
    trace_extra: dict[str, Any] | None = None,
    recorded_stages: dict[str, str] | None = None,
    existing: str | None = None,
) -> FlowResult | str:
    """解析日志并画流程图；库调用入口，与命令行共用同一实现。

    ``log`` 可以是文件路径、日志正文，或任何可逐行的文本流——遥测从 Sentry 取回的
    日志没有本地文件，直接传文本即可。``list_only=True`` 时返回分轮文本（与 ``--list``
    输出一致），不画图。

    ``trace_extra`` / ``recorded_stages`` 是增量绘制的钩子（只有 `cases` 用）：
    ``trace_extra`` 给 ``{"code": {parse_impl, raster_impl}, "input": {…}}``——代码摘要与
    调用方才知道的输入事实；``recorded_stages`` 是 manifest 里上一轮的分步摘要。两者都给、
    且 ``out`` 指向的文件还在时，本函数自己决定"跳过还是重画"：

    * 绘制实现（像素）没变、读取实现没变、输入与参数也没变 → 不读日志直接跳过（快路）；
    * 否则读一遍、算数据步，数据步全同 → 跳过；不同 → 画，并在 ``redraw_reason`` 里写清
      第一个不同的是哪一步。

    ``existing`` 是"旧图实际在哪"：`cases` 会同时认 `<名>.png` 与 `<名>_疑似过时.png`，
    命中带后缀的那个时把它传进来，否则这一轮会因为"普通名不存在"而白重画一遍。
    """
    opts = options or FlowOptions()
    vision = _norm_source(opts.vision if vision is None else vision)
    on_error = _norm_source(opts.on_error if on_error is None else on_error)
    if on_error is None and vision is not None:
        # 顺手带上同级 on_error（与 CLI 的既有行为一致）
        for root in (vision.roots if isinstance(vision, MediaResolver) else []):
            if root[0] == "dir":
                cand = os.path.join(os.path.dirname(root[1]), "on_error")
                if os.path.isdir(cand):
                    on_error = MediaResolver(cand)
                    break

    # 目标路径先算出来：增量判定的快路要看"旧图还在不在"，而它只依赖入参
    if out:
        target = os.path.abspath(out)
    else:
        base_dir = os.path.abspath(outdir) if outdir else DEFAULT_OUTDIR
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(base_dir, f"{_log_basename(log)}_{stamp}.png")

    recorded = dict(recorded_stages or {})
    # 旧图可能叫 `<名>.png`，也可能带 `_疑似过时` 后缀（`cases` 认得出来就传进来）
    old_file = existing or target
    stages: dict[str, str] = dict((trace_extra or {}).get("code") or {})
    if trace_extra is not None:
        stages["input"] = stage_digest(trace_extra.get("input") or {})
        stages["options"] = stage_digest(options_payload(opts))
        # 仓库状态只当"要不要重新核对「疑似过时」"的开关：变了就让下面读一遍日志
        stages["repo"] = stage_digest(trace_extra.get("repo") or {})
        if (
            not list_only
            and recorded
            and os.path.isfile(old_file)
            and recorded.get("raster_impl") == stages.get("raster_impl")
            and all(
                recorded.get(name) == stages.get(name)
                for name in ("parse_impl", "input", "options", "repo")
            )
        ):
            # 读日志的代码没变、输入与参数也没变 ⇒ 解析结果必然相同，不必把日志读一遍
            return _skipped_result(old_file, "轨迹一致（没解析）", stages)

    parsed = parse_log(
        log, opts.t_from, opts.t_to, opts.task_id, opts.keep_internal, px=opts.px
    )
    raw = round_steps(parsed, opts)
    if not raw:
        raise ValueError(describe_empty_window(parsed, opts, log))

    # 节点是否"疑似过时"：日志所属版本之后，该节点所在模块有没有提交。
    # 要在 build_notes 之前算，标过的节点名才能进注记。
    if opts.stale_check:
        if opts.version:
            # 手动指定（--version）或遥测证据包带下来的 release：直接当基线，不再去日志里抠
            found = (opts.version, opts.version_source or VERSION_SOURCE_TELEMETRY)
        else:
            # 用"这段内容的末尾时刻"去挑版本：一份 maafw.log 可能横跨好几天（实测 11-01 16:06 起、
            # 11-02 08:41 的任务），而资源可能中途更新过（同一份 MFA 日志里先后 beta.58 / beta.61）。
            at = parsed.span[1] if parsed.span else parsed.stats.last_ts
            found = _log_version_of(log, at)
        opts.stale = check_stale_nodes(
            Path(REPO_ROOT),
            {s.name for s in raw},
            found[0] if found else None,
            found[1] if found else "",
            # 运行期覆写建出来的节点（`BaseErrorNext` 的 `<父节点>ErrorNext`）：pipeline 里没有
            # 它属正常，不能报成"已改名/删除"
            override_nodes=parsed.override_nodes,
        )
        _apply_stale(opts.stale)

    notes = build_notes(parsed, opts)
    if opts.extra_notes:
        # 遥测关联放在最前：读图的人第一眼就该知道"这张图对应哪个线上问题"
        notes = [Note(text, kind) for text, kind in opts.extra_notes] + notes
    # 本窗口内实际存在的失败现场图：把文件名点出来。同名节点一天失败好几次时，
    # 只看框上的「失败现场」角标分不清是哪一张（实测 `BaseTaskStopError` 一小时里失败 4 次）。
    if opts.thumbs and on_error is not None:
        failure_index = OnErrorIndex(on_error)
        if failure_index.items:
            pad = timedelta(seconds=ON_ERROR_BACKFILL_SECONDS)
            if parsed.span:
                # 选中任务有自己的判定窗口。**不能**再用全局 min/max：框架级失败事件
                # （`Task timeout` / `node not completed`）没有 task 归属，同一份滚动日志里
                # 别的跑次会把窗口撑到跨天，于是把前一天另一个进程的图也列进来（踩过）。
                lo, hi = parsed.span[0], _pad_hi(parsed.span[1], pad.total_seconds())
            else:
                stamps = [s.ts for s in raw]
                lo, hi = min(stamps), _pad_hi(max(stamps), pad.total_seconds())
            # 只向右放宽：现场图是在失败那一刻（最后一步之后）抓的；向左放宽会把相邻的
            # **上一个** task 的图带进来（实测 `TaskEntrust` 与 `TaskTraining` 只差 51 ms）。
            in_window = window_failure_images(failure_index.items, lo, hi)
            if in_window:
                shown = "、".join(in_window[:3])
                more = f" 等 {len(in_window)} 张" if len(in_window) > 3 else ""
                notes.append(Note(f"失败现场图：{shown}{more}", "fail"))
            else:
                # 包里有图、但都不在本窗口（时刻对不上）：点出文件名与时刻，免得
                # "明明有图却没画"。不硬贴——那是另一次失败的现场，贴到本次失败框上会误导。
                outside = [
                    f"{os.path.basename(entry.member)}（{member_clock(entry)}）"
                    for _ts, _name, entry in failure_index.items
                ]
                shown = "、".join(outside[:3])
                more = f" 等 {len(outside)} 张" if len(outside) > 3 else ""
                notes.append(
                    Note(
                        f"本窗口没有现场图；包里的 {shown}{more} 属于另一次失败（时刻对不上）",
                        "fail",
                    )
                )

    truncated_start = False
    if opts.t_from is not None:
        first_ts = _first_log_ts(log)
        if first_ts is not None and first_ts > opts.t_from:
            # 日志被截断（遥测只带 tail）时，请求的窗口起点早于日志第一行。
            # 如实标注，而不是当成"任务从这里开始"来解读。
            truncated_start = True

    if list_only:
        parents, _ = roles(raw, opts.repeat_threshold)
        cut = (
            {n.strip() for n in opts.cut_on.split(",") if n.strip()}
            if opts.cut_on
            else choose_cut_set(raw, parents, opts.min_round_len)
        )
        hard_cut = {n for n in cut if n not in parents}
        rounds = build_rounds(raw, opts.min_round_len, cut, hard_cut)
        cols = compose_columns(
            parsed, rounds, opts.miss_min, opts.collapse, opts.miss_max
        )
        lines: list[str] = []
        for i, col in enumerate(cols):
            lines.append(f"--- 轮 {i + 1} ---")
            for s in col:
                tag = {"miss": "[未命中]", "event": "[事件]"}.get(s.kind, "")
                lines.append(
                    f"  {s.ts.strftime('%H:%M:%S.%f')[:-3]}  {stale_label(s.name):<34}{tag} {s.detail}"
                )
                if opts.parents:
                    # 纯文本不省候选（图上会折成"另 N 项"，这里给全，排查时省一次翻日志）
                    lines.append(f"  {'':<12}└ {s.parent_detail(full=True)}")
        lines.append(
            f"\n共 {len(parsed.steps)} 步 / {len(rounds)} 轮；"
            f"锚点={opts.anchor or anchor_of(raw, rounds)}；"
            f"分轮节点={','.join(sorted(cut))}"
        )
        # 父节点评估账：node done 很少时，这里是"这段时间到底在干什么"的唯一证据
        # （`reco` 行没有 task_id，只能靠带 task_id 的 Node.NextList 事件说清）
        pairs = [
            (tid, stat)
            for (tid, _n), stat in parsed.nextlist_stats.items()
            if opts.task_id is None or tid == opts.task_id
        ]
        pairs.sort(key=lambda kv: (-kv[1].failed, -(kv[1].starting + kv[1].succeeded)))
        if pairs:
            multi_task = len({t for t, _s in pairs if t is not None}) > 1
            lines.append("\n--- 父节点评估（Node.NextList）---")
            width = max(len(stat.name) for _t, stat in pairs) + 2
            for tid, stat in pairs[:12]:
                where = f" task={tid}" if multi_task and tid is not None else ""
                lines.append(
                    f"  {stat.name:<{width}}未命中 {stat.failed:>3} 轮 / 开始 {stat.starting:>3} / "
                    f"命中 {stat.succeeded:>3}{where}"
                )
                if stat.real_candidates:
                    lines.append(
                        f"  {'':<{width}}候选：{' ・ '.join(stat.real_candidates)}"
                    )
            if len(pairs) > 12:
                lines.append(
                    f"  ……另有 {len(pairs) - 12} 个派发者（按未命中轮数排前 12）"
                )
        for note in notes:
            lines.append(f"提示：{note}")
        return "\n".join(lines)

    os.makedirs(os.path.dirname(target), exist_ok=True)

    redraw_reason = ""
    if trace_extra is not None:
        stages["parse"] = stage_digest(parsed_payload(parsed))
        stages["notes"] = stage_digest(notes_payload(notes))
        stages["stale"] = stage_digest(stale_payload(opts.stale))
        if recorded and os.path.isfile(old_file):
            if (
                recorded.get("raster_impl") == stages.get("raster_impl")
                and same_data(recorded, stages)
            ):
                # 读取实现的代码变了，但这段日志的解析/注记/标记一个字都没变：图不用重画
                return _skipped_result(old_file, "轨迹一致（只有读取实现变了）", stages)
            redraw_reason = reason_of(recorded, stages)
        elif not recorded:
            # 没有旧记录（第一次画、或旧记录被 --clean-flow 清掉了）：不用解释
            redraw_reason = ""
        else:
            redraw_reason = "旧图不在了"

    opts.vision, opts.on_error = vision, on_error
    opts.truncated_start = truncated_start
    opts.notes = notes
    opts.log = (
        str(log)
        if isinstance(log, str) and "\n" not in log
        else _log_basename(log) + ".log"
    )
    # 先画到临时名、成功再换名：重画失败（Pillow 缺失、写盘出错）不会毁掉上一轮的好图，
    # 配合 `cases` 的"这次没画出来的 issue 不清理旧产物"，目录里始终留着能看的那一张。
    tmp_target = os.path.join(
        os.path.dirname(target),
        os.path.splitext(os.path.basename(target))[0] + ".rendering.png",
    )
    try:
        drawn = draw_flow(parsed, opts, tmp_target)
    except ModuleNotFoundError as exc:
        _remove_quiet(tmp_target)
        if (exc.name or "").startswith("PIL"):
            raise ModuleNotFoundError(
                "画图需要 Pillow，当前解释器里没有。发布包自带的 python 也不含它——"
                "请用装了 Pillow 的系统 Python 跑（或先 pip install pillow）。"
            ) from exc
        raise
    except BaseException:
        _remove_quiet(tmp_target)
        raise

    mermaid_path: str | None = None
    tmp_mermaid: str | None = None
    try:
        if mermaid:
            mermaid_path = os.path.splitext(target)[0] + ".mmd"
            tmp_mermaid = os.path.splitext(tmp_target)[0] + ".mmd"
            rnds = drawn.rounds_shown
            # 父节点那一行同时作为虚线"派发"边（实线仍是执行顺序，两者含义不同，别混着读）
            with open(tmp_mermaid, "w", encoding="utf-8") as fh:
                fh.write("flowchart TD\n")
                for i, r in enumerate(rnds):
                    span = drawn.spans[i]
                    head = (
                        f"第 {span.first + 1}–{span.last + 1} 轮 ×{span.count}"
                        if span.merged
                        else f"第 {span.first + 1} 轮"
                    )
                    fh.write(f"  subgraph R{i + 1}[{head}]\n    direction TB\n")
                    for j, s in enumerate(r):
                        fh.write(f'    n{i}_{j}["{stale_label(s.name)}"]\n')
                    for j in range(len(r) - 1):
                        fh.write(f"    n{i}_{j} --> n{i}_{j + 1}\n")
                    fh.write("  end\n")
                    if i:
                        fh.write(f"  n{i - 1}_{len(rnds[i - 1]) - 1} --> n{i}_0\n")
                if opts.parents:
                    index: dict[str, str] = {}
                    for i, r in enumerate(rnds):
                        for j, s in enumerate(r):
                            index.setdefault(s.name, f"n{i}_{j}")
                    for i, r in enumerate(rnds):
                        for j, s in enumerate(r):
                            if not s.parent or s.dispatched:
                                continue  # 自派发/自循环画出来只是噪音
                            src = index.get(s.parent)
                            if src and src != f"n{i}_{j}":
                                fh.write(f"  {src} -.->|派发| n{i}_{j}\n")
    except BaseException:
        _remove_quiet(tmp_target)
        if tmp_mermaid is not None:
            _remove_quiet(tmp_mermaid)
        raise

    os.replace(tmp_target, target)
    if mermaid_path is not None and tmp_mermaid is not None:
        os.replace(tmp_mermaid, mermaid_path)

    return FlowResult(
        path=target,
        mermaid=mermaid_path,
        width=drawn.width,
        height=drawn.height,
        steps=drawn.steps,
        boxes=drawn.boxes,
        rounds=drawn.rounds,
        columns=drawn.column_count,
        overlay_groups=drawn.overlay_groups,
        truncated_start=truncated_start,
        notes=list(notes),
        derived_from=parsed.derived_from,
        out_of_order=parsed.stats.out_of_order,
        stale=opts.stale,
        trace=trace_of(stages) if trace_extra is not None else "",
        trace_stages=stages,
        redraw_reason=redraw_reason,
    )


def render_overview(
    out: str,
    *,
    title: str,
    subtitle: str = "",
    fields: list[tuple[str, str]] | None = None,
    images: list[str] | None = None,
    notes: list[Note] | None = None,
    mermaid: bool = False,
    trace_extra: dict[str, Any] | None = None,
    recorded_stages: dict[str, str] | None = None,
) -> FlowResult:
    """把一条遥测 issue 画成「概览卡」PNG——没有完整 maafw.log 时的降级产物。

    与 `render_flow` 同用 PIL，落盘到 `out`；`--mermaid` 时再写一个 `.mmd`（把事实列成节点）。
    issue 本体只存文本（`IssueCard`），没有 on_error 图：图只有在
    `temp/evidence/<SHORT>/debug/on_error/` 被下载过时才贴，否则纯文字。

    `trace_extra` / `recorded_stages` 与 `render_flow` 同义：卡片内容（字段、注记、现场图）
    没变、绘制实现也没变时直接跳过，不去建图。
    """

    def _text(draw: Any, xy: tuple[int, int], s: str, font: Any, fill: Any) -> None:
        # PIL 的 `text` 在老版本不支持 `fill` 为 RGB 元组以外的写法，这里统一成元组
        draw.text(xy, s, font=font, fill=fill)

    from PIL import Image, ImageDraw

    fields = fields or []
    images = images or []
    notes = notes or []

    stages: dict[str, str] = dict((trace_extra or {}).get("code") or {})
    recorded = dict(recorded_stages or {})
    if trace_extra is not None:
        stages["card"] = stage_digest(
            {
                "title": title,
                "subtitle": subtitle,
                "fields": fields,
                "images": [os.path.basename(path) for path in images],
                "notes": notes_payload(notes),
            }
        )
        if (
            recorded
            and os.path.isfile(out)
            and recorded.get("raster_impl") == stages.get("raster_impl")
            and recorded.get("card") == stages.get("card")
        ):
            return _skipped_result(os.path.abspath(out), "轨迹一致", stages)

    font_title = load_font(int(24 * 1.0), bold=True)
    font_field = load_font(16)
    font_note = load_font(13)
    pad = 24
    line_h = 30
    title_h = 44
    sub_h = 26 if subtitle else 0
    fields_h = len(fields) * line_h
    thumb_w = 210
    thumb_h = 200
    img_area_h = (thumb_h + 24) if images else 0
    notes_h = (len(notes) + 1) * (line_h - 8) if notes else 0
    width = 920
    height = pad * 2 + title_h + sub_h + 6 + fields_h + 6 + img_area_h + notes_h
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, 6], fill=(170, 34, 34))
    y = pad
    _text(draw, (pad, y), title, font_title, (20, 20, 20))
    y += title_h
    if subtitle:
        _text(draw, (pad, y), subtitle, font_field, (90, 90, 90))
        y += sub_h
    y += 6
    for label, value in fields:
        _text(draw, (pad, y), label, font_field, (96, 104, 122))
        _text(draw, (pad + 150, y), str(value), font_field, (20, 20, 20))
        y += line_h
    y += 6
    if images:
        _text(
            draw,
            (pad, y),
            "失败现场图：",
            font_field,
            (170, 34, 34),
        )
        y += line_h
        x = pad
        for path in images[:4]:
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    scale = thumb_w / im.width
                    th = min(int(im.height * scale), thumb_h)
                    im = im.resize((thumb_w, max(1, th)))
                    img.paste(im, (x, y))
            except (OSError, ValueError):
                pass
            x += thumb_w + 12
        y += thumb_h + 24
    if notes:
        y += 4
        for n in notes:
            color = NOTE_COLORS.get(n.kind, NOTE_COLORS["info"])
            _text(draw, (pad, y), n.text, font_note, color)
            y += line_h - 8
    target = os.path.abspath(out)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp_target = os.path.splitext(target)[0] + ".rendering.png"
    try:
        img.save(tmp_target)
    except BaseException:
        _remove_quiet(tmp_target)
        raise
    mermaid_path: str | None = None
    tmp_mermaid: str | None = None
    try:
        if mermaid:
            mermaid_path = os.path.splitext(target)[0] + ".mmd"
            tmp_mermaid = os.path.splitext(tmp_target)[0] + ".mmd"
            with open(tmp_mermaid, "w", encoding="utf-8") as fh:
                fh.write("flowchart TD\n")
                fh.write(f'  title["{re.sub(r"[\"\[\]\\]", "", title)}"]\n')
                for i, (label, value) in enumerate(fields):
                    safe = re.sub(r'["\[\]]', "", str(value))[:40]
                    fh.write(f'  f{i}["{label}: {safe}"]\n')
    except BaseException:
        _remove_quiet(tmp_target)
        if tmp_mermaid is not None:
            _remove_quiet(tmp_mermaid)
        raise
    os.replace(tmp_target, target)
    if mermaid_path is not None and tmp_mermaid is not None:
        os.replace(tmp_mermaid, mermaid_path)
    return FlowResult(
        path=target,
        mermaid=mermaid_path,
        width=width,
        height=height,
        steps=0,
        boxes=len(fields),
        rounds=0,
        columns=0,
        overlay_groups=0,
        truncated_start=False,
        notes=list(notes),
        derived_from="overview",
        out_of_order=0,
        stale=None,
        trace=trace_of(stages) if trace_extra is not None else "",
        trace_stages=stages,
    )
