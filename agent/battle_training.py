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
from maa.pipeline import JTemplateMatch, JOCR
from numpy import ndarray, dtype
from re import search

from base import addListToTuple, toTuple

# ---------- Reco ----------


# 识别关卡名 返回还有挑战次数的关卡名字box
@AgentServer.custom_recognition("TrainingEnterOneRepo")
class TrainingEnterOneRepo(CustomRecognition):
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
        # 识别关卡是否还有剩余次数
        r = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Battle/BattleTraining/11.png"], roi=(192, 524, 900, 68)),
            argv.image,
        )
        if r is None or not r.hit:
            return

        # 识别关卡名字
        r2 = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(
                ["战斗演习", "狩猎行动", "拓展训练", "资源筹备"],
                roi=toTuple(r.box),
                roi_offset=(-63, -243, 126, 42),
            ),
            argv.image,
        )
        if r2 is None or not r2.hit:
            return
        b_r2 = r2.best_result
        assert type(b_r2) == OCRResult

        # 覆写关卡信息
        name = b_r2.text
        attach: dict[str, str] = (context.get_node_data("TrainingEnterOne") or {}).get(
            "attach", {}
        )
        context.override_pipeline(
            {
                "TrainingEnterOne": {"focus": {"Node.Action.Starting": f"选择{name}"}},
                "TrainingEnterFormation": {
                    "custom_recognition_param": attach[name],
                    "focus": {"Node.Action.Starting": f"选择难度{attach[name]}"},
                },
            }
        )

        # 返回文字位置
        return r2.box


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
        s = argv.custom_recognition_param
        level: str = json.loads(s) if s is not None else "六"

        # 识别难度
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([r".{0,}难度.+"], roi=(470, 125, 158, 434)),
            argv.image,
        )
        if not result or not result.hit:
            return

        # 遍历判断关卡难度
        for i in result.filtered_results:
            assert type(i) == OCRResult
            if level in i.text:
                return (960, i.box[1], 93, 97)
