"""分离面板日志/框架日志辅助：多路径、不猜父目录、不放宽身份。"""
import pytest

from tools.drawing.lib.cases.local_release import LocalReleases
from tools.drawing.lib.cases.pack import Case
from tools.drawing.lib.cases.cli import create_argument_parser, main


def native(tid=7, entry="TaskGift", ret="false", pid="Px1"):
    return f'[2026-11-17 12:00:00.000][INF][{pid}][Tx1][Tasker.cpp][L1][run] task end: [cb_detail={{"entry":"{entry}","task_id":{tid}}}] [ret={ret}]\n'


def setup(tmp_path):
    panel = tmp_path / "unrelated-panel"
    panel.mkdir()
    (panel / "panel.log").write_text('[2026-11-17 12:00:01.000][INF] event_id=abcdef\n', encoding="utf-8")
    debug = tmp_path / "another" / "debug"
    debug.mkdir(parents=True)
    (debug / "maafw.log").write_text(native(), encoding="utf-8")
    return panel, debug


def test_separate_directories_find_event(tmp_path):
    panel, debug = setup(tmp_path)
    group = LocalReleases.discover([], log_paths=[str(panel)], debug_paths=[str(debug)])
    assert group is not None
    hit = group.find_event("abcdef", "TaskGift")
    assert hit is not None and hit[1] == 7 and hit[3] == "Px1"
    assert hit[0].log_path == str(debug / "maafw.log")


def test_multiple_files_directories_and_duplicates(tmp_path):
    panel, debug = setup(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "maafw.bak.log").write_text(native(tid=9, ret="true"), encoding="utf-8")
    group = LocalReleases.discover([], log_paths=[str(panel), str(panel / "panel.log")], debug_paths=[str(debug), str(debug / "maafw.log"), str(extra)])
    assert group is not None and len(group.logs()) == 2
    assert group.find_event("abcdef", "TaskGift") is not None


def test_panel_only_is_identity_without_framework_run(tmp_path):
    panel, _ = setup(tmp_path)
    group = LocalReleases.discover([], log_paths=[str(panel)])
    assert group is not None and group.panel_event("abcdef") is not None
    assert group.logs() == [] and group.find_event("abcdef", "TaskGift") is None


def test_debug_only_requires_content_match(tmp_path):
    _, debug = setup(tmp_path)
    text = '[2026-11-17 12:00:00.000][INF][Px1][Tx1][PipelineTask.cpp][L1][run] PipelineTask node done [result={"name":"GiftClick","completed":true}] [task_id_=7]\n'
    (debug / "maafw.log").write_text(text, encoding="utf-8")
    group = LocalReleases.discover([], debug_paths=[str(debug)])
    assert group is not None and group.find_event("abcdef", "TaskGift") is None
    case = Case(label="gift", log_path=str(debug / "maafw.log"))
    assert group.find(case, task_id=7) is not None
    assert group.find(case, task_id=71) is None


def test_conflicting_framework_identities_are_rejected(tmp_path):
    panel, debug = setup(tmp_path)
    (debug / "maafw.bak.log").write_text(native(tid=8, pid="Px2"), encoding="utf-8")
    group = LocalReleases.discover([], log_paths=[str(panel)], debug_paths=[str(debug)])
    assert group is not None and group.find_event("abcdef", "TaskGift") is None


def test_conflicting_panel_event_times_are_rejected(tmp_path):
    panel, debug = setup(tmp_path)
    (panel / "other.log").write_text('[2026-11-17 13:00:01.000][INF] event_id=abcdef\n', encoding="utf-8")
    group = LocalReleases.discover([], log_paths=[str(panel)], debug_paths=[str(debug)])
    assert group is not None and group.find_event("abcdef", "TaskGift") is None


def test_missing_explicit_path_rejected(tmp_path):
    with pytest.raises(ValueError):
        LocalReleases.discover([], debug_paths=[str(tmp_path / "missing")])



def test_root_panel_is_not_borrowed_by_explicit_debug(tmp_path):
    panel, debug = setup(tmp_path)
    root = tmp_path / "install"
    (root / "logs").mkdir(parents=True)
    (root / "debug").mkdir()
    (root / "logs" / "panel.log").write_bytes((panel / "panel.log").read_bytes())
    (root / "debug" / "maafw.log").write_text(native(ret="true"), encoding="utf-8")
    group = LocalReleases.discover([str(root)], debug_paths=[str(debug)])
    assert group is not None and group.find_event("abcdef", "TaskGift") is None


def test_debug_directory_does_not_scan_agent(tmp_path):
    _, debug = setup(tmp_path)
    (debug / "agent").mkdir()
    (debug / "agent" / "maafw.log").write_text(native(tid=99), encoding="utf-8")
    group = LocalReleases.discover([], debug_paths=[str(debug)])
    assert group is not None and group.logs() == [str(debug / "maafw.log")]


def test_cli_multiple_and_repeated_paths():
    args = create_argument_parser().parse_args(["--local-log", "a", "b", "--local-log", "c", "--local-debug", "d", "e"])
    assert args.local_log == ["a", "b", "c"] and args.local_debug == ["d", "e"]


@pytest.mark.parametrize("flag", ["--local-log", "--local-debug"])
def test_disabled_auxiliary_conflicts_with_explicit_inputs(flag):
    with pytest.raises(SystemExit) as error:
        main([flag, "somewhere", "--no-local-release"])
    assert error.value.code == 2
