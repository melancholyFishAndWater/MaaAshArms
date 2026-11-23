"""启动链的必要能力与优先级契约；不是完整配置快照，也不模拟 MaaFW。"""

from copy import deepcopy
from pathlib import Path
import re

import pytest

from tools.validation.lib.refs.jsonc import load_jsonc

ROOT = Path(__file__).resolve().parents[1]
LAUNCHES = ("StartUpEnterIcon", "StartUpEnterPackageBlibili")
LOGINS = ("StartUpEnterLogin", "StartUpEnterSignIn")


def _targets(node):
    """只读 next 的引用名；不解释 JumpBack 栈、识别或运行时覆写。"""
    return [re.sub(r"^(?:\[[^\]]+\])+", "", name) for name in node.get("next", [])]


def _reachable(pipeline, source, target):
    """检查本任务局部 next 图可达；外部节点留给 validate-refs 核引用。"""
    pending = [source]
    seen = set()
    while pending:
        name = pending.pop()
        if name == target:
            return True
        if name in seen:
            continue
        seen.add(name)
        pending.extend(_targets(pipeline.get(name, {})))
    return False


def _assert_entry_contract(pipeline):
    choices = _targets(pipeline["TaskStartUp"])
    for launch in LAUNCHES:
        assert choices.index("StartUpStopInGame") < choices.index(launch)
    stop = pipeline["StartUpStopInGame"]
    assert stop["recognition"] == "Or" and stop["action"] == "StopTask"
    assert not stop.get("inverse", False)
    assert "BaseProbeHome" in stop["any_of"]


def _assert_launch_contract(pipeline, name):
    node = pipeline[name]
    for target in (
        "StartUpBranchLoading",
        "StartUpClickDisclaimer",
        "BaseClickErrorConfirm",
    ):
        assert _reachable(pipeline, name, target), (name, target)
    assert node["timeout"] > 0
    failure = node["focus"]["Node.PipelineNode.Failed"]
    assert isinstance(failure["content"], str) and failure["content"].strip()
    assert set(failure["display"]) & {"log", "notification", "modal"}
    assert pipeline["StartUpClickDisclaimer"]["action"] == "Click"


def _assert_login_contract(pipeline, name):
    node = pipeline[name]
    choices = _targets(node)
    assert node["action"] == "Click"
    assert "BaseErrorNext" in choices
    assert choices.index("BaseProbeHome") < choices.index(name)
    assert _reachable(pipeline, name, "BaseClickNoticeDisable")


def _assert_update_notice_contract(pipeline):
    for target in (*LOGINS, "StartUpClickUpdate"):
        assert _reachable(pipeline, "StartUpBranchLoading", target)
    update = pipeline["StartUpClickUpdate"]
    assert update["recognition"] == "And" and update["action"] == "Click"
    assert {"StartUpProbeDownloadText", "BaseProbeConfirm"} <= set(update["all_of"])
    assert _reachable(pipeline, "BaseClickNoticeDisable", "BaseClickNoticeClose")
    assert _reachable(pipeline, "BaseClickNoticeClose", "BaseClickActivityClose")
    for name in (
        "BaseClickNoticeDisable", "BaseClickNoticeClose", "BaseClickActivityClose"
    ):
        assert pipeline[name]["action"] == "Click"


@pytest.fixture(scope="module")
def startup():
    # 公告与活动关闭节点位于 base.json；启动链契约需要同时加载公共依赖。
    pipeline_dir = ROOT / "assets/resource/pipeline"
    return {
        **load_jsonc(pipeline_dir / "base.json"),
        **load_jsonc(pipeline_dir / "startup.json"),
    }


def test_startup_entry_and_in_game_stop(startup):
    _assert_entry_contract(startup)


@pytest.mark.parametrize("name", LAUNCHES)
def test_launch_routes_handle_loading_and_failure(startup, name):
    _assert_launch_contract(startup, name)


