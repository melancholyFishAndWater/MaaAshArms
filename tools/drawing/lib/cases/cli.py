"""命令行：参数解析、`main`、遥测注解接线与批量出图循环。

行为与拆分前的单文件版本一致；参数含义见 `tools/drawing/README.md`。
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime
from typing import Any

from tools._logging import configure_tool_logging
from tools.drawing.lib.flow_from_log import (
    DEFAULT_OUTDIR,
    VERSION_SOURCE_CLI,
    VERSION_SOURCE_TELEMETRY,
    FlowOptions,
    MediaEntry,
    parse_time,
    scan_tasks,
)

from .evidence import (
    _evidence_case,
    _evidence_event_note,
    _evidence_scene,
    _evidence_task_id,
    _fetch_evidence,
    _has_evidence,
    _terminal_dispatcher,
    _usable_evidence_case,
    set_evidence_dirs,
)
from . import release_mark
from .local_release import LocalRelease, default_root
from .output import (
    MANIFEST_NAME,
    clear_artifacts,
    load_manifest,
    prune_artifacts,
    save_manifest,
)
from .overview import run_overview
from .pack import (
    EVIDENCE_ROOT,
    Case,
    _evidence_manifest,
    _local_time,
    _pack_version,
    _pick_log,
    _telemetry_module,
    discover_cases,
    load_case,
)
from .runner import run_case
from .release_mark import set_enabled as set_release_mark_enabled
from .scene import export_scene, prune_scene, scene_dir, scene_entries, select_scene
from .signature import code_stages
from .version_filter import VersionFilter, label_of

log = logging.getLogger("tools.flow_cases")


def _sentry_failure_hint(exc: Exception) -> str:
    """Sentry CLI 没登录时返回一句可执行的下一步；否则返回空串。

    与 `tools/sentry/lib/report_common.py:authority_error_hint` 同口径（那边给 sentry 子命令
    用）。这里给绘图侧用：刷新遥测库失败后它会退回本地数据，但还会逐 issue 去调 `evidence`
    拉包，每次都白跑一趟联网子进程、每个 issue 再补一条失败告警。
    """
    text = str(exc).lower()
    if "not authenticated" in text or "auth login" in text:
        return (
            "Sentry 没登录：先执行一次 `sentry auth login`（浏览器授权），或把 SENTRY_CONFIG_DIR "
            "指到含已登录 cli.db 的目录。本次跳过证据包拉取，缺包的 issue 直接出概览卡。"
        )
    return ""

def _with_telemetry(options: FlowOptions, annotation) -> FlowOptions:
    """把遥测结论并进 options：产物名用问题号、注记带上、release 当版本基线。"""
    version = options.version or annotation.version or None
    source = options.version_source or (
        VERSION_SOURCE_TELEMETRY if annotation.version else ""
    )
    return replace(
        options,
        extra_notes=list(annotation.notes) or options.extra_notes,
        issue=annotation.chosen or options.issue,
        version=version,
        version_source=source,
    )


def _scene_anchor(card: Any, pack_case: Case | None):
    """这次失败的**时刻**：优先证据包 `event.received`，没有才用卡片上的 `received`。

    卡片的 `received` 不总等于失败时刻（实测 `PYTHON-3B`：卡片是 `13:52Z`，包里的失败事件是
    `09:33Z`、本地释放的现场图在 17:33 本地时），拿它当锚点会挑到另一场失败的图。
    """
    candidates: list[Case | None] = [pack_case] if pack_case is not None else []
    candidates.append(_evidence_case(card.short_id))
    for case in candidates:
        if case is None:
            continue
        event = _evidence_manifest(case).get("event") or {}
        found = _local_time(str(event.get("received") or ""))
        if found is not None:
            return found
    return _local_time(str(getattr(card, "received", "") or ""))


def _scene_for(
    card: Any, pack_case: Case | None, outdir: str, manifest: dict, origin: str
) -> list[str]:
    """导出一个 issue 的现场图：遥测证据包优先，遥测没图时用补充日志包里对得上的那张。

    返回写进 `images/` 的文件名（含内容没变、只是确认过的那些）。两个来源都没图时返回空表：
    清理时这一条会保住上一轮的图，不因为这次拿不到图就删掉。
    """
    when = _scene_anchor(card, pack_case)
    node = str(getattr(card, "node_name", "") or "")
    entries: list[tuple[MediaEntry, bool]] = [
        (MediaEntry(member=path, path=path), False)
        for path in _evidence_scene(card.short_id, when, node)
    ]
    if not entries and pack_case is not None:
        # 补充日志包（`--case` 的 debug 目录 / 导出包）：按失败时刻认"命中图"，
        # 名字带 `_日志包`，与遥测自带的那张分开
        entries = [
            (entry, True)
            for entry in select_scene(scene_entries(pack_case.on_error()), when, node)
        ]
    return export_scene(
        outdir,
        manifest,
        issue=card.short_id,
        label=str(getattr(card, "label", "") or ""),
        entries=entries,
        origin=origin,
    )


def _assist_pack(
    case: Case,
    options: FlowOptions,
    task_id: int | None,
    local: LocalRelease | None,
    label: str = "",
    event_id: str = "",
) -> tuple[Case, FlowOptions] | None:
    """本地 release 里认得出同一次运行：换成本地完整日志 + 本地图片，并加一条注记。

    只用**内容对账**（`local_release.find`：日志行逐字相同 + 同 task）。对不上就返回 None
    ——不按时间挑运行：本地日志的文件名是轮转时刻、不是内容区间，同一小时里可能有好几份。
    认出来之后，`Case` 只带那份日志（`root` 留空）：`vision()` / `on_error()` 按"日志所在
    目录"解析，正好是本地 `<debug>/vision`、`<debug>/on_error`。
    """
    if local is None:
        return None
    run = local.find(case, task_id, label=label or case.label, event_id=event_id)
    if run is None:
        return None
    version = options.version or _pack_version(case)
    notes = list(options.extra_notes)
    event_note = _evidence_event_note(case)
    if event_note is not None:
        notes.append(event_note)
    notes.extend((line, "local") for line in run.note_lines())
    return (
        Case(label=case.label, log_path=run.log_path),
        replace(
            options,
            version=version,
            version_source=options.version_source
            or (VERSION_SOURCE_TELEMETRY if version else ""),
            extra_notes=notes,
        ),
    )


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="把一个证据包（debug 目录 / MFA 导出 zip / 多卷）渲染成流程图，产出落到 temp/flow/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--case",
        nargs="+",
        required=False,
        help="证据包：一个 debug 目录、一个/多个归档（多卷一起给）、或一份日志文件；"
        "配合 --telemetry-bulk 时是可选的（不带就只画遥测概览卡）",
    )
    ap.add_argument(
        "--vision",
        default=None,
        help="截图目录（默认取 `<case>/vision`；传单个日志文件时取日志所在目录的 `vision/`）",
    )
    ap.add_argument(
        "--on-error",
        dest="on_error",
        default=None,
        help="on_error 现场图目录（默认取 `<case>/on_error`，单日志文件同理取日志所在目录）",
    )
    ap.add_argument(
        "--outdir", default=DEFAULT_OUTDIR, help=f"输出目录（默认 {DEFAULT_OUTDIR}）"
    )
    ap.add_argument("--from", dest="t_from", default=None, help="起始时间")
    ap.add_argument("--to", dest="t_to", default=None, help="结束时间")
    ap.add_argument("--task-id", type=int, default=None, help="只看某个 task_id")
    ap.add_argument(
        "--pid",
        default=None,
        help="只看某个进程（Px<进程号>）。一份 maafw.log 里常有两个进程复用同一 task_id，"
        "只给 --task-id 会把两趟混进同一张图，步骤数对不上就是它",
    )
    ap.add_argument("--cut-on", default=None, help="手动指定分轮节点（逗号分隔）")
    ap.add_argument(
        "--max-rounds", type=int, default=40, help="最多画多少轮（默认 40）"
    )
    ap.add_argument(
        "--collapse", type=int, default=3, help="连续同名节点折叠阈值（默认 3）"
    )
    ap.add_argument("--no-thumbs", dest="thumbs", action="store_false", help="不贴截图")
    ap.add_argument(
        "--no-parents",
        dest="parents",
        action="store_false",
        help="框里不写父节点那一行（父节点 = 本轮的派发者，来自 Node.NextList 事件）",
    )
    ap.add_argument(
        "--no-overlay",
        dest="overlay",
        action="store_false",
        help="不合并重复轮次（默认把连续、同形、整轮无截图且无失败框的轮次并成一列）",
    )
    ap.add_argument(
        "--overlay-min",
        type=int,
        default=2,
        help="连续几轮同形才合并（默认 2；0 或 1 等于关闭）",
    )
    ap.add_argument("--mermaid", action="store_true", help="额外输出 .mmd")
    ap.add_argument(
        "--version",
        default=None,
        help="手动指定资源版本（vX.Y.Z）作为「疑似过时」的基线；不传时遥测证据包会自己从 "
        "evidence.json 的 release 取，本地日志从「资源版本：vX.Y.Z」或同装 MFA 日志取",
    )
    ap.add_argument(
        "--telemetry",
        dest="telemetry",
        action="store_true",
        help="关联遥测问题（默认开）：按日志里的失败节点在本地遥测库反查，把问题号/次数/release "
        "写进注记与产物名，并用那个 release 当「疑似过时」基线",
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
        "--telemetry-version",
        dest="telemetry_version",
        action="append",
        metavar="SPEC",
        help="只画版本满足条件的遥测 issue：`>=v1.0.0-beta.61` / `<=v1.0.0-beta.55` / "
        "`==v1.0.0-beta.60`（运算符可省，默认 ==）；可重复，多条按「且」叠加（给上下界就是范围）。"
        "排序口径 alpha < beta < rc < 正式版，所以 `>=v1.0.0-beta.61` 也命中 rc 与 v1.0.0。"
        "release 解析不出本项目版本的 issue 不算命中；命中的 issue 版本不在范围的包（--case）跳过",
    )
    ap.add_argument(
        "--telemetry-days", type=int, default=14, help="本地遥测库保留最近 N 天（默认 14）"
    )
    ap.add_argument(
        "--telemetry-gap", type=int, default=3, help="反查的时间窗（天，默认 3）"
    )
    ap.add_argument(
        "--telemetry-keep-resolved",
        action="store_true",
        help="本地遥测库保留已解决/已忽略的 issue（默认清掉）",
    )
    ap.add_argument(
        "--telemetry-only", action="store_true", help="只打印反查到的遥测问题，不画图"
    )
    ap.add_argument("--force", action="store_true", help="忽略 manifest，强制重画")
    ap.add_argument(
        "--no-release-mark",
        dest="release_mark",
        action="store_false",
        help="不给产物名加「_已修复待发版」：默认会看 git——issue 的修复提交已经在最新 tag "
        "之后（还没打 tag 发版）时，图名追加这段，表示「已修、等发版」；只看 git、不联网，"
        "读不出 git 历史就不加。标记只改名不重画（图的内容没变）",
    )
    ap.add_argument(
        "--clean-flow",
        dest="clean_flow",
        action="store_true",
        help="批量出图（--telemetry-bulk）前先清空输出目录里的 png/mmd 与 manifest，再整批重画。"
        "默认是增量：manifest 里分步摘要一致就跳过，只在画完后删掉不属于本次 issue 集合、"
        "或已被本次重画取代的旧产物；同一目录下 explore-csv 落的 .log 与子目录不动",
    )
    ap.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        help="增量出图，但不删任何旧产物（目录里会留着上次的图、以及版本筛选范围外的图）",
    )
    ap.add_argument(
        "--evidence-dir",
        dest="evidence_dir",
        action="append",
        metavar="DIR",
        help="批量遥测模式下找证据包的根目录（可重复，先给的先找）：认 `<DIR>/<问题号>/debug`、"
        "`<DIR>/<问题号>/` 直接放日志、根目录名就是问题号三种布局，也认 `<DIR>` 下文件名带"
        f"问题号的归档（多卷一起给）。默认 {EVIDENCE_ROOT}（`evidence` 命令的产出位置，总会兜底）",
    )
    ap.add_argument(
        "--telemetry-bulk",
        action="store_true",
        help="批量出图：遍历本地遥测库里所有未解决 issue，每个出一张图；"
        "给了 --case 就用它给命中的 issue 出完整流程（作为补充），"
        "没命中的用该 issue 自己的证据包出完整流程（搜索根见 --evidence-dir）；"
        "连证据包都没有才出概览卡",
    )
    ap.add_argument(
        "--fetch-evidence",
        dest="fetch_evidence",
        action="store_true",
        default=True,
        help="本地没有证据包时自动调 `evidence` 拉取（默认开，需联网）；拉到了就用真实日志出流程",
    )
    ap.add_argument(
        "--no-fetch-evidence",
        dest="fetch_evidence",
        action="store_false",
        help="不自动拉取缺失的证据包（离线或不想联网时用）",
    )
    ap.add_argument(
        "--local-release",
        dest="local_release",
        default=default_root(),
        metavar="DIR",
        help="本地 release 安装目录（默认 D:/MaaAshArms，环境变量 MAA_DRAWING_LOCAL_ROOT 可改）："
        "画证据包时按**内容**对账（证据包日志里的行在本地日志里逐字相同 + 同 task），认得出"
        "同一次运行就用它在本地的完整日志、vision/ 与 on_error/ 辅助绘图；认不出就不用"
        "（不按时间猜——本地日志名是轮转时刻，不是内容区间）",
    )
    ap.add_argument(
        "--no-local-release",
        dest="local_release",
        action="store_const",
        const="",
        help="不让本地 release 参与绘图：只用证据包自己的日志与图片",
    )
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）；DEBUG 会多打解析/匹配细节",
    )
    ap.add_argument(
        "--log-file",
        default=None,
        help="日志文件路径（默认按运行时间戳建 temp/logs/flow_cases_<时间戳>_<pid>.log）；"
        "传 none / 空 只走控制台不写文件",
    )
    ap.add_argument(
        "--list-cases", action="store_true", help="只列出发现的证据包，不画"
    )
    ap.add_argument(
        "--all-tasks",
        action="store_true",
        help="按任务逐个输出（一个 (进程, task_id) 一张图）。任务号在各进程内复用，"
        "不加这个参数时整份日志混画一张；加了之后文件名带 _task<id>_px<进程>",
    )
    ap.set_defaults(thumbs=True, telemetry=True, prune=True, release_mark=True)
    return ap


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    set_evidence_dirs(args.evidence_dir)
    _, log_path = configure_tool_logging(
        "flow_cases", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)
    if args.telemetry_only and not args.telemetry:
        log.error(
            "--telemetry-only 要开着遥测（别同时给 --no-telemetry）。"
        )
        return 1
    if not args.case and not args.telemetry_bulk:
        log.error(
            "需要 --case <证据包> 或 --telemetry-bulk（两者都不给画不了）。"
        )
        return 1
    if args.telemetry_only and not args.case:
        log.error("--telemetry-only 需要 --case <日志/证据包> 才能反查。")
        return 1
    if args.telemetry_bulk:
        # 批量出图以遥测库为驱动，遥测必须开（默认就开，这里只是兜底 --no-telemetry）
        args.telemetry = True
    # 版本筛选（--telemetry-version）：先解析成条件，后面统一用 `version_filter` 判定。
    # 放在 bulk 开遥测之后，这样 `--telemetry-bulk --no-telemetry --telemetry-version …` 不算冲突。
    version_filter: VersionFilter | None = None
    if args.telemetry_version:
        if not args.telemetry:
            log.error("--telemetry-version 要开着遥测（别同时给 --no-telemetry）。")
            return 1
        try:
            version_filter = VersionFilter.parse(args.telemetry_version)
        except ValueError as exc:
            log.error("错误：%s", exc)
            return 1
    try:
        t_from, t_to = parse_time(args.t_from), parse_time(args.t_to)
    except SystemExit as exc:
        log.error("错误：%s", exc)
        return 1

    options = FlowOptions(
        t_from=t_from,
        t_to=t_to,
        task_id=args.task_id,
        px=args.pid,
        cut_on=args.cut_on,
        max_rounds=args.max_rounds,
        collapse=args.collapse,
        thumbs=args.thumbs,
        parents=args.parents,
        overlay=args.overlay,
        overlay_min=args.overlay_min,
        version=args.version,
        version_source=VERSION_SOURCE_CLI if args.version else "",
    )

    # 遥测（默认开）：先刷新一次本地库，之后每个包都只在本地查。
    # 刷新失败只提示，不影响画图——遥测是加分项，不是画图的前提。
    telemetry = _telemetry_module()
    telemetry_data: dict | None = None
    # Sentry CLI 不可用（没登录 / 凭据打不开）：刷新失败后不再逐 issue 重试拉包
    sentry_down = False
    if args.telemetry:
        if args.telemetry_offline:
            telemetry_data = telemetry.load_index()
        else:
            try:
                telemetry_data, changes = telemetry.refresh_default(
                    days=args.telemetry_days,
                    keep_resolved=args.telemetry_keep_resolved,
                )
                for change in changes:
                    log.info("提示：%s", change)
            except (RuntimeError, OSError, ImportError) as exc:
                log.warning(
                    "遥测库没刷新成（%s），用本地已有数据；若报「unable to open database file」，"
                    "把 SENTRY_CONFIG_DIR 指到含可写 cli.db 的目录",
                    exc,
                )
                telemetry_data = telemetry.load_index()
                hint = _sentry_failure_hint(exc)
                if hint:
                    log.warning("%s", hint)
                    sentry_down = True
    cards = telemetry.load_cards(telemetry_data) if telemetry_data else []
    # 问题名（遥测卡上的 `Reward` / `Factory`…）：进产物名，清理时也用它认
    # `PYTHON-3B_Reward.png` 这类带问题名的名字（版本筛掉的也要留着 label，否则清不掉）
    labels_by_issue = {
        card.short_id: card.label for card in cards if getattr(card, "label", "")
    }
    if version_filter is not None:
        # 筛一次库，后面（批量出图 / --telemetry-only）都用筛过的这批。
        # 两处排除分开说：`release` 缺失是数据不全，不是被条件筛掉的。
        total = len(cards)
        cards, no_version, out_of_range = version_filter.partition(cards)
        log.info(
            "版本筛选 %s：库内 %d 个 issue → 命中 %d 个"
            "（%d 个没有可解析 release、%d 个版本不在范围内）",
            version_filter.describe(),
            total,
            len(cards),
            len(no_version),
            len(out_of_range),
        )
        if cards:
            # 命中的名字要列出来：同一命令两次跑的张数会随库刷新变化（有人 resolve / 来了新问题），
            # 只报个数看不出是"库被清了"还是"新问题进来了"
            log.info("命中的 issue：%s", "、".join(card.short_id for card in cards))

    # 本地 release 辅助（默认开）：只按内容对账，认不出就不用；目录不在就当没开
    local: LocalRelease | None = None
    if args.local_release:
        local = LocalRelease.discover(args.local_release)
        if local is None:
            log.info(
                "提示：本地 release 不可用（%s 的 debug/ 下没有日志），本次只用证据包自己的文件",
                args.local_release,
            )
        else:
            log.info(
                "本地 release 辅助：%s（%d 份日志，按内容对账）",
                local.root,
                len(local.logs()),
            )

    single = args.case[0] if args.case else None
    whole_dir = (
        bool(single)
        and len(args.case) == 1
        and os.path.isdir(single)
        and not _pick_log(single)
    )
    cases: list[Case] = []
    if args.case:
        try:
            cases = (
                discover_cases(single)
                if whole_dir and single is not None
                else [load_case(args.case, task_id=options.task_id)]
            )
        except (FileNotFoundError, ValueError) as exc:
            log.error("错误：%s", exc)
            return 1
        if not cases:
            log.error("错误：%s 下没找到带日志的证据包。", single)
            return 1

    if args.list_cases:
        for case in cases:
            print(f"{case.label}\t{case.describe()}")
        return 0

    # `--clean-flow`：整目录清空后重画（大改版面、想彻底重来一遍时用）。默认增量，不清空；
    # `--telemetry-bulk` 之外（单包 / --all-tasks）两种都不清。
    if args.telemetry_bulk and args.clean_flow:
        removed = clear_artifacts(args.outdir)
        if removed:
            log.info(
                "批量出图前清空输出目录（--clean-flow）：%s（删掉 %d 个旧产物）",
                args.outdir,
                removed,
            )
        else:
            log.info("批量出图前清空输出目录（--clean-flow）：%s（没有旧产物）", args.outdir)
    os.makedirs(args.outdir, exist_ok=True)
    manifest = load_manifest(args.outdir)
    set_release_mark_enabled(args.release_mark)
    label, marked = release_mark.fixed_issue_ids()
    if marked:
        # 只影响产物名（标记出现/消失时只改名、不重画），所以单独打一行，不与"代码摘要"混在一起
        log.info(
            "已修复待发版（%s）：%s；这些图的名字会带「_%s」，打新 tag 推送后自动去掉",
            label,
            "、".join(sorted(marked)),
            release_mark.RELEASE_MARK,
        )
    elif args.release_mark:
        log.info("已修复待发版：最新 tag 之后没有 Fixes 的提交（%s）", label or "无可用范围")
    else:
        log.info("已修复待发版：--no-release-mark，产物名不加标记")
    code_now = code_stages()
    if args.telemetry_bulk:
        # 把"这次哪类代码变了"先摆出来：读取实现变了只会让下一轮多读一遍日志，
        # 绘制实现变了才会整批重画（判据见 `flowlog/trace.py`）
        previous = manifest.get("signature")
        previous = previous if isinstance(previous, dict) else {}
        log.info(
            "代码摘要：读取实现 %s，绘制实现 %s%s",
            code_now["parse_impl"],
            code_now["raster_impl"],
            (
                "（上次 读取 %s / 绘制 %s：%s）"
                % (
                    previous.get("parse_impl", "?"),
                    previous.get("raster_impl", "?"),
                    "没变" if dict(previous) == code_now else "变了，受影响的图会重画",
                )
                if previous
                else "（没有上次记录）"
            ),
        )
    drawn = skipped = failed = 0

    # 每个证据包要画的任务。默认一个（`--task-id` 指定，或全部混画一张）；
    # `--all-tasks` / `--telemetry-bulk` 时按 `(进程, task_id)` 逐个输出——task_id 在各进程内复用，
    # 只按编号切会把两次不同运行的任务混进同一张图。批量出图必须按任务切：
    # 命中的 issue 才能各出一张完整流程、文件名用问题号。
    drawn_issues: set[str] = set()
    # 现场图来源：问题号 → (遥测卡, 补充日志包)。遥测包没图的 issue 才用得到第二个，
    # 没有日志包命中时是 None（那就只导遥测的那张 / 一张都不导）。
    scene_sources: dict[str, tuple[Any, Case | None]] = {}
    # 本次保留的产物名（画成功 + 按轨迹跳过的）：批量清理时只有"不在这个集合里"的旧产物会被删。
    # 单包 / --all-tasks 不清理，也就不传它。
    kept: set[str] = set()
    # 本次什么都没画出来的 issue：清理时保住它们的旧图（重画失败时那张是唯一能看的）
    failed_issues: set[str] = set()
    jobs: list[tuple[Case, FlowOptions]] = []
    for case in cases:
        if args.all_tasks or args.telemetry_bulk:
            try:
                tasks = scan_tasks(case.log_source(), options.t_from, options.t_to)
            except (OSError, TypeError) as exc:
                failed += 1
                log.error("错误：%s: 列任务失败：%s", case.label, exc)
                continue
            if not tasks:
                log.info("提示：%s 里没有可画的任务，跳过", case.label)
                continue
            for task in tasks:
                jobs.append((case, replace(options, task_id=task.task_id, px=task.px)))
        else:
            jobs.append((case, options))

    telemetry_shown = 0
    version_skipped = 0
    for case, case_options in jobs:
        annotation = None
        if args.telemetry:
            annotation = telemetry.annotate(
                case.log_source(),
                index=telemetry_data,
                offline=args.telemetry_offline,
                issue=args.telemetry_issue,
                task_id=case_options.task_id,
                days=args.telemetry_days,
                gap=args.telemetry_gap,
            )
            for warning in annotation.warnings:
                log.info("提示：%s: %s", case.label, warning)
            if args.telemetry_only:
                # 预览也只列合条件的：否则 --telemetry-only 与真跑出来的那批对不上
                matches = (
                    [m for m in annotation.matches if version_filter.matches(m.card.release)]
                    if version_filter is not None
                    else annotation.matches
                )
                print(f"\n== {case.label} ==", file=sys.stdout)
                telemetry.format_matches(matches, sys.stdout)
                telemetry_shown += 1
                continue
            if (
                version_filter is not None
                and annotation.chosen
                and annotation.card is not None
                and not version_filter.matches(annotation.card.release)
            ):
                # 只认「已经确定身份」的包（高置信命中或显式 --telemetry-issue，`chosen` 才非空）：
                # 中/低置信的节点名匹配只进注记，不能凭它把用户点名要画的包丢掉
                log.info(
                    "版本筛选 %s：%s 命中的 %s 是 %s，不在范围内，跳过",
                    version_filter.describe(),
                    case.label,
                    annotation.chosen,
                    label_of(annotation.card.release),
                )
                version_skipped += 1
                continue
            case_options = _with_telemetry(case_options, annotation)
        # 本地 release 辅助：证据包认得出来就换成本地完整日志 + 本地图片（认不出不动）
        assisted = None
        if local is not None:
            task_hint = case_options.task_id
            if task_hint is None and annotation is not None and annotation.card is not None:
                task_hint = _evidence_task_id(case, annotation.card)[0]
            assisted = _assist_pack(
                case,
                case_options,
                task_hint,
                local,
                label=(
                    annotation.card.short_id
                    if annotation is not None and annotation.card is not None
                    else case.label
                ),
                event_id=(
                    annotation.card.event_id
                    if annotation is not None and annotation.card is not None
                    else ""
                ),
            )
        rendered_ok = False
        rendered_case = case
        last_error: Exception | None = None
        for render_case, render_options in (
            [assisted, (case, case_options)] if assisted else [(case, case_options)]
        ):
            try:
                result = run_case(
                    render_case,
                    render_options,
                    outdir=args.outdir,
                    mermaid=args.mermaid,
                    force=args.force,
                    manifest=manifest,
                    vision_override=args.vision,
                    on_error_override=args.on_error,
                    origin="bulk" if args.telemetry_bulk else "case",
                    kept=kept if args.telemetry_bulk else None,
                    issue_label=(
                        annotation.card.label
                        if annotation is not None and annotation.card is not None
                        else ""
                    ),
                )
            except (FileNotFoundError, ValueError, OSError) as exc:
                last_error = exc
                if render_case is not case:
                    log.warning(
                        "本地 release 日志画不出流程（%s），改回证据包：%s",
                        case.label,
                        exc,
                    )
                continue
            rendered_ok = True
            rendered_case = render_case
            break
        if not rendered_ok:
            failed += 1
            log.error("错误：%s: %s", case.label, last_error)
            continue
        drawn += result is not None
        skipped += result is None
        if annotation is not None and annotation.chosen and annotation.card is not None:
            # 这个任务对应的 issue：现场图统一在画图之后导（遥测优先，日志包兜底）。
            # 兜底用**实际画成的那份**日志包：本地 release 认出来时就是它的 `on_error/`
            scene_sources[annotation.chosen] = (annotation.card, rendered_case)
        if result is not None and annotation is not None and annotation.chosen:
            # 这个任务对应的问题号已经被日志包出成完整流程了，概览卡就跳过它
            drawn_issues.add(annotation.chosen)
    overview_drawn = 0
    evidence_drawn = 0
    overview_skipped = 0
    evidence_skipped = 0
    if args.telemetry_bulk:
        if not cards:
            if version_filter is not None:
                log.warning(
                    "版本筛选后没有可画的 issue（%s）：放宽 --telemetry-version 或去掉它再跑。",
                    version_filter.describe(),
                )
            else:
                log.warning(
                    "本地遥测库是空的，没东西可画。若还没配 Sentry CLI，先把 SENTRY_CONFIG_DIR "
                    "指到含可写 cli.db 的目录，再跑 "
                    "`python -m tools.sentry.cli telemetry --refresh`（需联网）。"
                )
        if sentry_down:
            log.info(
                "提示：Sentry 不可用，本次不逐 issue 拉取证据包；本地已有包的照常画，缺包的出概览卡"
            )
        for card in cards:
            if card.short_id in drawn_issues:
                continue  # 已经被日志包出成完整流程了
            scene_sources.setdefault(card.short_id, (card, None))
            # 本地没有证据包就自动拉取（离线 / --no-fetch-evidence / Sentry 没登录时跳过）
            if (
                not _has_evidence(card.short_id)
                and args.fetch_evidence
                and not args.telemetry_offline
                and not sentry_down
            ):
                _fetch_evidence(card.short_id)
            # 证据包里有日志就画完整流程，不再按标题里有没有 <节点>/<阶段> 挑：
            # 缺后缀只是标题写法（PYTHON-V / PYTHON-11 实测，日志尾部能画出 17 节点流程）。
            # 画不出时才退概览卡：没有日志、尾部解析不出流程、定位不到失败 task（下面两处）。
            ev_case = _evidence_case(card.short_id)
            # 版本筛选下，本地包可能是这个 issue 早先那次运行的（issue 跨多个版本）：
            # 包内 release 不符就重拉最新事件，重拉不到才退概览卡（见 `_usable_evidence_case`）
            stale_pack = False
            if ev_case is not None and version_filter is not None:
                ev_case = _usable_evidence_case(
                    card,
                    ev_case,
                    version_filter,
                    allow_fetch=(
                        args.fetch_evidence
                        and not args.telemetry_offline
                        and not sentry_down
                    ),
                )
                stale_pack = ev_case is None
            if ev_case is not None:
                task_id, task_count = _evidence_task_id(ev_case, card)
                if task_id is None and task_count > 1:
                    log.warning(
                        "证据包里有 %s 个 task，定位不到失败 task（%s），退概览卡",
                        task_count,
                        card.short_id,
                    )
                else:
                    ev_options = replace(
                        options,
                        issue=card.short_id,
                        # 只画失败的那个 task：证据包尾部常连着同一次运行的多个 task
                        # （Train → Escort → Battle → Training），整段画会接成一条链。
                        task_id=task_id,
                        px=None,
                        t_from=None,
                        t_to=None,
                    )
                    dispatcher = _terminal_dispatcher(ev_case, card, task_id)
                    if dispatcher and task_id is not None:
                        stem_extra = f"{dispatcher}_task{task_id}"
                    else:
                        stem_extra = dispatcher
                    # 本地 release 里认得出同一次运行就换成本地完整日志 + 本地图片；
                    # 本地日志画不出就退回证据包（两条路都失败才退概览卡）
                    assisted = _assist_pack(
                        ev_case,
                        ev_options,
                        task_id,
                        local,
                        label=card.short_id,
                        event_id=getattr(card, "event_id", ""),
                    )
                    rendered_ok = False
                    rendered_case = ev_case
                    last_error: Exception | None = None
                    for render_case, render_options in (
                        [assisted, (ev_case, ev_options)]
                        if assisted
                        else [(ev_case, ev_options)]
                    ):
                        try:
                            res = run_case(
                                render_case,
                                render_options,
                                outdir=args.outdir,
                                mermaid=args.mermaid,
                                force=args.force,
                                manifest=manifest,
                                vision_override=None,
                                on_error_override=None,
                                issue_only=True,
                                stem_extra=stem_extra,
                                origin="bulk",
                                kept=kept,
                                issue_label=getattr(card, "label", ""),
                            )
                        except (FileNotFoundError, ValueError, OSError) as exc:
                            last_error = exc
                            if render_case is not ev_case:
                                log.warning(
                                    "本地 release 日志画不出流程（%s），改回证据包：%s",
                                    card.short_id,
                                    exc,
                                )
                            continue
                        rendered_ok = True
                        rendered_case = render_case
                        evidence_drawn += res is not None
                        evidence_skipped += res is None
                        break
                    if rendered_ok:
                        # 现场图兜底用**实际画成的那份**日志包：本地 release 认出来时是它
                        scene_sources[card.short_id] = (card, rendered_case)
                        continue
                    # 证据包在但解析不出流程（例如日志尾部没有 pipeline 步骤）：
                    # 退概览卡，而不是把这条 issue 整个丢掉
                    log.warning(
                        "证据包画不出流程（%s），退概览卡：%s",
                        card.short_id,
                        last_error,
                    )
            try:
                res = run_overview(
                    card,
                    outdir=args.outdir,
                    mermaid=args.mermaid,
                    force=args.force,
                    manifest=manifest,
                    no_log_hint=ev_case is None and not stale_pack and not sentry_down,
                    origin="bulk",
                    kept=kept,
                )
            except (OSError, ValueError) as exc:
                failed += 1
                log.error("错误：%s: %s", card.short_id, exc)
                continue
            overview_drawn += res is not None
            overview_skipped += res is None
            if not any(
                manifest["cases"].get(item, {}).get("issue") == card.short_id
                for item in kept
            ):
                # 这条 issue 这次什么都没画出来（渲染失败 / 缺 Pillow / 证据包画不出流程又退不回卡）：
                # 清理时保住它的旧图——重画失败的那次，目录里那张是唯一还能看的
                failed_issues.add(card.short_id)

    # 现场图：把遥测证据包里这个 issue 的失败现场（离卡片上的失败时刻最近的那几张）复制到
    # `<outdir>/images/`；遥测包没图时用补充日志包里对得上的那张，名字带 `_日志包`。
    # 放在所有画图之后统一做：日志包那条分支画完就 `continue` 了，导出得等整轮跑完。
    scene_kept: set[str] = set()
    scene_total = 0
    for issue in sorted(scene_sources):
        card, pack_case = scene_sources[issue]
        try:
            names = _scene_for(
                card,
                pack_case,
                args.outdir,
                manifest,
                "bulk" if args.telemetry_bulk else "case",
            )
        except (OSError, ValueError) as exc:
            log.warning("提示：%s 的现场图导出失败（%s），这张不导", issue, exc)
            continue
        scene_kept.update(names)
        scene_total += len(names)
    if scene_total:
        log.info("现场图：%d 张 → %s", scene_total, scene_dir(args.outdir))

    pruned = 0
    pruned_scene = 0
    if args.telemetry_bulk and not args.telemetry_only:
        if args.clean_flow:
            log.info("提示：--clean-flow 已经清空过输出目录，本次不再单独清理")
        elif not args.prune:
            log.info("提示：--no-prune，本次不清理旧产物")
        elif cards:
            pruned = prune_artifacts(
                args.outdir,
                manifest,
                kept,
                issues={card.short_id for card in cards},
                failed_issues=failed_issues,
                labels=labels_by_issue,
            )
            if pruned:
                log.info(
                    "清理旧产物：%d 个（不在本次 %d 个 issue 集合内、或已被本次重画取代）",
                    pruned,
                    len(cards),
                )
            pruned_scene = prune_scene(
                args.outdir,
                manifest,
                scene_kept,
                issues={card.short_id for card in cards},
                keep_issues=failed_issues,
            )
            if pruned_scene:
                log.info(
                    "清理旧现场图：%d 张（不在本次 issue 集合内、或已被本次取代）",
                    pruned_scene,
                )
        else:
            # 库是空的 / 版本筛完为空：不能按"空集合"把目录清空，那通常意味着刷新失败或条件写错
            log.info("提示：本次没有可画的 issue，跳过旧产物清理")
        manifest["schema"] = 2
        manifest["signature"] = code_now
        manifest["bulk"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "version_filter": list(args.telemetry_version or []),
            "issues": [card.short_id for card in cards],
            "pruned": pruned,
        }
    save_manifest(args.outdir, manifest)
    if args.telemetry_only:
        print(f"\n共 {telemetry_shown} 个证据包（未画图）。")
        return 0
    tail = f"，失败 {failed}" if failed else ""
    parts = [
        f"绘制 {drawn} 个",
        f"跳过 {skipped + evidence_skipped + overview_skipped} 个",
    ]
    if version_skipped:
        parts.append(f"版本筛选排除 {version_skipped} 个")
    if evidence_drawn:
        parts.append(f"证据包流程 {evidence_drawn} 个")
    if overview_drawn:
        parts.append(f"概览 {overview_drawn} 个")
    if pruned:
        parts.append(f"清理 {pruned} 个旧产物")
    if pruned_scene:
        parts.append(f"清理 {pruned_scene} 张旧现场图")
    if scene_total:
        parts.append(f"现场图 {scene_total} 张")
    print(
        f"\n完成：{('，'.join(parts))}{tail}；"
        f"manifest → {os.path.join(args.outdir, MANIFEST_NAME)}"
    )
    return 1 if failed and not drawn else 0

