import json
import time
from typing import Any, TypedDict

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import (
    Context,
    JRecognitionType,
    JActionType,
    Rect,
    TemplateMatchResult,
    RecognitionDetail,
    OCRResult,
)
from maa.pipeline import JTemplateMatch, JOCR, JSwipe
from re import search
from numpy import ndarray, dtype

# 默认hit box 仅用于不需要坐标的节点
DEFAULT_HIT_BOX = [0] * 4


def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])


def is_hit(detail: RecognitionDetail | None) -> bool:
    return detail is not None and detail.hit


# ---------- Reco ----------


# TODO 更详细的log 画面不同 第一次匹配
# 如果连续k次画面不变 则返回roi box 一般和move系列搭配判断移动前后是否有明显变化
@AgentServer.custom_recognition("IsFreezesReco")
class IsFreezesReco(CustomRecognition):
    __freezes_dict: dict[tuple[int, str], int] = {}

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):

        # 提取变量
        p = (
            json.loads(argv.custom_recognition_param)
            if argv.custom_recognition_param
            else {}
        )
        from_ = p.get("from_", "unknow")  # 父节点
        thr = float(p.get("threshold", 0.99))  # 识别阈值
        k = p.get("k", 3)  # 连续不变次数
        name = f"probe_{from_}"  # 图片名字
        key = (argv.task_detail.task_id, from_)
        x, y, w, h = argv.roi

        # 保证画面处于静止
        context.wait_freezes(2000, box=(x, y, w, h))

        # 有上一张图像，则识别
        r = None
        if key in self.__freezes_dict.keys():
            r = context.run_recognition_direct(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(
                    template=[name], roi=(x, y, w, h), threshold=[thr], method=5
                ),
                argv.image,
            )

        # 判断是否命中
        same = bool(r and r.hit)

        # 存储本帧识别
        if not context.override_image(name, argv.image[y : y + h, x : x + w]):
            print(f"override_image failed: {name}")
            same = False

        # 存储连续识别成功次数
        self.__freezes_dict[key] = (self.__freezes_dict.get(key, 0) + 1) if same else 0

        # 条件返回识别成功
        if self.__freezes_dict[key] >= k:
            del self.__freezes_dict[key]
            return (x, y, w, h)


# ---------- Action ----------

# ---------- MoveUpDown ----------

_move_up_down_dict: dict[tuple[int, str], int] = {}
_move_entry: str = "MoveUp"


# 若还有滑动次数 返回非 None
@AgentServer.custom_recognition("MoveUpDownReco")
class MoveUpDownReco(CustomRecognition):
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
        param: dict[str, str] = s and json.loads(s) or {}
        from_ = param.get("from_", "unknow")
        x = int(param.get("x", 10))

        # 合成key
        key = (argv.task_detail.task_id, from_)

        # 判断是否还有次数
        times = _move_up_down_dict.get(key, 0)
        if times >= x * 2:
            return

        # 自增
        times += 1
        _move_up_down_dict[key] = times

        # 移动方向
        global _move_entry
        if times <= x:
            _move_entry = "MoveUp"
        else:
            _move_entry = "MoveDown"

        return DEFAULT_HIT_BOX


# 上滑动x次 下滑动x次
@AgentServer.custom_action("MoveUpDownAct")
class MoveUpDownAct(CustomAction):

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 移动
        r = context.run_action(_move_entry)

        # 返回结果
        if r is not None:
            return r.success
        return False
