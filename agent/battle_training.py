"""
训练所 agent 模块

INFO 使用命名规范版本 v1.1 校验年月日 26/10/12
"""

from typing import Any

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
from maa.pipeline import JActionType, JStopTask, JTemplateMatch, JOCR
from numpy import ndarray, dtype

from base import parse_attach, parse_params, toTuple, log, DEFAULT_HIT_BOX

# 总关卡名
_all = ["战斗演习", "狩猎行动", "拓展训练", "资源筹备"]
# 要进行的训练所关卡名: 难度
battle_targets: dict[str, str] = {}


# ---------- Reco ----------


# 识别所有已完成关卡的关卡名
@AgentServer.custom_recognition("TrainingGetStatusReco")
class TrainingGetStatusReco(CustomRecognition):

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

        # 识别关卡
        r = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Battle/BattleTraining/01.png"], roi=(192, 524, 900, 68)),
            argv.image,
        )
        if r is None or not r.hit:
            log.debug(f"{n_name} 识别关卡剩余次数失败，可能未进行过战斗")
            return DEFAULT_HIT_BOX

        # 获取已完成的关卡
        done_targets: list[str] = []
        for i in r.filtered_results:
            assert type(i) == TemplateMatchResult

            # 识别关卡名字
            r2 = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    _all,
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
            name = next((k for k in _all if k in b_r2.text), None)
            if name is None:
                log.debug(f"{n_name} 识别到的关卡名无法匹配: {b_r2.text}")
                continue

            # 追加
            done_targets.append(name)

        # 校验
        if len(done_targets) != len(r.filtered_results):
            log.warn(
                f"{n_name} 识别到的训练所关卡名数量与相应的状态数量不匹配，可能会出现非预期的情况。名字数 {len(done_targets)} / 状态数 {len(r.filtered_results)}"
            )

        # 准备写入更新 尝试覆写
        if len(done_targets) > 0:
            times = len([i for i in battle_targets if i not in done_targets])
            dic = {
                argv.node_name: {"next": ["TrainingDailyUpdate"]},
                "TrainingDailyUpdate": {
                    "custom_action_param": {"name": done_targets},
                    "next": ["TaskStop"] if times == 0 else [],
                },
            }
            ov_r2 = context.override_pipeline(dic)
            if not ov_r2:
                log.warn(f"更新训练所每日状态失败")

        return DEFAULT_HIT_BOX


# 识别目标关卡 返回box表示全部完成
@AgentServer.custom_recognition("TrainingCheckEndReco")
class TrainingCheckEndReco(CustomRecognition):
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
        node_name = argv.node_name

        # 识别 0/1 即没有次数、完成的关卡
        tm_r = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Battle/BattleTraining/01.png"], roi=(192, 524, 900, 68)),
            argv.image,
        )
        if tm_r is None or not tm_r.hit:
            log.debug(f"{node_name} 识别 0/1 失败")
            return

        # 完成数量不够，直接返回
        if len(tm_r.filtered_results) < len(battle_targets):
            log.debug(
                f"{node_name} 识别到完成关卡数量不足: {len(tm_r.filtered_results)} < {len(battle_targets)}"
            )
            return

        # 目标关卡数足够，任务结束
        if len(tm_r.filtered_results) == 4:
            log.debug(f"{node_name} 识别到完成关卡已达 4 个 任务结束")
            return DEFAULT_HIT_BOX

        # 遍历识别所有没有次数的关卡 找到目标关卡
        targets_done_count = 0
        for i in tm_r.filtered_results:
            assert type(i) == TemplateMatchResult

            # 识别关卡名字
            ocr_r = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    list(battle_targets.keys()),
                    roi=toTuple(i.box),
                    roi_offset=(-63, -243, 126, 42),
                ),
                argv.image,
            )
            if ocr_r is None or not ocr_r.hit:
                log.debug(
                    f"{node_name} 识别关卡名字失败 但只要不是连续4次就是符合预期的"
                )
                continue
            targets_done_count += 1

        # 目标关卡数足够，任务结束
        if targets_done_count == len(battle_targets):
            log.debug(f"{node_name} 完成关卡已达 {battle_targets} 个 任务结束")
            return DEFAULT_HIT_BOX


# 识别关卡名 返回还有挑战次数的关卡名字box
@AgentServer.custom_recognition("TrainingEnterOneReco")
class TrainingEnterOneReco(CustomRecognition):
    # TODO 当前已有难度
    # _level = ["一", "二", "三", "四", "五", "六"]

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
                        "focus": {"Node.Action.Starting": f"进入{name}"}
                    },
                    "TrainingEnterFormation": {
                        "custom_recognition_param": {"level": level},
                        "focus": {"Node.Action.Starting": f"选择{name} 难度{level}"},
                    },
                    "TrainingDailyUpdate": {"custom_action_param": {"name": [name]}},
                }
            )

            # 返回文字位置
            return b_r2.box


# 识别关卡难度 返回匹配难度的出击按钮box
@AgentServer.custom_recognition("TrainingEnterFormationReco")
class TrainingEnterFormationReco(CustomRecognition):
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
        node_name = argv.node_name
        level = "六"
        try:
            p = parse_params(argv.custom_recognition_param, "level")
            level: str = p["level"]
        except Exception as e:
            log.warn(f"{node_name} 解析param失败: {e}")

        # 识别难度
        result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([r".{0,}难度.+"], roi=(470, 125, 158, 434)),
            argv.image,
        )
        if not result or not result.hit:
            log.debug(f"{node_name} 识别难度失败")
            return

        # 遍历判断关卡难度
        for i in result.filtered_results:
            assert type(i) == OCRResult
            if level in i.text:
                return (960, i.box[1], 93, 97)


# ---------- Act ----------


# 初始化
@AgentServer.custom_action("TrainingInitAct")
class TrainingInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global battle_targets
        try:
            attach = parse_attach(context, argv)
        except Exception as e:
            log.error(f"{argv.node_name} 参数解析失败: {e}")
            return False

        battle_targets.clear()
        battle_targets.update({k: v for k, v in attach.items() if v != "不进行"})
        if len(battle_targets) == 0:
            log.info(f"{argv.node_name} 未选择任何训练所关卡，任务结束")
            context.run_action_direct(JActionType.StopTask, JStopTask())
            return True

        return True
