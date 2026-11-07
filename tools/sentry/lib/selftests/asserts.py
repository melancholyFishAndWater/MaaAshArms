"""断言与合成工件助手：`_eq` / `_contains` / `_raises` / 探针工件。

各用例模块共用；失败一律抛 `CheckFailure`，由 cli 汇总成退出码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .deps import CONFIG, Artifact, Measurement, load_artifact, save_artifact
from .fixtures import ARTIFACT_FIXTURE_TIME, MEASUREMENTS


def build_probe_artifact() -> Artifact:
    """合成一份覆盖全部 measurement 的工件，供往返与渲染断言使用。"""
    return Artifact(
        target=CONFIG.target,
        period="7d",
        measurements={
            name: Measurement(
                name=name,
                rows=rows,
                fields=(
                    "span.description",
                    "span.status",
                    "release",
                    "count_unique(trace)",
                ),
            )
            for name, rows in MEASUREMENTS.items()
        },
        generated_at=ARTIFACT_FIXTURE_TIME,
    )


def save_probe_artifact(path: Path) -> Artifact:
    """把合成工件写盘并读回，返回读回后的对象。"""
    save_artifact(build_probe_artifact(), path)
    return load_artifact(path)


class CheckFailure(Exception):
    """一条断言不成立。"""


def _eq(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise CheckFailure(f"{what}: 期望 {expected!r}，实际 {actual!r}")


def _contains(haystack: str, needle: str, what: str) -> None:
    if needle not in haystack:
        raise CheckFailure(f"{what}: 输出里找不到 {needle!r}")


def _note_text(notes: Any) -> str:
    """注记是 `Note(text, kind)`（图上按 kind 上色），断言只看文本。"""
    return " ".join(n.text for n in notes)


def _raises(action: Callable[[], Any], error: type[Exception], what: str) -> None:
    """断言动作抛出指定异常类型（入参错误这类路径要能测到）。

    这里必须捕任意 `Exception` 再报成断言失败：目的是"把意外的异常类型也讲清楚"，
    而不是让自检自己崩掉。抑制与 `except` 写在同一行——多行 `except (` 会被 ruff
    判成"抑制未使用"并连带报 S110/BLE001（见 tools/validation/README 的假红那节）。
    """
    try:
        action()
    except error:
        return
    except Exception as unexpected:  # noqa: BLE001
        raise CheckFailure(
            f"{what}: 期望 {error.__name__}，实际 {type(unexpected).__name__}"
        ) from None
    raise CheckFailure(f"{what}: 期望抛 {error.__name__}，但没有抛")



__all__ = [
    "build_probe_artifact",
    "save_probe_artifact",
    "CheckFailure",
    "_eq",
    "_contains",
    "_note_text",
    "_raises",
]
