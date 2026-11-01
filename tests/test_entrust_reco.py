"""委派链的纯决策识别：领奖窗口与"剩余次数是否已满"。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import entrust
from rect import DEFAULT_HIT_BOX


def test_get_reward_reco_without_window():
    """没有窗口就不命中——本轮任务链没成功委派，没记下可领取时间。"""
    assert entrust.EntrustRewardableNowReco().analyze(None, None) is None


def test_get_reward_reco_waits_before_due(monkeypatch):
    """没到可领取时间就不命中：委派刚发出去，还在跑。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() + timedelta(seconds=10)
    )

    assert entrust.EntrustRewardableNowReco().analyze(None, None) is None
    assert entrust._rewardable_by_timestamp is not None


def test_get_reward_reco_hits_when_due(monkeypatch):
    """过了可领取时间才命中，并清掉时间点，保证一次任务链只领一次。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() - timedelta(seconds=1)
    )

    assert entrust.EntrustRewardableNowReco().analyze(None, None) is DEFAULT_HIT_BOX
    assert entrust._rewardable_by_timestamp is None


def test_training_reward_reco_consumes_window(monkeypatch):
    """训练所那条不看时间：有窗口就消耗并命中一次。"""
    monkeypatch.setattr(
        entrust, "_rewardable_by_timestamp", datetime.now() + timedelta(seconds=10)
    )

    assert entrust.EntrustRewardableLateReco().analyze(None, None) is DEFAULT_HIT_BOX
    assert entrust._rewardable_by_timestamp is None


class _FakeCtx:
    """`EntrustProbeTouchMaxReco` 用到的两个入口：`run_action` 与 `override_pipeline`。

    用真类而不是 `SimpleNamespace`：`_number == 0` 那条分支走 `override_pipeline`
    （把本节点的 `next` 覆写成 `BaseTaskStopError`），`SimpleNamespace` 没有这个方法，
    用例会假红（`AttributeError: 'types.SimpleNamespace' object has no attribute
    'override_pipeline'`）。
    """

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.overrides: list[dict] = []

    def run_action(self, name: str) -> None:
        self.actions.append(name)

    def override_pipeline(self, pipeline: dict) -> bool:
        self.overrides.append(pipeline)
        return True


def _touch_max_ctx() -> _FakeCtx:
    return _FakeCtx()


def _patch_ocr(monkeypatch, text="5"):
    monkeypatch.setattr(
        entrust, "get_ocr_level_times", lambda context, argv: SimpleNamespace(text=text)
    )


def test_touch_max_reco_stops_on_zero(monkeypatch):
    """实际委派数为 0 属不该出现的状态：覆写本节点 next 指向 BaseTaskStopError，命中一次收口。"""
    ctx = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 0)

    assert (
        entrust.EntrustProbeTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is DEFAULT_HIT_BOX
    )
    assert ctx.overrides == [{"EntrustBranchTouchMax": {"next": ["BaseTaskStopError"]}}]
    assert ctx.actions == []


def test_touch_max_reco_hits_when_full(monkeypatch):
    ctx = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 5)
    monkeypatch.setattr(entrust, "_max", 20)
    monkeypatch.setattr(entrust, "_times", 20)

    assert (
        entrust.EntrustProbeTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is DEFAULT_HIT_BOX
    )
    assert ctx.actions == []
    assert ctx.overrides == []


def test_touch_max_reco_waits_when_not_full(monkeypatch):
    ctx = _touch_max_ctx()
    _patch_ocr(monkeypatch)
    monkeypatch.setattr(entrust, "_number", 5)
    monkeypatch.setattr(entrust, "_max", 20)
    monkeypatch.setattr(entrust, "_times", 10)

    assert (
        entrust.EntrustProbeTouchMaxReco().analyze(
            ctx, SimpleNamespace(node_name="EntrustBranchTouchMax")
        )
        is None
    )
    assert ctx.actions == []
    assert ctx.overrides == []
