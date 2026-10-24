"""武装押运路线一览页的"站号大数字"识别（试验模块，**未接入 `agent/main.py`**）。

## 目标

`roi=(308, 106, 138, 582)` 是路线一览左侧那列卡片。每张卡片上有一个站号大数字，
但它是**深灰描边压在卡纸纹理上**，不是黑底白字也不是纯色文字：

| 元素 | 实测灰度（0-255） | 说明 |
| --- | --- | --- |
| 站号数字核心 | 90~150 | 对比度只有约 1.3 倍 |
| 卡纸背景 | 180~200 | 上面还有 2~4px 的纸纹 |
| 黑字 `STATION` | < 60 | 不需要参与识别 |

PP-OCR 的文本检测在这种对比度下找不到文本框，所以走不了"直接 OCR"。

## 做法

按"去背景 → 抹黑字 → 定位裁切"三步走，**不做二值化**——二值化会在窄笔画（数字 `1`）上碎掉：

1. **卡片切分**：ROI 内按行找暗线（行灰度均值 < 25、厚度 ≤ 2px），取其中心作边界。
   卡片间距实测 179px，一屏可见 3 张完整卡片 + 1 张半截。
2. **背景估计**：`ratio = 灰度 / boxblur(灰度, 半径 45)`。半径必须 **大于数字宽度**
   （数字宽 48~68px），否则数字自身把局部均值拉低，`ratio` 整体大于 1、整卡全黑。
   这里不能用形态学开运算：开运算保留"比邻域暗的细结构"，而数字笔画正是暗的细结构，
   开运算结果会被数字自身污染（实测背景中位数从 180 掉到 56）。
3. **定位**：ratio ∈ (0.45, 0.93) 的连通域里，取数字列内高度 ≥ 55px 的最大块。
   **不做宽高比筛选**——1.png/9.png 背景偏暗时数字与纸纹粘连成 1.4+ 的宽块。
4. **裁切 + 清洗**：黑字（ratio < 0.45，含 STATION 与卡片黑边）置白后按 [0.45, 1.0]
   映射灰度，裁出带 4px 边距的数字切图，交给 `run_recognition_direct` 的 OCR
   `only_rec` 识别（喂 3 通道：`np.stack([view]*3, axis=2)`）。

**box 用切分时记下的那个（原图屏幕坐标），不要用 OCR 返回的 box**：裁切图上
OCR 只会给裁切内坐标，映射回来还得自己加偏移；而切分时已经算好了原图坐标。

## 实测结论（`temp/flow/temp/1.png` ~ `9.png` 共 9 张、27 个数字）

- 定位 27/27（`locate_digit`）。
- OCR `only_rec` 识别 27/27 全对（1-7/9.png 读出 1/2/3，8.png 读出 4/5/6），
  置信度 0.54~1.0，走 `Tasker.post_recognition` 实测。
- 整帧检测（`only_rec=false`）不可行：检测出来的是耗时数字与碎片噪声。

## 落地时还没定的部分

- 竖排站号（`9.png` 右侧路线图上的 `STATION 1`~`7`）是另一套坐标，未纳入本模块。
"""

import numpy as np

# 路线一览卡片列 ROI（x, y, w, h）
ROUTE_LIST_ROI = (308, 106, 138, 582)

# 卡片分隔线的判据：行灰度均值低于此值、且厚度不超过 MAX_SEP_THICK
SEP_ROW_MEAN = 25.0
MAX_SEP_THICK = 2
# 卡片高度下限，低于此值认为是半截卡片或噪条，直接丢
MIN_CARD_H = 70

# 背景估计半径，必须大于数字宽度
BG_RADIUS = 45
# 数字候选的 ratio 上界（数字核心 0.5~0.7，纸面 1.0）
RATIO_T = 0.93
# ratio 到灰度的映射区间：[RATIO_BLACK, RATIO_PAPER] -> [0, 255]
RATIO_BLACK = 0.35
RATIO_PAPER = 1.0

# 黑字/黑边的 ratio 上界：低于它的像素在切图时抹成纸面白。
# 实测 STATION 黑字 < 0.4、数字核心 0.5~0.7、纸面 1.0，取 0.45 正好隔开
TEXT_ERASE = 0.45

# 数字列范围（ROI 内 x 坐标）。27 个实测样本：数字 x 37~104，左侧 0~36 是穿孔边框
DIGIT_COL = (36, 112)
# 数字最小高度：实测 68~86，低于 55 的连通域是 STATION 文字碎片或纸纹
DIGIT_MIN_H = 55


def _boxblur(a: np.ndarray, r: int) -> np.ndarray:
    """半径 r 的箱式均值，用积分图做到与半径无关。"""
    p = np.pad(a, ((r + 1, r), (r + 1, r)), mode="edge")
    ii = p.cumsum(0).cumsum(1)
    n = 2 * r + 1
    return (ii[n:, n:] - ii[:-n, n:] - ii[n:, :-n] + ii[:-n, :-n]) / (n * n)


