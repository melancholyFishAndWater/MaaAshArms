"""Assemble the Android PI payload; native libraries belong to MaaFwApp, not PI.

Run ``python Android/package.py stage --version VERSION`` from any directory.
JSONC parsing uses the project's build-group dependency json-with-comments.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SUFFIXES = {".so", ".dll", ".dylib"}
IGNORED_DIRS = {"__pycache__", "debug", ".git", ".venv"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".log"}


def _output_path(root: Path, output: Path | str | None) -> Path:
    allowed = (root / "Android" / "staging").resolve()
    raw = Path(output) if output is not None else allowed
    if not raw.is_absolute():
        raw = root / raw
    # Never follow a link/junction into a source tree or outside the payload area.
    for part in (raw, *raw.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"Output must not use symlinks or junctions: {part}")
    target = raw.resolve()
    if target != allowed and not target.is_relative_to(allowed):
        raise ValueError(f"Output must be Android/staging or a child: {target}")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"Output must be an empty directory: {target}")
    return target


def _payload_files(source: Path) -> list[tuple[Path, Path]]:
    """Inventory first, rejecting links/native binaries before writing output."""
    result: list[tuple[Path, Path]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"Payload must not contain links: {path}")
        if not path.is_file():
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES or ".log." in path.name.lower():
            continue
        if path.suffix.lower() in NATIVE_SUFFIXES or ".so." in path.name.lower():
            raise ValueError(f"Native library must not be shipped as PI resource: {path}")
        result.append((path, relative))
    return result


def _configure_ocr(root: Path) -> None:
    # The existing tool owns the actual assets/MaaCommonAssets/OCR source layout.
    sys.path.insert(0, str(root))
    try:
        configure = importlib.import_module("tools.configure")
        configure.configure_ocr_model()
    finally:
        sys.path.pop(0)


def stage(version: str, output: Path | str | None = None, *, project_root: Path = ROOT) -> Path:
    """Build a fresh payload without overwriting/deleting existing contents."""
    root = project_root.resolve()
    target = _output_path(root, output)
    if not version.strip() or any(char in version for char in "\r\n\0"):
        raise ValueError("Version must be nonempty single-line text")

    import jsonc

    interface_source = root / "assets" / "interface.json"
    with interface_source.open(encoding="utf-8") as stream:
        interface = jsonc.load(stream)
    if not isinstance(interface, dict):
        raise ValueError("PI interface must be an object")
    interface["version"] = version
    interface["icon"] = "logo.png"
    interface["license"] = "MIT (resources); AGPL-3.0 (Android GUI)"

    copies = {
        "LICENSE": root / "LICENSE",
        "README.md": root / "README.md",
        "logo.ico": root / "assets" / "logo.ico",
        "logo.png": root / "logo.png",
        "LICENSE-MaaFwApp": root / "Android" / "MaaFwApp" / "LICENSE",
        "NOTICE-Android.md": root / "Android" / "NOTICE.md",
    }
    for source in [interface_source, *copies.values()]:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Required regular source file missing or linked: {source}")
    for source in [root / "assets" / "resource", root / "agent"]:
        if not source.is_dir() or source.is_symlink() or source.is_junction():
            raise ValueError(f"Required source directory missing or linked: {source}")

    _configure_ocr(root)
    ocr = root / "assets" / "resource" / "model" / "ocr"
    for name in ("det.onnx", "rec.onnx", "keys.txt"):
        model = ocr / name
        if not model.is_file() or model.stat().st_size == 0:
            raise ValueError(f"Required nonempty OCR model missing: {model}")

    inventory = []
    for name, source in (("resource", root / "assets" / "resource"), ("agent", root / "agent")):
        inventory.extend((path, Path(name) / relative) for path, relative in _payload_files(source))
    if not (root / "agent" / "main.py").is_file():
        raise ValueError("Required agent/main.py missing")
    payload_names = {relative.as_posix() for _, relative in inventory}
    for imported in interface.get("import", []):
        if not isinstance(imported, str) or imported not in payload_names:
            raise ValueError(f"PI import missing from payload: {imported}")

    target.mkdir(parents=True, exist_ok=True)
    for source, relative in inventory:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for name, source in copies.items():
        shutil.copy2(source, target / name)
    (target / "interface.json").write_text(
        json.dumps(interface, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage_parser = subparsers.add_parser("stage", help="Assemble a fresh Android PI payload")
    stage_parser.add_argument("--version", required=True)
    stage_parser.add_argument("--output", type=Path, default=Path("Android/staging"))
    args = parser.parse_args(argv)
    try:
        result = stage(args.version, args.output)
    except (OSError, ValueError, ImportError) as error:
        parser.exit(1, f"Android packaging failed: {error}\n")
    print(f"Android PI staged: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
