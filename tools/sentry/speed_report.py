"""跨版本速度报告：把「任务 × 版本」的耗时聚合变成可读的表与对比判定。

``fetch --field "p50(span.duration)" ...`` 取回的聚合行（`temp/speed/*.json`）是
「任务 × 版本」两维的矩阵。本模块做三件事，每件对应一个命令行开关：

1. **截断**（``--last N``，默认 5）：只保留最近 N 个版本。全量 434 行的矩阵没人读，
   实测最近 5 版是 55 行；x 轴按版本号排序而不是按出现顺序，跳号的版本（beta.39-ci.*
   之类）保持在其真实位置。
2. **样本量门槛**（``--min-count``，默认 5）：p50 在 ``n=1`` 时没有稳定含义（实测相邻版本
   波动中位数 95.4%、最大 11737%），不足门槛的版本**输出留空而不是给数字**——空着比
   画一条假趋势诚实。门槛不删行，行还在表里，只是标 ``样本不足``。
3. **重构前后对比**（``--compare A..B``）：输出两版的 p50/p95 比值与判定
   （改善 / 变慢 / 噪声内）。判据来自 FrontierRefactor 的教训：1080 次重构里 21.9% 的
   "提升"落在测量噪声内，必须把 "noise-bound" 与 "变慢" 分开报。

为未来用户量增长留的余地（当前 25 任务 × 39 版本，装机量小、样本稀疏）：

- ``min-count`` 是参数而不是常量：样本上来后调高门槛即可，报告结构不变。
- 判定阈值 ``NOISE_FACTOR`` 是参数：样本多后可以收紧到 1.1 或 1.05。
- 表格输出与绘图输出共用同一份 ``SpeedReport`` 数据类，用户量大了以后再加
  ``--format`` 出口不需要动判定逻辑。
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

try:
    from .config import CONFIG
except ImportError:
    from config import CONFIG

try:
    from .report_common import (
        load_artifact,
        module_summary,
        release_version_key,
        row_count,
        version_label,
        write_console_table,
    )
except ImportError:
    from report_common import (
        load_artifact,
        module_summary,
        release_version_key,
        row_count,
        version_label,
        write_console_table,
    )

# 默认样本量门槛：低于它的版本给「样本不足」。**默认 2 是被实测数据定的**：
# 30 天窗口里 434 个业务格子有 61.5% 是 n=1、只有 10.4% 达到 n≥5；门槛定 5 等于什么都不显示。
# 未来用户量上来后按 config.json 的 min_samples 同步调即可（这里刻意不读 config，
# 因为速度与失败率的口径不同——耗时阈值要跟着平台噪声调，不跟业务口径走）。
DEFAULT_MIN_COUNT = 2

# 截断窗口：只看最近 N 个版本。全量 434 行没人读；默认 8 版是实测里唯一还能看到
# 一些 n≥3 数据的窗口（最近 5 版全是 n=1~2）。0 = 全部。
DEFAULT_LAST = 8

# 噪声判定：新版 p50 与旧版比值落在 [1/NOISE_FACTOR, NOISE_FACTOR] 内算"噪声内"。
# FrontierRefactor 的教训：21.9% 的"提升"其实是噪声，必须与"变慢"分开报。
# 样本量上来后可收紧到 1.1（前两个数字就是按这个量级预留给参数的）。
DEFAULT_NOISE_FACTOR = 1.15

# 对比判定要求两侧样本都比 min_count 更足（默认 3）。理由见 CompareVerdict：
# 实测 Gift 从 128.4s 到 801ms 判出 0.01x「改善」，但两侧只有 n=4 / n=3——那几趟
# 跑的很可能不是同一关卡。样本不够时给「证据不足」而不是方向。
DEFAULT_COMPARE_MIN_COUNT = 3

MEASUREMENT_SPEED = "task_speed_op"
DURATION_COLUMNS = ("p50(span_duration)", "p95(span_duration)", "avg(span_duration)")


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class SpeedCell:
    """一个「任务 × 版本」格子的耗时与样本量。"""

    task: str
    version: str          # 展示用短标签，如 v1.0.0-beta.49
    p50: float
    p95: float
    avg: float
    count: int
    release: str = ""     # 原始 release 串；排序键只能从它算（RELEASE_PATTERN 带包名前缀）

    @property
    def enough_samples(self) -> bool:
        return self.count >= 0 and self.count > 0

    @property
    def sort_key(self) -> tuple | None:
        return release_version_key(self.release) if self.release else None


@dataclass(frozen=True)
class CompareVerdict:
    """两版耗时的比值判定：改善 / 变慢 / 噪声内 / 证据不足。

    ``verdict`` 的 ``insufficient`` 是第四种：两侧样本有一边低于 ``compare_min_count`` 时，
    比值算出来了也不能当结论——实测 ``Gift`` 从 128.4s 到 801ms（0.01x）看着像快了 160 倍，
    但两边样本只有 n=4 / n=3，更可能是那几趟跑的不是同一关卡。样本不够时**不硬给方向**。
    """

    task: str
    base_version: str
    new_version: str
    base_p50: float
    new_p50: float
    base_p95: float
    new_p95: float
    ratio: float
    verdict: str          # improved | regressed | noise | insufficient
    base_count: int = 0
    new_count: int = 0

    @property
    def enough_samples(self) -> bool:
        return self.verdict != "insufficient"

    @property
    def verdict_label(self) -> str:
        return {
            "improved": "改善",
            "regressed": "变慢",
            "noise": "噪声内",
            "insufficient": "证据不足",
        }[self.verdict]


@dataclass(frozen=True)
class SpeedReport:
    """一份速度报告：截断后的矩阵 + 对比判定。表格与绘图共用它。"""

    tasks: tuple[str, ...]
    versions: tuple[str, ...]                  # 排序后的版本标签（升序）
    cells: dict[tuple[str, str], SpeedCell]
    min_count: int
    noise_factor: float
    compare_min_count: int = DEFAULT_COMPARE_MIN_COUNT
    compares: tuple[CompareVerdict, ...] = field(default_factory=tuple)
    skipped_versions: tuple[str, ...] = field(default_factory=tuple)   # 无法解析版本号的 release

    def cell(self, task: str, version: str) -> SpeedCell | None:
        return self.cells.get((task, version))

    def enough(self, cell: SpeedCell) -> bool:
        return cell.count >= self.min_count


# ---------------------------------------------------------------- 解析


def _is_system_row(task: str) -> bool:
    """框架伪任务（`连接` / `开始前脚本` / `性能基准` / `结束后脚本`）不是用户任务。

    `task-failure` 用 `config.json` 的 `system_labels` 排掉同一批名字，这里复用同一份配置，
    免得两处口径分叉（它们耗时恒为毫秒级，混进任务表会把"哪个任务变慢了"读错）。
    """
    if task.startswith("mfa."):
        return True
    return task in CONFIG.system_labels or task in CONFIG.ignored_descriptions


def parse_duration_rows(rows: Sequence[dict]) -> dict[tuple[str, str], SpeedCell]:
    """把聚合行变成 {(任务, 版本标签): SpeedCell}；系统伪任务与解析不出版本号的行丢弃。"""
    cells: dict[tuple[str, str], SpeedCell] = {}
    for row in rows:
        task = row.get("description")
        release = row.get("release")
        if not task or not release:
            continue
        if _is_system_row(task):
            continue
        label = version_label(release)
        if label is None:
            continue
        count = row_count(row) or 0
        p50 = _duration(row, "p50(span_duration)")
        p95 = _duration(row, "p95(span_duration)")
        avg = _duration(row, "avg(span_duration)")
        if p50 is None:
            continue
        cells[(task, label)] = SpeedCell(
            task=task, version=label, p50=p50,
            p95=p95 if p95 is not None else p50,
            avg=avg if avg is not None else p50,
            count=count,
            release=release,
        )
    return cells


def _duration(row: dict, key: str) -> float | None:
    """取一个耗时聚合列；Sentry 对毫秒级 span 会返回秒（实测 p50=1.166 对应 1.17ms），
    这里不做单位换算，报告头部标注原始单位即可。"""
    value = row.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def sort_versions(cells: dict[tuple[str, str], SpeedCell]) -> tuple[list[str], tuple[str, ...]]:
    """按语义排序版本标签；返回 (排好的标签, 解析不出排序键的标签)。

    排序键从**原始 release** 算（`RELEASE_PATTERN` 带 `包名@` 前缀，喂短标签会全部失配
    从而退化成字典序——那样 `beta.7` 会排到 `beta.49` 后面）。解析不出的排到最后保持字典序。
    """
    keys: dict[str, tuple | None] = {}
    for (_task, version), cell in cells.items():
        keys.setdefault(version, cell.sort_key)
    parsed = sorted(
        ((key, label) for label, key in keys.items() if key is not None),
    )
    unparsed = sorted(label for label, key in keys.items() if key is None)
    return [label for _key, label in parsed] + unparsed, tuple(unparsed)


def build_speed_report(
    rows: Sequence[dict],
    *,
    last: int = DEFAULT_LAST,
    min_count: int = DEFAULT_MIN_COUNT,
    noise_factor: float = DEFAULT_NOISE_FACTOR,
    compare_min_count: int = DEFAULT_COMPARE_MIN_COUNT,
    task_filter: str | None = None,
    compare: str | None = None,
) -> SpeedReport:
    """从聚合行构建报告：解析 → 排序 → 截断 → 组装。"""
    cells = parse_duration_rows(rows)

    tasks = sorted({task for task, _version in cells})
    if task_filter:
        wanted = {t.strip() for t in task_filter.split(",") if t.strip()}
        tasks = [task for task in tasks if task in wanted]

    all_versions, skipped = sort_versions(cells)
    versions = all_versions[-last:] if last > 0 else all_versions

    kept = {
        (task, version): cell
        for (task, version), cell in cells.items()
        if task in tasks and version in versions
    }

    compares: tuple[CompareVerdict, ...] = ()
    if compare:
        compares = build_compares(kept, tasks, versions, compare, compare_min_count, noise_factor)

    return SpeedReport(
        tasks=tuple(tasks),
        versions=tuple(versions),
        cells=kept,
        min_count=min_count,
        noise_factor=noise_factor,
        compare_min_count=compare_min_count,
        compares=compares,
        skipped_versions=skipped,
    )


def build_compares(
    cells: dict[tuple[str, str], SpeedCell],
    tasks: Sequence[str],
    versions: Sequence[str],
    compare: str,
    compare_min_count: int,
    noise_factor: float,
) -> tuple[CompareVerdict, ...]:
    """解析 ``A..B``（两个版本标签），对每个任务输出比值判定。

    判定要**两侧都过 `compare_min_count`** 才给方向，否则一律 `insufficient`。
    """
    if ".." not in compare:
        raise ValueError(f"--compare 需要 A..B 形式（两个版本标签），得到 {compare!r}")
    base_label, new_label = (part.strip() for part in compare.split("..", 1))
    if base_label not in versions or new_label not in versions:
        known = "、".join(versions)
        raise ValueError(
            f"--compare 的版本不在截断窗口内：{base_label} / {new_label}。窗口内可选：{known}"
        )

    verdicts: list[CompareVerdict] = []
    for task in tasks:
        base = cells.get((task, base_label))
        new = cells.get((task, new_label))
        if base is None or new is None:
            continue
        ratio = new.p50 / base.p50 if base.p50 > 0 else float("inf")
        enough = base.count >= compare_min_count and new.count >= compare_min_count
        if not enough:
            verdict = "insufficient"
        elif ratio > noise_factor:
            verdict = "regressed"
        elif ratio < 1.0 / noise_factor:
            verdict = "improved"
        else:
            verdict = "noise"
        verdicts.append(
            CompareVerdict(
                task=task,
                base_version=base_label,
                new_version=new_label,
                base_p50=base.p50,
                new_p50=new.p50,
                base_p95=base.p95,
                new_p95=new.p95,
                ratio=ratio,
                verdict=verdict,
                base_count=base.count,
                new_count=new.count,
            )
        )
    return tuple(verdicts)


# ---------------------------------------------------------------- 输出


def format_ms(value: float) -> str:
    """毫秒值的人读格式：小于 1s 用 ms，否则用 s（保留 1 位小数）。"""
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.1f}ms"


def _cell_text(cell: SpeedCell | None, min_count: int) -> str:
    if cell is None:
        return "-"
    if cell.count < min_count:
        return f"n={cell.count}"
    return format_ms(cell.p50)


def _verdict_cell(verdict: CompareVerdict) -> str:
    """判定栏文字；证据不足时把两侧样本数一起打出来，省一次翻表。"""
    if verdict.verdict == "insufficient":
        return f"证据不足（n={verdict.base_count}/{verdict.new_count}）"
    return verdict.verdict_label


def _coverage(report: SpeedReport) -> tuple[int, int]:
    """窗口内 (够样本的格子数, 总格子数)。表格底部要如实报出来——
    当前装机量下样本极稀（实测 61.5% 是 n=1），不给这个数读者会以为空格是"没有这个版本"。
    """
    total = len(report.tasks) * len(report.versions)
    filled = sum(1 for cell in report.cells.values() if report.enough(cell))
    return filled, total


def write_console(report: SpeedReport, output) -> None:
    """表格：任务为行、版本为列，格子是 p50（样本不足标 n=N），另有对比判定表。"""
    headers = ["任务", *[f"{version}" for version in report.versions]]
    rows: list[list[str]] = []
    for task in report.tasks:
        rows.append([task, *(_cell_text(report.cell(task, v), report.min_count) for v in report.versions)])
    write_console_table(headers, rows, output, right_aligned=set(range(1, len(headers))))
    _write_coverage(report, output)

    if report.compares:
        output.write(
            "\n重构前后对比（p50 比值；阈值 x{:.2f}；两侧都要 n≥{} 才给方向）\n".format(
                report.noise_factor, report.compare_min_count
            )
        )
        write_console_table(
            ["任务", "基线", "新版", "基线 p50", "新版 p50", "比值", "判定"],
            [
                [
                    c.task, c.base_version, c.new_version,
                    format_ms(c.base_p50), format_ms(c.new_p50),
                    f"{c.ratio:.2f}x", _verdict_cell(c),
                ]
                for c in report.compares
            ],
            output,
            right_aligned={3, 4, 5},
        )


def _write_coverage(report: SpeedReport, output) -> None:
    """底部提示样本密度与未解析版本；样本稀是当前常态，必须说出来而不是让读者自己发现。"""
    filled, total = _coverage(report)
    parts = [f"样本密度：{filled}/{total} 个格子达到 n≥{report.min_count}"]
    if report.skipped_versions:
        parts.append("无法解析版本号已排到最后：" + "、".join(report.skipped_versions))
    output.write("> " + "；".join(parts) + "\n")


def write_markdown(report: SpeedReport, output) -> None:
    headers = ["任务", *report.versions]
    output.write("| " + " | ".join(headers) + " |\n")
    output.write("|" + "---|" * len(headers) + "\n")
    for task in report.tasks:
        cells = [_cell_text(report.cell(task, v), report.min_count) for v in report.versions]
        output.write("| " + " | ".join([task, *cells]) + " |\n")
    filled, total = _coverage(report)
    output.write(f"\n> 样本密度：{filled}/{total} 个格子达到 n≥{report.min_count}\n")

    if report.compares:
        output.write(
            "\n### 重构前后对比（p50 比值；阈值 x{:.2f}；两侧都要 n≥{} 才给方向）\n\n".format(
                report.noise_factor, report.compare_min_count
            )
        )
        output.write("| 任务 | 基线 | 新版 | 基线 p50 | 新版 p50 | 比值 | 判定 |\n")
        output.write("|---|---|---|---:|---:|---:|---|\n")
        for c in report.compares:
            output.write(
                f"| {c.task} | {c.base_version} | {c.new_version} | {format_ms(c.base_p50)} "
                f"| {format_ms(c.new_p50)} | {c.ratio:.2f}x | {_verdict_cell(c)} |\n"
            )


def write_json(report: SpeedReport, output) -> None:
    import json

    json.dump(
        {
            "versions": list(report.versions),
            "tasks": list(report.tasks),
            "min_count": report.min_count,
            "noise_factor": report.noise_factor,
            "cells": [
                {
                    "task": (task, version)[0],
                    "version": (task, version)[1],
                    "p50": cell.p50,
                    "p95": cell.p95,
                    "avg": cell.avg,
                    "count": cell.count,
                    "enough_samples": report.enough(cell),
                }
                for (task, version), cell in report.cells.items()
            ],
            "compares": [c.__dict__ | {"verdict_label": c.verdict_label} for c in report.compares],
        },
        output,
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------- CLI


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or "python -m tools.sentry.cli speed",
        description=module_summary(__doc__),
    )
    parser.add_argument("--input", help="fetch 落盘的速度聚合工件（.json）；不给则读 stdin")
    parser.add_argument(
        "--last", type=int, default=DEFAULT_LAST,
        help=f"只看最近 N 个版本（默认 {DEFAULT_LAST}；0 = 全部）",
    )
    parser.add_argument(
        "--min-count", type=int, default=DEFAULT_MIN_COUNT,
        help=f"样本量门槛：count 低于它的版本显示 n=N 而不是耗时（默认 {DEFAULT_MIN_COUNT}）",
    )
    parser.add_argument(
        "--noise-factor", type=float, default=DEFAULT_NOISE_FACTOR,
        help=f"重构对比的噪声阈值：比值落在 [1/f, f] 内算噪声（默认 {DEFAULT_NOISE_FACTOR}）",
    )
    parser.add_argument("--task", help="只看这些任务，逗号分隔（默认全部）")
    parser.add_argument(
        "--compare", metavar="A..B",
        help="对比两个版本的耗时，输出比值与判定（改善/变慢/噪声内/证据不足），例如 v1.0.0-beta.46..v1.0.0-beta.49",
    )
    parser.add_argument(
        "--compare-min-count", type=int, default=DEFAULT_COMPARE_MIN_COUNT,
        help=f"对比判定要求两侧各自的最低样本量（默认 {DEFAULT_COMPARE_MIN_COUNT}；不足给「证据不足」）",
    )
    parser.add_argument("--format", choices=("console", "markdown", "json"), default="console")
    parser.add_argument(
        "--chart", metavar="PATH",
        help="同时画 small multiples 图（每任务一格）到 PATH；需要 Pillow",
    )
    return parser


def collect_report(arguments: argparse.Namespace) -> SpeedReport:
    if arguments.input:
        artifact = load_artifact(arguments.input)
        if MEASUREMENT_SPEED not in artifact.measurements:
            available = "、".join(sorted(artifact.measurements)) or "（空）"
            raise ValueError(
                f"工件里没有 measurement '{MEASUREMENT_SPEED}'（speed 需要 span.description / release / "
                f"count_unique(trace) / p50|p95|avg(span.duration) 的聚合行）。现有: {available}"
            )
        rows = artifact.get(MEASUREMENT_SPEED)
    else:
        import json

        rows = json.load(sys.stdin)
        if isinstance(rows, dict):
            rows = rows.get("rows", [])

    return build_speed_report(
        rows,
        last=arguments.last,
        min_count=arguments.min_count,
        noise_factor=arguments.noise_factor,
        compare_min_count=arguments.compare_min_count,
        task_filter=arguments.task,
        compare=arguments.compare,
    )


def main(argv: Sequence[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO，
        # 直接调用会被类型检查报 reportAttributeAccessIssue；isinstance 收窄后静态与运行时都成立。
        # 这是 tools/sentry 各入口的统一写法，不要改回 hasattr。
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")

    arguments = create_argument_parser(prog).parse_args(argv)
    report = collect_report(arguments)

    writers = {"console": write_console, "markdown": write_markdown, "json": write_json}
    writers[arguments.format](report, sys.stdout)

    if arguments.chart:
        # 延迟导入：绘图需要 Pillow，发布包解释器没有它；不画图的路径不受影响。
        try:
            from .speed_charts import write_chart
        except ImportError:
            from speed_charts import write_chart
        write_chart(report, arguments.chart)
        print(f"\n图: {arguments.chart}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
