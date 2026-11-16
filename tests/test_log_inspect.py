"""本地日志筛选的黑盒契约：只读、精确过滤、来源与分页。"""
import json

import pytest

from tools.drawing.lib.log_inspect import main


def invoke(capsys, *args):
    assert main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def native(msg, pid="Px1", second=0):
    return f"[2026-11-16 20:00:{second:02d}.000][INF][{pid}][Tx1][Tasker.cpp][L1][run] {msg}\n"


@pytest.fixture
def logs(tmp_path):
    directory = tmp_path / "debug"
    directory.mkdir()
    first = directory / "maafw.bak.log"
    first.write_text(
        native('task start: [cb_detail={"entry":"TaskGift","task_id":7}]')
        + native('PipelineTask node done [result={"name":"Gift","completed":true}] [task_id_=7]', second=1)
        + native('task end: [cb_detail={"entry":"TaskGift","task_id":7}] [ret=true]', second=2)
        + native('task start: [cb_detail={"entry":"TaskOther","task_id":71}]', second=3)
        + native('PipelineTask node done [result={"name":"Gift2","completed":true}] [task_id_=71]', second=4)
        + native('task start: [cb_detail={"entry":"TaskZero","task_id":8}]', second=5)
        + native('task start: [cb_detail={"entry":"TaskGift","task_id":7}]', pid="Px2", second=6),
        encoding="utf-8",
    )
    second = directory / "maafw.log"
    second.write_text(native('task end: [cb_detail={"entry":"TaskGift","task_id":7}] [ret=false]'), encoding="utf-8")
    panel = tmp_path / "logs"
    panel.mkdir()
    (panel / "panel.log").write_text(
        "[2026-11-16 20:00:00.000][ERR] Exception: 示例\nTraceback detail\n"
        "[2026-11-16 20:00:01.000][INF] 开始任务：启动游戏\n", encoding="utf-8"
    )
    return tmp_path, first


def test_inventory_discovers_rotated_panel_and_deduplicates(logs, capsys):
    root, first = logs
    result = invoke(capsys, str(root), str(first))
    assert result["schema_version"] == 1
    assert result["total"] == 3
    assert len(result["items"]) == 3
    assert len({item["path"] for item in result["items"]}) == 3


def test_search_task_id_has_numeric_boundary(logs, capsys):
    root, _ = logs
    result = invoke(capsys, str(root), "--view", "search", "--task-id", "7", "--pid", "Px1")
    assert result["total"] == 4
    assert all("71" not in item["text"] for item in result["items"])
    assert all(item["line"] >= 1 and item["path"] for item in result["items"])


def test_node_filter_is_exact(logs, capsys):
    root, _ = logs
    result = invoke(capsys, str(root), "--view", "search", "--node", "Gift")
    assert result["total"] == 1
    assert '"Gift"' in result["items"][0]["text"]


def test_pagination_and_no_matches(logs, capsys):
    root, _ = logs
    result = invoke(capsys, str(root), "--view", "search", "--limit", "1", "--offset", "1")
    assert len(result["items"]) == 1 and result["has_more"]
    result = invoke(capsys, str(root), "--view", "search", "--contains", "DOES_NOT_EXIST")
    assert result["items"] == [] and result["total"] == 0 and not result["has_more"]


def test_panel_error_context_keeps_line_numbers(logs, capsys):
    root, _ = logs
    result = invoke(capsys, str(root / "logs"), "--view", "search", "--level", "ERR", "--contains", "Exception", "--context", "1")
    item = result["items"][0]
    assert item["line"] == 1 and item["level"] == "ERR"
    assert any(row["line"] == 2 and "Traceback detail" in row["text"] for row in item["context"])


def test_tasks_keep_sources_processes_and_zero_steps(logs, capsys):
    root, _ = logs
    result = invoke(capsys, str(root), "--view", "tasks")
    assert result["total"] == 5
    gift = [item for item in result["items"] if item["task_id"] == 7]
    assert len(gift) == 3
    assert {item["framework_ret"] for item in gift} >= {True, False}
    assert any(item["task_id"] == 8 and item["node_done_count"] == 0 for item in result["items"])


@pytest.mark.parametrize("args", [
    ["--limit", "0"], ["--offset", "-1"], ["--context", "-1"],
    ["--from", "bad-time"], ["--from", "2026-11-17 00:00:00", "--to", "2026-11-16 00:00:00"],
    ["--view", "tasks", "--contains", "foo"], ["--view", "inventory", "--task-id", "7"],
])
def test_invalid_arguments_fail_before_scan(logs, args):
    with pytest.raises(SystemExit) as error:
        main([str(logs[0]), *args])
    assert error.value.code == 2



def test_task_source_lines_are_bounded(tmp_path, capsys):
    path = tmp_path / "many.log"
    path.write_text("".join(native('PipelineTask node done [result={"name":"Gift"}] [task_id_=7]') for _ in range(35)), encoding="utf-8")
    result = invoke(capsys, str(path), "--view", "tasks")
    item = result["items"][0]
    assert item["node_done_count"] == 35
    assert len(item["source_lines"]) == 20 and item["source_lines_truncated"]
    assert item["source_lines"][0] == 1 and item["source_lines"][-1] == 35


def test_search_time_window_and_truncation(logs, capsys):
    root, first = logs
    result = invoke(capsys, str(first), "--view", "search", "--from", "2026-11-16 20:00:01", "--to", "2026-11-16 20:00:02", "--max-chars", "30")
    assert result["total"] == 2
    assert all(len(item["text"]) == 30 and item["truncated"] for item in result["items"])
    result = invoke(capsys, str(root), "--view", "search", "--limit", "2", "--offset", "999")
    assert result["items"] == [] and not result["has_more"] and result["total"] > 0


def test_tasks_window_keeps_full_count(logs, capsys):
    _, first = logs
    result = invoke(capsys, str(first), "--view", "tasks", "--task-id", "7", "--pid", "Px1", "--from", "2026-11-16 20:00:01", "--to", "2026-11-16 20:00:01")
    assert result["total"] == 1 and result["items"][0]["node_done_count"] == 1
    assert result["items"][0]["start"] == "2026-11-16T20:00:00.000"


def test_missing_input_returns_usage_error(tmp_path):
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "missing")])
    assert error.value.code == 2


def test_inputs_unchanged(logs, capsys):
    root, _ = logs
    before = {path: path.read_bytes() for path in root.rglob("*.log")}
    invoke(capsys, str(root), "--view", "tasks")
    assert all(path.read_bytes() == content for path, content in before.items())
