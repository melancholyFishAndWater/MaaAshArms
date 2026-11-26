import time
from re import search
from typing import Any, LiteralString
from numpy import frombuffer, ndarray, dtype

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import (
    Context,
    JRecognitionType,
    Rect,
    TemplateMatchResult,
    OCRResult,
)
from maa.pipeline import JTemplateMatch, JOCR

from agentlog import log
from params import parse_params
from rect import DEFAULT_HIT_BOX

# 今日 建造过的次数
built_times: int = 0
# 角色临时图片: 结束时间戳
_timestamp_by_dolls_image: dict[tuple[Any, LiteralString, bytes], float] = {}
# 尝试领取的角色图片
_reward_image: ndarray


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


# 获取临时 DOLLS 图片
def _get_dolls_image(
    image: ndarray,
    cancel_box: tuple[int, int, int, int],
) -> ndarray:
    """
    获取临时 DOLLS 图片

    Returns:
        ndarray: 临时 DOLLS 图片
    """
    offset = (-1047, 186, 82, -33)
    x = cancel_box[0] + offset[0]
    y = cancel_box[1] + offset[1]
    w = cancel_box[2] + offset[2]
    h = cancel_box[3] + offset[3]
    new_image = image[y : y + h, x : x + w]
    return new_image


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
        # 提取变量
        n_name = argv.node_name

        boxes = _get_cancel_buttons(context, argv.image)
        if not boxes:
            log.debug(f"{n_name} 获取红叉位置失败，可能画面暂未恢复")
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

            # 保存给 FactoryGetRewardNextAct 用
            global _reward_image
            _reward_image = _get_dolls_image(argv.image, i)

            return i


# 返回None或第一个可建造角色的选择区域
@AgentServer.custom_recognition("FactoryClickCharacterReco")
class FactoryClickCharacterReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> tuple[int, int, int, int] | None:
        # 常量
        node_name = argv.node_name
        PAGE = (235, 157, 140, 230)
        HORIZONTAL = 22 + PAGE[2]
        VERTICAL = 26 + PAGE[3]

        # 判断是否在选择界面
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(["选择需要建造的"], roi=(184, 54, 351, 78)),
            argv.image,
        )
        if not result or not result.hit:
            log.debug(f"{node_name} 不在选择建造页面 不选择")
            return

        # 遍历识别找到不处于正在建造的角色的box
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
        else:
            log.warn(f"{node_name} 识别可选择角色失败")


# 检测任务是否满足结束条件 目前 满足建造次数则结束 全忙碌则结束
@AgentServer.custom_recognition("FactoryCheckTaskEndReco")
class FactoryCheckTaskEndReco(CustomRecognition):

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
        # 提取变量
        n_name = argv.node_name

        try:
            d = parse_params(argv.custom_recognition_param, "build_times")
            max_ = d["build_times"]
        except Exception as e:
            log.warn(f"{n_name} 解析参数失败: {e!r}")
            return

        # 满足建造次数则结束
        if built_times >= max_:
            log.info("达到建造次数，任务结束")
            return DEFAULT_HIT_BOX

        # 获得取消按钮
        boxes = _get_cancel_buttons(context, argv.image)
        if not boxes:
            log.debug(f"{n_name} 获取红叉失败")
            return

        # 遍历以获得所有状态
        for i in boxes:
            # 获取 dolls 图片
            dools_image = _get_dolls_image(argv.image, i)

            if not context.override_image("temp", dools_image):
                log.warn(f"{n_name} 保存临时图片失败")
                continue

            # 遍历图片分析相识度
            for k in sorted(_timestamp_by_dolls_image.keys()):
                image = frombuffer(k[2], dtype=dtype(k[1])).reshape(k[0])
                same_d = context.run_recognition_direct(
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(["temp"], threshold=[0.8]),
                    image,
                )
                if same_d is not None and same_d.hit:
                    log.debug(f"{n_name} 状态存在，跳过识别时间: {dools_image[:5]}...")
                    break
            else:
                log.debug(f"{n_name} 状态不存在，新增识别时间: {dools_image[:5]}...")

                # 时间OCR
                time_result = _get_time(context, argv.image, i)
                if not time_result or not time_result.hit:
                    log.debug(f"{n_name} 识别时间失败: {dools_image[:5]}...")
                    continue
                best_time = time_result.best_result
                assert type(best_time) == OCRResult

                # 提取时间
                m = search(r"(\d\d):(\d\d)", best_time.text)
                if not m:
                    log.debug(f"{n_name} 正则提取时间失败: {best_time.text}")
                    continue

                offset_sec = int(m.group(1)) * 60 + int(m.group(2))
                sec = time.time() + offset_sec
                key = (dools_image.shape, dools_image.dtype.str, dools_image.tobytes())
                _timestamp_by_dolls_image[key] = sec
                log.debug(
                    f"{n_name} 新增识别状态: {dools_image[:5]}... 结束时间戳: {sec}"
                )

        # 全忙碌则结束
        if len(_timestamp_by_dolls_image) == 3:
            if all(v > time.time() for v in _timestamp_by_dolls_image.values()):
                log.info(f"全部建造槽位处于忙碌，任务结束")
                return DEFAULT_HIT_BOX


# ---------- action ----------


# 初始化 FactoryChooseNumber 的 index
@AgentServer.custom_action("FactoryBranchHomeAct")
class FactoryBranchHomeAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global built_times
        log.debug(f"{argv.node_name} 执行初始化")
        try:
            d = parse_params(argv.custom_action_param, "once_build_number")
            r = context.override_pipeline(
                {"FactoryChooseNumber": {"index": d["once_build_number"] - 1}}
            )
            if not r:
                log.warn(f"{argv.node_name} 覆写失败 可能影响建造数")
        except Exception as e:
            log.warn(f"{argv.node_name} 初始化失败: {e!r}")

        return True


# 建造成功 建造数自增
@AgentServer.custom_action("FactoryChooseStartSuccessAct")
class FactoryChooseStartSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global built_times
        built_times += 1
        log.debug(f"{argv.node_name} 建造成功，建造数加一，当前数量: {built_times}")
        return True


# 领取成功或失败(角色已满)都执行 从字典中删除
@AgentServer.custom_action("FactoryGetRewardNextAct")
class FactoryGetRewardNextAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        if not context.override_image("temp", _reward_image):
            log.warn(f"{argv.node_name} 保存临时图片失败")
            return True

        # 遍历图片分析相识度
        for k in _timestamp_by_dolls_image.keys():
            image = frombuffer(k[2], dtype=dtype(k[1])).reshape(k[0])
            same_d = context.run_recognition_direct(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(["temp"], threshold=[0.8]),
                image,
            )
            if same_d is not None and same_d.hit:
                del _timestamp_by_dolls_image[k]
                log.debug(f"{argv.node_name} 状态存在，删除 key: {k[:5]}...")
                return True

        return True
