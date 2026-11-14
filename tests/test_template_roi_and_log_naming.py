"""模板必须能放进固定 ROI；本地诊断图与遥测图命名和清理隔离。"""
import struct
from pathlib import Path

from tools.validation.lib.refs.model import Ctx, Node
from tools.validation.lib.refs.rules_misc import check_template_roi
from tools.drawing.lib.cases.output import base_filename, prune_artifacts
from tools.drawing.lib.cases.pack import Case
from tools.drawing.lib.flowlog.render import FlowOptions


def test_template_roi_dimensions(tmp_path):
    image = tmp_path / "Confirm.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 93, 51))
    configs = {
        "Bad": {"recognition": "TemplateMatch", "template": "Confirm.png", "roi": [0, 0, 105, 46]},
        "Exact": {"recognition": "TemplateMatch", "template": "Confirm.png", "roi": [0, 0, 93, 51]},
        "Dynamic": {"recognition": "TemplateMatch", "template": "Confirm.png", "roi": "OtherNode"},
        "Offset": {"recognition": "TemplateMatch", "template": "Confirm.png", "roi": [0, 0, 100, 60], "roi_offset": [0, 0, 0, -14]},
        "Nested": {"recognition": {"type": "TemplateMatch", "param": {"template": ["Confirm.png"], "roi": [0, 0, 90, 51]}}},
        "Inline": {"recognition": "And", "all_of": [{"recognition": "TemplateMatch", "template": "Confirm.png", "roi": [0, 0, 90, 51]}]},
    }
    ctx = Ctx(repo_root=tmp_path)
    ctx.image_roots = [tmp_path]
    ctx.nodes = {name: Node(name, data, tmp_path / "pipeline.json") for name, data in configs.items()}
    check_template_roi(ctx)
    assert {f.message.split(" ")[0] for f in ctx.findings} == {"Bad", "Offset", "Nested", "Inline"}
    assert all(f.severity == "error" and f.id == "C8-template-larger-than-roi" for f in ctx.findings)
    assert "93×51" in ctx.findings[0].message and "105×46" in ctx.findings[0].message


def test_local_log_filename_and_pruning(tmp_path):
    log = tmp_path / "maafw.log"
    log.write_text("", encoding="utf-8")
    case = Case(label="local", log_path=str(log))
    stem = base_filename(case, FlowOptions(issue="PYTHON-99"), issue_only=True, issue_label="Gift")
    assert stem.startswith("PYTHON-99_Gift_命中日志")
    name = stem + ".png"
    (tmp_path / name).write_bytes(b"test")
    manifest = {"cases": {name: {"issue": "PYTHON-99", "origin": "bulk"}}}
    assert prune_artifacts(str(tmp_path), manifest, set(), issues=set()) == 0
    assert (tmp_path / name).is_file()
