"""给识别/动作回调提供"当前节点是被谁的 next 命中的"。

框架的参数里没有父节点（`TaskDetail` 只有 task_id / entry / node_id_list / status / nodes），
能拿到的派发者只有 `Node.NextList` 事件里的 `name`（= 这一轮正在评估 next 的那个节点）。

坑：任务第一轮是框架给入口合成的自派发（`PipelineTask.cpp:38,43`），那里的 `name` 就是入口
自己；而节点自循环时 `name` 也等于自己。两者只能靠轮次区分，见 `father_name()`。
"""

from maa.agent.agent_server import AgentServer
from maa.context import Context, ContextEventSink
from maa.event_sink import NotificationType
from maa.pipeline import JNodeAttr
from maa.tasker import Tasker, TaskerEventSink


class _ParentTracker(ContextEventSink):
    """记录每个任务当前一轮的派发者与候选名单。

    状态挂在类上而不是实例上：下面的模块级函数直接读这三个字典，实例只用来注册。
    """

    rounds: dict[int, int] = {}  # task_id -> 已出现的 NextList 轮数
    current: dict[int, str] = {}  # task_id -> 当前轮 NextList 的 name（= 派发者）
    candidates: dict[int, list[JNodeAttr]] = {}  # task_id -> 当前轮的 next_list

    def on_node_next_list(
        self,
        context: Context,
        noti_type: NotificationType,
        detail: ContextEventSink.NodeNextListDetail,
    ) -> None:
        """Starting 时记下派发者与候选名单。

        Succeeded / Failed 带同一个 name，重复写没有意义，所以只处理 Starting。
        用 == 而不是 is：这层传进来时可能是裸 int，不保证是 NotificationType 实例。
        事件一定早于同一轮的识别回调：`PipelineTask.cpp:272` 的 notify 在跑识别（`:308`）之前，
        且 agent 侧这些消息在同一个 msg 线程里按收包顺序处理。
        """
        if noti_type == NotificationType.Starting:
            task_id = detail.task_id
            _ParentTracker.rounds[task_id] = _ParentTracker.rounds.get(task_id, 0) + 1
            _ParentTracker.current[task_id] = detail.name
            _ParentTracker.candidates[task_id] = detail.next_list


def dispatcher(task_id: int) -> str | None:
    """当前轮的派发者节点名，原样返回（第一轮它是入口节点自己）。"""
    return _ParentTracker.current.get(task_id)


def father_name(task_id: int, node_name: str) -> str | None:
    """派发 node_name 的父节点名；没有父节点时返回 None。

    只有任务第一轮没有父节点：那一轮的候选名单是框架给入口合成的 [入口] 自己
    （PipelineTask.cpp:38,43），回调必然属于入口节点，所以那里 name == node_name。
    之后每轮的 name 都是真实派发者，节点自循环（FactoryGetReward 那类）时也等于
    node_name —— 所以判"有没有父节点"必须带上轮次，只比 name 会把自循环误判成没有父节点。
    轮次按本进程见到的 NextList 数：agent 若在任务中途重启，会把那一轮当成第一轮。
    """
    father = _ParentTracker.current.get(task_id)
    if father is None:
        return None
    if father == node_name and _ParentTracker.rounds.get(task_id, 0) <= 1:
        return None
    return father


def candidates(task_id: int) -> tuple[JNodeAttr, ...] | None:
    """当前轮 next_list 的副本；该任务还没出现过 NextList_Starting 时返回 None。

    返回元组而不是内部 list；内部那份是 tracker 的状态，直接给出会被调用方改到。
    用 None 而不是 []，next_list 本身可以为空（即没有配置 next），两者含义不同。
    """
    items = _ParentTracker.candidates.get(task_id)
    return tuple(items) if items is not None else None


def reset(task_id: int) -> None:
    """清掉一个任务的记录"""
    _ParentTracker.rounds.pop(task_id, None)
    _ParentTracker.current.pop(task_id, None)
    _ParentTracker.candidates.pop(task_id, None)


class _TaskCleaner(TaskerEventSink):
    """任务结束时清掉它的记录，免得 task_id 越攒越多。

    必须单独一个 sink：两路消息都经 `self._on_raw_notification` 分发（按 Python 的 MRO），
    若把 on_tasker_task 写进 _ParentTracker，Tasker.Task.* 会命中 ContextEventSink 那份实现，
    只走到 on_unknown_notification，on_tasker_task 永远不执行。

    时机是安全的：`Tasker.Task.Succeeded/Failed` 在 `task_ptr->run()` 返回之后才发
    （`Tasker.cpp:353,362`），该任务的节点事件已经全部发完，reset 不会截断还在跑的那一轮。
    任务非正常结束（进程被杀）就不会有这条事件，留着几条记录也无妨。
    """

    def on_tasker_task(
        self,
        tasker: Tasker,
        noti_type: NotificationType,
        detail: TaskerEventSink.TaskerTaskDetail,
    ) -> None:
        if noti_type in (NotificationType.Succeeded, NotificationType.Failed):
            reset(detail.task_id)


# 手动注册而不是 @AgentServer.context_sink() / @AgentServer.tasker_sink()：那两个装饰器不是泛型
# （声明返回 type[ContextEventSink]），被装饰的类名会丢掉自己声明的 rounds/current/candidates
AgentServer.add_context_sink(_ParentTracker())
AgentServer.add_tasker_sink(_TaskCleaner())
