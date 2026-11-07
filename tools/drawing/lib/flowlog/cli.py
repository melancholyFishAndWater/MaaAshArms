"""命令行：参数表、`FlowOptions` 组装、遥测注解接线、`main`。

与渲染逻辑分离：库调用方直接构造 `FlowOptions` 调 `render_flow`，不必过 CLI。
参数默认值就是文档里承诺的默认值，改默认值要同步 README。"""

from __future__ import annotations


import argparse
import logging
import sys
from datetime import datetime

from .logparse import scan_tasks

from .model import _fmt_ts

from .notes import stale_summary

from .paths import DEFAULT_OUTDIR, REPO_ROOT

from .render import FlowOptions, FlowResult, _pick_subdir, render_flow

from .stale import VERSION_SOURCE_CLI, VERSION_SOURCE_TELEMETRY

log = logging.getLogger("tools.flow_from_log")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%H:%M:%S",
        "%H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            today = datetime.now().date()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt
    raise SystemExit(f"看不懂的时间格式：{s}")


def default_output_dir() -> str:
    """默认输出目录：仓库根的 temp/flow。"""
    return DEFAULT_OUTDIR


def build_flow_options(args: argparse.Namespace) -> FlowOptions:
    """命令行参数 → FlowOptions（库调用方可以直接构造，不必过 CLI）。"""
    return FlowOptions(
        t_from=parse_time(args.t_from),
        t_to=parse_time(args.t_to),
        task_id=args.task_id,
        px=args.pid,
        keep_internal=args.keep_internal,
        anchor=args.anchor,
        cut_on=args.cut_on,
        collapse=args.collapse,
        repeat_threshold=args.repeat_threshold,
        min_round_len=args.min_round_len,
        miss_min=args.miss_min,
        miss_max=args.miss_max,
        max_rounds=args.max_rounds,
        ports=args.ports,
        cols_per_band=args.cols_per_band,
        parents=args.parents,
        overlay=args.overlay,
        overlay_min=args.overlay_min,
        stale_check=args.stale_check,
        version=args.version,
        version_source=VERSION_SOURCE_CLI if args.version else "",
        vision=_pick_subdir(args.vision, "vision") if args.vision else None,
        on_error=_pick_subdir(args.on_error, "on_error") if args.on_error else None,
        thumbs=args.thumbs,
        thumb_height=args.thumb_height,
        box_width=args.box_width,
        box_height=args.box_height,
        gap_y=args.gap_y,
        gap_error=args.gap_error,
        gap_x=args.gap_x,
        font_size=args.font_size,
        scale=args.scale,
        title=args.title,
    )


