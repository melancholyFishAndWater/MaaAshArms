"""Synthetic APK tests; no Android SDK or device is involved."""

import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "android_verify_apk", Path(__file__).resolve().parents[1] / "Android/verify_apk.py"
)
assert _SPEC is not None and _SPEC.loader is not None
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def valid_entries(abis=("arm64-v8a",)):
    interface = {
        "interface_version": 2, "agent": {"child_exec": "python"},
        "resource": [{"path": ["./resource"]}],
        "import": ["resource/tasks/option.json"],
    }
    pi = {name: b"content" for name in (
        "agent/main.py", "logo.png", "LICENSE", "LICENSE-MaaFwApp", "NOTICE-Android.md",
        "resource/model/ocr/det.onnx", "resource/model/ocr/rec.onnx", "resource/model/ocr/keys.txt",
    )}
    pi["interface.json"] = json.dumps(interface)
    pi["resource/tasks/option.json"] = "{}"
    pi["resource/pipeline/startup.json"] = "{}"
    bundle = {f"{abi}/bin/python3": b"ELF-python" for abi in abis}
    outer = {f"lib/{abi}/{library}": b"ELF-library" for abi in abis for library in verify.LIBRARIES}
    outer["assets/pi.zip"] = archive_bytes(pi)
    outer["assets/agent/bundle.zip"] = archive_bytes(bundle)
    outer["assets/agent/agent-runtime.json"] = json.dumps({
        "runtimes": [{"location": "bundle", "executable": "bin/python3", "args": ["-u", "agent/main.py"]}]
    })
    return outer


def run_verify(entries, abis=("arm64-v8a",)):
    verify.verify_apk(io.BytesIO(archive_bytes(entries)), list(abis))


@pytest.mark.parametrize("abis", [("arm64-v8a",), ("x86_64",), ("arm64-v8a", "x86_64")])
def test_valid_apk_layout(abis):
    run_verify(valid_entries(abis), abis)


@pytest.mark.parametrize("name", [
    "lib/arm64-v8a/libMaaAgentServer.so", "lib/arm64-v8a/libMaaAndroidNativeControlUnit.so",
    "assets/pi.zip", "assets/agent/bundle.zip", "assets/agent/agent-runtime.json",
])
def test_missing_outer_file(name):
    entries = valid_entries()
    entries.pop(name)
    with pytest.raises(ValueError, match="Missing or empty"):
        run_verify(entries)


def test_extra_native_architecture_rejected():
    entries = valid_entries()
    entries["lib/x86_64/libMaaUtils.so"] = b"extra"
    with pytest.raises(ValueError, match="native ABI mismatch"):
        run_verify(entries)


def test_missing_native_architecture_rejected():
    with pytest.raises(ValueError, match="native ABI mismatch"):
        run_verify(valid_entries(), ("arm64-v8a", "x86_64"))


@pytest.mark.parametrize("bundle", [
    {"arm64-v8a/bundle/bin/python3": b"wrong-layout"},
    {"arm64-v8a/bin/python3": b"python", "x86_64/bin/python3": b"extra"},
    {"arm64-v8a/bin/python3": b""},
])
def test_bad_bundle_rejected(bundle):
    entries = valid_entries()
    entries["assets/agent/bundle.zip"] = archive_bytes(bundle)
    with pytest.raises(ValueError):
        run_verify(entries)


@pytest.mark.parametrize("name", ["LICENSE-MaaFwApp", "resource/tasks/option.json", "resource/model/ocr/keys.txt", "agent/main.py"])
def test_missing_pi_payload(name):
    entries = valid_entries()
    with zipfile.ZipFile(io.BytesIO(entries["assets/pi.zip"])) as pi:
        payload = {entry: pi.read(entry) for entry in pi.namelist() if entry != name}
    entries["assets/pi.zip"] = archive_bytes(payload)
    with pytest.raises(ValueError, match="Missing or empty"):
        run_verify(entries)


def test_native_file_in_pi_rejected():
    entries = valid_entries()
    with zipfile.ZipFile(io.BytesIO(entries["assets/pi.zip"])) as pi:
        payload = {entry: pi.read(entry) for entry in pi.namelist()}
    payload["resource/model/helper.so"] = b"native"
    entries["assets/pi.zip"] = archive_bytes(payload)
    with pytest.raises(ValueError, match="leaked"):
        run_verify(entries)


def test_zip_traversal_rejected():
    entries = valid_entries()
    entries["../outside"] = b"unsafe"
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        run_verify(entries)


def test_descriptor_requires_python_runtime():
    entries = valid_entries()
    entries["assets/agent/agent-runtime.json"] = '{"runtimes": []}'
    with pytest.raises(ValueError, match="descriptor"):
        run_verify(entries)
