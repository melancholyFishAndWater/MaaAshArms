import time, json
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

# 建造次数
build_times: int = 0
# 角色名: 结束时间戳
_timestamp_by_build_end_names: dict[str, float] = {}
# 尝试领取的角色名
_reward_name: str = ""


# 获取红叉位置
def _get_cancel_buttons(
    context: Context, image: ndarray
) -> list[tuple[int, int, int, int]] | None:
    result = context.run_recognition_direct(
        JRecognitionType.TemplateMatch,
        JTemplateMatch(["Factory/CancelButtonGreen.png"], roi=(1125, 90, 111, 370)),
        image,
    )
    if result and result.hit:
        l = result.filtered_results
        final = []
        for i in l:
            assert type(i) == TemplateMatchResult
            box = i.box
            final.append((box[0], box[1], box[2], box[3]))
        return final


# 基于红叉位置获取角色名字
def _get_name(context: Context, image: ndarray, cancel_box: tuple[int, int, int, int]):
    return context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR(
            [".+"],
            roi=cancel_box,
            roi_offset=(-1047, 186, 82, -33),
        ),
        image,
    )


# 基于红叉位置获取剩余时间
def _get_time(context: Context, image: ndarray, cancel_box: tuple[int, int, int, int]):
    return context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR([r"\d\d:\d\d"], roi=cancel_box, roi_offset=(-628, 210, 60, 0)),
        image,
    )


# ---------- reco ----------


# 若时间归零，返回红叉位置
@AgentServer.custom_recognition("FactoryGetRewardReco")
class FactoryGetRewardReco(CustomRecognition):
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
        boxes = _get_cancel_buttons(context, argv.image)
        if not boxes:
            return
        for i in boxes:
            # 时间
            time_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(expected=["00:00"], roi=i, roi_offset=(-628, 210, 60, 0)),
                argv.image,
            )
            if time_result is None or not time_result.hit:
                continue

            # 名字
            name_result = _get_name(context, argv.image, i)
            if name_result is None or not name_result.hit:
                continue
            b_name = name_result.best_result
            assert type(b_name) == OCRResult

            # 赋值给 FactoryGetRewardSuccessAct 用
            global _reward_name
            _reward_name = b_name.text

            return i


# 返回None或第一个可建造角色的选择区域
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
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(["Factory/Busy.png"], roi=roi),
                    argv.image,
                )
                if not result2 or not result2.hit:
                    return roi


# 根据建造数返回选择位置
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
        # 获得红叉位置
        boxes = _get_cancel_buttons(context, argv.image)
        if not boxes:
            return
        for i in boxes:
            # 判断是否是选择建造
            result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(["请添加订单数"], roi=toTuple(i), roi_offset=(-125, 160, 60, 0)),
                argv.image,
            )
            if not result or not result.hit:
                continue

            # 获得建造按钮位置
            build_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    ["建造"], roi=(i[0], i[1], 772, 102), roi_offset=(-920, 102, 0, 0)
                ),
                argv.image,
            )
            if not build_result or not build_result.hit:
                continue

            # 返回选择位置
            chooses = build_result.filtered_results
            number = int(argv.custom_recognition_param)
            final = len(chooses) >= number and chooses[number - 1] or chooses[-1]
            assert type(final) == OCRResult
            return final.box


# 检测任务是否满足其他结束条件 目前 满足建造次数则结束 全忙碌则结束
@AgentServer.custom_recognition("FactoryCheckTaskEndRepo")
class FactoryCheckTaskEndRepo(CustomRecognition):

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
        # 满足建造次数则结束
        max_ = int(argv.custom_recognition_param)
        if build_times >= max_:
            return DEFAULT_HIT_BOX

        # 获得取消按钮
        boxes = _get_cancel_buttons(context, argv.image)
        if not boxes:
            return

        # 遍历以获得所有状态
        for i in boxes:
            # 名字OCR
            name_result = _get_name(context, argv.image, i)
            if not name_result or not name_result.hit:
                continue
            b_name = name_result.best_result
            assert type(b_name) == OCRResult

            # 在字典中则跳过
            if b_name.text in _timestamp_by_build_end_names.keys():
                continue

            # 时间OCR
            time_result = _get_time(context, argv.image, i)
            if not time_result or not time_result.hit:
                continue
            b_time = time_result.best_result
            assert type(b_time) == OCRResult

            # 提取时间
            m = search(r"(\d\d):(\d\d)", b_time.text)
            if not m:
                continue

            offset_sec = int(m.group(1)) * 60 + int(m.group(2))
            _timestamp_by_build_end_names[b_name.text] = time.time() + offset_sec

        # 全忙碌则结束
        if len(_timestamp_by_build_end_names) == 3:
            if all(v > time.time() for v in _timestamp_by_build_end_names.values()):
                return DEFAULT_HIT_BOX


# ---------- action ----------


# 初始化 _timestamp_by_build_end_names build_times
@AgentServer.custom_action("TaskFactoryNextAct")
class TaskFactoryNextAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global build_times
        build_times = 0
        _timestamp_by_build_end_names.clear()
        return True


# 建造成功 建造数自增
@AgentServer.custom_action("FactoryChooseStartSuccessAct")
class FactoryChooseStartSuccess(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global build_times
        build_times += 1
        return True


# 领取成功
@AgentServer.custom_action("FactoryGetRewardSuccessAct")
class FactoryGetRewardSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 从字典中删除
        if _reward_name in _timestamp_by_build_end_names.keys():
            del _timestamp_by_build_end_names[_reward_name]
        return True
