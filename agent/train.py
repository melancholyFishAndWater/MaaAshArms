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

from base import addListToTuple, is_hit, toTuple

_TrainStatus = Literal["acceptable", "rewardable", "busy"]

# 列车状态列表
_train_list: list[_Train] = []
# 当前路线名
_current_info_name = ""


# 列车类
class _Train:
    def __init__(self, stutus: _TrainStatus, route: str | Literal["unknow"]) -> None:
        self.stutus: _TrainStatus = stutus
        self.route: str | Literal["unknow"] = route


# 移动火车路线列表
def _info_move(context: Context, start: int, end: int):
    r = context.run_action_direct(
        JActionType.Swipe,
        JSwipe(begin=(120, start, 10, 10), end=[(120, end, 10, 10)]),
    )
    context.wait_freezes(200, box=(18, 103, 247, 595))
    return r


def _info_move_up(context: Context):
    return _info_move(context, 400, 300)


def _info_move_down(context: Context):
    return _info_move(context, 300, 400)


# 在列表顶部
def _in_info_top(context: Context, image: ndarray) -> bool:
    r = context.run_recognition_direct(
        JRecognitionType.OCR, JOCR(["荒原路线"], roi=(31, 147, 150, 49)), image
    )
    if r and r.hit:
        return True
    return False


# 在列表底部
def _in_info_bottom(context: Context, image: ndarray) -> bool:
    # 是否有未解锁路线
    locked_result = context.run_recognition_direct(
        JRecognitionType.TemplateMatch,
        JTemplateMatch(["Train/ArrowLocked.png"], roi=(177, 122, 79, 542)),
        image,
    )
    if locked_result and locked_result.hit:
        return True

    # 是否为最终路线
    last_result = context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR(["新大陆路线"], roi=(52, 563, 118, 73)),
        image,
    )
    if last_result and last_result.hit:
        return True
    return False


# 对指定区域识别列车派遣数 返回 tuple[已发车数, 上限, box] | None
def _get_train_status_number(
    context: Context, image: ndarray, roi: JTarget
) -> tuple[int, int, Rect | None] | None:
    r = context.run_recognition_direct(
        JRecognitionType.OCR,
        JOCR([r"\d/\d"], roi=roi),
        image,
    )
    if not r or not r.hit:
        return

    # 提取返回值
    ocr_r = r.best_result
    assert type(ocr_r) == OCRResult

    # 正则匹配并比较
    m = search(r"(\d)/(\d)", ocr_r.text)
    if not m:
        return
    return (int(m.group(1)), int(m.group(2)), r.box)


# ---------- Reco ----------


# TODO 警告用户识别失败
# 从主页获取列车状态并记录出发数，若非全部出发，则返回点击box
@AgentServer.custom_recognition("TrainGetStatusReco")
class TrainGetStatusReco(CustomRecognition):
    def append_train(
        self,
        context: Context,
        image: ndarray,
        roi: tuple[int, int, int, int],
        stutus: _TrainStatus,
    ):
        """识别路线名字，并添加列车到列表中

        Args:
            context (Context): 上下文
            image (ndarray): 图像
            roi (tuple[int, int, int, int]): roi
            stutus (_TrainStatus): 列车状态
        """
        # 路线运行状态到路线文字的偏移量
        OFFSET = (-150, -5, 57, 45)

        name_result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([".+"], roi=roi, roi_offset=OFFSET),
            image,
        )
        assert name_result and name_result.hit
        assert name_result.best_result and type(name_result.best_result) == OCRResult
        _train_list.append(_Train(stutus, name_result.best_result.text + "路线"))

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

        # 识别派遣数
        r = _get_train_status_number(context, argv.image, (331, 129, 60, 53))
        if r is None:
            return

        # 提取变量 min为未领取数+未完成数
        global _train_list
        _train_list = []
        max_ = r[1]

        # 识别未抵达列车
        busy_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/TrainPending.png"]),
            argv.image,
        )
        if busy_result and busy_result.hit:
            # 若识别均忙 提前结束
            if len(busy_result.filtered_results) == max_:
                context.run_action_direct(JActionType.StopTask, JStopTask())
                return

            # 遍历添加列车
            for i in busy_result.filtered_results:
                assert type(i) == TemplateMatchResult
                self.append_train(context, argv.image, toTuple(i.box), "busy")

        # 识别已抵达列车
        rewardable_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                ["Train/TrainCompleted.png"],
                roi=(41, 189, 319, 165),
            ),
            argv.image,
        )
        if rewardable_result and rewardable_result.hit:
            # 遍历添加可领取列车
            for i in rewardable_result.filtered_results:
                assert type(i) == TemplateMatchResult
                self.append_train(context, argv.image, toTuple(i.box), "rewardable")

            # 修改 TrainIsRewardable 节点的 max_hit
            context.override_pipeline(
                {
                    "TrainIsRewardable": {
                        "max_hit": len(rewardable_result.filtered_results)
                    }
                }
            )

        # 补充未出发列车
        for i in range(max_ - len(_train_list)):
            _train_list.append(_Train("acceptable", "unknow"))

        # 若识别到已抵达列车 则返回点击位置
        if rewardable_result and rewardable_result.hit:
            return rewardable_result.box

        # 搜索 前往派遣 的box
        can_train_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/NoTrain.png"]),
            argv.image,
        )
        if not can_train_result:
            return
        return can_train_result.box


