"""产物保留、范围过滤与破坏性清理必须在副作用前检查互斥。"""
import pytest
from tools.drawing.lib.cases import cli


@pytest.mark.parametrize("flags", [
    ["--telemetry-version", ">=v1.0.0-beta.73"],
    ["--telemetry-issue", "PYTHON-40"],
    ["--task-id", "7"], ["--pid", "Px1"],
    ["--from", "12:00"], ["--to", "13:00"],
    ["--clean-flow"],
])
def test_no_prune_conflicts_before_side_effects(flags, monkeypatch, capsys):
    monkeypatch.setattr(cli, "set_evidence_dirs", lambda *_: pytest.fail("conflict reached side effects"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--telemetry-bulk", "--no-prune", *flags])
    assert error.value.code == 2
    assert "--no-prune" in capsys.readouterr().err


@pytest.mark.parametrize("preview", ["--telemetry-only", "--list-cases"])
def test_clean_conflicts_with_preview(preview, monkeypatch):
    monkeypatch.setattr(cli, "set_evidence_dirs", lambda *_: pytest.fail("conflict reached side effects"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--telemetry-bulk", "--clean-flow", preview])
    assert error.value.code == 2


def test_unfiltered_no_prune_is_valid_parser_input():
    args = cli.create_argument_parser().parse_args(["--telemetry-bulk", "--no-prune"])
    assert not args.prune
