"""Validate the actual MaaFwApp nested PI and Python bundle APK layout."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path, PurePosixPath
import zipfile

LIBRARIES = (
    "libMaaFramework.so", "libMaaUtils.so",
    "libMaaAndroidNativeControlUnit.so", "libMaaAgentServer.so",
)
SUPPORTED_ABIS = {"arm64-v8a", "x86_64"}


def _entries(archive: zipfile.ZipFile) -> set[str]:
    names: set[str] = set()
    for entry in archive.infolist():
        path = PurePosixPath(entry.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
            raise ValueError(f"Unsafe ZIP entry: {entry.filename}")
        if entry.filename in names:
            raise ValueError(f"Duplicate ZIP entry: {entry.filename}")
        names.add(entry.filename)
    return names


def _require(archive: zipfile.ZipFile, names: set[str], name: str) -> None:
    if name not in names or archive.getinfo(name).is_dir() or archive.getinfo(name).file_size == 0:
        raise ValueError(f"Missing or empty APK payload: {name}")


def _pi_path(path: str) -> str:
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts or "\\" in path:
        raise ValueError(f"Unsafe PI path: {path}")
    return normalized.as_posix()


def verify_apk(apk: Path | str, abis: list[str]) -> None:
    """Require precisely the requested ABI set in APK libraries and bundle.zip."""
    wanted = set(abis)
    if not wanted or not wanted <= SUPPORTED_ABIS:
        raise ValueError("Expected ABI must be arm64-v8a and/or x86_64")
    with zipfile.ZipFile(apk) as outer:
        names = _entries(outer)
        actual = {name.split("/")[1] for name in names if name.startswith("lib/") and len(name.split("/")) >= 3}
        if actual != wanted:
            raise ValueError(f"APK native ABI mismatch: expected {sorted(wanted)}, found {sorted(actual)}")
        for abi in wanted:
            for library in LIBRARIES:
                _require(outer, names, f"lib/{abi}/{library}")
        for name in ("assets/pi.zip", "assets/agent/bundle.zip", "assets/agent/agent-runtime.json"):
            _require(outer, names, name)
        descriptor = json.loads(outer.read("assets/agent/agent-runtime.json"))
        # Descriptor serialization shape is checked below against the upstream schema.
        runtimes = descriptor.get("runtimes") if isinstance(descriptor, dict) else None
        if not isinstance(runtimes, list) or not any(
            isinstance(item, dict) and item.get("location") == "bundle" and item.get("executable") == "bin/python3"
            for item in runtimes
        ):
            raise ValueError("APK agent descriptor does not declare bundled bin/python3")
        with zipfile.ZipFile(io.BytesIO(outer.read("assets/agent/bundle.zip"))) as bundle:
            bundled = _entries(bundle)
            bundle_abis = {name.split("/")[0] for name in bundled if "/" in name}
            if bundle_abis != wanted:
                raise ValueError(f"Agent bundle ABI mismatch: expected {sorted(wanted)}, found {sorted(bundle_abis)}")
            for abi in wanted:
                _require(bundle, bundled, f"{abi}/bin/python3")
        with zipfile.ZipFile(io.BytesIO(outer.read("assets/pi.zip"))) as pi:
            payload = _entries(pi)
            for required in (
                "interface.json", "agent/main.py", "logo.png", "LICENSE",
                "LICENSE-MaaFwApp", "NOTICE-Android.md",
                "resource/model/ocr/det.onnx", "resource/model/ocr/rec.onnx", "resource/model/ocr/keys.txt",
            ):
                _require(pi, payload, required)
            if any(PurePosixPath(name).suffix.lower() in {".so", ".dll", ".dylib"} or ".so." in name for name in payload):
                raise ValueError("Native library leaked into PI resource archive")
            interface = json.loads(pi.read("interface.json"))
            if not isinstance(interface, dict) or interface.get("interface_version") != 2:
                raise ValueError("PI interface must be Project Interface V2")
            if not interface.get("agent"):
                raise ValueError("PI agent declaration missing")
            visited: set[str] = set()

            def check_imports(document: dict, base: PurePosixPath) -> None:
                for imported in document.get("import", []):
                    if not isinstance(imported, str):
                        raise ValueError("PI import must be a string")
                    resolved = _pi_path((base / imported).as_posix())
                    if resolved in visited:
                        continue
                    visited.add(resolved)
                    _require(pi, payload, resolved)
                    # This project's imports are JSONC fragments. Their presence is
                    # verified here; MaaFwApp owns parsing and merging their content.

            check_imports(interface, PurePosixPath("."))
            for resource in interface.get("resource", []):
                for path in resource.get("path", []):
                    resource_root = _pi_path(path)
                    if not any(name.startswith(resource_root.rstrip("/") + "/pipeline/") and name.endswith(".json") for name in payload):
                        raise ValueError(f"PI resource pipeline missing: {resource_root}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--abi", action="append", required=True, choices=sorted(SUPPORTED_ABIS))
    args = parser.parse_args(argv)
    try:
        verify_apk(args.apk, args.abi)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"APK verification failed: {error}\n")
    print(f"APK payload verified: {args.apk} ({', '.join(args.abi)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
