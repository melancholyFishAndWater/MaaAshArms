"""
存储一日一次的任务信息的模块
"""

import json
from datetime import timedelta, datetime
from pathlib import Path

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

from base import DEFAULT_HIT_BOX, addListToTuple, is_hit, toTuple, log

# 凌晨 5 点跨天
OFFSET = timedelta(hours=5)

# 当前数据
_current_data = {}


def _claim_today_read(name: str, state: str = "ash_arms_daily.json") -> bool:
    """
    返回这个名字是否是今日第一次

    Args:
        name (str): 名字，可以是模块名或者任务名等
        state (str, optional): 存储文件名字. 默认 "ash_arms_daily.json".

    Returns:
        bool: 是否是今日第一次
    """
    global _current_data
    today = (datetime.now() - OFFSET).date().isoformat()
    path = Path(state)

    try:
        _current_data = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

        # 删除非今天的数据
        _current_data = {k: v for k, v in _current_data.items() if v == today}
    except Exception as e:
        _current_data = {}
        log.warn(f"{e}")

    if _current_data.get(name) == today:
        return False
    _current_data[name] = today
    return True


def _claim_today_write(state: str = "ash_arms_daily.json"):
    """
    更新数据每日一次数据

    Args:
        state (str, optional): 存储文件名. Defaults to "ash_arms_daily.json".
    """
    path = Path(state)
    path.write_text(json.dumps(_current_data), encoding="utf-8")


@AgentServer.custom_recognition("DaliyReco")
class DaliyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 提取变量
        p = argv.custom_recognition_param
        bool_ = bool(p) if p is not None else True

        # 判断是否跳过此次任务
        if bool_ and _claim_today_read(argv.node_name):
            return DEFAULT_HIT_BOX
        else:
            s = f"今日已执行过{argv.node_name}，跳过"
            log.info(s)
            context.run_action(
                "TaskStop",
                pipeline_override={"TaskStop": {"focus": {"Node.Action.Succeeded": s}}},
            )


@AgentServer.custom_action("DaliyAct")
class DaliyAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        _claim_today_write()
        log.info("更新每日一次数据")
        return True
