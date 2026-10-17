"""`agent/shared/params.py`：pipeline 传给 agent 的参数解析。"""

from types import SimpleNamespace

import pytest

import params


def test_parse_params_empty_forms():
    """未传参有三种写法：None、空串、字面量 "null"，都应视同空字典。"""
    assert params.parse_params(None) == {}
    assert params.parse_params("") == {}
    assert params.parse_params("null") == {}


def test_parse_params_rejects_bad_input():
    with pytest.raises(ValueError):
        params.parse_params("[]")  # 不是对象
    with pytest.raises(ValueError):
        params.parse_params('{"a":')  # JSON 坏掉
    with pytest.raises(ValueError):
        params.parse_params(None, "a")  # 空参却要求必填


def test_parse_params_checks_required_keys():
    with pytest.raises(ValueError):
        params.parse_params('{"a": 1}', "b")
    assert params.parse_params('{"a": 1}', "a") == {"a": 1}


def test_parse_attach():
    ctx = SimpleNamespace(
        get_node_data=lambda name: {"attach": {"stage_name": "结晶废墟", "level": 10}}
    )
    argv = SimpleNamespace(node_name="EntrustInit")

    assert params.parse_attach(ctx, argv, "stage_name") == {
        "stage_name": "结晶废墟",
        "level": 10,
    }
    with pytest.raises(ValueError):
        params.parse_attach(ctx, argv, "missing_key")
