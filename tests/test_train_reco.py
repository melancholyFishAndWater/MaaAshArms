"""`agent/train.py` 的"能不能发车"判定。"""

from types import SimpleNamespace

import train
from rect import DEFAULT_HIT_BOX


def _ctx():
    """记录 `clear_hit_count` 调用：这个判定不该复位任何节点的命中预算。"""
    calls = []
    return SimpleNamespace(clear_hit_count=lambda name: calls.append(name)), calls


def test_acceptable_when_any_free(monkeypatch):
    """有一条不是 busy 就能发车。

    这里曾经跟着 `clear_hit_count("TrainMoveToBottom")`：那个 `max_hit: 1` 的滑动兜底节点
    于 2026-10-24 随自定义列车路线移除（`098657d`），复位点一并删掉，所以断言"没有复位"。
    """
    ctx, calls = _ctx()
    monkeypatch.setattr(
        train,
        "_train_list",
        [SimpleNamespace(stutus="busy"), SimpleNamespace(stutus="rewardable")],
    )

    assert (
        train.TrainBranchAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is DEFAULT_HIT_BOX
    )
    assert calls == []


def test_not_acceptable_when_all_busy(monkeypatch):
    ctx, calls = _ctx()
    monkeypatch.setattr(train, "_train_list", [SimpleNamespace(stutus="busy")])

    assert (
        train.TrainBranchAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is None
    )
    assert calls == []


def test_not_acceptable_on_empty_list(monkeypatch):
    ctx, calls = _ctx()
    monkeypatch.setattr(train, "_train_list", [])

    assert (
        train.TrainBranchAcceptableReco().analyze(
            ctx, SimpleNamespace(node_name="TrainLoopAcceptable")
        )
        is None
    )
    assert calls == []
