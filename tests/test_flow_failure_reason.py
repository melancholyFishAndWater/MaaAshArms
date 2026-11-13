"""终止失败原因必须区分明确证据与推断，不跨任务/进程取证。"""
import json
from tools.drawing.lib.flowlog.logparse import parse_log


def line(second, message, px="Px1"):
    return f"[2026-11-13 11:10:{second:06.3f}][INF][{px}][Tx1][Event.cpp][L1][notify] {message}\n"


def event(second, kind, name="Page", tid=7, px="Px1"):
    data = {"name": name, "task_id": tid, "list": [{"name": "Confirm"}]}
    return line(second, f"[msg={kind}] [details={json.dumps(data)}]", px)


def terminal(prefix=""):
    return prefix + event(48.7, "Node.PipelineNode.Failed") + event(48.8, "Tasker.Task.Failed")


def test_unknown_failure_is_explicit():
    parsed = parse_log(terminal(), task_id=7, t_from=None, t_to=None, keep_internal=False)
    assert any("原因未知" in step.name for step in parsed.events)
    assert not any("超时" in step.name for step in parsed.events)


def test_repeated_misses_are_only_suspected_timeout():
    prefix = event(28, "Node.NextList.Failed") + event(46.8, "Node.NextList.Failed")
    parsed = parse_log(terminal(prefix), task_id=7, t_from=None, t_to=None, keep_internal=False)
    failure = next(step for step in parsed.events if "疑似识别超时" in step.name)
    assert "×2" in failure.detail and "缺少明确超时日志" in failure.detail
    assert failure.task_id == 7


def test_success_resets_miss_evidence():
    prefix = event(28, "Node.NextList.Failed") + event(46, "Node.NextList.Failed") + event(47, "Node.NextList.Succeeded")
    assert not any("超时" in step.name for step in parse_log(terminal(prefix), task_id=7, t_from=None, t_to=None, keep_internal=False).events)


def test_foreign_task_or_process_is_not_timeout_evidence():
    for tid, px in ((8, "Px1"), (7, "Px2")):
        prefix = event(28, "Node.NextList.Failed", tid=tid, px=px) + event(46.8, "Node.NextList.Failed", tid=tid, px=px)
        assert not any("超时" in step.name for step in parse_log(terminal(prefix), task_id=7, t_from=None, t_to=None, keep_internal=False).events)


def test_explicit_timeout_remains_confirmed_and_not_duplicated():
    prefix = line(48.6, "Task timeout [pretask.name=Page] [duration_since(start_clock)=21000ms] [pretask.reco_timeout=20000ms]")
    parsed = parse_log(terminal(prefix), task_id=7, t_from=None, t_to=None, keep_internal=False)
    assert len(parsed.events) == 1
    assert parsed.events[0].name == "识别超时 Page"
    assert "21000ms" in parsed.events[0].detail
