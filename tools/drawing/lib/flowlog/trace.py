"""分步摘要：把"这次画图走过的每一步"规范化后各取一个 sha，供增量绘制判断"要不要重画"。

判据分两类，写死在 `STAGE_ORDER` 与 `STAGE_LABELS` 里：

* **数据步**（`input` / `options` / `parse` / `notes` / `stale` / `card`）——每一步的产出各算一个
  sha。选包、定位 task、识别口径、注记文案改了，只有"这段数据真的变了"的那张图重画。
* **实现步**（`parse_impl` / `raster_impl`）——代码摘要，由 `cases/signature.py` 算好后经
  `trace_extra` 传进来。数据步没法覆盖"画像素那段代码改了"：改了 `draw.py` 的框线颜色、
  `paint.py` 的字号，这次运行的每一步数据一字不变，图却不一样。
* **`repo`**——仓库 `HEAD`。它只影响「疑似过时」标记，所以只当"要不要重新核对"的开关：
  变了就再读一遍日志算 `stale`，数据没变照样跳过；不算进"数据步"。

`parse_impl` 是**快路**用的：它没变、输入与参数也没变时，解析结果必然相同
（`parse_log` 只读日志正文、窗口、`task_id`、`px`、`keep_internal`），于是可以不读日志直接跳过；
它变了就下来读一遍，再比数据步——数据步全同 → 不重画，只是"读了但没用上"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# 报告"哪里变了"的顺序：先报上游原因，读的人按这个顺序排查（绘制实现变了最不可忽略）
STAGE_ORDER = (
    "raster_impl",
    "parse_impl",
    "input",
    "options",
    "parse",
    "notes",
    "stale",
    "card",
    "repo",
)

# 阶段名 → 终端里的说法（`重画（<说法>）：<文件名>`）
STAGE_LABELS = {
    "raster_impl": "绘制实现变了",
    "parse_impl": "读取实现变了",
    "input": "输入变了",
    "options": "绘制参数变了",
    "parse": "解析结果变了",
    "notes": "注记变了",
    "stale": "疑似过时结果变了",
    "card": "问题数据变了",
    "repo": "仓库状态变了（只影响「疑似过时」标记）",
}

# 决定"数据步是否全同"的阶段：实现步与 `repo` 不在里面。
# `repo` 只影响「疑似过时」标记，而标记已经落在 `stale` 里——把它算进数据步会让"只提交了代码"
# 也整批重画。实现步同理：它们只说明"代码变了"，变没变影响要看解析出来的数据。
DATA_STAGES = ("input", "options", "parse", "notes", "stale", "card")

# 渲染过程中才填好的选项字段：进 sha 会让"解析前先比一遍"永远不等（那时它们还是空值）
_DERIVED_OPTION_FIELDS = frozenset(
    {"vision", "on_error", "notes", "stale", "log", "truncated_start"}
)


def canonical(value: Any) -> Any:
    """把对象转成可稳定序列化的形态：dataclass 按字段名、时间转 ISO、集合转有序列表。

    稳定是关键：同一个对象图两次调用必须得到同一个 JSON 字节串，否则 sha 会假变。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # 用 repr：0.1 与 0.10000000000000001 在 == 下相等，但输出文本不同，取 repr 才稳定
        return repr(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: canonical(item) for key, item in sorted(value.items())}
        # 非字符串键（`ParsedLog.nextlist_stats` 是 `(task_id, 节点名)`）：转成有序的键值对
        return sorted(([canonical(key), canonical(item)] for key, item in value.items()), key=repr)
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonical(item) for item in value), key=repr)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: canonical(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "__dict__"):
        return {key: canonical(item) for key, item in sorted(vars(value).items())}
    return repr(value)


def stage_digest(value: Any) -> str:
    """一段数据的 sha256 短摘要（16 位十六进制）。"""
    payload = json.dumps(
        canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def options_payload(options: Any) -> dict[str, Any]:
    """`FlowOptions` → `options` 阶段的载荷：只取入参，去掉渲染时填的派生字段。"""
    return {
        key: value
        for key, value in vars(options).items()
        if key not in _DERIVED_OPTION_FIELDS
    }


def notes_payload(notes: Iterable[Any]) -> list[list[str]]:
    """注记列表 → `notes` 阶段的载荷（文本 + 颜色分类）。"""
    return [[note.text, note.kind] for note in notes]


def parsed_payload(parsed: Any) -> Any:
    """`parse` 阶段的载荷：`override_nodes` 为空时**不进摘要**。

    加一个"只有特定输入才会非空"的字段（这里是运行期 `override_pipeline` 建出来的节点名）
    不该让所有旧图的摘要一起变——`canonical()` 会把它当成空集写进载荷，等于整批白重画。
    空集本来也不带信息：渲染结果与没有这个字段时完全一样。
    """
    payload = canonical(parsed)
    if isinstance(payload, dict) and not payload.get("override_nodes"):
        payload.pop("override_nodes", None)
    return payload


def stale_payload(check: Any) -> Any:
    """`stale` 阶段的载荷：`synthesized` 为空时不进摘要（理由同 `parsed_payload`）。"""
    payload = canonical(check)
    if isinstance(payload, dict) and not payload.get("synthesized"):
        payload.pop("synthesized", None)
    return payload


def trace_of(stages: dict[str, str]) -> str:
    """整条轨迹的 sha：把各阶段摘要按固定顺序再取一次。"""
    return stage_digest({name: stages.get(name, "") for name in STAGE_ORDER})


def same_data(recorded: dict[str, str], current: dict[str, str]) -> bool:
    """数据步是否全同（缺失当空：概览卡没有 `parse`/`notes`/`stale`，两边都没有就相等）。"""
    return all(recorded.get(name) == current.get(name) for name in DATA_STAGES)


def reason_of(recorded: dict[str, str], current: dict[str, str]) -> str:
    """第一个不同的阶段 → 终端里的说法；全同返回空串。"""
    if not recorded:
        return "没有旧记录"
    for name in STAGE_ORDER:
        if name not in recorded:
            continue
        if recorded[name] != current.get(name):
            return STAGE_LABELS.get(name, f"{name} 变了")
    # 旧记录里没有的阶段（例如升级前只记了文件名）：当成"记录不全"，如实说清楚
    if any(name not in recorded for name in current):
        return "旧记录没有分步摘要"
    return ""
