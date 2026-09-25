import json
import time
from typing import Any, TypedDict

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import (
    Context,
    JRecognitionType,
    JActionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import JTemplateMatch, JOCR, JSwipe
from re import search
from numpy import ndarray, dtype

# last_freezes_image: ndarray | None = None

# 默认hit box 仅用于不需要坐标的节点
DEFAULT_HIT_BOX = [0] * 4


def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])


def is_hit(detail: RecognitionDetail | None) -> bool:
    return detail is not None and detail.hit


# ---------- Reco ----------

# TEST
# 返回是否冻结
# @AgentServer.custom_recognition("CheckFreezesRepo")
# class CheckFreezesRepo(CustomRecognition):
#     def analyze(
#         self, context: Context, argv: CustomRecognition.AnalyzeArg
#     ) -> (
#         CustomRecognition.AnalyzeResult
#         | Rect
#         | list[int]
#         | ndarray[tuple[Any, ...], dtype[Any]]
#         | tuple[int, int, int, int]
#         | None
#     ):
#         global last_freezes_image
#         if not last_freezes_image:
#             last_freezes_image = argv.image
#             return
#         return [0] * 4


# ---------- MoveUpDown ----------

_move_up_down_dict: dict[tuple[int, str], int] = {}
_move_entry: str = "MoveUp"


# 若还有滑动次数 返回非 None
@AgentServer.custom_recognition("MoveUpDownReco")
class MoveUpDownReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> (
        CustomRecognition.AnalyzeResult
        | Rect
        | list[int]
        | ndarray[tuple[Any, ...], dtype[Any]]
        | tuple[int, int, int, int]
        | None
    ):
        # 提取变量
        s = argv.custom_recognition_param
        param: dict[str, str] = s and json.loads(s) or {}
        from_ = param.get("from_", "unknow")
        x = int(param.get("x", 10))

        # 合成key
        key = (argv.task_detail.task_id, from_)

        # 判断是否还有次数
        times = _move_up_down_dict.get(key, 0)
        if times >= x * 2:
            return

        # 自增
        times += 1
        _move_up_down_dict[key] = times

        # 移动方向
        global _move_entry
        if times <= x:
            _move_entry = "MoveUp"
        else:
            _move_entry = "MoveDown"

        return DEFAULT_HIT_BOX


# 上滑动x次 下滑动x次
@AgentServer.custom_action("MoveUpDownAct")
class MoveUpDownAct(CustomAction):

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 移动
        r = context.run_action(_move_entry)

        # 返回结果
        if r is not None:
            return r.success
        return False
