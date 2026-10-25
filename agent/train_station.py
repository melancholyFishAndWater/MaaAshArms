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

按"去背景 → 定位 → 抹黑字"三步走，**不做二值化**——二值化会在窄笔画（数字 `1`）上碎掉：

1. **卡片切分**：ROI 内按行找暗线（行灰度均值 < 25、厚度 ≤ 2px），取其中心作边界。
   卡片间距实测 179px，一屏可见 3 张完整卡片 + 1 张半截。
2. **背景估计**：`ratio = 灰度 / boxblur(灰度, 半径 45)`。半径必须 **大于数字宽度**
   （数字宽 48~68px），否则数字自身把局部均值拉低，`ratio` 整体大于 1、整卡全黑。
   这里不能用形态学开运算：开运算保留"比邻域暗的细结构"，而数字笔画正是暗的细结构，
   开运算结果会被数字自身污染（实测背景中位数从 180 掉到 56）。
3. **定位**：ratio ∈ (0.45, 0.93) 的连通域里，取数字列内高度 ≥ 55px 的最大块。
   **不做宽高比筛选**——1.png/9.png 背景偏暗时数字与纸纹粘连成 1.4+ 的宽块。
4. **抹黑字**：STATION 是纯黑（0~40）、数字是深灰（85~115），用绝对灰度分开。
   只在 STATION 所在行带（卡片内 y 40~70）取纯黑像素，**只沿水平方向**扩张 3px
   吃掉左右抗锯齿灰边，再用局部纸面亮度填充抹除处。各向同性扩张会把数字腰部
   切出一条亮带（3_2.png / 7_2.png 的数字 4 被拦腰截断，OCR 读不出）。
5. **裁切**：带 4px 边距裁出数字切图，交给 `run_recognition_direct` 的 OCR
   `only_rec` 识别（喂 3 通道：`np.stack([view]*3, axis=2)`）。

**box 用切分时记下的那个（原图屏幕坐标），不要用 OCR 返回的 box**：裁切图上
OCR 只会给裁切内坐标，映射回来还得自己加偏移；而切分时已经算好了原图坐标。

## 实测结论

`temp/escort_station/image/` 共 18 张截图、54 个数字（2026-10-25，本地 PP-OCRv6 实测）：

- 定位 54/54（`locate_digit`）。
- OCR `only_rec` 读出 53/54：17/18 张截图全对
  （1~7.png、9.png 读 1/2/3；8.png 与 `*_2.png` 读 4/5/6）。
- 唯一失败是 `8_no_5.png` 中间那张：第五站已"完成"、整卡被压暗，
  数字灰度低于黑字阈值，抹黑字时一并抹掉。**按需求接受**。
- 整帧检测（`only_rec=false`）不可行：检测出来的是耗时数字与碎片噪声。

## 落地时还没定的部分

- 竖排站号（`9.png` 右侧路线图上的 `STATION 1`~`7`）是另一套坐标，未纳入本模块。
- `8_no_5.png` 那类"已完成"的暗卡若要支持，得给整卡压暗的截图单独一条
  相对阈值的分支（当前绝对灰度阈值对它失效）。
