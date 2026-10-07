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
from maa.pipeline import JOCR
from re import search

from base import log, parse_params

# 章节名
chapter_name: str = "结晶废墟"
# 关卡小节数
level: int = 10


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
        JOCR([r".*\d\d"], roi=(158, 290, 991, 346), replace=[["o", "0"], ["O", "0"]]),
        argv.image,
    )


# ---------- Reco ----------


# 返回匹配的章节名
@AgentServer.custom_recognition("EntrustEnterChapterReco")
class EntrustEnterChapterReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        node_name = argv.node_name

        r = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([chapter_name], roi=(380, 127, 826, 447)),
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


# ---------- Action ----------


# 初始化全局变量
@AgentServer.custom_action("EntrustInitAct")
class EntrustInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        node_name = argv.node_name
        try:
            global chapter_name, level
            p = parse_params(argv.custom_action_param, "chapter_name", "level")
            chapter_name = p["chapter_name"]
            level = p["level"]
            return True
        except Exception as e:
            log.debug(f"{node_name} 解析param失败")
            log.ferror(f"{e}")
            return False


# 覆写启用领取奖励
@AgentServer.custom_action("EntrustEnableRewardAct")
class EntrustEnableRewardAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return context.override_pipeline({"TaskEntrustGetReward": {"enabled": True}})
