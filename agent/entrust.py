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
)
from maa.pipeline import JOCR, JActionType, JStopTask

from agentlog import log
from params import parse_attach
from rect import DEFAULT_HIT_BOX

# 总章节及其大关
CHAPTER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("边境巡回篇·上", ("荒原", "结晶废墟", "沙漠", "沙漠腹地", "冰原", "冰原核心")),
    ("边境巡回篇·下", ("森丘", "森丘深处", "火山", "火山中心")),
    ("废都残响篇", ("禁区边缘", "荒芜禁区", "污染区", "辐射区", "核心辐射区")),
    ("空王座篇·上", ("阴霾腹地", "文明坟场", "至高废墟")),
    ("空王座篇·下", ("阴霾深处", "疮痍废土", "深渊之底", "失落世界")),
    ("红帷幕·上", ("光辉年代", "反叛的种子", "先知的衣钵")),
    ("红帷幕·下", ("预言者之道", "天使祭祀场", "灰烬战线")),
    ("铁幕残照", ("无主之地", "死城")),
)
# 总章节名
CHAPTER_NAMES: tuple[str, ...] = tuple(i[0] for i in CHAPTER)
# 目标章节名
target_chapter: str = "边境巡回篇·上"
# 大关名
stage_name: str = "结晶废墟"
# 目标小关的数字
level: int = 10
# 配置的委派次数
_times = 0
# 实际目标委派次数
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
        JOCR([r"-\d{1,2}"], roi=(158, 290, 991, 346), replace=[["o", "0"], ["O", "0"]]),
        argv.image,
    )


def get_ocr_level_times(
    context: Context, argv: CustomRecognition.AnalyzeArg
) -> OCRResult | None:
    """获取当前委派次数

    Raises:
        Exception: 识别失败时抛出异常
    """

    global _number, _max
    node_name = argv.node_name

    # 条件识别 今日剩余次数
    if _number == 0 or _max == 0:
        # 识别数字
        r = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([r"\d+/\d+"], roi=(396, 591, 61, 28)),
            argv.image,
        )
        if r is None or not r.hit:
            raise Exception("识别本日剩余次数失败")
        b_r = r.best_result
        assert type(b_r) == OCRResult

        # 正则匹配剩余次数
        m = search(r"(\d+)/\d+", b_r.text)
        if m is None:
            raise Exception("剩余次数正则匹配失败")

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
            log.debug(f"{node_name} 覆写委派次数日志失败")

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
    """返回实际委派次数与当前画面派遣次数的差值

    Raises:
        Exception: 识别失败时抛出异常
    """
    try:
        r = get_ocr_level_times(context, argv)
    except Exception as e:
        raise e

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
        try:
            r = get_difference_number(context, argv)
        except Exception as e:
            log.debug(f"{argv.node_name} 识别委派次数失败: {e!r}")
            return
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
        try:
            r = get_difference_number(context, argv)
        except Exception as e:
            log.debug(f"{argv.node_name} 识别委派次数失败: {e!r}")
            return
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
        try:
            r = get_difference_number(context, argv)
        except Exception as e:
            log.debug(f"{argv.node_name} 识别委派次数失败: {e!r}")
            return
        if r is None:
            return
        if r < 0:
            return [319, 642, 22, 24]


# 返回是否可以领取奖励
@AgentServer.custom_recognition("EntrustRewardableNowReco")
class EntrustRewardableNowReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        global _rewardable_by_timestamp
        if _rewardable_by_timestamp is not None:
            if datetime.now() > _rewardable_by_timestamp:
                _rewardable_by_timestamp = None
                return DEFAULT_HIT_BOX


# 返回是否有委派任务（执行中或执行完毕）
@AgentServer.custom_recognition("EntrustRewardableLateReco")
class EntrustRewardableLateReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        global _rewardable_by_timestamp
        if _rewardable_by_timestamp is not None:
            _rewardable_by_timestamp = None
            return DEFAULT_HIT_BOX


# 返回是否委派完剩余次数
@AgentServer.custom_recognition("EntrustProbeTouchMaxReco")
class EntrustProbeTouchMaxReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        try:
            r = get_ocr_level_times(context, argv)
        except Exception as e:
            log.debug(f"{argv.node_name} 识别委派次数失败: {e!r}")
            return

        if r is None:
            return

        if _number == 0:
            log.ferror(f"{argv.node_name} 错误 不可能出现的实际委派数字0 任务结束")
            context.override_pipeline(
                {"EntrustBranchTouchMax": {"next": ["BaseTaskStopError"]}}
            )
            return DEFAULT_HIT_BOX

        if _max <= _times:
            return DEFAULT_HIT_BOX


