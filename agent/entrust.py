from re import search
from typing import Literal
from datetime import timedelta, datetime

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import (
    Context,
    JRecognitionType,
    Rect,
    RecognitionDetail,
    OCRResult,
    ContextEventSink,
)
from maa.pipeline import JOCR
from maa.event_sink import NotificationType

from agentlog import log
from params import parse_attach
from rect import DEFAULT_HIT_BOX

# 大关名
stage_name: str = "结晶废墟"
# 目标小关的数字
level: int = 10
# 配置的委派次数
_times = 0
# 实际委派次数
_number = 0
# 最大委派次数
_max = 0
# 可领取奖励的时间
_rewardable_by_timestamp: datetime | None = None


def get_level_ocr(
    context: Context, argv: CustomRecognition.AnalyzeArg
) -> RecognitionDetail | None:
    """
    进行关卡小节数ocr

    Args:
        context (Context): 上下文
        argv (CustomRecognition.AnalyzeArg): 参数

    Returns:
        RecognitionDetail | None: 识别结果
    """
    return context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR(
            [r".+\d{1,2}"], roi=(158, 290, 991, 346), replace=[["o", "0"], ["O", "0"]]
        ),
        argv.image,
    )


def get_ocr_level_times(
    context: Context, argv: CustomRecognition.AnalyzeArg
) -> OCRResult | None:
    """获取当前委派次数"""

    global _number, _max
    node_name = argv.node_name

    # 条件识别 今日剩余次数
    if _number == 0 or _max == 0:
        try:
            # 识别数字
            r = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR([r"\d+/\d+"], roi=(396, 591, 61, 28)),
                argv.image,
            )
            if r is None or not r.hit:
                log.debug(f"{node_name} 识别本日剩余次数失败")
                raise
            b_r = r.best_result
            assert type(b_r) == OCRResult

            # 正则匹配剩余次数
            m = search(r"(\d+)/\d+", b_r.text)
            if m is None:
                log.debug(f"{node_name} 剩余次数正则匹配失败")
                raise

            # 设置最大次数
            _max = int(m.group(1))
            if _times > 0:
                _number = min(_times, _max)
            else:
                _number = _max

            # 覆写日志
            result = context.override_pipeline(
                {node_name: {"focus": {"Node.Action.Succeeded": f"委派 {_number} 次"}}}
            )
            if not result:
                log.debug(f"{node_name} 覆写失败")
        except Exception as e:
            log.debug(f"{node_name} 捕获错误: {e}")
            log.debug(f"{node_name} 将期望剩余次数设为 30")
            _number = 30

    # 识别当前次数
    r2 = context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR([r"\d{1,2}"], roi=(375, 640, 56, 33), replace=[["o", "0"], ["O", "0"]]),
        argv.image,
    )
    if r2 is None or not r2.hit:
        return

    assert type(r2.best_result) == OCRResult
    return r2.best_result


def get_difference_number(
    context: Context, argv: CustomRecognition.AnalyzeArg
) -> int | None:
    """返回实际委派次数与当前画面派遣次数的差值"""
    r = get_ocr_level_times(context, argv)

    if r is None:
        log.debug(f"{argv.node_name} 识别委派剩余次数失败")
        return

    result = _number - int(r.text)
    log.debug(f"{argv.node_name} 识别实际委派次数与当前画面派遣次数的差值: {result}")
    return result


# ---------- Reco ----------


# 返回匹配的大关名 box
@AgentServer.custom_recognition("EntrustEnterStageReco")
class EntrustEnterStageReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        node_name = argv.node_name

        r = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([stage_name], roi=(380, 127, 826, 447)),
            image=argv.image,
        )
        if r is None or not r.hit:
            log.debug(f"{node_name} 未匹配到符合章节名")
            return
        return r.box


# 识别关卡小节 返回数字 box
@AgentServer.custom_recognition("EntrustChooseLevelReco")
class EntrustChooseLevelReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | list[int] | None:
        # 提取变量
        node_name = argv.node_name

        r = get_level_ocr(context, argv)
        if r is None or not r.hit:
            log.debug(f"{node_name} 识别关卡数字失败")
            return

        for i in r.filtered_results:
            assert type(i) == OCRResult

            m = search(r".*(\d\d)", i.text)
            if not m:
                continue

            if int(m.group(1)) == level:
                log.debug(f"识别到匹配关卡数字: {i.box}")
                return i.box


# 返回当前画面委派次数是否满足抬起
@AgentServer.custom_recognition("EntrustProbeCanStartReco")
class EntrustProbeCanStartReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        r = get_difference_number(context, argv)
        if r is None:
            return
        if r == 0:
            return DEFAULT_HIT_BOX


# 返回是否应该点击加号
@AgentServer.custom_recognition("EntrustClickPlusReco")
class EntrustClickPlusReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        r = get_difference_number(context, argv)
        if r is None:
            return
        if r > 0:
            return [465, 642, 22, 24]


# 返回是否应该点击减号
@AgentServer.custom_recognition("EntrustClickMinusReco")
class EntrustClickMinusReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        r = get_difference_number(context, argv)
        if r is None:
            return
        if r < 0:
            return [319, 642, 22, 24]


