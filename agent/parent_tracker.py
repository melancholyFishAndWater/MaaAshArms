"""给识别/动作回调提供"当前节点是被谁的 next 命中的"。

框架的回调参数里没有父节点，唯一来源是 `Node.NextList` 事件的 `name`（= 这一轮评估 next 的节点）。
第一轮是框架给入口合成的自派发（`PipelineTask.cpp:38,43`），`name` 就是入口自己；节点自循环时 `name`
也等于自己，两者只能按轮次区分，见 `father_name()`。
"""

from maa.agent.agent_server import AgentServer
from maa.context import Context, ContextEventSink
from maa.event_sink import NotificationType
from maa.pipeline import JNodeAttr
from maa.tasker import Tasker, TaskerEventSink


class _ParentTracker(ContextEventSink):
    """记录每任务当前一轮的派发者与候选名单。状态挂类上，实例只用来注册。"""

    rounds: dict[int, int] = {}  # task_id -> NextList 轮数
    current: dict[int, str] = {}  # task_id -> 当前轮派发者名
    candidates: dict[int, list[JNodeAttr]] = {}  # task_id -> 当前轮 next_list

    def on_node_next_list(
        self,
        context: Context,
        noti_type: NotificationType,
        detail: ContextEventSink.NodeNextListDetail,
    ) -> None:
        """Starting 时记下派发者与候选名单。

        只处理 Starting：Succeeded / Failed 的 name 相同。用 `==` 而非 `is`，这层传进来的可能是裸 int。
        事件早于同轮识别回调（`PipelineTask.cpp:272` 对 `:308`），同一 msg 线程按序处理，所以回调执行时
        状态已是本轮。
        """

        if noti_type == NotificationType.Starting:
            task_id = detail.task_id
            _ParentTracker.rounds[task_id] = _ParentTracker.rounds.get(task_id, 0) + 1
            _ParentTracker.current[task_id] = detail.name
            _ParentTracker.candidates[task_id] = detail.next_list


def dispatcher(task_id: int) -> str | None:
    """当前轮的派发者名，原样返回，不做首轮判断。

    首轮这里是入口自己（框架合成的自派发），自循环时是本节点。要"没有父节点"这个结论用 `father_name()`；
    还没收到 NextList_Starting（或已 reset）时两者都给 None。
    """
    return _ParentTracker.current.get(task_id)


def father_name(task_id: int, node_name: str) -> str | None:
    """当前轮的派发者名；没有父节点时返回 None。

    与 `dispatcher()` 的唯一差别是首轮：那一轮的候选名单是框架合成的 [入口] 自己，
    回调必然属于入口，所以 name == node_name，这里返回 None，而
    dispatcher 仍返回入口名。第 2 轮起 name 无论是否自循环都是真实派发者，两者返回同一个值。

    判"有没有父节点"必须带上轮次：自循环时 name 也等于 node_name，只比名字会把自循环误判成没有父节点。
    轮次按本进程见到的 NextList 数，agent 中途重启会把那一轮当成第一轮。
    """

    father = dispatcher(task_id)
    if father is None:
        return None
    if father == node_name and _ParentTracker.rounds.get(task_id, 0) <= 1:
        return None
    return father


def candidates(task_id: int) -> tuple[JNodeAttr, ...] | None:
    """当前轮 next_list 的副本；没出现过 NextList_Starting 时返回 None。

    返回元组防止调用方改到内部状态；None 与空列表含义不同（next_list 本身可以为空）。
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

    必须独立成类：两路消息都走 `self._on_raw_notification`，写进 _ParentTracker 会按 MRO 命中
    ContextEventSink 那份，on_tasker_task 永不执行。
    Succeeded/Failed 在 run() 返回后才发（`Tasker.cpp:353,362`），节点事件已发完，reset 不会截断本轮。
    """

    def on_tasker_task(
        self,
        tasker: Tasker,
        noti_type: NotificationType,
        detail: TaskerEventSink.TaskerTaskDetail,
    ) -> None:
        if noti_type in (NotificationType.Succeeded, NotificationType.Failed):
            reset(detail.task_id)


# 手写注册：context_sink() / tasker_sink() 不是泛型，装饰后类名会丢掉 rounds/current/candidates
AgentServer.add_context_sink(_ParentTracker())
AgentServer.add_tasker_sink(_TaskCleaner())
