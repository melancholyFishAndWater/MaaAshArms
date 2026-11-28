from typing import Any
from numpy import ndarray, dtype

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context, Rect
from maa.pipeline import JOCR

from agentlog import log
from rect import DEFAULT_HIT_BOX
from direct import ocr

# -------------------- Reco --------------------


@AgentServer.custom_recognition("BattleStopNoMetalReco")
class BattleStopNoMetalReco(CustomRecognition):
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
        # 1) 识别需要合金数量
        need_d = ocr(
            context,
            jocr=JOCR([r"^\d$"], roi=(768, 630, 42, 40)),
            argv=argv,
        )
        if need_d is None:
            log.debug(f"{argv.node_name} OCR识别需要合金数量失败")
            return None

        # 2) 识别已有合金数量
        have_d = ocr(
            context,
            jocr=JOCR([r"^\d$"], roi=(965, 20, 99, 28)),
            argv=argv,
        )
        if have_d is None:
            log.debug(f"{argv.node_name} OCR识别已有合金数量失败")
            return None

        # 3) 若合金不足，则返回 box
        if int(have_d.text) < int(need_d.text):
            log.debug(f"{argv.node_name} 合金不足")
            return DEFAULT_HIT_BOX
        else:
            log.debug(f"{argv.node_name} 合金充足")

        return None
