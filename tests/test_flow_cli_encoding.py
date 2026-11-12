"""GBK console output must not abort the flow CLI listing."""

import os
import subprocess
import sys
from pathlib import Path


def test_flow_list_gbk_replaces_unencodable_characters():
    root = Path(__file__).resolve().parents[1]
    code = '''\
from unittest.mock import patch
from tools.drawing.lib.flowlog import cli
with patch.object(cli, "render_flow", return_value="候选：甲・乙"):
    raise SystemExit(cli.main(["fake.log", "--no-telemetry", "--list", "--log-file", "none"]))
'''
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "gbk:strict"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode("gbk", errors="replace")
    assert "候选：甲?乙" in result.stdout.decode("gbk")
