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
from re import search


@AgentServer.custom_recognition("MaxTrainFlagFromMainPageReco")
class MaxTrainFlagFromMainPageReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(expected=["列车派遣数"], roi=(227, 130, 165, 54)),
            argv.image,
        )
        if result is None or not result.hit:
            return
        best = result.best_result
        assert type(best) == OCRResult
        m = search(r"(\d)/(\d)$", best.text)
        if m and (m.group(1) < m.group(2)):
            return result.box


@AgentServer.custom_recognition("DoTrainReco")
class DoTrainReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Training/CanTrainButton.png"]),
            argv.image,
        )
        if result is None or not result.hit:
            return

        def func(i):
            assert type(i) == TemplateMatchResult
            return i.box[1]

        return min(result.filtered_results, key=func)
