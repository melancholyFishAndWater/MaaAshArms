"""测试公共装置：让 agent 模块按顶层名可见，并在用例间重置模块级状态。

按 `agent/main.py:9-12` 的做法把 `agent/shared` 与 `agent` 都插进 sys.path——
两边都按顶层名互相 import（`from rect import ...`），只加 `agent` 不够。
"""

import sys
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
for _path in (AGENT_DIR / "shared", AGENT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """entrust / train 的判定依赖模块级全局，用例之间必须互不影响。

    用 monkeypatch 而不是手工赋值：它在用例结束时自动还原原始对象。
    """
    import entrust
    import train

    monkeypatch.setattr(entrust, "_rewardable_by_timestamp", None)
    monkeypatch.setattr(entrust, "_times", 0)
    monkeypatch.setattr(entrust, "_number", 0)
    monkeypatch.setattr(entrust, "_max", 0)
    monkeypatch.setattr(train, "_train_list", [])
