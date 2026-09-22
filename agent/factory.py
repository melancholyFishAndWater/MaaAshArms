import time, json
from re import search
from typing import Any

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
from maa.pipeline import JActionType, JClick, JTemplateMatch, JOCR, JColorMatch
from numpy import imag, ndarray, dtype
from base import addListToTuple, toTuple

# 收获次数
_count = 0
# 角色名称: 建造结束时间戳
_timestamp_by_build_end_names: dict[str, float] = {}

# 相对红×的坐标
_NAME_WH = (120, 25)
_NAME_OFFSET = (-1037, 188, 0, 0)
_TIME_WH = (117, 59)
_TIME_OFFSET = (-628, 210, 0, 0)


# 获取红叉位置
def _getCancelButton(
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
def _getName(context: Context, image: ndarray, cancel_box):
    return context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR(
            [".+"],
            roi=(cancel_box[0], cancel_box[1]) + _NAME_WH,
            roi_offset=_NAME_OFFSET,
        ),
        image,
    )


# ---------- reco ----------


# 如果满足任务结束条件，返回非None
@AgentServer.custom_recognition("CheckFactoryEndReco")
class CheckFactoryEndReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        global _timestamp_by_build_end_names, _count
        if _count >= int(argv.custom_recognition_param):
            return []
        if (len(_timestamp_by_build_end_names) == 3) and all(
            v > time.time() for v in _timestamp_by_build_end_names.values()
        ):
            return []


# 若时间归零，返回红叉位置
@AgentServer.custom_recognition("FactoryTimeEndRepo")
class FactoryTimeEndRepo(CustomRecognition):
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
        boxes = _getCancelButton(context, argv.image)
        if not boxes:
            return
        for i in boxes:
            time_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(expected=["00:00"], roi=i, roi_offset=TIME),
                argv.image,
            )
            if time_result and time_result.hit:
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
        boxes = _getCancelButton(context, argv.image)
        if not boxes:
            return
        for i in boxes:
            result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(["请添加订单数"], roi=toTuple(i), roi_offset=(-125, 160, 60, 0)),
                argv.image,
            )
            if not result or not result.hit:
                continue
            build_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    ["建造"], roi=(i[0], i[1], 772, 102), roi_offset=(-920, 102, 0, 0)
                ),
                argv.image,
            )
            if not build_result or not build_result.hit:
                continue
            chooses = build_result.filtered_results
            number = int(argv.custom_recognition_param)
            final = len(chooses) >= number and chooses[number - 1] or chooses[-1]
            assert type(final) == OCRResult
            return final.box


# 获取订单工厂对象状态 始终返回None
@AgentServer.custom_recognition("GetFactoryItemStatusRepo")
class GetFactoryItemStatusRepo(CustomRecognition):
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
        boxes = _getCancelButton(context, argv.image)
        if not boxes:
            return
        global _timestamp_by_build_end_names
        for i in boxes:
            time_result = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    [r"\d\d:\d\d"], roi=(i[0], i[1]) + _TIME_WH, roi_offset=_TIME_OFFSET
                ),
                argv.image,
            )
            if not time_result or not time_result.hit:
                continue
            name_result = _getName(context, argv.image, i)
            if not name_result or not name_result.hit:
                continue
            b_time = time_result.best_result
            b_name = name_result.best_result
            assert type(b_time) == OCRResult
            assert type(b_name) == OCRResult
            m = search(r"(\d\d):(\d\d)", b_time.text)
            if not m:
                continue
            offset_sec = int(m.group(1)) * 60 + int(m.group(2))
            _timestamp_by_build_end_names[b_name.text] = time.time() + offset_sec
        return None


# ---------- action ----------


# 初始化全局变量
@AgentServer.custom_action("InitFactoryItemGetterAct")
class InitFactoryItemGetterAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _timestamp_by_build_end_names, _count
        _timestamp_by_build_end_names = {}
        _count = 0
        return True


# WARN 没有识别到名字就强制结束任务链 不过一般情况下不会发生，毕竟红叉+时间已经足以框住名字
# 收取完成的订单直到目标位置出现 创建新的订单
@AgentServer.custom_action("FactoryTimeEndAct")
class FactoryTimeEndAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        OFFSET = (-890, 156, -30, -30)
        name_result = _getName(context, argv.reco_detail.raw_image, argv.box)
        if not name_result or not name_result.hit:
            return False
        b_name = name_result.best_result
        assert type(b_name) == OCRResult
        run_result = context.run_action_direct(
            JActionType.Click, JClick(toTuple(argv.box), target_offset=OFFSET)
        )
        if run_result and run_result.success:
            global _timestamp_by_build_end_names
            if b_name.text in _timestamp_by_build_end_names.keys():
                del _timestamp_by_build_end_names[b_name.text]
            return True
        return False


# 订单工厂专用移动器 记得设置 max_hit
@AgentServer.custom_action("FactoryMoverAct")
class FactoryMoverAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _timestamp_by_build_end_names
        n = len(_timestamp_by_build_end_names)
        if n == 3:
            last_ts = next(reversed(_timestamp_by_build_end_names.values()))
            action = "MoveUp" if last_ts < time.time() else "MoveDown"
        else:
            action = "MoveUp" if n < 3 else "MoveDown"
        result = context.run_action(action)
        if not result:
            return False
        return result.success


# 判断收获成功 收获次数+1
@AgentServer.custom_action("FactoryChooseStartEndAct")
class FactoryChooseStartEndAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _count
        _count += 1
        return True
