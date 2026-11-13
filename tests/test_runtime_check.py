"""Runtime preflight failures must remain Python errors before loading maa."""

import pytest
import runtime_check


def test_non_windows_skips_probe(monkeypatch):
    monkeypatch.setattr(runtime_check.sys, "platform", "linux")
    monkeypatch.setattr(runtime_check, "windows_temp_probe", lambda: pytest.fail("probe called"))
    runtime_check.ensure_runtime_ready()


@pytest.mark.parametrize("integrity", ["Low", "Medium", "High"])
def test_writable_temp_allows_startup(monkeypatch, integrity):
    monkeypatch.setattr(runtime_check.sys, "platform", "win32")
    monkeypatch.setattr(runtime_check, "windows_integrity", lambda: integrity)
    monkeypatch.setattr(runtime_check, "windows_temp_probe", lambda: (True, "probe", 0))
    runtime_check.ensure_runtime_ready()


def test_permission_failure_reports_base_and_fix(monkeypatch):
    monkeypatch.setattr(runtime_check.sys, "platform", "win32")
    monkeypatch.setattr(runtime_check, "windows_integrity", lambda: "Low")
    monkeypatch.setattr(runtime_check, "windows_temp_probe", lambda: (False, "C:/Temp/probe", 13))
    with pytest.raises(SystemExit) as exc:
        runtime_check.ensure_runtime_ready()
    text = str(exc.value)
    assert "Low" in text and "13" in text and "C:/Temp/probe" in text
    assert "基础解释器" in text and "python_integrity.ps1" in text


def test_unreadable_token_does_not_mask_temp_failure(monkeypatch):
    monkeypatch.setattr(runtime_check.sys, "platform", "win32")
    def fail():
        raise OSError("token denied")
    monkeypatch.setattr(runtime_check, "windows_integrity", fail)
    monkeypatch.setattr(runtime_check, "windows_temp_probe", lambda: (False, "probe", 13))
    with pytest.raises(SystemExit, match="unknown"):
        runtime_check.ensure_runtime_ready()
