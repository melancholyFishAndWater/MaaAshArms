"""Offline checks for the Android PI staging entry point."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "android_package", Path(__file__).resolve().parents[1] / "Android" / "package.py"
)
assert _SPEC is not None and _SPEC.loader is not None
package = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(package)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    sources = {
        "assets/interface.json": '// JSONC input\n{"interface_version": 2, "version": "old", "license": "MIT", "icon": "logo.ico", "import": ["resource/tasks/option.json"]}',
        "assets/resource/tasks/option.json": "{}",
        "assets/resource/pipeline/startup.json": "{}",
        "assets/resource/model/ocr/det.onnx": "det",
        "assets/resource/model/ocr/rec.onnx": "rec",
        "assets/resource/model/ocr/keys.txt": "keys",
        "agent/main.py": "print('agent')\n",
        "agent/shared/helper.py": "# helper\n",
        "agent/debug/agent.log": "private log",
        "agent/__pycache__/main.pyc": "cache",
        "agent/helper.pyc": "cache",
        "agent/trace.log.2026-11-23": "rotated log",
        "LICENSE": "MIT license",
        "README.md": "README",
        "assets/logo.ico": "ico",
        "logo.png": "png",
        "Android/MaaFwApp/LICENSE": "AGPL license",
        "Android/NOTICE.md": "Android notices",
    }
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    calls = []
    monkeypatch.setattr(package, "_configure_ocr", lambda path: calls.append(path))
    return root, calls


def test_stage_assembles_payload_without_changing_source(project):
    root, calls = project
    original = (root / "assets/interface.json").read_bytes()
    target = package.stage("v1.2.3", project_root=root)
    interface = json.loads((target / "interface.json").read_text(encoding="utf-8"))
    assert interface["version"] == "v1.2.3"
    assert interface["icon"] == "logo.png"
    assert interface["license"] == "MIT (resources); AGPL-3.0 (Android GUI)"
    assert calls == [root]
    assert (root / "assets/interface.json").read_bytes() == original
    for relative in (
        "resource/tasks/option.json", "resource/model/ocr/det.onnx", "agent/main.py",
        "agent/shared/helper.py", "LICENSE", "LICENSE-MaaFwApp", "NOTICE-Android.md", "logo.png", "logo.ico",
    ):
        assert (target / relative).is_file(), relative
    for relative in ("agent/debug", "agent/__pycache__", "agent/helper.pyc", "agent/trace.log.2026-11-23"):
        assert not (target / relative).exists(), relative


@pytest.mark.parametrize("output", [".", "assets", "Android", "Android/MaaFwApp", "Android/staging/../../assets"])
def test_reject_unsafe_output(project, output):
    root, calls = project
    with pytest.raises(ValueError, match="Output must"):
        package.stage("v1", output, project_root=root)
    assert calls == []


def test_reject_nonempty_without_deleting(project):
    root, _ = project
    target = root / "Android/staging"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="empty directory"):
        package.stage("v1", project_root=root)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_accept_empty_child_output(project):
    root, _ = project
    target = root / "Android/staging/test-payload"
    target.mkdir(parents=True)
    assert package.stage("v1", target, project_root=root) == target


@pytest.mark.parametrize("model", ["det.onnx", "rec.onnx", "keys.txt"])
def test_missing_ocr_fails_before_output(project, model):
    root, _ = project
    (root / "assets/resource/model/ocr" / model).write_bytes(b"")
    with pytest.raises(ValueError, match="OCR model"):
        package.stage("v1", project_root=root)
    assert not (root / "Android/staging").exists()


@pytest.mark.parametrize("relative", ["agent/libnative.so", "resource/model/libnative.so.1", "resource/helper.dll"])
def test_native_payload_is_rejected(project, relative):
    root, _ = project
    source = root / (relative if relative.startswith("agent/") else "assets/" + relative)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"native")
    with pytest.raises(ValueError, match="Native library"):
        package.stage("v1", project_root=root)
    assert not (root / "Android/staging").exists()


def test_missing_import_is_rejected(project):
    root, _ = project
    (root / "assets/resource/tasks/option.json").rename(root / "assets/resource/tasks/other.json")
    with pytest.raises(ValueError, match="PI import missing"):
        package.stage("v1", project_root=root)


@pytest.mark.parametrize("version", ["", " ", "v1\nv2", "v1\0"])
def test_bad_version_rejected(project, version):
    root, _ = project
    with pytest.raises(ValueError, match="Version"):
        package.stage(version, project_root=root)
