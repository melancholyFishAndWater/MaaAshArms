"""分类命名与现场图证据隔离的离线回归测试。"""

import json
from types import SimpleNamespace

import pytest

from tools.drawing.lib.cases import evidence, overview, scene
from tools.drawing.lib.cases.pack import Case


@pytest.mark.parametrize(
    ("match", "node", "stage", "notes", "category"),
    [
        (False, "controller_initialization_failed", "controller", [], "证据错配"),
        (True, "controller_initialization_failed", "controller", [], "设备异常"),
        (True, "node", "action", [("Tasker not inited", "fail")], "疑似设备断连"),
        (None, "node", "action", [("Tasker not inited", "fail")], ""),
        (True, "node", "action", [], ""),
    ],
)
def test_classification_and_filename(tmp_path, monkeypatch, match, node, stage, notes, category):
    card = SimpleNamespace(node_name=node, stage=stage)
    assert overview._overview_category(card, match, notes) == category
    monkeypatch.setattr(overview, "mark_suffix", lambda _issue: "_已修复待发版")
    name = overview.overview_filename(
        "PYTHON-1A", str(tmp_path), {"cases": {}}, {"short_id": "PYTHON-1A"},
        label="Reward", category=category,
    )
    suffix = f"_{category}" if category else ""
    assert name == f"PYTHON-1A_Reward{suffix}_已修复待发版.png"


def test_mismatched_evidence_never_supplies_images(tmp_path, monkeypatch):
    pack = tmp_path / "PYTHON-1A"
    image = pack / "debug" / "on_error" / "run" / "2026.11.09-17.37.19.721_Node.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"synthetic image")
    manifest = {
        "issue": {"title": "Maa task failed: Reward"},
        "event": {"id": "event-a"},
        "events": [{"event_id": "event-a", "images": ["debug/on_error/run/2026.11.09-17.37.19.721_Node.png"]}],
    }
    (pack / "evidence.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(evidence, "_EVIDENCE_DIRS", [str(tmp_path)])
    case = Case(label="PYTHON-1A", root=str(pack / "debug"))
    card = SimpleNamespace(title="Maa task failed: Reward", label="Reward", event_id="event-a")
    assert evidence._case_matches_card(case, card) is True
    assert evidence._evidence_on_error("PYTHON-1A", card) == [str(image)]
    card.event_id = "event-b"
    assert evidence._case_matches_card(case, card) is False
    assert evidence._evidence_on_error("PYTHON-1A", card) == []
    assert evidence._evidence_scene("PYTHON-1A", card=card) == []
    card.event_id = "event-a"
    card.title = "Maa task failed: Train"
    card.label = "Train"
    assert evidence._case_matches_card(case, card) is False
    assert evidence._evidence_on_error("PYTHON-1A", card) == []


def test_invalidated_bulk_images_only_preserve_history(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    records = {
        "PYTHON-1A_old.png": {"issue": "PYTHON-1A", "origin": "bulk"},
        "PYTHON-1A_case.png": {"issue": "PYTHON-1A", "origin": "case"},
        "PYTHON-1B_no_attachment.png": {"issue": "PYTHON-1B", "origin": "bulk"},
    }
    unrecorded = "PYTHON-1A_unrecorded.png"
    for name in [*records, unrecorded]:
        (images / name).write_bytes(name.encode())
    manifest = {"images": records}
    removed = scene.prune_scene(
        str(tmp_path), manifest, set(), issues={"PYTHON-1A", "PYTHON-1B"},
        invalidated_issues={"PYTHON-1A"},
    )
    assert removed == 0
    assert set(manifest["images"]) == set(records)
    assert (images / "PYTHON-1A_old.png").read_bytes() == b"PYTHON-1A_old.png"
    for name in ["PYTHON-1A_case.png", "PYTHON-1B_no_attachment.png", unrecorded]:
        assert (images / name).read_bytes() == name.encode()

    replacement = "PYTHON-1A_new.png"
    (images / replacement).write_bytes(b"verified replacement")
    manifest["images"][replacement] = {"issue": "PYTHON-1A", "origin": "bulk"}
    removed = scene.prune_scene(
        str(tmp_path), manifest, {replacement}, issues={"PYTHON-1A", "PYTHON-1B"},
        invalidated_issues={"PYTHON-1A"},
    )
    assert removed == 1
    assert not (images / "PYTHON-1A_old.png").exists()
    assert (images / replacement).read_bytes() == b"verified replacement"
    for name in ["PYTHON-1A_case.png", "PYTHON-1B_no_attachment.png", unrecorded]:
        assert (images / name).read_bytes() == name.encode()
