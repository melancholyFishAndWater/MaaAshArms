from typing import Any

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
from maa.pipeline import JTemplateMatch, JOCR, JColorMatch
from numpy import imag, ndarray, dtype
from base import addListToTuple

# 相对红×的坐标
# LAST = [-224, 142, 0, 0]  # 最后一个槽位
# INVERVAL = [-75.6, 0, 0, 0]  # 槽位间隔
factory_times = 0  # 执行任务次数 结束标志


# 获取红×位置
def getCancelButton(
    context: Context, argv: CustomRecognition.AnalyzeArg
) -> list[tuple[int, int, int, int]] | None:
    result = context.run_recognition_direct(
        JRecognitionType.TemplateMatch,
        JTemplateMatch(["Factory/CancelButtonGreen.png"], roi=(1125, 90, 111, 370)),
        argv.image,
    )
    if result and result.hit:
        l = result.filtered_results
        final = []
        for i in l:
            assert type(i) == TemplateMatchResult
            box = i.box
            final.append((box[0], box[1], box[2], box[3]))
        return final


@AgentServer.custom_recognition("FlagInFactoryRepo")
class FlagInFactoryRepo(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        result = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR(["订单工厂"], roi=(81, 10, 242, 53)), argv.image
        )
        if result is None or not result.hit:
            return
        factory_times = 0
        return result.box


# 如果任务结束，返回非None
@AgentServer.custom_recognition("FactoryTaskEndFlagRepo")
class FactoryTaskEndFlagRepo(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if factory_times >= 3:
            return []


# 返回None或红×位置
@AgentServer.custom_recognition("FactoryTimeEndFlagRepo")
class FactoryTimeEndFlagRepo(CustomRecognition):
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
        TIME = (-628, 210, 60, 0)  # 时间坐标的偏移量
        l = getCancelButton(context, argv)
        if not l:
            return
        for i in l:
            result2 = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(expected=["00:00"], roi=i, roi_offset=TIME),
                argv.image,
            )
            if result2 and result2.hit:
                return i


# 返回None或第一个可建造角色位置
@AgentServer.custom_recognition("FactoryChooseCharaterRepo")
class FactoryChooseCharaterRepo(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> tuple[int, int, int, int] | None:
        PAGE = (235, 157, 140, 230)
        HORIZONTAL = 22 + PAGE[2]
        VERTICAL = 26 + PAGE[3]
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(["选择需要建造的"], roi=(184, 54, 351, 78)),
            argv.image,
        )
        if not result or not result.hit:
            return
        for i in range(2):
            for j in range(5):
                roi = (
                    PAGE[0] + HORIZONTAL * j,
                    PAGE[1] + VERTICAL * i,
                    PAGE[2],
                    PAGE[3],
                )
                result2 = context.run_recognition_direct(
                    JRecognitionType.OCR, JOCR(["生产中"], roi=roi), argv.image
                )
                if not result2 or not result2.hit:
                    return roi


@AgentServer.custom_recognition("FactoryChooseNumberRepo")
class FactoryChooseNumberRepo(CustomRecognition):
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
        btns = getCancelButton(context, argv)
        assert btns
        for i in btns:
            result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    ["建造"], roi=(i[0], i[1], 772, 102), roi_offset=(-920, 102, 0, 0)
                ),
                argv.image,
            )
            if not result or not result.hit:
                continue
            chooses = result.filtered_results

            def func(i):
                return i.box[0]

            final = min(chooses, key=func)
            assert type(final) == OCRResult
            print(final.box)
            return final.box
