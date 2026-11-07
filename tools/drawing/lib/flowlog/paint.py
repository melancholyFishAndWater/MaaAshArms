"""字体、调色板与文本度量：`load_font` / `text_width` / `_note_lines`、节点与父节点
配色表。

只被 `draw` 使用；颜色是固定表，不随数据变化。字体按 Windows 常见路径回退，
找不到时退回 PIL 默认字体（框会变窄）。"""

from __future__ import annotations


import os
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Note


# --------------------------------------------------------------------------- #
# 画图
# --------------------------------------------------------------------------- #


def load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for c in cands:
        if os.path.isfile(c):
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def text_width(font: Any, text: str) -> float:
    """字体测宽。FreeType 字体用 getlength；退回位图字体（没有这个方法）时按字符数估算。"""
    try:
        return float(font.getlength(text))
    except AttributeError:
        return len(text) * 10.0


def _note_lines(notes: list[Note], font, maxw: int) -> list[tuple[str, str]]:
    """把诊断注记排成若干行，返回 `[(行文本, kind)]`。

    **一条注记独占一行**（超宽就在自身内硬折，续行沿用同一个 kind）：这样每一行只有一种
    颜色，"哪句是前提、哪句是警告"一眼能分开。原来把多条塞进一行用 `|` 隔开，全是同一个颜色。
    """
    if not notes:
        return []
    out: list[tuple[str, str]] = []
    for idx, note in enumerate(notes):
        text = ("注：" if idx == 0 else "　　") + note.text
        while text and text_width(font, text) > maxw:
            cut = len(text) - 1
            while cut > 1 and text_width(font, text[:cut]) > maxw:
                cut -= 1
            out.append((text[:cut], note.kind))
            text = "　　　　" + text[cut:]
        if text.strip():
            out.append((text, note.kind))
    return out


PALETTE = {
    "hit": ((232, 241, 255), (72, 110, 180), (26, 43, 74)),
    "custom": ((240, 235, 255), (122, 92, 180), (52, 32, 92)),
    "direct": ((238, 238, 240), (130, 130, 140), (52, 52, 58)),
    "miss": ((255, 236, 233), (200, 92, 74), (110, 30, 20)),
    "error": ((255, 224, 224), (190, 40, 40), (110, 0, 0)),
    "event": ((228, 246, 236), (56, 140, 96), (20, 70, 46)),
    "task": ((226, 232, 240), (60, 74, 100), (18, 26, 42)),
}

# 父节点分组的三色一套：(分组框底色, 分组框边框, 角标底色)。角标上是白字，所以角标色要够深。
# 同名的父节点永远拿同一套（一眼看出"又是它派发的"），不同父节点按首次出现顺序轮换这 6 套。
PARENT_COLORS: tuple[
    tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]], ...
] = (
    ((234, 239, 250), (124, 140, 186), (74, 88, 140)),  # 蓝
    ((234, 246, 241), (86, 156, 132), (34, 110, 86)),  # 青绿
    ((248, 239, 228), (190, 148, 84), (146, 102, 32)),  # 琥珀
    ((244, 234, 245), (158, 114, 170), (114, 70, 128)),  # 紫
    ((233, 246, 233), (104, 158, 94), (58, 116, 52)),  # 绿
    ((250, 235, 238), (186, 112, 124), (146, 66, 82)),  # 玫红
)


# 注记按类型分色（只用作前景色，浅底上都够对比）。`info` 是默认灰。
NOTE_COLORS: dict[str, tuple[int, int, int]] = {
    "info": (96, 104, 122),  # 灰：普通前提说明
    "version": (30, 110, 70),  # 绿：这次用的资源版本
    "stale": (168, 112, 30),  # 琥珀：疑似过时 / 记录被裁断
    "agent": (176, 48, 48),  # 红：agent 缺位
    "derive": (54, 96, 150),  # 蓝：由识别记录还原
    "time": (108, 74, 150),  # 紫：时间戳回跳已重排
    "fail": (170, 34, 34),  # 深红：本窗口的失败现场图（点名文件名）
}