# 返回是否可以领取奖励
@AgentServer.custom_recognition("TaskEntrustGetRewardReco")
class TaskEntrustGetRewardReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        global _rewardable_by_timestamp
        if _rewardable_by_timestamp is not None:
            if datetime.now() > _rewardable_by_timestamp:
                _rewardable_by_timestamp = None
                return DEFAULT_HIT_BOX


# 返回是否有委派任务（执行中或执行完毕）
@AgentServer.custom_recognition("TaskEntrustTrainingGetRewardReco")
class TaskEntrustTrainingGetRewardReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        global _rewardable_by_timestamp
        if _rewardable_by_timestamp is not None:
            _rewardable_by_timestamp = None
            return DEFAULT_HIT_BOX


# 返回是否委派完剩余次数
@AgentServer.custom_recognition("EntrustBranchTouchMaxReco")
class EntrustBranchTouchMaxReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        r = get_ocr_level_times(context, argv)
        if r is None:
            return

        if _number == 0:
            log.ferror(f"{argv.node_name} 错误 不应出现的实际委派数字0 任务结束")
            context.run_action("TaskStopError")
            return

        if _max <= _times:
            return DEFAULT_HIT_BOX


# 返回是否在目标大关内
@AgentServer.custom_recognition("EntrustProbeStageNameReco")
class EntrustProbeStageNameReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        r = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([stage_name], roi=(808, 103, 207, 31)),
            argv.image,
        )
        if r is None or not r.hit:
            log.debug(f"{argv.node_name} 识别大关名字失败")
            return

        for i in r.filtered_results:
            assert type(i) == OCRResult
            if stage_name not in i.text:
                log.debug(f"{argv.node_name} 识别的大关名不匹配 {stage_name}/{i.text}")
                return
            else:
                return DEFAULT_HIT_BOX


# ---------- Action ----------


# 初始化全局变量
@AgentServer.custom_action("EntrustInitAct")
class EntrustInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        node_name = argv.node_name
        try:
            global stage_name, level, _number, _rewardable_by_timestamp, _times, _max
            attach = parse_attach(context, argv, "stage_name", "level", "times")
            stage_name = attach["stage_name"]
            level = attach["level"]
            _times = attach["times"]
            _number = 0
            _max = 0
            _rewardable_by_timestamp = None
            return True
        except Exception as e:
            log.debug(f"{node_name} 解析attach失败")
            log.ferror(f"{e}")
            return False


# 设置领奖时间
@AgentServer.custom_action("EntrustStartSuccessAct")
class EntrustStartSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _rewardable_by_timestamp
        _rewardable_by_timestamp = datetime.now() + timedelta(seconds=30)
        return True


# ----- EntrustMover -----

# 移动方向 默认右滑
_move: Literal["MoveRight", "MoveLeft"] = "MoveRight"


# 识别画面 并修改移动方向
@AgentServer.custom_recognition("EntrustMoverReco")
class EntrustMoverReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | list[int] | None:
        global _move
        node_name = argv.node_name

        # 识别
        r = get_level_ocr(context, argv)
        if r is None:
            log.debug(f"{node_name} 识别关卡数字失败")
            return

        # 遍历提取
        numbers = []
        for i in r.filtered_results:
            assert type(i) == OCRResult

            try:
                m = search(r"(\d+)", i.text)
                if m is None:
                    log.debug(f"{node_name} 未提取到数字: {i.text!r}")
                    continue
                numbers.append(int(m.group(1)))
            except Exception as e:
                log.debug(f"{node_name} 将识别结果转化失败: {e!r}")
                continue

        # 校验提取
        if len(numbers) == 0:
            log.debug(f"{node_name} 未提取到有效数字")
            return

        # 右滑取最小 左滑取最大
        number = min(numbers) if _move == "MoveRight" else max(numbers)

        # 校验数字
        if number <= 0:
            log.debug(f"{node_name} 识别到当前画面关卡数字不为正数: {number}")
            return

        # 修改移动方向
        if number >= level:
            log.debug(f"{node_name} 设置移动方式为右滑")
            _move = "MoveRight"
        else:
            log.debug(f"{node_name} 设置移动方式为左滑")
            _move = "MoveLeft"

        return DEFAULT_HIT_BOX


# 移动画面
@AgentServer.custom_action("EntrustMoverAct")
class EntrustMoverAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        r = context.run_action(_move)
        if r is not None and r.success:
            return True
        return False


# ----- EntrustCheckStageLocked -----

_locked_count = 0


# 连续三次识别时 返回box
@AgentServer.custom_recognition("EntrustCheckStageLockedReco")
class EntrustCheckStageLockedReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        node_name = argv.node_name

        ocr_r = context.run_recognition("EntrustProbeChaapterFinsh", argv.image)
        if ocr_r is not None and ocr_r.hit:
            log.debug(f"{node_name} 当前画面所有章节完成 跳过累加")
            return
        elif ocr_r is None:
            log.debug(f"{node_name} ocr失败 跳过累加")
            return

        global _locked_count
        _locked_count += 1
        if _locked_count >= 3:
            log.debug(
                f"{node_name} 当前画面所有章节未完成 累加 当前次数: {_locked_count}"
            )
            return DEFAULT_HIT_BOX


# 注册上下文监听器 当next中有节点命中时，清空累积次数
@AgentServer.context_sink()
class _LoopErrorResetSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail) -> None:
        global _locked_count
        if noti_type == NotificationType.Succeeded:
            _locked_count = 0
