"""assets/resource/pipeline/base.json 里通用节点的实现。

`base.json` 放"哪个任务都能引用"的通用节点，本模块放它们的识别/动作回调，注册名 = 节点名 +
后缀（识别 `<节点名>Reco`、动作 `<节点名>Act`，见 docs/zh_cn/develop/node-naming.md 第七节）。
对应关系：`IsFreezes`→`IsFreezesReco`、`TaskStopError`→`TaskStopErrorAct`、
`MoveUpDown`→`MoveUpDownReco`/`MoveUpDownAct`、`LoopErrorByTimes`→`LoopErrorByTimesReco`。

放在 agent/shared/ 下靠 agent/main.py 把它注入 sys.path 后按顶层名导入；
main.py 的 import 不能漏——装饰器只在模块导入时执行，漏了就是静默不注册。
"""

import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import (
    Context,
    ContextEventSink,
    JRecognitionType,
)
from maa.define import OCRResult
from maa.event_sink import NotificationType
from maa.pipeline import JOCR, JTemplateMatch

from agentlog import log
from params import parse_params
from parent_tracker import father_name
from rect import DEFAULT_HIT_BOX

# ---------- Reco ----------


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
        context.wait_freezes(1000, box=(x, y, w, h))

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
        else:
            log.debug(f"{argv.node_name} 第一次识别冻结，跳过识别")

        # 判断是否冻结
        same = bool(r and r.hit)
        if same:
            log.debug(f"{argv.node_name} 检测到画面冻结")

        # 存储本帧识别
        if not context.override_image(name, argv.image[y : y + h, x : x + w]):
            print(f"override_image failed: {name}")
            same = False
            log.warn(f"{argv.node_name} 覆写图片失败，冻结次数重置")

        # 存储连续识别成功次数
        self.__freezes_dict[key] = (self.__freezes_dict.get(key, 0) + 1) if same else 0

        # 条件返回识别成功
        if self.__freezes_dict[key] >= k:
            del self.__freezes_dict[key]
            log.debug(f"{argv.node_name} 画面冻结次数超过{k}，返回识别区域box")
            return (x, y, w, h)


@AgentServer.custom_recognition("BaseBranchErrorConfirmReco")
class BaseBranchErrorConfirmReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 标题
        ocr_r1 = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR([".+"], roi=(412, 243, 474, 40)), argv.image
        )
        if ocr_r1 is not None and ocr_r1.hit:
            assert type(ocr_r1.best_result) == OCRResult
            log.info(ocr_r1.best_result.text)
        else:
            log.debug(f"{argv.node_name} 未捕获到错误模态弹窗标题")

        # 内容
        ocr_r2 = context.run_recognition_direct(
            JRecognitionType.OCR, JOCR([".+"], roi=(359, 293, 566, 141)), argv.image
        )
        if ocr_r2 is not None and ocr_r2.hit:
            for i in ocr_r2.filtered_results:
                assert type(i) == OCRResult
                log.info(i.text)
        else:
            log.debug(f"{argv.node_name} 未捕获到错误模态弹窗内容")

        return DEFAULT_HIT_BOX


# ---------- Act ----------


# 始终执行失败
@AgentServer.custom_action("TaskStopErrorAct")
class TaskStopErrorAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return False


# ----- LoopErrorByTimes -----

_current_task_id: int = -1
# 父节点名 -> 连续未命中轮数
_times_by_name: dict[str, int] = {}


# 返回 box 以结束任务
@AgentServer.custom_recognition("LoopErrorByTimesReco")
class LoopErrorByTimesReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 提取变量
        global _current_task_id
        task_id = argv.task_detail.task_id
        node_name = argv.node_name
        name = father_name(argv.task_detail.task_id, node_name)
        if name is None:
            log.warn(f"{node_name} 获取父节点失败")
            return
        if _current_task_id != task_id:
            _current_task_id = task_id
            _times_by_name.clear()

        # 解析param
        times = 10
        try:
            p = parse_params(argv.custom_recognition_param, "times")
            times = p["times"]
        except Exception as e:
            log.warn(f"{argv.node_name} 解析param失败: {e}")

        # 判断和更新字典
        key = name
        _times_by_name[key] = _times_by_name.get(key, 0) + 1
        if _times_by_name[key] >= times:
            log.warn(f"{name} 连续 {_times_by_name[key]} 轮未命中，结束任务")
            return DEFAULT_HIT_BOX
        return


# 注册上下文监听器 当next中有节点命中时，清空累积次数
@AgentServer.context_sink()
class _LoopErrorResetSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail) -> None:
        if noti_type == NotificationType.Succeeded:
            _times_by_name.pop(detail.name, None)


# ----- MoveUpDown -----

_move_up_down_dict: dict[tuple[int, str], int] = {}
_move_entry: str = "MoveUp"


# 若还有滑动次数 返回非 None
@AgentServer.custom_recognition("MoveUpDownReco")
class MoveUpDownReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 提取变量
        s = argv.custom_recognition_param
        param: dict[str, str] = s and json.loads(s) or {}
        from_ = param.get("from_", "unknow")
        x = int(param.get("x", 10))
        n_name = argv.node_name

        # 合成key
        key = (argv.task_detail.task_id, from_)

        # 判断是否还有次数
        times = _move_up_down_dict.get(key, 0)
        if times >= x * 2:
            log.debug(f"{n_name} 移动次数耗尽，取消移动")
            return
        log.debug(f"{n_name} 当前移动次数: {times}")

        # 自增
        times += 1
        _move_up_down_dict[key] = times

        # 移动方向
        global _move_entry
        if times <= x:
            _move_entry = "MoveUp"
        else:
            _move_entry = "MoveDown"
        log.debug(f"{n_name} 当前移动方向: {_move_entry}")

        return DEFAULT_HIT_BOX


# 上滑动x次 下滑动x次
@AgentServer.custom_action("MoveUpDownAct")
class MoveUpDownAct(CustomAction):

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 移动
        r = context.run_action(_move_entry)
        log.debug(f"{argv.node_name} 执行移动: {_move_entry}")

        # 返回结果
        if r is not None:
            return r.success
        return False
