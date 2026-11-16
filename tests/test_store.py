"""Store 持久化与 Daily 每日门控的离线回归。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import store
from rect import DEFAULT_HIT_BOX


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def daily(data_dir):
    return store.Daily[bool]("t.json", False)


def _argv():
    return SimpleNamespace(custom_recognition_param=None, node_name="GiftStopDaily")


def test_default_then_roundtrip(data_dir):
    value = store.Store[bool]("state.json", False)
    assert value.value is False
    value.set(True)
    assert store.Store[bool]("state.json", False).value is True
    assert list(data_dir.glob("*.tmp")) == []


def test_daily_roundtrip(daily, data_dir):
    assert daily.value is False
    assert daily.act(_argv(), True)
    assert store.Daily[bool]("t.json", False).value is True


def test_cross_day_resets(daily, monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 11, 16, 4, 59)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(store, "datetime", Clock)
    assert daily.act(_argv(), True)
    assert daily.value is True
    Clock.current = datetime(2026, 11, 16, 5, 0)
    assert daily.value is False


def test_reco_hits_when_done_and_disables_node(daily):
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or True)
    assert daily.reco(ctx, _argv()) is None
    assert calls == []
    daily.act(_argv(), True)
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX
    assert calls == [{"GiftStopDaily": {"enabled": False}}]


@pytest.mark.parametrize("result", [False, None])
def test_reco_keeps_hit_when_disable_fails(daily, result):
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or result)
    daily.act(_argv(), True)
    assert daily.reco(ctx, _argv()) is DEFAULT_HIT_BOX
    assert calls == [{"GiftStopDaily": {"enabled": False}}]


def test_reco_skipped_entirely_when_disabled(daily):
    calls = []
    ctx = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or True)
    argv = _argv()
    argv.custom_recognition_param = '{"enable": false}'
    daily.act(argv, True)
    assert daily.reco(ctx, argv) is None
    assert calls == []


def test_failed_replace_restores_value_and_cleans_temp(data_dir, monkeypatch):
    value = store.Store[bool]("state.json", False)
    value.set(True)

    def fail(*args):
        raise OSError("test replace failure")

    monkeypatch.setattr(store.os, "replace", fail)
    value.set(False)
    assert value.value is True
    assert store.Store[bool]("state.json", False).value is True
    assert list(data_dir.glob("*.tmp")) == []


def test_invalid_state_uses_independent_default(data_dir):
    (data_dir / "state.json").write_text('"wrong type"', encoding="utf-8")
    default = []
    value = store.Store[list]("state.json", default)
    value.value.append(1)
    assert default == []
