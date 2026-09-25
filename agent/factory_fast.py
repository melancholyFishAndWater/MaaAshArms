import time, json
import factory
from re import search
from typing import Any, TypedDict

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import (
    Context,
    JRecognitionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import (
    JActionType,
    JClick,
    JStopTask,
    JTemplateMatch,
    JOCR,
    JColorMatch,
)
from numpy import ndarray, dtype
from base import DEFAULT_HIT_BOX, addListToTuple, toTuple

# ---------- reco ----------


# 检测是否大于建造次数
@AgentServer.custom_recognition("FastFactoryStopOnMaxCountReco")
class FastFactoryStopOnMaxCountReco(CustomRecognition):

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
        max_ = int(argv.custom_recognition_param)
        if factory.build_times >= max_:
            return DEFAULT_HIT_BOX


# ---------- action ----------


# 启用跳过功能
@AgentServer.custom_action("FastFactoryInitAct")
class FastFactoryInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        factory.build_times = 0
        return context.override_pipeline({"FactorySkip": {"enabled": True}})
