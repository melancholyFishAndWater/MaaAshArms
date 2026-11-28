from re import search

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context, OCRResult
from maa.pipeline import JOCR

from agentlog import log
from rect import DEFAULT_HIT_BOX, toTuple
from direct import ocr

# ---------- reco ----------


# 识别礼物数量。若识别到的礼物数量差值大于等于3，则认为送完了，然后返回 box
@AgentServer.custom_recognition("GiftBranchFinishReco")
class GiftBranchFinishReco(CustomRecognition):
    # (任务id, reco_id): 初始礼物数量
    dict_gift_count: dict[tuple[int, int], int] = {}

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 1) 获取选择礼物的节点信息
        d = context.tasker.get_latest_node("GiftChooseFirstGift")
        if d is None:
            log.debug(f"{argv.node_name} 获取节点 GiftChooseFirstGift 的信息失败")
            return None
        elif d.recognition is None:
            log.debug(
                f"{argv.node_name} 节点 GiftChooseFirstGift 的 recognition 信息为空"
            )
            return None
        elif d.recognition.box is None:
            log.debug(f"{argv.node_name} 节点 GiftChooseFirstGift 的 box 信息为空")
            return None

        # 2) 基于当时 box 获取礼物数量
        r = ocr(
            context,
            jocr=JOCR(
                [r"[xX]?\d+"],
                roi=toTuple(d.recognition.box),
                roi_offset=(-40, -40, 80, 80),
            ),
            argv=argv,
        )
        if r is None:
            log.debug(f"{argv.node_name} 节点 OCR 识别失败")
            return None

        # 3) 初始化字典
        key = (argv.task_detail.task_id, d.recognition.reco_id)
        if key not in self.dict_gift_count:
            self.dict_gift_count[key] = 0

        # 4) 获取相关数字
        # 4.1) 类型校验
        if not type(d.recognition.best_result) == OCRResult:
            log.debug(
                f"{argv.node_name} GiftChooseFirstGift 节点 best_result 类型不合法 type={type(d.recognition.best_result)}"
            )
            return None

        # 4.2) 获取原始数字
        m = search(r"(\d+)", d.recognition.best_result.text)
        if m is None:
            log.debug(
                f"{argv.node_name} best_result 中的 OCR 识别的礼物数量不合法 r.text={d.recognition.best_result.text}"
            )
            return None
        ocr_number = int(m.group(1))

        # 4.3) 获取当前数字
        m2 = search(r"(\d+)", r.text)
        if m2 is None:
            log.debug(f"{argv.node_name} 节点 OCR 识别的礼物数量不合法 r.text={r.text}")
            return None
        number = int(m2.group(1))

        # 5) 校验数字
        if ocr_number < 0 or number < 0:
            log.debug(
                f"{argv.node_name} 节点 OCR 识别的礼物数量为负数 ocr_number={ocr_number}, number={number}"
            )
            self.dict_gift_count.pop(key, None)
            return None

        # 6) 更新字典
        self.dict_gift_count[key] = ocr_number - number

        # 7) 判断值
        if sum(self.dict_gift_count.values()) >= 3:
            log.debug(
                f"{argv.node_name} 节点 OCR 识别的礼物数量差值大于等于3 number={number}, ocr_number={ocr_number}"
            )
            self.dict_gift_count.pop(key, None)
            return DEFAULT_HIT_BOX

        return None
