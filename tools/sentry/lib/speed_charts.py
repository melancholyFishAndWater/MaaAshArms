"""速度趋势的 small multiples 图（每任务一格，共享版本轴）。

为什么是 small multiples 而不是一张多序列折线图：速度数据是「任务 × 版本」的二维矩阵
（实测 25 任务 × 39 版本）。把 25 条序列塞进一张图，没人能读出单条曲线；业界的标准解法是
trellising / small multiples——每条序列独立成格、共享 x 轴（见 CDC COVE 与 Power BI 的
small multiples 文档）。每格的任务名独立标注，所以每格都能单独读。

关键取舍：

- **样本量不足的版本留白，不连线**。p50 在 n=1 时没有稳定含义（实测相邻版本波动中位数
  95.4%），把它画成实心点是在图上放一条假趋势。
- **只画有 ≥2 个达标点的任务**。实测 25 个任务里有一半在窗口内只有 0~1 个数据点，画出来
  是一堆空格子与散点，占地方又不给信息。

依赖 Pillow（与 `tools/drawing` 同一个约束：发布包解释器不含它，本机系统 Python 才有）。
"""

from __future__ import annotations

import os
from typing import Any

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)

MARGIN = 40
PANEL_W = 200
PANEL_H = 110
PANEL_GAP = 26
HEADER = 74
LEGEND = 40
COLUMNS_PER_BAND = 6          # 每行放几格
AXIS_LABELS_PER_BAND = 3      # 每行底轴标几个版本（首/中/尾）


def _load_font(size: int, bold: bool = False):
    """取中文字体；Pillow 只在真正画图时导入，所以 import 本模块不需要它。"""
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fmt(value: float) -> str:
    """面板角落的极值标签：秒级不写小数，免得挤在一起。"""
    if value >= 1000:
        return f"{value / 1000:.0f}s"
    return f"{value:.0f}"


def _task_points(report: Any, task: str) -> list[tuple[int, float, bool]]:
    """一个任务在窗口内各版本的 (版本下标, p50, 是否够样本)。"""
    out: list[tuple[int, float, bool]] = []
    for vi, version in enumerate(report.versions):
        cell = report.cell(task, version)
        if cell is not None:
            out.append((vi, cell.p50, report.enough(cell)))
    return out


def _select_tasks(report: Any, min_panels: int) -> list[tuple[str, list]]:
    """挑值得画的任务：达标点 ≥ min_panels，按点数降序（信息最多的排前面）。"""
    scored = [(task, _task_points(report, task)) for task in report.tasks]
    chosen = [
        (task, points)
        for task, points in scored
        if sum(1 for _i, _v, ok in points if ok) >= min_panels
    ]
    chosen.sort(key=lambda item: -sum(1 for _i, _v, ok in item[1] if ok))
    return chosen


def render_speed_chart(report: Any, out_path: str, *, min_panels: int = 2) -> tuple[int, int]:
    """把 SpeedReport 画成 small multiples；返回 (宽, 高)。

    ``report`` 是 ``speed_report.SpeedReport``——刻意用 duck typing 引用，避免与
    speed_report 形成 import 环，也让本模块能脱离报告层单测。
    """
    from PIL import Image, ImageDraw

    if not report.versions or not report.tasks:
        raise ValueError("没有可画的版本或任务；检查 --last / --task 是否把窗口筛空了")

    drawable = _select_tasks(report, min_panels)
    if not drawable:
        raise ValueError(
            f"窗口内没有任务有 {min_panels} 个以上达标样本点；放宽 --last / --min-count 再试"
        )

    f_title = _load_font(20, bold=True)
    f_panel = _load_font(14, bold=True)
    f_axis = _load_font(12)

    count = len(drawable)
    rows = (count + COLUMNS_PER_BAND - 1) // COLUMNS_PER_BAND
    width = MARGIN * 2 + COLUMNS_PER_BAND * (PANEL_W + PANEL_GAP)
    height = HEADER + rows * (PANEL_H + PANEL_GAP) + LEGEND + MARGIN

    img = Image.new("RGB", (width, height), (250, 251, 253))
    d = ImageDraw.Draw(img)

    d.text((MARGIN, 14), "跨版本耗时趋势（每任务一格，共享 x 轴）", font=f_title, fill=(20, 26, 40))
    d.text(
        (MARGIN, 40),
        f"最近 {len(report.versions)} 个版本 · 样本量门槛 n≥{report.min_count}"
        f"（不足的版本留白不连线）· 只画有 ≥{min_panels} 个达标点的任务（共 {count} 个）",
        font=f_axis,
        fill=(96, 104, 122),
    )

    labels = report.versions
    usable_w = PANEL_W - 30
    step = usable_w / max(1, len(labels) - 1)

    for index, (task, points) in enumerate(drawable):
        row, col = divmod(index, COLUMNS_PER_BAND)
        px = MARGIN + col * (PANEL_W + PANEL_GAP)
        py = HEADER + row * (PANEL_H + PANEL_GAP)

        d.rectangle([px, py, px + PANEL_W, py + PANEL_H], fill=(255, 255, 255), outline=(210, 214, 224))
        d.text((px + 4, py + 3), task, font=f_panel, fill=(26, 43, 74))

        real = [v for _i, v, _ok in points]
        vmin, vmax = min(real), max(real)
        span = (vmax - vmin) or 1.0

        def _x(vi: int, base: int = px) -> float:
            return base + 15 + vi * step

        def _y(value: float, base: int = py) -> float:
            return base + PANEL_H - 18 - (value - vmin) / span * (PANEL_H - 42)

        prev: tuple[float, float] | None = None
        for vi, value, enough in points:
            if not enough:
                prev = None      # 样本不足处断开，不跨过它连线
                continue
            x, y = _x(vi), _y(value)
            if prev is not None:
                d.line([prev, (x, y)], fill=(72, 110, 180), width=2)
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(72, 110, 180))
            prev = (x, y)

        d.text((px + 4, py + PANEL_H - 16), _fmt(vmin), font=f_axis, fill=(140, 148, 162))
        d.text((px + PANEL_W - 36, py + PANEL_H - 16), _fmt(vmax), font=f_axis, fill=(140, 148, 162))

        # 每行底轴标首/中/尾；面板内不重复，省得挤
        if col == 0:
            band_y = py + PANEL_H + 5
            picks = {0, len(labels) // 2, len(labels) - 1}
            for vi in sorted(picks):
                d.text(
                    (_x(vi) - 18, band_y),
                    labels[vi].replace("v1.0.0-", ""),
                    font=f_axis,
                    fill=(96, 104, 122),
                )

    d.text(
        (MARGIN, height - 26),
        f"横轴：版本（最近 {len(labels)} 个，等距排列；跳号版本保持在其位置）· 纵轴：该任务 p50 耗时",
        font=f_axis,
        fill=(140, 148, 162),
    )

    img.save(out_path)
    return img.width, img.height


def write_chart(report: Any, out_path: str, *, min_panels: int = 2) -> tuple[int, int]:
    """Pillow 缺失时给可操作的提示（与 `tools/drawing` 一致的措辞）。"""
    try:
        return render_speed_chart(report, out_path, min_panels=min_panels)
    except ModuleNotFoundError as exc:
        if (exc.name or "").startswith("PIL"):
            raise ModuleNotFoundError(
                "画图需要 Pillow，当前解释器里没有。发布包自带的 python 也不含它——"
                "请用装了 Pillow 的系统 Python 跑（或先 pip install pillow）。"
            ) from exc
        raise