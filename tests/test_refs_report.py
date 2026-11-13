"""validate-refs 显示级别过滤与退出码隔离回归。"""
import json
from pathlib import Path

import pytest

from tools.validation.lib.refs.model import Ctx
from tools.validation.lib.refs.report import report


@pytest.mark.parametrize("fmt", ["text", "github", "json"])
@pytest.mark.parametrize("severity", [None, "error", "warn", "info"])
def test_severity_filters_display_only(fmt, severity, capsys):
    ctx = Ctx(repo_root=Path("."))
    for level in ("error", "warn", "info"):
        ctx.add(f"A99-{level}", level, Path("fixture.json"), 1, f"marker-{level}")
    assert report(ctx, fmt, "error", set(), severity) == 1
    output = capsys.readouterr().out
    for level in ("error", "warn", "info"):
        assert (f"marker-{level}" in output) == (severity is None or severity == level)
    if fmt == "json":
        data = json.loads(output)
        assert data["counts"] == {level: int(severity is None or severity == level) for level in ("error", "warn", "info")}


def test_ignore_and_fail_on_still_apply(capsys):
    ctx = Ctx(repo_root=Path("."))
    ctx.add("A99-error", "error", Path("fixture.json"), 1, "hidden-error")
    ctx.add("A99-warn", "warn", Path("fixture.json"), 1, "hidden-warn")
    assert report(ctx, "json", "error", {"A99-error"}, "info") == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    assert report(ctx, "json", "warn", {"A99-error"}, "info") == 1
    capsys.readouterr()
    assert report(ctx, "json", "none", set(), "warn") == 0
