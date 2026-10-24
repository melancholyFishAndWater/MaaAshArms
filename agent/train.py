from __future__ import annotations

from re import search
from numpy import ndarray
from typing import Literal

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import (
    Context,
    JRecognitionType,
    JActionType,
    Rect,
    TemplateMatchResult,
    OCRResult,
)
from maa.pipeline import JTarget, JTemplateMatch, JOCR, JSwipe

from agentlog import log
from rect import DEFAULT_HIT_BOX, toTuple
from params import parse_attach

_TrainStatus = Literal["acceptable", "rewardable", "busy"]

# 列车状态列表
_train_list: list[_Train] = []
# 当前路线名
_current_info_name = ""
# 目标路线列表
_targets: dict[str, int] = {}
# 总路线
_ROUTE: dict[str, int] = {
    "荒原": 7,
    "沙漠": 6,
    "冰原": 6,
    "森丘": 6,
    "熔被": 6,
    "警区": 6,
    "辐射区": 6,
    "遗迹": 6,
    "新大陆": 6,
}


# 列车类
class _Train:
    def __init__(self, stutus: _TrainStatus, route: str | Literal["unknow"]) -> None:
        self.stutus: _TrainStatus = stutus
        self.route: str | Literal["unknow"] = route

    def __str__(self) -> str:
        return f"列车 id:{id(self)} 状态:{self.stutus} 路线:{self.route}"


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


def _log_train_list():
    log.debug("调试显示: ")
    for i in range(len(_train_list)):
        log.debug(f"列表元素{i}: {_train_list[i]}")


# ---------- Reco ----------


# 判断是否任务结束
@AgentServer.custom_recognition("TrainCheckEndReco")
class TrainCheckEndReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 校验
        if len(_train_list) == 0:
            log.debug(f"{argv.node_name} 警告 列车对象列表为空")
            return

        # 判断
        if all(i.stutus == "busy" for i in _train_list):
            log.info(f"{len(_train_list)}列列车忙碌，全部忙碌，任务结束")
            return DEFAULT_HIT_BOX


# 返回目标STATION
@AgentServer.custom_recognition("TrainEnterFormationReco")
class TrainEnterFormationReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        import numpy as np
        from train_station import extract_station_digits

        node_name = argv.node_name
        if _targets and _current_info_name in _targets.keys():
            target_number = _targets[_current_info_name]
            log.debug(f"{node_name} 使用候选 {_current_info_name}: {target_number}")
        else:
            target_number = _ROUTE[_current_info_name]
            log.debug(f"{node_name} 使用备用 {_current_info_name}: {target_number}")

        # 1) 切分：拿到每张卡片的 (屏幕坐标 box, 黑字抹除后的灰度切图)
        crops = extract_station_digits(argv.image)

        # 站号, 屏幕box
        for box, view in crops:
            # 2) 灰度切图 (H, W) 转 3 通道 —— run_recognition_direct 要 (H, W, 3)
            img = np.stack([view] * 3, axis=2)

            # 3) only_rec=true：跳过检测，直接把 roi 区域当一行文本识别
            #    roi 必须精确到整张切图；expected 限定单个数字；threshold 收紧到 0.3
            r = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    [r"\d"],
                    roi=(0, 0, view.shape[1], view.shape[0]),
                    only_rec=True,
                    threshold=0.3,
                ),
                img,
            )
            if r is None or not r.hit:
                log.debug(f"{node_name} 识别STATION数字失败")
                continue

            # 4) 取结果
            number: int = 0
            for i in r.filtered_results:
                if isinstance(i, OCRResult) and i.text.isdigit():
                    number = int(i.text)
                    break
            else:
                log.debug(f"{node_name} ocr识别数字失败")
                continue

            # 5) 比对数字是否匹配
            if target_number != number:
                continue

            # 6) 识别发车按钮
            t_r = context.run_recognition_direct(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(
                    ["Train/CanTrainButton.png"],
                    roi=box,
                    roi_offset=(511, -27, 75, 96),
                ),
                argv.image,
            )

            if t_r is None or not t_r.hit:
                log.debug(f"{node_name} 识别发车按钮失败")
                return
            return t_r.box


