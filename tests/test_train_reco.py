"""`agent/train.py` 的"能不能发车"判定。"""

from types import SimpleNamespace

import train
from rect import DEFAULT_HIT_BOX


def _ctx():
    calls = []
    return SimpleNamespace(clear_hit_count=lambda name: calls.append(name)), calls


def test_acceptable_when_any_free(monkeypatch):
    """有一条不是 busy 就能发车，并把滑动兜底的命中预算清零。"""
    ctx, calls = _ctx()
    monkeypatch.setattr(
        train,
        "_train_list",
        [SimpleNamespace(stutus="busy"), SimpleNamespace(stutus="rewardable")],
    )

    assert (
        train.TrainLoopAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is DEFAULT_HIT_BOX
    )
    assert calls == ["TrainMoveToBottom"]


def test_not_acceptable_when_all_busy(monkeypatch):
    ctx, calls = _ctx()
    monkeypatch.setattr(train, "_train_list", [SimpleNamespace(stutus="busy")])

    assert (
        train.TrainLoopAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is None
    )
    assert calls == []


def test_not_acceptable_on_empty_list(monkeypatch):
    ctx, calls = _ctx()
    monkeypatch.setattr(train, "_train_list", [])

    assert (
        train.TrainLoopAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is None
    )
    assert calls == []
