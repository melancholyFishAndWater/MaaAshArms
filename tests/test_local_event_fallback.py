"""遥测包缺失/错配时，本地辅助必须独立确认当前 event 与失败入口。"""
from tools.drawing.lib.cases.local_release import LocalRelease, LocalReleases


def setup(root, event="abc", entry="TaskFactory", ret="false", extra=""):
    (root / "debug").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "logs" / "panel.log").write_text(f"[2026-11-14 14:02:16.100][INF] event_id={event}\n", encoding="utf-8")
    raw = '[2026-11-14 14:02:16.000][INF][Px1][Tx1][Tasker.cpp][L1][run] task end: [cb_detail={"entry":"' + entry + '","task_id":7}] [ret=' + ret + ']\n'
    (root / "debug" / "maafw.log").write_text(raw + extra, encoding="utf-8")
    return LocalRelease.discover(str(root))


def test_current_event_without_telemetry_pack(tmp_path):
    local = setup(tmp_path)
    found = local.find_event("abc", "TaskFactory")
    assert found is not None and found[1] == 7 and found[3] == "Px1"
    assert "abc" in found[0].evidence


def test_wrong_event_or_entry_rejected(tmp_path):
    local = setup(tmp_path)
    assert local.find_event("other", "TaskFactory") is None
    assert local.find_event("abc", "TaskEscort") is None


def test_success_is_not_failure_evidence(tmp_path):
    local = setup(tmp_path, ret="true")
    assert local.find_event("abc", "TaskFactory") is None


def test_ambiguous_failures_rejected(tmp_path):
    extra = '[2026-11-14 14:02:15.900][INF][Px1][Tx1][Tasker.cpp][L1][run] task end: [cb_detail={"entry":"TaskFactory","task_id":8}] [ret=false]\n'
    local = setup(tmp_path, extra=extra)
    assert local.find_event("abc", "TaskFactory") is None


def test_group_finds_verified_event(tmp_path):
    local = setup(tmp_path)
    assert LocalReleases([local]).find_event("abc", "TaskFactory") is not None


def test_assistance_chooses_more_complete_log(tmp_path):
    from tools.drawing.lib.cases.cli import _assist_pack
    from tools.drawing.lib.cases.pack import Case
    from tools.drawing.lib.flowlog.render import FlowOptions
    from tools.drawing.lib.cases.local_release import LocalRun
    from types import SimpleNamespace

    def node(index):
        return f'[2026-11-14 14:02:0{index}.000][INF][Px1][Tx1][PipelineTask.cpp][L1][run] PipelineTask node done [result={{"name":"Step{index}","completed":true}}] [task_id_=7]\n'

    pack = tmp_path / "pack.log"
    full = tmp_path / "full.log"
    pack.write_text(node(1), encoding="utf-8")
    full.write_text(node(1) + node(2), encoding="utf-8")
    run = LocalRun(str(full), str(tmp_path), None, None, "identity verified", 1)
    local = SimpleNamespace(find=lambda *args, **kwargs: run)
    result = _assist_pack(Case(label="test", log_path=str(pack)), FlowOptions(), 7, local)
    assert result is not None and result[0].log_path == str(full)
    pack.write_text(node(1) + node(2) + node(3), encoding="utf-8")
    assert _assist_pack(Case(label="test", log_path=str(pack)), FlowOptions(), 7, local) is None
