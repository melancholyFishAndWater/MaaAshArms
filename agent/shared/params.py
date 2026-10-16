"""读 pipeline 传给 agent 的参数：节点的 custom_*_param 与 attach。

放在 agent/shared/ 下靠 agent/main.py 把它注入 sys.path 后按顶层名导入（`from params import ...`）。
键的命名约束见 docs/zh_cn/develop/node-naming.md 第七节（字典 + snake_case）。
"""

import json
from typing import Any

from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition


# TODO 全部替换
def parse_params(raw: str | None, *required_keys: str) -> dict[str, Any]:
    """解析 MaaFW 传入的 JSON 参数字符串。

    处理各种"空/异常"输入，并可选校验必填字段。

    Args:
        raw: 原始参数字符串。可能为 None 或空串（表示未传参）。
             MaaFW 在节点未写 custom_*_param 时，也可能传入字面量 "null"。
        *required_keys: 需要校验存在的必填字段名。若提供，则缺少时抛异常。

    Returns:
        解析后的参数字典。若 raw 为空且无必填字段，返回空字典 {}。

    Raises:
        ValueError: 以下任一情况：
            - raw 为空但存在必填字段
            - JSON 解析失败
            - 解析结果为 null 但存在必填字段
            - 解析结果不是对象(dict)
            - 缺少必填字段
    """
    if not raw:
        if required_keys:
            raise ValueError(f"参数为空，需要字段: {list(required_keys)}")
        return {}
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON解析失败: {e}") from e
    if params is None:
        # MaaFW 在节点未写 custom_*_param 时传的是字面量 "null" 而非空串，视同缺省
        if required_keys:
            raise ValueError(f"参数为空，需要字段: {list(required_keys)}")
        return {}
    if not isinstance(params, dict):
        raise ValueError(f"参数必须是对象，得到: {type(params).__name__}")
    if required_keys:
        missing = [k for k in required_keys if k not in params]
        if missing:
            raise ValueError(f"缺少必填字段: {missing}")
    return params


def parse_attach(
    context: Context,
    argv: CustomRecognition.AnalyzeArg | CustomAction.RunArg,
    *required_keys: str,
) -> dict[str, Any]:
    """获取节点自身的 attach

    可选校验必填字段

    Args:
        context (Context): 上下文
        argv (CustomRecognition.AnalyzeArg | CustomAction.RunArg): 参数

    Raises:
        ValueError: 缺少必填字段

    Returns:
        dict[str, Any]: 校验过的 attach 字典
    """
    attach: dict[str, str] = (context.get_node_data(argv.node_name) or {}).get(
        "attach", {}
    )
    if required_keys:
        missing = [k for k in required_keys if k not in attach]
        if missing:
            raise ValueError(f"缺少必填字段: {missing}")
    return attach
