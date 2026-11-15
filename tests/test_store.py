"""`agent/store.py`：每日一次的状态文件与跳过判定。

状态目录用测试自己管的 `tests/.store-tmp/`，不用 pytest 的 `tmp_path`——
后者建在系统临时目录，受限环境下会被拒绝访问。
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import store
from rect import DEFAULT_HIT_BOX


@pytest.fixture
def data_dir():
    root = Path(__file__).resolve().parent / ".store-tmp"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def daily(data_dir, monkeypatch):
    """把状态目录重定向到 data_dir，避免写进仓库的 debug/。"""
    monkeypatch.setattr(store, "_data_dir", lambda: data_dir)
    return store.Store[bool]("t.json")


def _argv(entry="TaskGift"):
    return SimpleNamespace(
        custom_recognition_param=None,
        task_detail=SimpleNamespace(entry=entry),
        node_name="GiftStopDaily",
    )


def test_default_then_roundtrip(daily, data_dir):
    assert daily.get("TaskGift") is False

    daily.set("TaskGift", True)

    again = store.Store[bool]("t.json")
    assert again.get("TaskGift") is True
    # 原子写：临时文件必须清干净
    assert list(data_dir.glob("*.tmp")) == []


def test_cross_day_resets(daily):
    daily.set("TaskGift", True)

    daily._today = "1999-01-01"  # 冒充昨天

    assert daily.get("TaskGift") is False


def test_reco_hits_until_done_then_disables_node(daily):
    """今日未做返回命中；已做则禁用本节点并返回 None，不直接执行停止动作。"""
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or True)
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX
    assert calls == []
    daily.set("TaskGift", True)
    assert daily.reco(ctx, _argv()) is None
    assert calls == [{"GiftStopDaily": {"enabled": False}}]


@pytest.mark.parametrize("result", [False, None])
def test_reco_returns_miss_when_disable_fails(daily, result):
    """禁用失败不恢复旧命中语义，仍返回 None，且尝试禁用的是 argv 的节点。"""
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or result)
    daily.set("TaskGift", True)
    assert daily.reco(ctx, _argv()) is None
    assert calls == [{"GiftStopDaily": {"enabled": False}}]


def test_reco_skipped_entirely_when_disabled(daily):
    """`enable: false`（任务选项关掉每日一次）时不做跳过判定。"""
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or True)
    argv = _argv()
    argv.custom_recognition_param = '{"enable": false}'

    daily.set("TaskGift", True)
    assert daily.reco(ctx, argv) is DEFAULT_HIT_BOX
    assert calls == []
