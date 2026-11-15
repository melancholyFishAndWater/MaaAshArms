"""startup 重构后的入口、启动回退和登录链结构契约；不运行设备。"""
from pathlib import Path

import pytest
from tools.validation.lib.refs.jsonc import load_jsonc

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def startup():
    return load_jsonc(ROOT / "assets/resource/pipeline/startup.json")


def test_startup_entry_and_in_game_stop(startup):
    assert startup["TaskStartUp"]["next"] == ["StartUpStopInGame", "StartUpEnterIcon", "StartUpEnterPackageBlibili"]
    stop = startup["StartUpStopInGame"]
    assert stop["recognition"] == "Or" and stop["action"] == "StopTask"
    assert "BaseProbeHome" in stop["any_of"]


@pytest.mark.parametrize("name", ["StartUpEnterIcon", "StartUpEnterPackageBlibili"])
def test_launch_routes_handle_loading_and_failure(startup, name):
    node = startup[name]
    assert "StartUpBranchLoading" in node["next"]
    assert "[JumpBack]StartUpClickDisclaimer" in node["next"]
    assert "[JumpBack]BaseClickErrorConfirm" in node["next"]
    assert node["timeout"] > 0
    assert "modal" in node["focus"]["Node.PipelineNode.Failed"]["display"]


def test_icon_and_package_actions(startup):
    assert startup["StartUpEnterIcon"]["action"] == "Click"
    assert startup["StartUpEnterIcon"]["recognition"] == "TemplateMatch"
    assert startup["StartUpEnterPackageBlibili"]["action"] == "StartApp"
    assert startup["StartUpEnterPackageBlibili"]["package"] == "com.asharmdolls.bilibili"


@pytest.mark.parametrize("name", ["StartUpEnterLogin", "StartUpEnterSignIn"])
def test_login_routes_retry_until_home(startup, name):
    node = startup[name]
    assert node["action"] == "Click"
    assert node["next"][0] == "BaseErrorNext"
    assert "BaseProbeHome" in node["next"]
    assert "[JumpBack]StartUpClickNoticeDisable" in node["next"]
    assert name in node["next"]


def test_loading_update_and_notice_links(startup):
    choices = startup["StartUpBranchLoading"]["next"]
    assert {"StartUpEnterLogin", "StartUpEnterSignIn", "[JumpBack]StartUpClickUpdate"} <= set(choices)
    update = startup["StartUpClickUpdate"]
    assert update["recognition"] == "And" and update["action"] == "Click"
    assert update["all_of"] == ["StartUpProbeDownloadText", "BaseProbeConfirm"]
    assert startup["StartUpClickNoticeDisable"]["next"] == ["StartUpClickNoticeClose"]
    assert startup["StartUpClickNoticeClose"]["action"] == "Click"
