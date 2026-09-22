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

from base import addListToTuple

# 列车出发数
_train_pending_count: int = 0
# 列车完成数
_train_completed_count: int = 0


# 移动火车路线列表
def _InfoMove(context: Context, start: int, end: int):
    r = context.run_action_direct(
        JActionType.Swipe,
        JSwipe(begin=(120, start, 10, 10), end=[(120, end, 10, 10)]),
    )
    context.wait_freezes(200)
    return r


def _InfoMoveUp(context: Context):
    return _InfoMove(context, 400, 300)


def _InfoMoveDown(context: Context):
    return _InfoMove(context, 300, 400)


# 从主页获取列车状态并记录出发数，若非全部出发，则返回点击box
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
        pending_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/TrainPending.png"]),
            argv.image,
        )
        if completed_result:
            _train_completed_count = len(completed_result.filtered_results)
        if pending_result:
            _train_pending_count = len(pending_result.filtered_results)
        if _train_pending_count == 2:
            return
        if _train_completed_count > 0:
            assert completed_result
            context.override_pipeline(
                {
                    "CheckTrainReward": {
                        "max_hit": len(completed_result.filtered_results)
                    }
                }
            )
            return completed_result.box
        # 搜索 前往派遣的box
        can_train_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/NoTrain.png"]),
            argv.image,
        )
        if not can_train_result:
            return
        return can_train_result.box


# 若出发数为2，返回非None表示任务结束
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
            return [0] * 4


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


# TODO 想要其他路线
# 武装押运专用路线移动器
@AgentServer.custom_recognition("TrainInfoMoverReco")
class TrainInfoMoverReco(CustomRecognition):
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
        global _train_pending_count, _train_completed_count
        for i in range(10):
            _InfoMoveUp(context)
            # 是否有奖励
            blue = context.run_recognition("CheckTrainReward", argv.image)
            if blue and blue.hit:
                return [0] * 4

            # 是否有未解锁路线
            locked_result = context.run_recognition_direct(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(["Train/ArrowLocked.png"]),
                argv.image,
            )
            if locked_result and locked_result.hit:
                return [0] * 4

            # 是否为最终路线
            last_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(["新大陆路线"], roi=(52, 563, 118, 73)),
                argv.image,
            )
            if last_result and last_result.hit:
                return [0] * 4


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
