from typing import Any, Literal

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
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

_count = 0


# 检测防卫局任务是否结束 目前是检测到十次后返回非 None
@AgentServer.custom_recognition("CheckEscortEndReco")
class CheckEscortEndReco(CustomRecognition):
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
        global _count
        if _count < 10:
            _count += 1
        else:
            _count = 0
            return [0] * 4