def test_icon_and_package_actions(startup):
    assert startup["StartUpEnterIcon"]["action"] == "Click"
    assert startup["StartUpEnterIcon"]["recognition"] == "TemplateMatch"
    assert startup["StartUpEnterPackageBlibili"]["action"] == "StartApp"
    assert (
        startup["StartUpEnterPackageBlibili"]["package"] == "com.asharmdolls.bilibili"
    )


@pytest.mark.parametrize("name", LOGINS)
def test_login_routes_check_home_before_retry(startup, name):
    _assert_login_contract(startup, name)


def test_loading_update_and_notice_capabilities(startup):
    _assert_update_notice_contract(startup)


def test_contracts_allow_auxiliary_nodes_and_indirection(startup):
    """仅验证局部契约容忍辅助插入；不证明变体具备完整运行等价性。"""
    pipeline = deepcopy(startup)
    pipeline["EntryAuxiliary"] = {"recognition": "DirectHit", "enabled": False}
    pipeline["TaskStartUp"]["next"].insert(0, "EntryAuxiliary")
    for name in LAUNCHES:
        bridge = name + "Bridge"
        pipeline[bridge] = {"next": pipeline[name]["next"]}
        pipeline[name]["next"] = [bridge]
        pipeline[name]["focus"]["Node.PipelineNode.Failed"]["display"] = ["log"]
    for name in LOGINS:
        choices = pipeline[name]["next"]
        choices.remove("BaseErrorNext")
        choices.insert(choices.index("BaseProbeHome") + 1, "BaseErrorNext")
    pipeline["StartUpClickUpdate"]["all_of"].reverse()
    # 原配置默认从首项取框，调序例子必须同步保留原来的落框节点。
    pipeline["StartUpClickUpdate"]["box_index"] = 1
    pipeline["NoticeBridge"] = {"next": ["BaseClickNoticeClose"]}
    pipeline["BaseClickNoticeDisable"]["next"] = ["NoticeBridge"]
    _assert_entry_contract(pipeline)
    for name in LAUNCHES:
        _assert_launch_contract(pipeline, name)
    for name in LOGINS:
        _assert_login_contract(pipeline, name)
    _assert_update_notice_contract(pipeline)


@pytest.mark.parametrize("name", LAUNCHES)
def test_contract_rejects_launch_before_in_game_stop(startup, name):
    pipeline = deepcopy(startup)
    choices = pipeline["TaskStartUp"]["next"]
    choices.remove("StartUpStopInGame")
    choices.append("StartUpStopInGame")
    assert choices.index(name) < choices.index("StartUpStopInGame")
    with pytest.raises(AssertionError):
        _assert_entry_contract(pipeline)


@pytest.mark.parametrize("name", LOGINS)
def test_contract_rejects_home_after_retry(startup, name):
    pipeline = deepcopy(startup)
    choices = pipeline[name]["next"]
    choices.remove("BaseProbeHome")
    choices.append("BaseProbeHome")
    with pytest.raises(AssertionError):
        _assert_login_contract(pipeline, name)


@pytest.mark.parametrize(
    "broken",
    ["loading", "failure-content", "failure-display", "update-confirm", "notice-close"],
)
def test_contract_rejects_lost_required_capability(startup, broken):
    pipeline = deepcopy(startup)
    if broken == "loading":
        pipeline[LAUNCHES[0]]["next"] = []
        check = lambda: _assert_launch_contract(pipeline, LAUNCHES[0])
    elif broken.startswith("failure-"):
        failure = pipeline[LAUNCHES[0]]["focus"]["Node.PipelineNode.Failed"]
        failure["content" if broken == "failure-content" else "display"] = (
            "" if broken == "failure-content" else []
        )
        check = lambda: _assert_launch_contract(pipeline, LAUNCHES[0])
    else:
        if broken == "update-confirm":
            pipeline["StartUpClickUpdate"]["all_of"].remove("BaseProbeConfirm")
        else:
            pipeline["BaseClickNoticeDisable"]["next"] = []
        check = lambda: _assert_update_notice_contract(pipeline)
    with pytest.raises(AssertionError):
        check()
