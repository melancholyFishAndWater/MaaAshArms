"""本地校验编排的覆盖范围、失败传播与依赖边界。"""
from types import SimpleNamespace

from tools.validation.lib import preflight


def test_plan_contains_ci_and_extra_gates():
    steps = preflight.build_steps()
    assert len({step.name for step in steps}) == len(steps)
    commands = [" ".join(step.command) for step in steps]
    for token in ("sync --locked", "npm", "export --locked", "check-python", "validate-schema", "validate-refs", "pytest -q tests", "ruff check tools", "check_pyright.mjs", "selftest", "check-sentry-offline", "diff --check"):
        assert any(token in command for command in commands), token
    assert sum(step.compare_requirements for step in steps) == 1
    for step in steps:
        assert all(any(prior.name == name for prior in steps[:steps.index(step)]) for name in step.requires)


def test_requirements_comparison_does_not_mutate(tmp_path):
    actual = tmp_path / "export.txt"
    expected = tmp_path / "requirements.txt"
    expected.write_bytes(b"package==1\r\n")
    actual.write_bytes(b"package==1\n")
    assert preflight.compare_requirements(actual, expected)
    actual.write_text("package==2\n", encoding="utf-8")
    assert not preflight.compare_requirements(actual, expected)
    assert expected.read_bytes() == b"package==1\r\n"


def test_list_never_executes(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("list must not execute")
    monkeypatch.setattr(preflight.subprocess, "run", forbidden)
    assert preflight.main(["--list"]) == 0
    assert "pytest" in capsys.readouterr().out


def test_dependency_failure_skips_dependents_but_runs_independent(monkeypatch, capsys):
    steps = [
        preflight.Step("deps", ("fake-deps",)),
        preflight.Step("dependent", ("fake-dependent",), ("deps",)),
        preflight.Step("independent", ("fake-independent",)),
    ]
    monkeypatch.setattr(preflight, "build_steps", lambda: steps)
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        return SimpleNamespace(returncode=1 if command[0] == "fake-deps" else 0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.main([]) == 1
    assert calls == [("fake-deps",), ("fake-independent",)]
    output = capsys.readouterr().out
    assert "FAIL" in output and "SKIP" in output and "PASS" in output



def test_export_mismatch_fails_without_overwriting_source(tmp_path, monkeypatch):
    expected = tmp_path / "requirements.txt"
    expected.write_text("package==1\n", encoding="utf-8")
    step = preflight.Step("export", ("fake-export", "{requirements_export}"), compare_requirements=True)

    def run(command, **kwargs):
        from pathlib import Path
        Path(command[1]).write_text("package==2\n", encoding="utf-8")
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert kwargs["cwd"] == tmp_path
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.run_steps([step], repo=tmp_path) == 1
    assert expected.read_text(encoding="utf-8") == "package==1\n"


def test_all_success_returns_zero(monkeypatch):
    monkeypatch.setattr(preflight, "build_steps", lambda: [preflight.Step("ok", ("fake",))])
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert preflight.main([]) == 0


def test_launch_failure_returns_nonzero(monkeypatch):
    monkeypatch.setattr(preflight, "build_steps", lambda: [preflight.Step("missing", ("nonexistent",))])
    def fail(*args, **kwargs):
        raise FileNotFoundError("test missing command")
    monkeypatch.setattr(preflight.subprocess, "run", fail)
    assert preflight.main([]) == 1


def test_cleanup_failure_preserves_summary(monkeypatch, capsys):
    class DeniedCleanup:
        name = "synthetic-preflight-temp"

        def cleanup(self):
            raise PermissionError("synthetic cleanup denied")

    monkeypatch.setattr(preflight.tempfile, "TemporaryDirectory", lambda **kwargs: DeniedCleanup())
    monkeypatch.setattr(preflight.subprocess, "run", lambda command, **kwargs: SimpleNamespace(
        returncode=1 if command[0] == "bad" else 0
    ))
    steps = [preflight.Step("ok", ("ok",)), preflight.Step("bad", ("bad",)),
             preflight.Step("dependent", ("unused",), ("bad",))]
    assert preflight.run_steps(steps) == 1
    output = capsys.readouterr().out
    assert "=== preflight 汇总 ===" in output
    assert "PASS ok" in output and "FAIL bad" in output and "SKIP dependent" in output
    assert "FAIL cleanup" in output
    assert "synthetic-preflight-temp" in output and "synthetic cleanup denied" in output
    assert "PASS 1 / FAIL 2 / SKIP 1" in output


def test_cleanup_failure_turns_success_into_failure(monkeypatch, capsys):
    class DeniedCleanup:
        name = "synthetic-preflight-temp"

        def cleanup(self):
            raise PermissionError("synthetic cleanup denied")

    monkeypatch.setattr(preflight.tempfile, "TemporaryDirectory", lambda **kwargs: DeniedCleanup())
    monkeypatch.setattr(preflight.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert preflight.run_steps([preflight.Step("ok", ("fake",))]) == 1
    assert "PASS 1 / FAIL 1 / SKIP 0" in capsys.readouterr().out
