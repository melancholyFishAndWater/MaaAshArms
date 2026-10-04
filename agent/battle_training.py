import json
from typing import Any
from unittest import result

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
from maa.pipeline import JActionType, JStopTask, JTemplateMatch, JOCR
from numpy import ndarray, dtype
from re import search

from base import addListToTuple, toTuple, log, parse_params

# 总关卡名
_all = ["战斗演习", "狩猎行动", "拓展训练", "资源筹备"]
# 要进行的训练所关卡名: 难度
battle_targets: dict[str, str] = {}

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

        # 识别关卡是否还有剩余次数
        r = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Battle/BattleTraining/11.png"], roi=(192, 524, 900, 68)),
            argv.image,
        )
        if r is None or not r.hit:
            log.debug(f"{n_name} 识别关卡剩余次数失败")
            return

        # 遍历识别还有次数的关卡名
        expected = list(battle_targets.keys())
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
                log.debug(f"{n_name} 识别关卡名字失败")
                continue
            b_r2 = r2.best_result
            assert type(b_r2) == OCRResult

            # 提取关卡信息
            name = next((k for k in battle_targets if k in b_r2.text), None)
            if name is None:
                log.debug(f"{n_name} 识别到的关卡名无法匹配: {b_r2.text}")
                continue
            level = battle_targets.get(name)

            # 覆写
            context.override_pipeline(
                {
                    "TrainingEnterOne": {
                        "focus": {"Node.Action.Starting": f"选择{name}"}
                    },
                    "TrainingEnterFormation": {
                        "custom_recognition_param": level,
                        "focus": {"Node.Action.Starting": f"选择{name} 难度{level}"},
                    },
                    "TrainingDailyUpdate": {"custom_action_param": {"name": name}},
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
            log.debug(f"{n_name} 识别难度失败")
            return

        # 遍历判断关卡难度
        for i in result.filtered_results:
            assert type(i) == OCRResult
            if level in i.text:
                return (960, i.box[1], 93, 97)


# ---------- Act ----------


# 初始化
@AgentServer.custom_action("TaskTrainingAct")
class TaskTrainingAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        try:
            # 提取参数
            global battle_targets
            attach: dict[str, str] = (context.get_node_data(argv.node_name) or {}).get(
                "attach", {}
            )

            battle_targets.clear()
            battle_targets.update({k: v for k, v in attach.items() if v != "不进行"})
            if len(battle_targets) == 0:
                log.info(f"{argv.node_name} 未选择任何训练所关卡，任务结束")
                context.run_action_direct(JActionType.StopTask, JStopTask())
                return False
            # 设置结束需要识别的数量
            context.override_pipeline(
                {"TrainingEnd": {"index": len(battle_targets) - 1}}
            )
            return True
        except Exception as e:
            log.error(f"{argv.node_name} 参数解析失败: {e}")
            return False
