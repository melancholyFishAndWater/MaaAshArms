"""按资源版本筛选遥测 issue（`cases --telemetry-version`）。

条件写法：`[运算符]版本`，运算符缺省是 `==`，也认 `>=` / `<=` / `>` / `<` / `!=`。
版本写法：`v1.0.0-beta.61` / `1.0.0-beta.61` / `v1.0.0`，也接受整串 release
（`MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61`）。两边都归一到同一套排序键再比：
`(major, minor, patch, 预发布档位, 预发布序号)`，档位是 alpha < beta < rc < 正式版。
这套键沿用 `tools/sentry/lib/report_common.py` 的 `release_version_key()`，不另造一套排序。

于是 `>=v1.0.0-beta.61` 同时命中 beta.62、rc.1 与 v1.0.0；`<=v1.0.0-beta.55` 只命中 beta.55 及更早。
issue 的 `release` 解析不出本项目版本时**任何条件都不命中**：版本未知不能算落进范围。

多个条件按「且」叠加：`--telemetry-version ">=v1.0.0-beta.55" --telemetry-version "<=v1.0.0-beta.60"`
就是只取这两版之间。

`partition()` 是给调用方的分堆助手：命中的留下，剩下两堆分开计数——「release 解析不出」与
「版本不在范围内」的处置不同（前者是数据缺字段，后者才是真的被筛掉），日志里要分开说。
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # 只给类型用：运行期走 `_sentry_release()` 延迟 import，见那里的说明
    from tools.sentry.lib import report_common

VersionKey = tuple[int, int, int, int, int]

# 运算符：长的写在前面，`>=` 不会被拆成 `>` + `=v1.0.0`
_OPS: dict[str, Callable[[VersionKey, VersionKey], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}
_SPEC_RE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(\S.*)$")
# 裸版本号（`v` 可省）。release 串不在这里解析，交给报告侧的 release_pattern
_BARE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$", re.IGNORECASE)
_SPEC_HELP = "写成 >=v1.0.0-beta.61 / <=1.0.0 / ==v1.0.0-rc.1（运算符可省，默认 ==）"


class _Card(Protocol):
    """`partition()` 只用到卡片这两个字段（`telemetry_index.IssueCard` 结构上满足）。

    声明成只读属性：`IssueCard` 是 frozen dataclass，可写属性的 Protocol 会和它不兼容。
    """

    @property
    def short_id(self) -> str: ...

    @property
    def release(self) -> str: ...


def _sentry_release() -> "report_common":
    """延迟 import 报告侧的 release 解析。

    画图工具的其余子命令不碰 `tools.sentry`（它会读 `tools/sentry/config.json`，
    而且 `telemetry_index` → `evidence` 在模块级 import 了画图模块，模块级 import 会成环）。
    """
    from tools.sentry.lib import report_common

    return report_common


def parse_version(text: str) -> tuple[VersionKey, str] | None:
    """版本号或整串 release → `(排序键, vX.Y.Z[-pre.N] 标签)`；解析不出返回 None。

    整串 release 走报告侧的 `release_pattern`（双 `@` 拼接时取**最后一个**匹配，
    否则 `MFA@v2.16.1+MaaAshArms@v1.0.0-beta.61` 会解析成客户端版本 2.16.1）。
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    rules = _sentry_release()
    label = rules.version_label(value)
    key = rules.release_version_key(value)
    if label is not None and key is not None:
        return key, label

    match = _BARE_RE.match(value)
    if match is None:
        return None
    major, minor, patch, prerelease, number = match.groups()
    if prerelease is None or number is None:
        return (
            (int(major), int(minor), int(patch), rules.STABLE_RELEASE_RANK, 0),
            f"v{major}.{minor}.{patch}",
        )
    rank = rules.PRERELEASE_RANKS.get(prerelease.lower())
    if rank is None:
        return None
    return (
        (int(major), int(minor), int(patch), rank, int(number)),
        f"v{major}.{minor}.{patch}-{prerelease.lower()}.{number}",
    )


def label_of(text: str) -> str:
    """日志里显示的版本标签：解析不出就原样返回，空值写成「（无 release）」。"""
    parsed = parse_version(text)
    if parsed is not None:
        return parsed[1]
    return (text or "").strip() or "（无 release）"


@dataclass(frozen=True)
class Condition:
    """一条版本条件：`>=v1.0.0-beta.61` 拆成运算符 + 归一后的版本。"""

    op: str
    label: str
    key: VersionKey

    def matches(self, key: VersionKey) -> bool:
        return bool(_OPS[self.op](key, self.key))


@dataclass(frozen=True)
class VersionFilter:
    """一组版本条件，按「且」叠加；`--telemetry-version` 的解析结果。"""

    conditions: tuple[Condition, ...]

    @classmethod
    def parse(cls, specs: Sequence[str]) -> VersionFilter:
        """解析 `--telemetry-version` 的原始取值；看不懂就抛 `ValueError`（带写法说明）。

        看不懂的取值**不能静默当成空筛选**：那样会把"条件没生效"当成"库里没有合条件的 issue"。
        """
        conditions: list[Condition] = []
        for raw in specs:
            text = (raw or "").strip()
            match = _SPEC_RE.match(text) if text else None
            if match is None:
                raise ValueError(f"版本条件「{raw}」看不懂：{_SPEC_HELP}")
            op = match.group(1) or "=="
            parsed = parse_version(match.group(2))
            if parsed is None:
                raise ValueError(f"版本条件「{raw}」里的版本号看不懂：{_SPEC_HELP}")
            key, label = parsed
            conditions.append(Condition(op=op, label=label, key=key))
        if not conditions:
            raise ValueError(f"版本条件不能为空：{_SPEC_HELP}")
        return cls(tuple(conditions))

    def matches(self, release: str) -> bool:
        """这条 release（或版本号）是否命中全部条件；解析不出本项目版本的一律不命中。"""
        parsed = parse_version(release)
        if parsed is None:
            return False
        key = parsed[0]
        return all(condition.matches(key) for condition in self.conditions)

    def describe(self) -> str:
        """给人看的条件（`>=v1.0.0-beta.55 且 <=v1.0.0-beta.60`），日志里直接打这串。"""
        return " 且 ".join(f"{c.op}{c.label}" for c in self.conditions)

    def partition(
        self, cards: Sequence[_Card]
    ) -> tuple[list[_Card], list[str], list[str]]:
        """按版本把卡片分堆：`(命中的, release 解析不出的问题号, 版本不在范围内的问题号)`。"""
        kept: list[_Card] = []
        unknown: list[str] = []
        outside: list[str] = []
        for card in cards:
            parsed = parse_version(card.release)
            if parsed is None:
                unknown.append(card.short_id)
            elif all(condition.matches(parsed[0]) for condition in self.conditions):
                kept.append(card)
            else:
                outside.append(card.short_id)
        return kept, unknown, outside