# 返回是否在目标大关内
@AgentServer.custom_recognition("EntrustProbeStageTextReco")
class EntrustProbeStageTextReco(CustomRecognition):
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
                continue
            else:
                return DEFAULT_HIT_BOX


# 返回上一章还是下一章的box
@AgentServer.custom_recognition("EntrustClickSwitchChapterReco")
class EntrustClickSwitchChapterReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        node_name = argv.node_name

        # 识别当前章节
        ocr_r = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR([".+"], roi=(6, 119, 383, 354)), argv.image
        )
        if ocr_r is None or not ocr_r.hit:
            log.debug(f"{node_name} 识别当前章节名失败")
            return

        for i in CHAPTER_NAMES:
            # 目标大关字符串拆成子串列表
            str_targets: list[str] = [i[0], i[1]] + ([i[-1]] if i[-1] in "上下" else [])

            # 遍历识别结果
            for j in ocr_r.filtered_results:
                assert type(j) == OCRResult
                # 遍历校验子串 若子串匹配成功 移除子串
                for k in str_targets.copy():
                    if k in j.text:
                        str_targets.remove(k)

            # 若子串全部匹配 循环终止
            if len(str_targets) == 0:
                current_chapter_name = i
                break
        else:
            log.debug(f"{node_name} 查找当前关卡名索引失败")
            return

        current_index = CHAPTER_NAMES.index(current_chapter_name)
        target_index = CHAPTER_NAMES.index(target_chapter)
        log.debug(f"{node_name} current: {current_index}, target: {target_index}")

        # 返回点击区域 不容易识别到下一章的文字 直接点击固定区域 识别是否完成此章节会容易点且方便
        if current_index < target_index:
            ocr_r2 = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR([r"(\d)/\1"], roi=(45, 527, 125, 40)),
                argv.image,
            )
            if ocr_r2 is None:
                log.debug(f"{node_name} 识别当前章节是否完成失败")
            elif not ocr_r2.hit:
                log.warn(
                    f"当前章节未完成 无法前往下一章 请确保大关名字输入正确 任务结束"
                )
                context.run_action_direct(JActionType.StopTask, JStopTask())
                return

            return [1247, 280, 1, 1]  # 下一章
        elif current_index > target_index:
            return [37, 279, 1, 1]  # 上一章


# ---------- Action ----------


# 初始化全局变量
@AgentServer.custom_action("EntrustInitAct")
class EntrustInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        node_name = argv.node_name
        try:
            global stage_name, level, _number, _rewardable_by_timestamp, _times, _max, target_chapter
            attach = parse_attach(context, argv, "stage_name", "level", "times")
            stage_name = attach["stage_name"]
            t_target_chapter = next(
                (i[0] for i in CHAPTER if any(stage_name in j for j in i[1])), None
            )  # 子串匹配 确保只填一部分大关名也能匹配中
            if t_target_chapter is None:
                log.error(f"未找到 {stage_name} 所属章节名，请确认大关名是否正确")
                r = context.run_action_direct(JActionType.StopTask, JStopTask())
                return r is not None and r.success

            target_chapter = t_target_chapter
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


# 领取超时处理 防止部分任务强制失败
@AgentServer.custom_action("EntrustStopRewardTimeoutAct")
class EntrustStopRewardTimeoutAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        log.warn(f"{argv.node_name} 领取委派奖励超时，部分任务可能会失败")
        global _rewardable_by_timestamp
        if _rewardable_by_timestamp is not None:
            _rewardable_by_timestamp = None
        return True


# ----- EntrustMover -----

# 移动方向 默认右滑
_move: Literal["BaseMoveRight", "BaseMoveLeft"] = "BaseMoveRight"


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
                m = search(r"-(\d+)", i.text)
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
        number = min(numbers) if _move == "BaseMoveRight" else max(numbers)

        # 校验数字
        if number <= 0:
            log.debug(f"{node_name} 识别到当前画面关卡数字不为正数: {number}")
            return

        # 修改移动方向
        if number >= level:
            log.debug(f"{node_name} 设置移动方式为右滑")
            _move = "BaseMoveRight"
        else:
            log.debug(f"{node_name} 设置移动方式为左滑")
            _move = "BaseMoveLeft"

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