# 判断是否任务结束
@AgentServer.custom_recognition("TrainCheckEndReco")
class TrainCheckEndReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        assert len(_train_list) > 0
        if all(i.stutus == "busy" for i in _train_list):
            return [0] * 4


# ----- 领取 -----


# 判断是否可以领取火车奖励
@AgentServer.custom_recognition("TrainIsRewardableReco")
class TrainIsRewardableReco(CustomRecognition):

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if any(i.stutus == "rewardable" for i in _train_list):
            return [0] * 4


# ----- 发车 -----


# 判断是否可以发车
@AgentServer.custom_recognition("TrainIsAcceptableReco")
class TrainIsAcceptableReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if any(i.stutus != "busy" for i in _train_list):
            return [0] * 4


# 返回路线名称box 或 None
# 候选词数小于2则按解锁状态返回返回 优先最新路线
@AgentServer.custom_recognition("TrainClickAcceptInfoReco")
class TrainClickAcceptInfoReco(CustomRecognition):
    # 新路线的时候更新
    _ROUTE = [
        "新大陆",
        "遗迹",
        "辐射区",
        "警区",
        "熔被",
        "森丘",
        "冰原",
        "沙漠",
        "荒原",
    ]

    # TODO 按主线进度或存储？
    # 路线解锁状态
    route_status: dict[str, bool] = {}

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        # 若不能发车 则跳过判定 测试时需注释掉
        if all(i.stutus == "busy" for i in _train_list):
            return
        global _current_info_name

        # 文字匹配
        r = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR([r".+路线"], roi=(49, 125, 123, 505)), argv.image
        )
        if not r or not r.hit:
            return

        # 候选列表
        s = argv.custom_recognition_param
        param: list[str] = s and json.loads(s) or []

        # 识别并记录当前画面路线解锁状态 若满足候选列表则返回box
        for i in r.filtered_results:
            # 提取变量
            assert type(i) == OCRResult
            text = i.text
            keys = self.route_status.keys()

            # 若没识别过此路线
            if text not in keys:
                # 识别路线是否解锁
                r2 = context.run_recognition_direct(
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(
                        ["Train/ArrowDart.png", "Train/ArrowLight.png"],
                        roi=toTuple(i.box),
                        roi_offset=(127, -16, -40, 69),
                        method=10001,
                    ),
                    argv.image,
                )
                if r2 and r2.hit:
                    self.route_status[text] = True
                elif r2:
                    # 没有匹配到箭头 尝试匹配锁的图片
                    r3 = context.run_recognition_direct(
                        JRecognitionType.TemplateMatch,
                        JTemplateMatch(
                            ["Train/ArrowLocked.png"],
                            roi=toTuple(i.box),
                            roi_offset=(127, -16, -40, 69),
                            method=10001,
                        ),
                        argv.image,
                    )
                    if r3 and r3.hit:
                        self.route_status[text] = False

            # 若在候选词中 且 路线解锁 则 返回路线名称box
            if text in param:
                if text in self.route_status.keys() and self.route_status[text]:
                    _current_info_name = text
                    return i.box

        # 若候选词列表长度大于等于2 则跳过默认返回
        if len(param) >= 2:
            return
        r_text: dict[str, Rect | None] = {i.text: i.box for i in r.filtered_results}  # type: ignore
        # 依次匹配列表
        for i in self._ROUTE:
            text = i + "路线"

            # 未获得该路线状态 匹配结束
            if text not in self.route_status.keys():
                break

            # 路线未解锁 匹配下一个
            if not self.route_status[text]:
                continue

            # 路线正在忙碌 匹配下一个
            if any(j.stutus == "busy" and j.route == text for j in _train_list):
                continue

            if text in r_text.keys():
                _current_info_name = text
                return r_text[text]


# TODO 目标路线
# @AgentServer.custom_recognition("TrainAcceptReco")
# class TrainAcceptReco(CustomRecognition):
#     def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg) -> CustomRecognition.AnalyzeResult | Rect | list[int] | ndarray[tuple[Any, ...], dtype[Any]] | tuple[int, int, int, int] | None:
#         return super().analyze(context, argv)

# ---------- Action ----------


# 尝试移动列表
@AgentServer.custom_action("TrainInfoMoverAct")
class TrainInfoMoverAct(CustomAction):
    _move_up = True

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:

        # 是否修改移动方向
        if _in_info_bottom(context, argv.reco_detail.raw_image):  # BUG
            self._move_up = False
        elif _in_info_top(context, argv.reco_detail.raw_image):  # BUG
            self._move_up = True

        # 移动方向
        if self._move_up:
            r = _info_move_up(context)
        else:
            r = _info_move_down(context)

        return r and r.success or False


# 发车成功
@AgentServer.custom_action("TrainBusyAct")
class TrainBusyAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        first = next(i for i in _train_list if i.stutus == "acceptable")
        first.stutus = "busy"
        first.route = _current_info_name
        return True


# 领取奖励成功
@AgentServer.custom_action("TrainGetRewardEndAct")
class TrainGetRewardEndAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        first = next(i for i in _train_list if i.stutus == "rewardable")
        first.stutus = "acceptable"
        return True
