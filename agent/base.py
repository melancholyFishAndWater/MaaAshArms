import time
from typing import Any

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

last_freezes_image: ndarray | None = None


def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])


def is_hit(detail: RecognitionDetail | None) -> bool:
    return detail is not None and detail.hit


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
