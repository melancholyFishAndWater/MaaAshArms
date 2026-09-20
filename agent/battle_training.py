from typing import Any
from unittest import result

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import (
    Context,
    JRecognitionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import JTemplateMatch, JOCR
from numpy import ndarray, dtype
from re import search

from base import addListToTuple, toTuple


@AgentServer.custom_recognition("EnterTrainingTeamRepo")
class EnterTrainingTeamRepo(CustomRecognition):
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
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([r".{0,}难度.+"], roi=(470, 125, 158, 434)),
            argv.image,
        )
        if not result or not result.hit:
            return
        l = result.filtered_results
        for i in l:
            assert type(i) == OCRResult
            m = search(r"难度(.)", i.text)
            if not m:
                continue
            if m.group(1) == "六":
                return (960, i.box[1], 93, 97)
