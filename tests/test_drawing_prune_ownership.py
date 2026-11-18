"""来源标签不决定归属：bulk遵从范围，case主图及现场图保留。"""
import pytest

from tools.drawing.lib.cases.output import prune_artifacts
from tools.drawing.lib.cases.scene import prune_scene


@pytest.mark.parametrize("origin,marked,expected", [
    ("bulk", True, False), ("bulk", False, False),
    ("case", True, True), ("case", False, True),
    ("unknown", True, True), ("", True, True),
])
def test_scope_cleanup_respects_origin(tmp_path, origin, marked, expected):
    name = "PYTHON-3C_Escort" + ("_命中日志" if marked else "") + "_疑似过时.png"
    image = tmp_path / name
    image.write_bytes(b"png")
    sidecar = image.with_suffix(".mmd")
    sidecar.write_text("graph TD", encoding="utf-8")
    manifest = {"cases": {name: {"issue": "PYTHON-3C", "origin": origin}}}
    prune_artifacts(str(tmp_path), manifest, set(), issues={"PYTHON-49"})
    assert image.exists() is expected and sidecar.exists() is expected
    assert (name in manifest["cases"]) is expected


@pytest.mark.parametrize("origin,expected", [("bulk", False), ("case", True), ("unknown", True)])
def test_scene_scope_matches_chart_ownership(tmp_path, origin, expected):
    (tmp_path / "images").mkdir()
    name = "PYTHON-3C_Escort.png"
    image = tmp_path / "images" / name
    image.write_bytes(b"png")
    manifest = {"images": {name: {"issue": "PYTHON-3C", "origin": origin}}}
    prune_scene(str(tmp_path), manifest, set(), issues={"PYTHON-49"})
    assert image.exists() is expected
    assert (name in manifest["images"]) is expected


def test_bulk_local_old_chart_is_removed_when_replaced(tmp_path):
    old = "PYTHON-3C_Escort_命中日志_疑似过时.png"
    new = "PYTHON-3C_Escort_命中日志.png"
    for name in (old, new):
        (tmp_path / name).write_bytes(b"png")
    manifest = {"cases": {name: {"issue": "PYTHON-3C", "origin": "bulk"} for name in (old, new)}}
    assert prune_artifacts(str(tmp_path), manifest, {new}, issues={"PYTHON-3C"}) == 1
    assert not (tmp_path / old).exists() and (tmp_path / new).exists()


def test_failed_bulk_issue_keeps_chart_and_scene(tmp_path):
    name = "PYTHON-3C_Escort_命中日志.png"
    (tmp_path / name).write_bytes(b"png")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / name).write_bytes(b"png")
    manifest = {"cases": {name: {"issue": "PYTHON-3C", "origin": "bulk"}}, "images": {name: {"issue": "PYTHON-3C", "origin": "bulk"}}}
    assert prune_artifacts(str(tmp_path), manifest, set(), issues=set(), failed_issues={"PYTHON-3C"}) == 0
    assert prune_scene(str(tmp_path), manifest, set(), issues=set(), keep_issues={"PYTHON-3C"}) == 0


def test_unrecorded_local_chart_is_not_guessed_bulk(tmp_path):
    image = tmp_path / "PYTHON-3C_Escort_命中日志.png"
    image.write_bytes(b"png")
    assert prune_artifacts(str(tmp_path), {"cases": {}}, set(), issues=set()) == 0
    assert image.exists()
