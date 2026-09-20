from typing import Any

from maa.agent.agent_server import AgentServer
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

from base import addListToTuple

# 列车出发数
_train_pending_count: int = 0
# 列车完成数
_train_completed_count: int = 0


# 从主页获取列车状态 并 记录出发数
@AgentServer.custom_recognition("GetTrainStatusReco")
class GetTrainStatusReco(CustomRecognition):
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
        # TODO 警告用户没有检测到
        global _train_pending_count, _train_completed_count
        _train_pending_count = 0
        _train_completed_count = 0
        completed_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                ["Train/TrainCompleted.png"],
                roi=(41, 189, 319, 165),
            ),
            argv.image,
        )
        if not completed_result:
            return
        pending_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/TrainPending.png"]),
            argv.image,
        )
        if not pending_result:
            return
        _train_completed_count = len(completed_result.filtered_results)
        _train_pending_count = len(pending_result.filtered_results)
        if _train_pending_count == 2:
            return
        if _train_completed_count > 0:
            assert completed_result.box
            return completed_result.box
        # 点击 前往派遣
        can_train_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/NoTrain.png"]),
            argv.image,
        )
        if not can_train_result:
            return
        assert can_train_result.box
        return can_train_result.box


# 若列车全部出发，返回非None
@AgentServer.custom_recognition("CheckTrainEndReco")
class CheckTrainEndReco(CustomRecognition):
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
        global _train_pending_count
        if _train_pending_count == 2:
            return []


# 若有可领取资源，返回非None
@AgentServer.custom_recognition("CheckTrainRewardReco")
class CheckTrainRewardReco(CustomRecognition):
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
        global _train_completed_count
        if _train_completed_count != 0:
            return []


# 火车出发 出发数加一
@AgentServer.custom_recognition("TrainBusyReco")
class TrainBusyReco(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/CancelTrain.png"], roi=(886, 128, 119, 533)),
            argv.image,
        )
        if result is None or not result.hit:
            return
        global _train_pending_count
        _train_pending_count += 1
        return result.box


# TODO 发车Reco
# @AgentServer.custom_recognition("DoTrainReco")
# class DoTrainReco(CustomRecognition):
#     def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
#         result = context.run_recognition_direct(
#             JRecognitionType.TemplateMatch,
#             JTemplateMatch(["Training/CanTrainButton.png"]),
#             argv.image,
#         )
#         if result is None or not result.hit:
#             return

#         def func(i):
#             assert type(i) == TemplateMatchResult
#             return i.box[1]

#         return min(result.filtered_results, key=func)