# ---------- Action ----------


# 发车成功
@AgentServer.custom_action("TrainGoSuccessAct")
class TrainGoSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        first = next((i for i in _train_list if i.stutus == "acceptable"), None)

        # 判断是否 None
        if first is None:
            log.debug(f"{argv.node_name} 警告 列车对象列表中无目标对象")
            first = _Train("busy", _current_info_name)
            _log_train_list()
            _train_list.append(first)
        else:
            first.stutus = "busy"
            first.route = _current_info_name

        log.info(f"路线 {_current_info_name} 发车成功")
        return True


# 领取奖励成功
@AgentServer.custom_action("TrainGetRewardSuccessAct")
class TrainGetRewardSuccessAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        first = next((i for i in _train_list if i.stutus == "rewardable"), None)

        # 判断
        if first is None:
            log.debug(f"{argv.node_name} 警告 列车对象列表中无目标对象")
            first = _Train("busy", _current_info_name)
            _log_train_list()
            _train_list.append(first)
        else:
            first.stutus = "acceptable"

        log.info("领取列车奖励成功")
        return True


# ----- Init -----


# 是否初始化过
@AgentServer.custom_recognition("TrainProbeInitReco")
class TrainProbeInitReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return DEFAULT_HIT_BOX if len(_train_list) > 0 else None


# 从一览页面获取列车状态并记录出发数，若非全部出发，则返回点击box
@AgentServer.custom_recognition("TrainInitReco")
class TrainInitReco(CustomRecognition):
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

        name_result = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR([".+"], roi=roi, roi_offset=(-150, -5, 57, 45)),
            image,
        )
        assert name_result and name_result.hit
        assert name_result.best_result and type(name_result.best_result) == OCRResult
        _train_list.append(_Train(stutus, name_result.best_result.text))

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        global _targets
        # 提取变量
        node_name = argv.node_name

        attach = {}
        try:
            attach = parse_attach(
                context,
                argv,
                "level_one",
                "level_one_number",
                "level_two",
                "level_two_number",
            )

            def f(s: str):
                _targets[attach[f"level_{s}"]] = attach[f"level_{s}_number"]

            f("one")
            f("two")
        except Exception as e:
            log.debug(f"{node_name} 解析attach失败: {e!r}")
            log.warn(f"解析attach失败 配置的优先选择路线将不生效")

        # 识别派遣数
        r = _get_train_status_number(context, argv.image, (331, 129, 60, 53))
        if r is None:
            log.debug(f"{node_name} 识别派遣数失败")
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
                log.debug("关闭列车状态页面")
                context.run_task("TrainClickStatusArrow")
                _log_train_list()
                return

            # 遍历添加列车
            for i in busy_result.filtered_results:
                assert type(i) == TemplateMatchResult
                self.append_train(context, argv.image, toTuple(i.box), "busy")
        else:
            log.debug(f"{node_name} 识别未抵达列车失败")

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

        else:
            log.debug(f"{node_name} 识别已抵达列车失败")

        # 补充未出发列车
        for i in range(max_ - len(_train_list)):
            _train_list.append(_Train("acceptable", "unknow"))

        # 获取识别结果
        t_a, t_b, t_r = 0, 0, 0
        for i in _train_list:
            if i.stutus == "acceptable":
                t_a += 1
            elif i.stutus == "busy":
                t_b += 1
            else:
                t_r += 1
        log.debug(f"识别结果: 可接取数:{t_a}, 可领取数:{t_r}, 未抵达数:{t_b}")

        # 若识别到已抵达列车 则返回点击位置
        if not rewardable_result:
            log.debug(f"{node_name} 识别已抵达列车失败")
        elif rewardable_result.hit:
            _log_train_list()
            return rewardable_result.box

        # 搜索 前往派遣 的box
        can_train_result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Train/NoTrain.png"]),
            argv.image,
        )
        if not can_train_result:
            log.debug(f"{node_name} 识别前往派遣失败")
        elif can_train_result.hit:
            _log_train_list()
            return can_train_result.box

        log.error(f"{node_name} 识别错误")
        _log_train_list()


# ----- 领取 -----


