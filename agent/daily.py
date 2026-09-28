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

OFFSET = timedelta(hours=5)  # 凌晨 5 点跨天


def _claim_today(name: str, state: str = "ash_arms_daily.json") -> bool:
    """
    返回这个名字是否是今日第一次

    Args:
        name (str): 名字，可以是模块名或者任务名等
        state (str, optional): 存储文件名字. 默认 "ash_arms_daily.json".

    Returns:
        bool: 是否没有在今日存储过
    """
    today = (datetime.now() - OFFSET).date().isoformat()
    path = Path(state)

    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as e:
        data = {}
        log.warn(f"{e}")

    # 删除非今天的数据
    data = {k: v for k, v in data.items() if v == today}

    if data.get(name) == today:
        return False
    data[name] = today
    path.write_text(json.dumps(data), encoding="utf-8")
    return True


@AgentServer.custom_recognition("DaliyReco")
class DaliyReco(CustomRecognition):
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
        p = argv.custom_recognition_param
        bool_ = bool(p) if p is not None else True

        # 判断是否跳过此次任务
        if bool_ and _claim_today(argv.node_name):
            return DEFAULT_HIT_BOX
        else:
            s = f"今日已执行过{argv.node_name}，跳过"
            log.info(s)
            context.run_action(
                "TaskStop",
                pipeline_override={"TaskStop": {"focus": {"Node.Action.Succeeded": s}}},
            )
