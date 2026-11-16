"""职责边界回归：日志层独立，绘图兼容共享格式并跟踪摘要。"""
import ast
import importlib
from pathlib import Path

from tools.logs import cli
from tools.logs.lib import formats
from tools.drawing.lib.flowlog import constants
from tools.drawing.lib.cases import signature


def test_logs_does_not_import_consumers():
    root = Path(__file__).resolve().parents[1] / "tools" / "logs"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(name.startswith(("tools.drawing", "tools.sentry", "PIL", "maa")) for name in names), str(path)


def test_shared_formats_preserve_old_import_surface():
    for name in ("LINE_RE", "TASK_START_RE", "TASK_END_RE", "NODE_DONE_RE"):
        assert getattr(constants, name) is getattr(formats, name)
    assert "tools/logs/lib/formats.py" in signature._PARSE_IMPL_FILES


def test_drawing_no_longer_owns_inspection():
    drawing_cli = importlib.import_module("tools.drawing.cli")
    assert "inspect" not in drawing_cli.COMMANDS


def test_logs_entrypoint_dispatch(tmp_path, capsys):
    path = tmp_path / "example.log"
    path.write_text("[2026-11-16 20:00:00.000][INF] example\n", encoding="utf-8")
    assert cli.main(["inspect", str(path)]) == 0
    assert '"total": 1' in capsys.readouterr().out
    assert cli.main(["--help"]) == 0
    assert "inspect" in capsys.readouterr().out
