#!/usr/bin/env python
"""把 MaaFramework 的 maafw.log 还原成"实际执行流程图"。

输入：一份 maafw.log（`[时间][级别][Px..][Tx..][文件][L行][函数] 消息` 格式）+ 可选的 vision 目录
（`debug/vision/<时间>_<节点名>_<reco_id>.jpg`，里面是识别时的截图、ROI 框和模板缩略图）。
输出：一张按时间线排列的 PNG 流程图。

布局约定：
  * 横向 = 轮次（round），从左到右，**不换行**：宽度不够就一直往右延伸（要换行用 --cols-per-band N）；
    纵向 = 一轮内的执行顺序，从上到下。
  * 端口（--ports pos，默认）按相邻方向定：
      - 同一列里上下相邻的节点 → **上入下出**（出口 = 下中，入口 = 上中）；
      - 跨轮左右相邻的节点 → **左入右出**（出口 = 右中，入口 = 左中，竖线走两列之间的空档）。
    --ports role 保留旧规则：重要节点（跨轮重复出现）左下出/右上进，父节点（有 next 的派发者）
    左下出/右下进。
  * 未命中/超时/失败这类框之间留更大的间距（--gap-error），不跟正常步骤挤在一起。

贴图（都可以不给，也可以只给其中一半）：
  * --vision 目录里的 `<时间>_<节点名>_<reco_id>.jpg` 按 reco_id 精确对齐，裁出当时的识别区域贴进框；
  * 未命中的聚合框贴"最后一次尝试"那张；框架没存图的节点（Custom / DirectHit）退到同节点的
    `*_wait_freezes.jpg`、再退到同一时刻的邻近帧，角标写"邻近帧"；
  * --on-error 目录里的 `<时间>_<节点名>.png` 是失败现场图：优先按名字贴回**被记录的那个节点**，
    同时贴到超时/失败事件框上；角标是**红底白字的"失败现场 HH:MM:SS.mmm"**（时刻取自文件名，
    直接对上 `on_error/<文件>`），缩略图另加红框——同一次失败里十几张识别帧是同一个画面的截图，
    只有这几张是失败那一刻。贴回同名节点限"时间上紧挨着"（默认 8 s），隔几分钟的另一次失败不贴；
  * 连 --vision 都不给也能出图（纯节点流程图）；只给 --on-error 时，那张/那几张现场图会贴到对应节点上；
  * --vision / --on-error 既能指目录本身，也能指它们的父目录（例如 `debug/`），子目录自动识别。

用法示例：
  python tools/drawing/cli.py flow D:/MaaAshArms/debug/maafw.log \
      --vision D:/MaaAshArms/debug/vision \
      --from "2026-10-11 21:39:56" --to "2026-10-11 21:43:29" \
      --out escort.png

只做解析、不画图：加 --list。

输出默认落到**仓库根的 `temp/flow/`**（不再写到日志旁边）；`--out` / `--outdir` 可覆盖。

作为库调用（遥测侧就这么用——远程没有本地日志文件，只有 Sentry 取回的日志正文）：

  from tools.drawing.lib.flow_from_log import FlowOptions, MediaResolver, render_flow

  # 日志正文可以直接传字符串；图片证据可以是目录、zip/多卷、或内存里的字节
  res = render_flow(log_text,
                    vision=MediaResolver(["log-part01.zip", "log-part02.zip"]),
                    options=FlowOptions(t_from=t0, t_to=t1, task_id=200000118))
  print(res.path, res.width, res.height, res.steps)

批量与幂等（同一个证据包重跑会跳过）用 `python tools/drawing/cli.py cases`，
经 `python -m tools.sentry.cli flow` 也能调用，产出目录一致。

**远程遥测拿不到 vision 识别帧**：MFA 的失败附件显式跳过 `vision/`，且图片包上限
5 MiB（本机 `debug/vision` 有 248.6 MB）。所以从 Sentry 取回的证据只能画
"节点循环 + on_error 失败现场图"；要看识别帧得让对方手动导出（勾"包含 vision 图片"）。

实现分了哪几个模块（本文件只剩入口：sys.path 引导 + re-export + main）：
  flowlog/logparse.py  日志 → ParsedLog（行切分、事件匹配、仅事件模式合成、任务扫描）
  flowlog/model.py     Step / ParsedLog / Dispatcher 等数据结构与时间戳小工具
  flowlog/constants.py 日志正则、渲染器版本、候选/派发展示上限
  flowlog/layout.py    折叠、分轮、父节点分组、重复轮签名
  flowlog/media.py     vision / on_error / zip 条目与 MediaResolver
  flowlog/paint.py     字体、调色板、文本度量
  flowlog/draw.py      像素绘制（draw_flow）
  flowlog/notes.py     诊断注记（Note）
  flowlog/stale.py     「疑似过时」检查（走 tools/_pipeline_state.py）
  flowlog/render.py    render_flow / render_overview / FlowOptions / FlowResult
  flowlog/cli.py       argparse 与 main
  flowlog/paths.py     仓库根与默认产出目录
"""

from __future__ import annotations

import sys
from pathlib import Path

# 共享实现（git / JSONC / 节点定义）在 `tools/_pipeline_state.py`。直接跑本文件时
# 仓库根不在 `sys.path` 上，这里先补一次（与 `tools/perf/lib/rounds.py` 同一写法）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.drawing.lib.flowlog import (
    DEFAULT_OUTDIR,
    RENDERER_VERSION,
    VERSION_SOURCE_CLI,
    VERSION_SOURCE_TELEMETRY,
    FlowOptions,
    FlowResult,
    MediaResolver,
    Note,
    _is_zip,
    parse_log,
    parse_time,
    render_flow,
    render_overview,
    scan_tasks,
    stale_summary,
    _first_log_ts,
    _open_log_lines,
    build_notes,
    describe_empty_window,
    round_steps,
    parent_groups,
    parse_nextlist,
    resolve_parent,
    stall_notes,
    Step,
    ColumnSpan,
    MediaEntry,
    backfill_failure_thumbs,
    column_signature,
    member_clock,
    overlay_repeats,
    window_failure_images,
    LINE_RE,
    NODE_DONE_RE,
    TRUNCATION_HEADS,
    parse_ts,
    main,
)


__all__ = [
    "DEFAULT_OUTDIR",
    "RENDERER_VERSION",
    "VERSION_SOURCE_CLI",
    "VERSION_SOURCE_TELEMETRY",
    "FlowOptions",
    "FlowResult",
    "MediaResolver",
    "Note",
    "_is_zip",
    "parse_log",
    "parse_time",
    "render_flow",
    "render_overview",
    "scan_tasks",
    "stale_summary",
    "_first_log_ts",
    "_open_log_lines",
    "build_notes",
    "describe_empty_window",
    "round_steps",
    "parent_groups",
    "parse_nextlist",
    "resolve_parent",
    "stall_notes",
    "Step",
    "ColumnSpan",
    "MediaEntry",
    "backfill_failure_thumbs",
    "column_signature",
    "member_clock",
    "overlay_repeats",
    "window_failure_images",
    "LINE_RE",
    "NODE_DONE_RE",
    "TRUNCATION_HEADS",
    "parse_ts",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
