"""第一层：持久化与每日门控的业务契约，不断言框架调用时机。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import store


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


@pytest.mark.parametrize("params,done,expected_hit", [
    (None, False, False),
    (None, True, True),
    ('{"enable": true}', False, False),
    ('{"enable": true}', True, True),
    ('{"enable": false}', False, False),
    ('{"enable": false}', True, False),
])
def test_daily_gate_business_contract(daily, params, done, expected_hit):
    """只约束是否跳过业务任务，不约束通过哪次框架调用优化扫描。"""
    if done:
        daily.act(_argv(), True)
    argv = _argv()
    argv.custom_recognition_param = params
    ctx = SimpleNamespace(override_pipeline=lambda patch: True)
    result = daily.reco(ctx, argv)
    assert (result is not None) == expected_hit


def test_daily_gate_uses_custom_completion_policy(data_dir):
    daily = store.Daily[int]("count.json", 0)
    ctx = SimpleNamespace(override_pipeline=lambda patch: True)
    daily.act(_argv(), 2)
    assert daily.reco(ctx, _argv(), func=lambda value: value >= 3) is None
    daily.act(_argv(), 3)
    assert daily.reco(ctx, _argv(), func=lambda value: value >= 3) is not None


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
