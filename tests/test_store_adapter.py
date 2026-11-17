"""第二层：Daily 到 MaaFW 的接线策略；策略改变时仅复核这一层。"""
from types import SimpleNamespace

import pytest

import store
from rect import DEFAULT_HIT_BOX


def make_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path)
    return store.Daily[bool]("adapter.json", False)


def test_completed_gate_returns_framework_box_without_disabling(tmp_path, monkeypatch):
    daily = make_daily(tmp_path, monkeypatch)
    argv = SimpleNamespace(node_name="CallerGate", custom_recognition_param=None)
    daily.act(argv, True)
    calls = []
    context = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or True)
    assert daily.reco(context, argv) == DEFAULT_HIT_BOX
    assert calls == []


@pytest.mark.parametrize("params,done", [(None, False), ('{"enable": false}', True)])
@pytest.mark.parametrize("override_result", [True, False, None])
def test_nonmatching_gate_disables_only_current_node_and_remains_miss(tmp_path, monkeypatch, params, done, override_result):
    """当前优化策略：不命中的门控退出候选扫描；覆写失败也不能变成命中。"""
    daily = make_daily(tmp_path, monkeypatch)
    argv = SimpleNamespace(node_name="AnotherDailyGate", custom_recognition_param=params)
    if done:
        daily.act(argv, True)
    calls = []
    context = SimpleNamespace(override_pipeline=lambda patch: calls.append(patch) or override_result)
    assert daily.reco(context, argv) is None
    assert calls == [{"AnotherDailyGate": {"enabled": False}}]
