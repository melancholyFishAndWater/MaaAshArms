import json
from typing import Any, Literal

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import (
    Context,
    JRecognitionType,
    JActionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import JStopTask, JTarget, JTemplateMatch, JOCR, JSwipe
from re import search
from numpy import ndarray, dtype

from base import DEFAULT_HIT_BOX, addListToTuple, is_hit, toTuple, log, parse_params

# 自律完成次数
_finish_count = 0
# 最大完成次数
_max_count = 5


# ---------- Act ----------


# 初始化全局变量
@AgentServer.custom_action("BattleReplaysInitAct")
class BattleReplaysInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count, _max_count
        _finish_count = 0
        _max_count = 5

        try:
            p = parse_params(argv.custom_action_param, "times")
            _max_count = p["times"]
        except Exception as e:
            log.warn(f"{argv.node_name} 解析param失败: {e}")

        return True


# 点击再次出击成功 离开奖励页面 完成数自增
@AgentServer.custom_action("BattleReplaysChooseReattackSuccessAct")
class BattleReplaysChooseReattackSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count
        _finish_count += 1
        if _finish_count >= _max_count:
            log.info("满足结束次数，任务结束")
            context.run_action("TaskStop")
        return True


# 清空 [点击出击按钮] 的hit_count
@AgentServer.custom_action("BattleReplaysClearAttackHitAct")
class BattleReplaysClearAttackHitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return context.clear_hit_count("BattleReplaysChooseAttack")