# 判断是否可以领取火车奖励
@AgentServer.custom_recognition("TrainLoopRewardReco")
class TrainLoopRewardReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if any(i.stutus == "rewardable" for i in _train_list):
            return DEFAULT_HIT_BOX


# ----- 发车 -----


# 判断是否可以发车
@AgentServer.custom_recognition("TrainLoopAcceptableReco")
class TrainLoopAcceptableReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if any(i.stutus != "busy" for i in _train_list):
            return DEFAULT_HIT_BOX


# 返回目标路线名称box 或 None
# 候选词数小于2则按解锁状态返回返回 优先最新路线
@AgentServer.custom_recognition("TrainClickAcceptInfoReco")
class TrainClickAcceptInfoReco(CustomRecognition):
    # 路线解锁状态
    route_status: dict[str, bool] = {}

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Rect | None:
        # 提取变量
        global _current_info_name
        node_name = argv.node_name

        # 文字匹配
        r = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR([r".+路线"], roi=(49, 125, 123, 505)), argv.image
        )
        if not r or not r.hit:
            log.debug(f"{node_name} 路线名称识别失败")
            return

        # 识别并记录当前画面路线解锁状态 若满足候选列表则返回box
        for i in r.filtered_results:
            # 提取变量
            assert type(i) == OCRResult
            text = i.text.replace("路线", "")

            # 去杂项
            if "武装押运" in text:
                continue
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
                    log.debug(f"{node_name} 识别到路线 {text} 已解锁")
                elif r2 is not None:
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
                        log.info(f"识别到路线 {text} 锁定")
                    else:
                        log.debug(f"{node_name} 失败路线锁定失败")
                else:
                    log.debug(f"{node_name} 识别路线箭头失败")

            # 若路线繁忙 跳过此路线
            if any(text in i.route for i in _train_list):
                continue

            # 若在候选词中 且 路线解锁 则 返回路线名称box
            if text in _targets.keys():
                if text in self.route_status.keys() and self.route_status[text]:
                    log.info(f"选择候选关卡: {text}")
                    _current_info_name = text
                    return i.box

        # 若候选词列表长度大于等于2 则跳过默认返回
        if len(_targets) >= 2:
            log.debug(f"{node_name} 候选词充足，跳过使用备选候选词")
            return

        # 依次匹配备选关卡
        r_text: dict[str, Rect | None] = {i.text: i.box for i in r.filtered_results}  # type: ignore
        for i in reversed(_ROUTE.keys()):
            text = i

            # 未获得该路线状态 匹配结束
            if text not in self.route_status.keys():
                log.debug(f"{node_name} 未获得路线 {text} 状态，跳过返回")
                break

            # 路线未解锁 匹配下一个
            if not self.route_status[text]:
                log.debug(f"{node_name} 路线 {text} 未解锁，匹配下一个")
                continue

            # 路线正在忙碌 匹配下一个
            if any(j.stutus == "busy" and j.route == text for j in _train_list):
                log.debug(f"{node_name} 路线 {text} 未抵达，匹配下一个")
                continue

            if text in r_text.keys():
                _current_info_name = text.replace("路线", "")
                log.info(f"选择路线: {text}")
                return r_text[text]


# ----- 移动路线列表 -----

_move_up = True


# 识别并修改移动方向
@AgentServer.custom_recognition("TrainInfoMoverReco")
class TrainInfoMoverReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 提取变量
        n_name = argv.node_name

        # 是否修改移动方向
        if _in_info_bottom(context, argv.image):
            log.debug(f"{n_name} 列车路线在底部，设置路线移动方向为下滑")
            _move_up = False
        elif _in_info_top(context, argv.image):
            log.debug(f"{n_name} 列车路线在顶部，设置路线移动方向为上滑")
            _move_up = True

        return DEFAULT_HIT_BOX


# 尝试移动列表
@AgentServer.custom_action("TrainInfoMoverAct")
class TrainInfoMoverAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 提取变量
        n_name = argv.node_name

        # 移动方向
        if _move_up:
            r = _info_move_up(context)
            log.debug(f"{n_name} 上滑路线")
        else:
            r = _info_move_down(context)
            log.debug(f"{n_name} 下滑路线")

        return r and r.success or False
