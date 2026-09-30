import json
from typing import Any
from unittest import result

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import (
    Context,
    JRecognitionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import JActionType, JStopTask, JTemplateMatch, JOCR
from numpy import ndarray, dtype
from re import search

from base import addListToTuple, toTuple, log

# ---------- Reco ----------


# 识别关卡名 返回还有挑战次数的关卡名字box
@AgentServer.custom_recognition("TrainingEnterOneRepo")
class TrainingEnterOneRepo(CustomRecognition):
    # 当前已有难度
    _level = ["一", "二", "三", "四", "五", "六"]

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        # 提取变量
        n_name = argv.node_name
        attach: dict[str, str] = (context.get_node_data("TrainingEnterOne") or {}).get(
            "attach", {}
        )

        # 判断是否为空
        if len(attach.keys()) == 0:
            log.info("无训练所可选关卡，任务结束")
            context.run_action_direct(JActionType.StopTask, JStopTask())
            return
        else:
            # 设置结束需要识别的数量
            context.override_pipeline({"TrainingEnd": {"index": f"{len(attach) - 1}"}})

        # 识别关卡是否还有剩余次数
        r = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Battle/BattleTraining/11.png"], roi=(192, 524, 900, 68)),
            argv.image,
        )
        if r is None or not r.hit:
            log.warn(f"{n_name} 识别关卡剩余次数失败")
            return

        # 遍历识别还有次数的关卡名
        expected = list(attach.keys())
        for i in r.filtered_results:
            assert type(i) == TemplateMatchResult

            # 识别关卡名字
            r2 = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    expected,
                    roi=toTuple(i.box),
                    roi_offset=(-63, -243, 126, 42),
                ),
                argv.image,
            )
            if r2 is None or not r2.hit:
                log.warn(f"{n_name} 识别关卡名字失败")
                continue
            b_r2 = r2.best_result
            assert type(b_r2) == OCRResult

            # 提取关卡信息
            name = b_r2.text
            level = attach.get(name, self._level[-1])
            if level not in self._level:
                log.warn(
                    f"{n_name} 错误的参数. attach: {attach}. expected: {expected}. _level: {self._level}"
                )
                continue

            # 覆写
            context.override_pipeline(
                {
                    "TrainingEnterOne": {
                        "focus": {"Node.Action.Starting": f"选择{name}"}
                    },
                    "TrainingEnterFormation": {
                        "custom_recognition_param": level,
                        "focus": {"Node.Action.Starting": f"选择难度{level}"},
                    },
                }
            )

            # 返回文字位置
            return b_r2.box


# 识别关卡难度 返回匹配难度的出击按钮box
@AgentServer.custom_recognition("TrainingEnterFormationRepo")
class TrainingEnterFormationRepo(CustomRecognition):
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
        s = argv.custom_recognition_param
        level: str = json.loads(s) if s is not None else "不进行"

        # 识别难度
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([r".{0,}难度.+"], roi=(470, 125, 158, 434)),
            argv.image,
        )
        if not result or not result.hit:
            log.warn(f"{n_name} 识别难度失败")
            return

        # 遍历判断关卡难度
        for i in result.filtered_results:
            assert type(i) == OCRResult
            if level in i.text:
                return (960, i.box[1], 93, 97)
