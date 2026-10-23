"""委派链的纯决策识别：领奖窗口与"剩余次数是否已满"。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import entrust
from rect import DEFAULT_HIT_BOX


def test_get_reward_reco_without_window():
    """没有窗口就不命中——本轮任务链没成功委派，没记下可领取时间。"""
    assert entrust.TaskEntrustGetRewardReco().analyze(None, None) is None


def test_get_reward_reco_waits_before_due(monkeypatch):
    """没到可领取时间就不命中：委派刚发出去，还在跑。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() + timedelta(seconds=10)
    )

    assert entrust.TaskEntrustGetRewardReco().analyze(None, None) is None
    assert entrust._rewardable_by_timestamp is not None


def test_get_reward_reco_hits_when_due(monkeypatch):
    """过了可领取时间才命中，并清掉时间点，保证一次任务链只领一次。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() - timedelta(seconds=1)
    )

    assert entrust.TaskEntrustGetRewardReco().analyze(None, None) is DEFAULT_HIT_BOX
    assert entrust._rewardable_by_timestamp is None


def test_training_reward_reco_consumes_window(monkeypatch):
    """训练所那条不看时间：有窗口就消耗并命中一次。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() + timedelta(seconds=10)
    )

    assert (
        entrust.TaskEntrustTrainingGetRewardReco().analyze(None, None)
        is DEFAULT_HIT_BOX
    )
    assert entrust._rewardable_by_timestamp is None


def _touch_max_ctx():
    calls = []
    return SimpleNamespace(run_action=lambda name: calls.append(name)), calls


def _patch_ocr(monkeypatch, text="5"):
    monkeypatch.setattr(
        entrust, "get_ocr_level_times", lambda context, argv: SimpleNamespace(text=text)
    )


def test_touch_max_reco_stops_on_zero(monkeypatch):
    """实际委派数为 0 属不该出现的状态，直接结束任务。"""
    ctx, calls = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 0)

    assert (
        entrust.EntrustBranchTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is None
    )
    assert calls == ["TaskStopError"]


def test_touch_max_reco_hits_when_full(monkeypatch):
    ctx, calls = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 5)
    monkeypatch.setattr(entrust, "_max", 20)
    monkeypatch.setattr(entrust, "_times", 20)

    assert (
        entrust.EntrustBranchTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is DEFAULT_HIT_BOX
    )
    assert calls == []


def test_touch_max_reco_waits_when_not_full(monkeypatch):
    ctx, calls = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 5)
    monkeypatch.setattr(entrust, "_max", 20)
    monkeypatch.setattr(entrust, "_times", 10)

    assert (
        entrust.EntrustBranchTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is None
    )
    assert calls == []
