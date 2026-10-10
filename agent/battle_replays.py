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
@AgentServer.custom_action("ReplaysInitAct")
class ReplaysInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count, _max_count
        _finish_count = 0
        _max_count = 5

        try:
            p = parse_params(argv.custom_action_param, "times")
            _max_count = p["times"]
            log.debug(f"{argv.node_name} 设置 _max_count为{_max_count}")
        except Exception as e:
            log.warn(f"{argv.node_name} 解析param失败: {e}")

        return True


# 点击再次出击成功 离开奖励页面 完成数自增
@AgentServer.custom_action("ReplaysChooseReattackSuccessAct")
class ReplaysChooseReattackSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count
        _finish_count += 1
        if _max_count > 0 and _finish_count >= _max_count:
            log.info("满足结束次数，任务结束")
            context.run_action("TaskStop")
        return True


# 代理结束 往面板输出日志
@AgentServer.custom_action("ReplaysEndAct")
class ReplaysEndAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        log.info(f"代理结束，已完成次数：{context.get_hit_count(argv.node_name)}")
        return True
