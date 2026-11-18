"""在线遥测失败必须阻断绘图；只有显式 offline 可以读取本地库。

入口契约，不依赖遥测快照的目录结构或提交实现；所有网络边界均替换。
"""
from types import SimpleNamespace

import pytest

from tools.drawing.lib.cases import cli as cases_cli
from tools.drawing.lib.cases.pack import Case
from tools.drawing.lib.flowlog import cli as flow_cli
from tools.sentry.lib import telemetry_index as telemetry


def forbidden(*args, **kwargs):
    pytest.fail("在线遥测失败后不得读取旧库、渲染或清理产物")


def unavailable(*args, **kwargs):
    raise RuntimeError("test telemetry network unavailable")


@pytest.mark.parametrize("cleanup", [False, True], ids=["incremental", "clean-flow"])
def test_cases_online_failure_stops_before_render_and_cleanup(monkeypatch, tmp_path, cleanup):
    stub = SimpleNamespace(load_or_refresh=unavailable, load_index=forbidden, load_cards=forbidden)
    monkeypatch.setattr(cases_cli, "_telemetry_module", lambda: stub)
    for name in ("run_case", "run_overview", "clear_artifacts", "prune_artifacts", "prune_scene", "save_manifest", "load_manifest", "_fetch_evidence"):
        monkeypatch.setattr(cases_cli, name, forbidden)
    arguments = ["--telemetry-bulk", "--log-file", "none", "--no-fetch-evidence", "--outdir", str(tmp_path / "unused")]
    if cleanup:
        arguments.append("--clean-flow")
    assert cases_cli.main(arguments) == 1
    assert not (tmp_path / "unused").exists()


def test_cases_explicit_offline_reads_local_index_without_refresh(monkeypatch):
    reads = []
    index = {"issues": {}}
    stub = SimpleNamespace(
        load_or_refresh=forbidden,
        load_index=lambda: reads.append("local") or index,
        load_cards=lambda value: [],
    )
    monkeypatch.setattr(cases_cli, "_telemetry_module", lambda: stub)
    monkeypatch.setattr(cases_cli.LocalReleases, "discover", lambda *args, **kwargs: None)
    monkeypatch.setattr(cases_cli, "load_case", lambda *args, **kwargs: Case(label="fake", log_path="fake.log"))
    monkeypatch.setattr(cases_cli, "run_case", forbidden)
    monkeypatch.setattr(cases_cli, "prune_artifacts", forbidden)
    assert cases_cli.main(["--case", "fake.log", "--list-cases", "--telemetry-offline", "--log-file", "none", "--no-fetch-evidence"]) == 0
    assert reads == ["local"]


def test_flow_online_failure_returns_one_without_render_or_old_index(monkeypatch):
    monkeypatch.setattr(telemetry, "load_or_refresh", unavailable)
    monkeypatch.setattr(telemetry, "load_index", forbidden)
    monkeypatch.setattr(telemetry, "load_cards", forbidden)
    # flow 的适配接口是 annotate；复用真实 annotate，不能用会提前抛错的假实现掩盖吞异常。
    stub = SimpleNamespace(annotate=telemetry.annotate)
    monkeypatch.setattr(flow_cli, "_telemetry_module", lambda: stub)
    monkeypatch.setattr(flow_cli, "render_flow", forbidden)
    assert flow_cli.main(["fake.log", "--telemetry", "--log-file", "none"]) == 1


def test_flow_explicit_offline_allows_local_read_and_render(monkeypatch):
    reads = []
    renders = []
    monkeypatch.setattr(telemetry, "load_or_refresh", forbidden)
    monkeypatch.setattr(telemetry, "load_index", lambda: reads.append("local") or {"issues": {}})
    monkeypatch.setattr(telemetry, "load_cards", lambda *args, **kwargs: [])
    monkeypatch.setattr(flow_cli, "_telemetry_module", lambda: SimpleNamespace(annotate=telemetry.annotate))
    monkeypatch.setattr(flow_cli, "render_flow", lambda *args, **kwargs: renders.append(args[0]) or "offline steps")
    assert flow_cli.main(["fake.log", "--telemetry-offline", "--list", "--log-file", "none"]) == 0
    assert reads == ["local"] and renders == ["fake.log"]


def test_annotate_propagates_online_refresh_failure(monkeypatch):
    monkeypatch.setattr(telemetry, "load_or_refresh", unavailable)
    monkeypatch.setattr(telemetry, "load_index", forbidden)
    monkeypatch.setattr(telemetry, "load_cards", forbidden)
    with pytest.raises(RuntimeError, match="test telemetry network unavailable"):
        telemetry.annotate("fake.log", offline=False)


def test_telemetry_refresh_cli_network_failure_returns_one(monkeypatch):
    monkeypatch.setattr(telemetry.report_common, "resolve_sentry_command", lambda: "fake-sentry")
    monkeypatch.setattr(telemetry, "refresh", unavailable)
    monkeypatch.setattr(telemetry, "load_index", forbidden)
    monkeypatch.setattr(telemetry, "load_cards", forbidden)
    assert telemetry.main(["--refresh", "--log-file", "none"]) == 1