"""

import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# 路线一览卡片列 ROI（x, y, w, h）
ROUTE_LIST_ROI = (308, 106, 138, 582)

# 卡片分隔线的判据：行灰度均值低于此值、且厚度不超过 MAX_SEP_THICK
SEP_ROW_MEAN = 25.0
MAX_SEP_THICK = 2
# 卡片高度下限，低于此值认为是半截卡片或噪条，直接丢
MIN_CARD_H = 70

# 背景估计半径，必须大于数字宽度
BG_RADIUS = 45
# ratio 到灰度的映射区间：[RATIO_BLACK, RATIO_PAPER] -> [0, 255]
RATIO_BLACK = 0.35
RATIO_PAPER = 1.0

# **绝对灰度**阈值（0-255）。这三档在 17 张实测截图上分得很开：
#   STATION 黑字 0~40（纯黑）、站号数字笔画 85~115（深灰）、卡纸 190~200
# 用绝对灰度而不是背景比值：比值会随卡纸明暗漂移，背景偏暗时黑字比值升到 0.6，
# 抹不掉，OCR 会把 STATION 残片读成 "TAMIE"（2026-10-25 实测）。
TEXT_GRAY_MAX = 60.0  # 低于此值视为纯黑字/黑边
TEXT_ERASE_RADIUS = 3  # 纯黑像素的**水平**扩张半径（吃掉左右的抗锯齿灰边）
TEXT_EDGE_GRAY = 120.0  # 抹除处填充背景时，剔除灰度低于此值的样本（黑字心+抗锯齿边）
TEXT_FILL_RADIUS = 21  # 抹除处的填充背景用中值滤波，半径要盖住字高（~14px）
# STATION 文字所在的行带（卡片内相对 y）。实测 17 张截图 54 张卡片的纯黑行段
# 全部落在 46~65（8_no_5.png 中间那张整卡压暗、整行都是黑，不在此判据内）。
TEXT_BAND = (40, 70)
# 数字候选的背景比值区间：数字 0.5~0.85、纸面 1.0、黑字 < 0.4
DIGIT_RATIO_MIN = 0.45
DIGIT_RATIO_MAX = 0.93
# 数字候选的背景比值区间：数字 0.5~0.85、纸面 1.0、黑字 < 0.4
DIGIT_RATIO_MIN = 0.45
DIGIT_RATIO_MAX = 0.93

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


def _median_row(a: np.ndarray, k: int, skip_below: float = 0.0) -> np.ndarray:
    """横向 k 宽的中值滤波：每行取左右各 k//2 列的中值。

    `skip_below` 先剔除灰度低于该值的样本再取中值——用来排除 STATION 的
    黑字心（0~40）**和它的抗锯齿灰边**（60~110）。不排除灰边的话，
    中值被灰边拉低，填回去还是一条灰带（OCR 会当噪声）。
    卡纸横向均匀、纵向有山峦纹理，横向中值能给出"这一行是纸面"的亮度。
    """
    p = np.pad(a, ((0, 0), (k // 2, k // 2)), mode="edge")
    win = sliding_window_view(p, (1, k))  # (H, W, 1, k)
    if skip_below <= 0:
        return np.median(win, axis=-1).reshape(a.shape)
    # 低灰样本置为 NaN 后取中值。整窗口都低时 nanmedian 会报 All-NaN 警告
    # （8_no_5.png 那张整卡压暗的卡片就会触发），这里显式压掉并回退到 255。
    masked = np.where(win < skip_below, np.nan, win)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(masked, axis=-1)
    med = np.where(np.isnan(med), 255.0, med)
    return med.reshape(a.shape)


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


def locate_digit(
    ratio: np.ndarray, card: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """在卡片内定位站号数字的 bbox，返回 ROI 相对坐标 (x, y, w, h)，找不到返回 None。

    掩膜用**背景比值**（0.45~0.93）而不是绝对灰度：比值是"相对卡纸"的，
    卡纸明暗变化时数字比值稳定，绝对灰度会跟着漂移（背景暗时数字也暗到 65 以下，
    固定下限就把它滤掉了）。实测 17 张截图 54 张卡片全部命中。

    几何判据：数字高 ≥ 55px、落在数字列内。**不做宽高比上限**——背景偏暗时
    数字与纸纹粘连成 1.4+ 的宽块，按宽高比筛会把它们全部滤掉。
    """
    top, bot = card
    m = (ratio[top:bot] > DIGIT_RATIO_MIN) & (ratio[top:bot] < DIGIT_RATIO_MAX)
    m[:, : DIGIT_COL[0]] = False
    for comp in sorted(_components(m), key=len, reverse=True):
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        bh = max(ys) - min(ys) + 1
        if bh >= DIGIT_MIN_H:
            return min(xs), min(ys), max(xs) - min(xs) + 1, bh
    return None


def _erase_horizontal(mask: np.ndarray, radius: int) -> np.ndarray:
    """只沿**水平方向**膨胀 `radius` 像素。

    STATION 是横排文字，抹它只需要横向扩张（吃掉左右两侧的抗锯齿灰边）。
    各向同性扩张会把**竖直方向**也撑开 3px，正好切进数字的腰部——
    3_2.png / 7_2.png 的数字 4 被拦腰切断，OCR 读不出来。
    """
    out = mask.copy()
    for _ in range(radius):
        out = out | np.roll(out, 1, 1) | np.roll(out, -1, 1)
    return out


def clean_digit_view(gray: np.ndarray) -> np.ndarray:
    """原始灰度转成"纸面白、数字暗、STATION 消失"的 uint8 灰度。

    **`gray` 是单张卡片区域**（卡片内相对坐标），不是整幅 ROI——
    `TEXT_BAND` 是卡片内相对 y。

    STATION 文字是**纯黑**（0~40），站号数字是**深灰**（85~115），卡纸 190~200，
    三者用绝对灰度就能分开。但只抹字心会留下 1~2px 的抗锯齿灰边，
    OCR 会把残片读成 "STATIO"/"TAMIE"（2026-10-25 实测失败）。

    做法分三步：
    1. **只在 STATION 所在的行带内**（卡片内 y 40~70，实测 17 张截图 54 张卡片
       的纯黑行段全部落在 46~65）取纯黑像素。数字笔画是深灰不是纯黑，不受影响；
       卡片底部 y 108+ 的黑边与黄条也不在行带内。
    2. **只沿水平方向**扩张 3px，吃掉左右的抗锯齿灰边。不做各向同性扩张——
       那会把竖直方向也撑开 3px，切进数字腰部，数字 4 被拦腰截断。
    3. 抹除处填**局部纸面亮度**（横向中值，剔除低灰样本），不是填纯白——
       填白会留下一个纯白矩形，轮廓依然存在，OCR 照样当文字读。

    数字不做明暗拉伸：OCR rec 模型对绝对明暗不敏感，拉伸反而把抗锯齿边拉成硬边。
    """
    out = gray.copy()
    h, w = gray.shape
    band = np.zeros_like(gray, dtype=bool)
    b0, b1 = TEXT_BAND
    band[max(b0, 0) : min(b1, h), :] = True

    black = (gray < TEXT_GRAY_MAX) & band
    grown = _erase_horizontal(black, TEXT_ERASE_RADIUS)
    # 局部背景：横向中值（卡纸横向均匀、纵向有山峦纹理），剔除黑字与灰边样本
    bg = _median_row(gray, TEXT_FILL_RADIUS, skip_below=TEXT_EDGE_GRAY)
    out[grown] = bg[grown]
    return np.clip(out, 0, 255).astype(np.uint8)


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

    out: list[tuple[tuple[int, int, int, int], np.ndarray]] = []
    for top, bot in split_cards(gray):
        box = locate_digit(ratio, (top, bot))
        if box is None:
            continue
        dx, dy, dw, dh = box
        pad = 4
        # 清洗按**卡片**做：`TEXT_BAND` 是卡片内相对 y
        card_view = clean_digit_view(gray[top:bot])
        y0 = max(dy - pad, 0)
        y1 = min(dy + dh + pad, bot - top)
        x0 = max(dx - pad, 0)
        x1 = min(dx + dw + pad, gray.shape[1])
        out.append(((x + dx, y + top + dy, dw, dh), card_view[y0:y1, x0:x1]))
    return out