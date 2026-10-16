"""MFA 注入给 agent 的 PI_* 环境变量。

MFA 注入的是 PI_* 全集（Extensions/MaaFW/AgentHelper.cs:581-590）。这里只有"客户端 / 资源 /
控制器"的身份与上下文；用户在面板上选的选项不走环境变量，而是开始任务时变成 pipeline_override
落到节点上（读它用 context.get_node_data()）。

环境变量在 agent 生命周期里不会变，所以 pi_env() 缓存；force 留给测试。

放在 agent/shared/ 下靠 agent/main.py 把它注入 sys.path 后按顶层名导入（`from pienv import ...`）。
"""

import json
import os
from typing import Any

from agentlog import log

PI_ENV_KEYS = (
    "PI_INTERFACE_VERSION",  # PI 规范版本
    "PI_CLIENT_NAME",  # 客户端名，如 MFAAvalonia
    "PI_CLIENT_VERSION",  # 客户端版本
    "PI_CLIENT_LANGUAGE",  # 界面语言，如 zh_cn
    "PI_CLIENT_MAAFW_VERSION",  # 客户端链接的 MaaFramework 版本，如 v5.12.2
    "PI_VERSION",  # 项目版本（interface.json 的 version）
    "PI_CONTROLLER",  # 选中的 controller[] 条目，JSON 文本
    "PI_RESOURCE",  # 选中的 resource[] 条目，JSON 文本
)

_pi_env: dict[str, str] | None = None


def pi_env(force: bool = False) -> dict[str, str]:
    """读取 PI_* 环境变量（进程内只读一次；缺的键给空串）。

    环境变量在 agent 生命周期里不会变，所以缓存；force 留给测试。
    返回的字典视为只读，不要就地改。
    """
    global _pi_env
    if force or _pi_env is None:
        _pi_env = {k: os.environ.get(k, "") for k in PI_ENV_KEYS}
    return _pi_env


def _as_string(value: Any) -> str:
    return "" if value is None else str(value)


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_as_string(item) for item in value if item is not None]


def _pi_json(key: str) -> dict[str, Any] | None:
    """把 JSON 文本型的环境变量解析成 dict。

    解析失败只记日志并返回 None —— 客户端没传或传坏了都不该让 agent 停摆。
    """
    raw = pi_env().get(key, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warn(f"{key} 不是合法 JSON: {e} | 原文前 200 字: {raw[:200]}")
        return None
    if not isinstance(value, dict):
        log.warn(f"{key} 应为对象，得到 {type(value).__name__}")
        return None
    return value


def pi_controller() -> dict[str, Any] | None:
    """选中的 controller[] 条目。"""
    return _pi_json("PI_CONTROLLER")


def pi_resource() -> dict[str, Any] | None:
    """选中的 resource[] 条目。"""
    return _pi_json("PI_RESOURCE")


def pi_client_language() -> str:
    return pi_env()["PI_CLIENT_LANGUAGE"]


def pi_controller_type() -> str:
    c = pi_controller()
    return _as_string(c.get("type")) if c else ""


def pi_controller_name() -> str:
    c = pi_controller()
    return _as_string(c.get("name")) if c else ""


def pi_resource_name() -> str:
    r = pi_resource()
    return _as_string(r.get("name")) if r else ""


def pi_resource_label() -> str:
    """资源显示名；取不到 label 就回落 name。"""
    r = pi_resource()
    if not r:
        return ""
    return _as_string(r.get("label")) or _as_string(r.get("name"))


def pi_resource_paths() -> list[str]:
    r = pi_resource()
    return _as_string_list(r.get("path")) if r else []


def pi_check_maafw_version() -> bool:
    """比对客户端链接的框架版本与本包 MaaFw 版本；不一致只警告（握手会给出更准确的报错）。

    两处来源的写法不同：PI_CLIENT_MAAFW_VERSION 形如 v5.12.2，包元数据是 5.12.2。
    """
    from importlib.metadata import PackageNotFoundError, version

    client = pi_env()["PI_CLIENT_MAAFW_VERSION"].lstrip("v")
    if not client:
        return True  # 不是 MFA 拉起的（VS Code 调试 / 本机冒烟），不判
    try:
        ours = version("maafw")
    except PackageNotFoundError:
        return True
    if client != ours:
        log.warn(
            f"客户端 MaaFramework {client} 与包内 MaaFw {ours} 不一致，"
            f"可能以 Protocol version mismatch 握手失败"
        )
        return False
    return True


def pi_log_snapshot() -> None:
    """启动时打一行汇总：客户端到底传没传 PI_*、传了什么。

    对齐 M9A 的 log_pi_environment()：出问题时不用让用户去 dump 环境变量。
    非 MFA 拉起时各字段都是空，会打成 '-'，这是预期的。
    """
    env = pi_env()
    log.debug(
        f"PI: interface={env['PI_INTERFACE_VERSION'] or '-'} "
        f"client={env['PI_CLIENT_NAME'] or '-'}/{env['PI_CLIENT_VERSION'] or '-'} "
        f"lang={env['PI_CLIENT_LANGUAGE'] or '-'} "
        f"maafw={env['PI_CLIENT_MAAFW_VERSION'] or '-'} "
        f"project={env['PI_VERSION'] or '-'} "
        f"controller={pi_controller_type() or '-'} "
        f"controller_ok={pi_controller() is not None} "
        f"resource_ok={pi_resource() is not None}"
    )
