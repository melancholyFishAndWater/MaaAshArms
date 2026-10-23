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
        node_name="GiftProbeDaily",
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


def test_reco_always_hits_but_stops_when_done(daily):
    """reco 恒命中（返回 DEFAULT_HIT_BOX）；今日已做时额外跑 TaskStop 结束任务。

    跳过不再用"返回 None"表达：识别恒命中，结束只由 TaskStop 负责——
    未命中会让 `[once]` 探测节点静默失败，任务收不到明确的停止指令。
    """
    calls = []
    ctx = SimpleNamespace(
        run_action=lambda name, pipeline_override=None: (
            calls.append(name) or SimpleNamespace(success=True)
        )
    )

    # 今日未做 → 命中，不跑 TaskStop
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX
    assert calls == []

    # 今日已做 → 仍命中（恒返回 box），但已跑 TaskStop
    daily.set("TaskGift", True)
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX
    assert calls == ["TaskStop"]


def test_reco_still_stops_when_taskstop_fails(daily):
    """TaskStop 跑失败也要返回命中：识别结果与收尾动作互不牵连。"""
    ctx = SimpleNamespace(run_action=lambda name, pipeline_override=None: None)

    daily.set("TaskGift", True)
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX


def test_reco_skipped_entirely_when_disabled(daily):
    """`enable: false`（任务选项关掉每日一次）时不做跳过判定。"""
    calls = []
    ctx = SimpleNamespace(
        run_action=lambda name, pipeline_override=None: (
            calls.append(name) or SimpleNamespace(success=True)
        )
    )
    argv = _argv()
    argv.custom_recognition_param = '{"enable": false}'

    daily.set("TaskGift", True)
    assert daily.reco(ctx, argv) is DEFAULT_HIT_BOX
    assert calls == []
