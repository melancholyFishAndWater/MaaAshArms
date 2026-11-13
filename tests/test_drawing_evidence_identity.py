"""Synthetic regression tests for event-scoped evidence images."""

import json
from types import SimpleNamespace

import pytest

from tools.drawing.lib.cases import evidence


@pytest.fixture
def pack(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_EVIDENCE_DIRS", [str(tmp_path)])
    directory = tmp_path / "PYTHON-ABC"
    directory.mkdir()
    card = SimpleNamespace(short_id="PYTHON-ABC", title="Maa task failed: Train", label="Train", event_id="event-1")
    return directory, card


def manifest(pack, images, *, event="event-1", title="Maa task failed: Train", events=True):
    data = {"issue": {"title": title}, "event": {"id": event}, "written": {"images": images}}
    if events:
        data["events"] = [{"event_id": event, "images": images}]
    (pack / "evidence.json").write_text(json.dumps(data), encoding="utf-8")


def image(pack, path):
    target = pack / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"image")
    return str(target.resolve())


@pytest.mark.parametrize("path", ["on_error/current.png", "debug/on_error/current.png"])
def test_current_event_in_both_layouts(pack, path):
    directory, card = pack
    expected = image(directory, path)
    manifest(directory, [path])
    assert evidence._evidence_on_error(card.short_id, card) == [expected]


@pytest.mark.parametrize("path", ["on_error/same.png", "debug/on_error/same.png"])
def test_simultaneous_layouts_do_not_remap_same_name(pack, path):
    directory, card = pack
    flat = image(directory, "on_error/same.png")
    standard = image(directory, "debug/on_error/same.png")
    manifest(directory, [path])
    assert evidence._evidence_on_error(card.short_id, card) == [standard if path.startswith("debug/") else flat]
    (directory / path).unlink()
    assert evidence._evidence_on_error(card.short_id, card) == []


def test_traversal_absolute_and_sibling_prefix_rejected(pack):
    directory, card = pack
    image(directory, "on_error/valid.png")
    image(directory, "outside.png")
    image(directory, "on_error-other/secret.png")
    outside = str((directory / "outside.png").resolve())
    manifest(directory, ["on_error/../outside.png", "on_error/../../PYTHON-ABC/on_error-other/secret.png", outside])
    assert evidence._evidence_on_error(card.short_id, card) == []


@pytest.mark.parametrize("kind", ["event", "title", "no_events", "other_event"])
def test_only_matched_current_event_is_used(pack, kind):
    directory, card = pack
    path = "on_error/current.png"
    image(directory, path)
    manifest(directory, [path], event="event-2" if kind == "event" else "event-1", title="Maa task failed: Other" if kind == "title" else card.title, events=kind != "no_events")
    if kind == "other_event":
        file = directory / "evidence.json"
        data = json.loads(file.read_text(encoding="utf-8"))
        data["events"][0]["event_id"] = "event-2"
        file.write_text(json.dumps(data), encoding="utf-8")
    assert evidence._evidence_on_error(card.short_id, card) == []


def test_no_card_preserves_recursive_legacy_behavior(pack):
    directory, card = pack
    expected = image(directory, "on_error/run/a.PNG")
    assert evidence._evidence_on_error(card.short_id) == [expected]
