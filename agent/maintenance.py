"""维修会任务的组合识别。

纯 pipeline 表达不了「有忙碌按钮 且 没有接取按钮 且 没有完成按钮」：

- `And` / `Or` 解引用节点时只取被引用节点的 `recognition` 与 `recognition_param`
  （`Task/Component/Recognizer.cpp:389`），节点顶层的 `inverse` 不会跟进来；
- `all_of` / `any_of` 的对象元素只解析 `recognition` / `recognition_param` / `sub_name`
  （`Resource/PipelineTypes.h:54-60`、`Resource/PipelineParser.cpp:1952-1986`），没有取反位；
- 节点自己的 `inverse` 反转的是**整个**节点（`Task/TaskBase.cpp:82-87`），拿它当
  「既没有 A 也没有 B」的出口，在按钮还没渲染出来的那一帧必然命中。

所以这个三条件的与放在 agent 侧，并要求连续若干轮都成立。三张图都在这边直接做模板匹配
（`run_recognition_direct`），不再为它们在 pipeline 里留探针节点；**图片路径要写成字面量**，
校验脚本靠字面量核对图片是否存在，包一层函数传变量就查不到了（`C7-agent-template-dynamic`）。
"""

from maa.agent.agent_server import AgentServer
from maa.context import Context, Rect
from maa.custom_recognition import CustomRecognition
from maa.pipeline import JRecognitionType, JTemplateMatch

from agentlog import log

# 卡片右侧那一列：三种按钮都只出现在这里
_STRIP = (1021, 162, 134, 311)
# 连续成立的轮数：卡片是逐张画的，齿轮可能比另一张卡的「+」「完成」先出来，
# 单帧判定会在半渲染的帧上把任务提前收掉
_REQUIRED_STREAK = 2
# task_id -> 已连续成立的轮数
_streak_by_task: dict[int, int] = {}


@AgentServer.custom_recognition("MaintenanceProbeAllBusyReco")
class MaintenanceProbeAllBusyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult | Rect | None:
        task_id = argv.task_detail.task_id

        # 阳性判据：卡片上有「加派飞船」齿轮
        busy = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Maintenance/Busy.png"], roi=_STRIP),
            argv.image,
        )
        if not busy or not busy.hit:
            _streak_by_task.pop(task_id, None)
            return None

        # 必须同时缺席的：可接取（+）
        able = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Maintenance/Able.png"], roi=_STRIP),
            argv.image,
        )
        if able and able.hit:
            _streak_by_task.pop(task_id, None)
            return None

        # 必须同时缺席的：可领取（完成）
        done = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(["Maintenance/Done.png"], roi=_STRIP),
            argv.image,
        )
        if done and done.hit:
            _streak_by_task.pop(task_id, None)
            return None

        streak = _streak_by_task.get(task_id, 0) + 1
        if streak < _REQUIRED_STREAK:
            _streak_by_task[task_id] = streak
            log.debug(f"{argv.node_name} 全忙判定 {streak}/{_REQUIRED_STREAK}，继续观察")
            return None

        _streak_by_task.pop(task_id, None)
        log.debug(f"{argv.node_name} 全忙判定成立")
        return busy.box