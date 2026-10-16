"""坐标与命中框的搬运件。

回调返回的 box 是"识别成功"的唯一载体：返回非 None 即命中，返回 None 即未命中。
`DEFAULT_HIT_BOX` 就是给**不需要坐标**的节点（DirectHit 那类：只判"到这一步了"）当返回值用的，
全仓库共用同一个列表对象。

放在 agent/shared/ 下靠 agent/main.py 把它注入 sys.path 后按顶层名导入（`from rect import ...`）：
共享件被顶层模块与 shared/ 内的模块同时消费，写 `shared.rect` 会另起一份模块对象。
"""

from maa.context import RecognitionDetail

# 默认hit box 仅用于不需要坐标的节点
DEFAULT_HIT_BOX = [0] * 4


def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])


def is_hit(detail: RecognitionDetail | None) -> bool:
    return detail is not None and detail.hit