def _dark_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """把布尔数组切成连续 True 段的 (起, 止) 列表（闭区间）。"""
    runs: list[tuple[int, int]] = []
    y = 0
    n = len(mask)
    while y < n:
        if mask[y]:
            s = y
            while y < n and mask[y]:
                y += 1
            runs.append((s, y - 1))
        else:
            y += 1
    return runs


def split_cards(gray: np.ndarray) -> list[tuple[int, int]]:
    """按分隔暗线把灰度图切成若干卡片区间，返回 (上, 下) 行号（不含 padding）。

    首尾各留 3px 边距，避开分隔线自身的过渡像素。
    """
    h = gray.shape[0]
    row_mean = gray.mean(axis=1)
    seps = [
        s
        for s, e in _dark_runs(row_mean < SEP_ROW_MEAN)
        if e - s <= MAX_SEP_THICK and 4 < s < h - 12
    ]
    edges = [0, *seps, h]
    cards: list[tuple[int, int]] = []
    for k in range(len(edges) - 1):
        top, bot = edges[k] + 3, edges[k + 1] - 3
        if bot - top >= MIN_CARD_H:
            cards.append((top, bot))
    return cards


def background_ratio(gray: np.ndarray, radius: int = BG_RADIUS) -> np.ndarray:
    """除以局部背景，得到对比度比值：数字约 0.5~0.85，纸面 1.0，黑字 < 0.4。"""
    return gray / (_boxblur(gray, radius) + 1.0)


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """4-连通域（纯 python BFS，卡片只有 138x150，量级没问题）。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out: list[list[tuple[int, int]]] = []
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            stack = [(sy, sx)]
            seen[sy, sx] = True
            comp: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            out.append(comp)
    return out


def locate_digit(ratio: np.ndarray, card: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """在卡片内定位站号数字的 bbox，返回 ROI 相对坐标 (x, y, w, h)，找不到返回 None。

    判据（27 个实测样本校准）：数字高 68~86 且落在数字列内。不做宽高比上限——
    1.png / 9.png 背景偏暗时数字与纸纹粘连成 1.4+ 的宽块，按宽高比筛会把它们
    全部滤掉；改用"数字列内最高的连通域"这一条就够了。
    """
    top, bot = card
    m = (ratio[top:bot] > TEXT_ERASE) & (ratio[top:bot] < RATIO_T)
    m[:, : DIGIT_COL[0]] = False
    for comp in sorted(_components(m), key=len, reverse=True):
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        bh = max(ys) - min(ys) + 1
        if bh >= DIGIT_MIN_H:
            return min(xs), min(ys), max(xs) - min(xs) + 1, bh
    return None


def clean_digit_view(ratio: np.ndarray) -> np.ndarray:
    """ratio 转成"纸面白、数字暗、黑字消失"的 uint8 灰度。

    黑字（ratio < `TEXT_ERASE`）置为纸面 1.0 再映射，STATION 文字与卡片黑边
    就不会进入 OCR 视野；数字核心 0.5~0.7 映射到 0~100。
    """
    r = np.where(ratio < TEXT_ERASE, 1.0, ratio)
    return np.clip((r - TEXT_ERASE) / (1.0 - TEXT_ERASE) * 255.0, 0, 255).astype(np.uint8)


def extract_station_digits(
    image: np.ndarray, roi: tuple[int, int, int, int] = ROUTE_LIST_ROI
) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    """从整屏截图取出每张卡片的 (数字框, 数字切图)。

    返回按从上到下的卡片顺序；`box` 是**原图屏幕坐标** (x, y, w, h)，调用方
    拿它即可点击或换算，不必依赖 OCR 返回的 box（裁切图上 OCR 只会给裁切内
    坐标，映射回来还得自己加偏移——直接用切分时记下的这个 box 更省事）。
    `view` 是黑字抹除后的灰度切图 (H, W)，喂给 `run_recognition_direct` 的
    OCR `only_rec` 前用 `np.stack([view]*3, axis=2)` 转成 3 通道。
    """
    x, y, w, h = roi
    patch = image[y : y + h, x : x + w]
    gray = patch[:, :, :3].astype(np.float32).mean(axis=2)
    ratio = background_ratio(gray)
    view = clean_digit_view(ratio)

    out: list[tuple[tuple[int, int, int, int], np.ndarray]] = []
    for card in split_cards(gray):
        box = locate_digit(ratio, card)
        if box is None:
            continue
        dx, dy, dw, dh = box
        pad = 4
        top, _ = card
        y0 = max(top + dy - pad, 0)
        y1 = min(top + dy + dh + pad, gray.shape[0])
        x0 = max(dx - pad, 0)
        x1 = min(dx + dw + pad, gray.shape[1])
        out.append(((x + dx, y + top + dy, dw, dh), view[y0:y1, x0:x1]))
    return out