def _telemetry_module():
    """延迟 import `tools.sentry.lib.telemetry_index`。

    `telemetry_index` 反过来要用 `tools.sentry.lib.evidence`，而 `evidence` 在模块级 import
    `tools.drawing.lib.flow_cases`——所以只能在真正要用遥测时才把它拉进来，避免 import 环。
    直接跑本文件（`python tools/drawing/cli.py flow`）时仓库根不在 `sys.path` 上，这里补一次。
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools.sentry.lib import telemetry_index

    return telemetry_index


def apply_telemetry(args, options: FlowOptions) -> bool:
    """按 CLI 开关把遥测关联灌进 `options`；返回 False = `--telemetry-only` 已处理完、不该再画图。

    只在开关打开时联网（`--telemetry-offline` 除外）。刷新失败只打提示，不影响画图——
    遥测是"加分项"，不是画图的前提。
    """
    module = _telemetry_module()
    annotation = module.annotate(
        args.log,
        offline=args.telemetry_offline,
        issue=args.telemetry_issue,
        task_id=options.task_id,
        days=args.telemetry_days,
        gap=args.telemetry_gap,
    )
    if annotation.notes:
        options.extra_notes = list(annotation.notes)
    if annotation.chosen:
        options.issue = annotation.chosen
    if options.version is None and annotation.version:
        options.version = annotation.version
        options.version_source = VERSION_SOURCE_TELEMETRY
    for warning in annotation.warnings:
        log.info("提示：%s", warning)
    if args.telemetry_only:
        module.format_matches(annotation.matches, sys.stdout)
        return False
    return True


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="maafw.log → 实际执行流程图（横向=轮次、纵向=轮内顺序；"
        "默认输出到仓库根的 temp/flow/）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "log",
        help="maafw.log 路径（也可以是 MFA 导出的 zip 里的那份，见 --vision 的说明）",
    )
    ap.add_argument(
        "--vision",
        default=None,
        help="vision 目录，或 MFA 导出包（zip / 多个 archive-partNN.zip），或它们的父目录（如 debug/）",
    )
    ap.add_argument(
        "--on-error",
        dest="on_error",
        default=None,
        help="on_error 目录或导出包（可选，默认取 --vision 同级的 on_error；给超时/失败框贴现场图）",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=f"输出 PNG（默认 {DEFAULT_OUTDIR}/<日志名>_<时间戳>.png）",
    )
    ap.add_argument(
        "--outdir", default=None, help=f"只改输出目录（默认 {DEFAULT_OUTDIR}）"
    )
    ap.add_argument(
        "--from",
        dest="t_from",
        default=None,
        help="起始时间，支持 'HH:MM:SS' 或完整时间戳",
    )
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间")
    ap.add_argument("--task-id", type=int, default=None, help="只看某个 task_id")
    ap.add_argument(
        "--pid",
        type=str,
        default=None,
        help="只看某个进程（`Px<pid>` 形态），与 --task-id 一起用于区分跨进程复用的任务编号",
    )
    ap.add_argument(
        "--anchor", default=None, help="手动指定循环锚点（只影响标题显示，默认自动）"
    )
    ap.add_argument(
        "--cut-on",
        default=None,
        help="手动指定分轮节点，逗号分隔（默认自动：父节点里重复出现的；只有一轮时再取最高频节点）",
    )
    ap.add_argument(
        "--collapse",
        type=int,
        default=3,
        help="连续同名节点折叠为 ×N 的阈值（默认 3，1=不折叠）",
    )
    ap.add_argument(
        "--repeat-threshold",
        type=int,
        default=2,
        help="出现次数 >= N 视为重要节点（左下出/右上进，默认 2）",
    )
    ap.add_argument(
        "--min-round-len", type=int, default=2, help="一轮至少多少步（默认 2）"
    )
    ap.add_argument(
        "--miss-min",
        type=int,
        default=5,
        help="连续未命中几次才画成聚合框（默认 5，0=全部显示）",
    )
    ap.add_argument(
        "--miss-max",
        type=int,
        default=8,
        help="每轮最多画几个未命中聚合框（默认 8，0=不限）",
    )
    ap.add_argument("--max-rounds", type=int, default=40, help="最多画多少轮")
    ap.add_argument(
        "--ports",
        choices=["pos", "role"],
        default="pos",
        help="端口规则：pos=按方向（竖直相邻上入下出、水平相邻左入右出，默认）；"
        "role=按节点角色（重要节点左下出/右上进，父节点左下出/右下进）",
    )
    ap.add_argument(
        "--cols-per-band",
        type=int,
        default=0,
        help="一行放几轮；默认 0 = 不换行，空间不够就一直往右延伸",
    )
    ap.add_argument(
        "--keep-internal",
        action="store_true",
        help="保留 agent 内联识别（recognition/OCR/<uuid>）",
    )
    ap.add_argument("--no-thumbs", dest="thumbs", action="store_false", help="不贴截图")
    ap.add_argument(
        "--no-parents",
        dest="parents",
        action="store_false",
        help="框里不写父节点那一行。父节点 = **本轮的派发者**（来自 Node.NextList 事件），"
        "不是配置里的层级：next 是有序候选集，同一节点可被多处派发，所以是调用图不是树",
    )
    ap.add_argument(
        "--no-overlay",
        dest="overlay",
        action="store_false",
        help="不合并重复轮次。默认把**连续、同形、整轮无截图且无失败框**的轮次并成一列"
        "（列头写「第 a–b 轮 · ×N」，右侧橙条标出），图因此窄很多；"
        "要看逐轮时间就关掉它",
    )
    ap.add_argument(
        "--overlay-min",
        type=int,
        default=2,
        help="连续几轮同形才合并（默认 2；0 或 1 等于关闭）",
    )
    ap.add_argument(
        "--no-stale-check",
        dest="stale_check",
        action="store_false",
        help="不查「节点疑似过时」。默认取日志里的「资源版本：vX.Y.Z」（MFA 自己报的；"
        "安装目录名不带版本、更新后也是错的，不作来源）。再看该节点所在 pipeline 模块在那个版本"
        "之后有没有提交；有就把节点名标成「疑似过时」。只标疑似，不判断节点本身有没有变。"
        "日志里没有「资源版本」那行时不做这项检查，注记会写明原因",
    )
    ap.add_argument(
        "--version",
        default=None,
        help="手动指定资源版本（vX.Y.Z）作为「疑似过时」的基线；不传时优先取日志里的"
        "「资源版本：vX.Y.Z」，再到同装 MFA 日志，再到遥测证据包 evidence.json 的 release",
    )
    ap.add_argument(
        "--telemetry",
        dest="telemetry",
        action="store_true",
        help="画本地包时关联遥测问题（默认开）：按日志里的失败节点在本地遥测库里反查，"
        "把问题号/次数/release 写进注记，并用那个 release 当「疑似过时」基线",
    )
    ap.add_argument(
        "--no-telemetry", dest="telemetry", action="store_false", help="不关联遥测（纯离线）"
    )
    ap.add_argument(
        "--telemetry-offline",
        action="store_true",
        help="只读本地遥测库、不联网刷新（库不存在时就不反查）",
    )
    ap.add_argument(
        "--telemetry-issue", default=None, help="指定问题号（如 PYTHON-2X），不再自动反查"
    )
    ap.add_argument(
        "--telemetry-days", type=int, default=14, help="本地遥测库保留最近 N 天（默认 14）"
    )
    ap.add_argument(
        "--telemetry-gap", type=int, default=3, help="反查的时间窗（天，默认 3）"
    )
    ap.add_argument(
        "--telemetry-only", action="store_true", help="只打印反查到的遥测问题，不画图"
    )
    ap.add_argument(
        "--thumb-height", type=int, default=132, help="截图高度 px（默认 132）"
    )
    ap.add_argument("--box-width", type=int, default=470)
    ap.add_argument("--box-height", type=int, default=66)
    ap.add_argument(
        "--gap-y", type=int, default=36, help="同一列里相邻框的间距（默认 36）"
    )
    ap.add_argument(
        "--gap-error",
        type=int,
        default=64,
        help="未命中/异常框相邻时的间距（默认 64，别挤在一起）",
    )
    ap.add_argument("--gap-x", type=int, default=120, help="列间距（默认 120）")
    ap.add_argument("--font-size", type=int, default=16)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--list", action="store_true", help="只打印解析出的步骤，不画图")
    ap.add_argument(
        "--list-tasks",
        action="store_true",
        help="只列出日志里的所有任务（进程 / task_id / 入口 / 起止 / 步数），不画图。"
        "多进程日志里同一编号会出现在不同进程下，配 --pid + --task-id 才能唯一定位",
    )
    ap.add_argument(
        "--mermaid", action="store_true", help="额外输出同名 .mmd（Mermaid 文本流程图）"
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    ap.add_argument(
        "--log-file",
        default=None,
        help="日志文件路径（默认按运行时间戳建 temp/logs/flow_from_log_<时间戳>_<pid>.log）；"
        "传 none / 空 只走控制台",
    )
    ap.set_defaults(
        thumbs=True, parents=True, overlay=True, stale_check=True, telemetry=True
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = create_argument_parser().parse_args(argv)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools._logging import configure_tool_logging

    _, log_path = configure_tool_logging(
        "flow_from_log", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)
    options = build_flow_options(args)

    if args.telemetry_only and not args.telemetry:
        log.error("--telemetry-only 要开着遥测（别同时给 --no-telemetry）。")
        return 1
    if args.telemetry and not apply_telemetry(args, options):
        return 0

    if args.list_tasks:
        tasks = scan_tasks(args.log, options.t_from, options.t_to)
        if not tasks:
            log.error(
                "日志里没有任何任务（没有 PipelineTask node done，也没有 task start）。"
            )
            return 1
        head = (
            f"{'进程':<9} {'task_id':>9}  {'步数':>4}  {'开始':<23} {'结束':<23} 入口"
        )
        print(head)
        print("-" * len(head))
        for t in tasks:
            print(
                f"{t.px:<9} {t.task_id:>9}  {t.steps:>4}  "
                f"{_fmt_ts(t.first_ts):<23} {_fmt_ts(t.last_ts):<23} {t.entry}"
            )
        print(
            f"\n共 {len(tasks)} 个任务。多进程日志里同一 task_id 会出现在不同进程下，"
            f"用 `--pid <进程> --task-id <编号>` 唯一定位一个。"
        )
        return 0

    try:
        result = render_flow(
            args.log,
            out=args.out,
            outdir=args.outdir,
            options=options,
            mermaid=args.mermaid,
            list_only=args.list,
        )
    except ValueError as exc:
        log.error("错误：%s", exc)
        return 1
    except ModuleNotFoundError as exc:
        log.error("错误：%s", exc)
        return 1

    if args.list:
        print(result)
        return 0

    assert isinstance(result, FlowResult)
    merged = (
        f"（{result.overlay_groups} 段重复已合并 → {result.columns} 列）"
        if result.overlay_groups
        else ""
    )
    print(
        f"步骤 {result.steps}（折叠后 {result.boxes} 个框）/ 轮次 {result.rounds}{merged}"
        f" → {result.path}  ({result.width}×{result.height})"
    )
    # 「疑似过时」命中：画完图后显式打一行（notes 里也有，但那是 stderr 的注记）
    summary = stale_summary(result.stale)
    if summary:
        log.info(summary)
    if result.truncated_start:
        log.info(
            "提示：日志第一行晚于 --from，这份日志只包含了窗口的后半段（遥测附件只带 tail），"
            "窗口开头的过程不在图里"
        )
    for note in result.notes:
        # 与图上第二行同一批注记：终端里再打一遍，免得只看 stdout 的人漏掉读图前提
        log.info("提示：%s", note.text)
    if result.mermaid:
        print(f"mermaid → {result.mermaid}")
    return 0
