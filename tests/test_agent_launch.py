"""开发态 uv 配置不能泄漏到发布包的 Python 参数中。"""

import copy
import runpy
import sys
from pathlib import Path

import jsonc
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    monkeypatch.setattr(sys, "argv", ["install.py", "v0.0.0-test", "win", "x86_64"])
    return runpy.run_path(str(ROOT / "tools" / "install.py"), run_name="install_test")


@pytest.mark.parametrize("platform", ["win", "linux", "macos", "android"])
@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("as_list", [False, True])
def test_release_agent_replaces_entire_dev_command(installer, platform, bundled, as_list):
    interface = jsonc.loads((ROOT / "assets" / "interface.json").read_text(encoding="utf-8"))
    original = copy.deepcopy(interface["agent"])
    if as_list:
        interface["agent"] = [interface["agent"]]
    runtime = ROOT / ".python-runtime" if bundled else None
    installer["rewrite_agent"].__globals__["os_name"] = platform
    installer["rewrite_agent"](interface, runtime)
    agent = interface["agent"][0] if as_list else interface["agent"]
    expected = "python" if not bundled else (
        "python/python.exe" if platform == "win" else "python/bin/python3"
    )
    assert original["child_exec"] == "uv"
    assert agent["child_exec"] == expected
    assert agent["child_args"] == ["-u", "./agent/main.py"]


@pytest.mark.parametrize("bundled", [False, True])
def test_smoke_rejects_uv_argument_leak(installer, tmp_path, bundled):
    root = tmp_path / "package"
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "main.py").write_text("", encoding="utf-8")
    if bundled:
        (root / "python" / "bin").mkdir(parents=True)
        (root / "python" / "bin" / "python3").write_text("", encoding="utf-8")
    state = installer["smoke_check"].__globals__
    state["install_path"] = root
    state["os_name"] = "android"  # 此测试只验 agent 配置，不伪装原生库。
    agent = {
        "child_exec": "python/bin/python3" if bundled else "python",
        "child_args": ["-u", "./agent/main.py"],
    }
    config = root / "interface.json"
    config.write_text(jsonc.dumps({"agent": agent}), encoding="utf-8")
    runtime = root / "python" if bundled else None
    installer["smoke_check"](runtime)
    agent["child_args"] = ["-u", "run", "--locked", "python", "./agent/main.py"]
    config.write_text(jsonc.dumps({"agent": agent}), encoding="utf-8")
    with pytest.raises(SystemExit):
        installer["smoke_check"](runtime)
