"""多入口参数冲突在联网/日志/取证前返回 argparse 错误。"""
import pytest
from tools.drawing.lib.cases import cli as cases
from tools.drawing.lib.flowlog import cli as flow
from tools.sentry.lib.evidence_lib import cli as evidence


@pytest.mark.parametrize("flags", [
    ["--telemetry", "--no-telemetry"],
    ["--fetch-evidence", "--no-fetch-evidence"],
    ["--telemetry-bulk", "--no-telemetry"],
    ["--telemetry-refresh", "--telemetry-offline"],
    ["--local-release", "x", "--no-local-release"],
    ["--list-cases", "--telemetry-only"],
    ["--all-tasks", "--task-id=7"],
    ["--no-overlay", "--overlay-min=3"],
    ["--no-telemetry", "--telemetry-issue=X"],
    ["--no-telemetry", "--telemetry-refresh"],
    ["--clean-flow"],
])
def test_cases_conflicts(flags, monkeypatch):
    monkeypatch.setattr(cases, "set_evidence_dirs", lambda *_: pytest.fail("side effect reached"))
    with pytest.raises(SystemExit) as result:
        cases.main(["--case", "missing.log", *flags])
    assert result.value.code == 2


@pytest.mark.parametrize("flags", [
    ["--list", "--list-tasks"], ["--list", "--telemetry-only"],
    ["--list-tasks", "--telemetry-only"],
    ["--telemetry-refresh", "--telemetry-offline"],
    ["--no-telemetry", "--telemetry-issue=X"],
    ["--no-stale-check", "--version=v1.0.0"],
    ["--no-overlay", "--overlay-min=2"],
])
def test_flow_conflicts(flags):
    with pytest.raises(SystemExit) as result:
        flow.main(["missing.log", *flags])
    assert result.value.code == 2


@pytest.mark.parametrize("flags", [
    ["--no-images", "--no-log"], ["--list", "--force"],
    ["--no-log", "--sort=newest"], ["--no-log", "--log-limit=5"],
    ["--no-log", "--keep-duplicates"],
])
def test_evidence_conflicts(flags, monkeypatch):
    monkeypatch.setattr(evidence.report_common, "resolve_sentry_command", lambda: pytest.fail("network setup reached"))
    with pytest.raises(SystemExit) as result:
        evidence.main(flags)
    assert result.value.code == 2


def test_no_log_allows_implicit_log_defaults(monkeypatch):
    class Reached(Exception):
        pass
    def stop():
        raise Reached
    monkeypatch.setattr(evidence.report_common, "resolve_sentry_command", stop)
    with pytest.raises(Reached):
        evidence.main(["--no-log"])
