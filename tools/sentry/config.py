"""Sentry 报告项目配置。

统一从同级目录下的 config.json 读取；未配置或缺失必填项时直接抛出异常，
不做隐式硬编码回退（照 M9A `tools/sentry/config.py` 的做法，便于换项目时只改 JSON）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectConfig:
    # Sentry 目标仓库 (<org>/<project>)
    target: str
    # 项目 Release 标识前缀 (例如 "MaaAshArms")
    project_prefix: str
    # 可选: 自定义 Release 正则表达式（未指定时按 project_prefix 自动构造）
    release_pattern: str | None = None
    # 各渠道上报运行总量的 umbrella span 名称
    task_run_spans: tuple[str, ...] = ()
    # 任务列表中忽略的私有 span 前缀
    ignored_prefixes: tuple[str, ...] = ()
    # 任务列表中忽略的私有 span 后缀
    ignored_suffixes: tuple[str, ...] = ()
    # 按描述名忽略的 span：MFA 框架内部的"伪任务"（界面不可选、没有任务失败语义）
    ignored_descriptions: tuple[str, ...] = ()
    # 系统级 span 描述 → 可读标签；这些名字只在失败时上报，本来就该单列
    system_labels: dict[str, str] = field(default_factory=dict)
    # 认定一个版本「有足够用户」所需的最低独立用户数
    min_release_unique_users: int = 3
    # 展示失败率所需的最低样本数（低于此值标为样本不足）
    min_samples: int = 20


def _parse_str_list(value: Any, field_name: str, config_file: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"配置文件 {config_file} 中的 '{field_name}' 必须是字符串列表。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"配置文件 {config_file} 中的 '{field_name}' 列表项必须是字符串，发现: {type(item).__name__}"
            )
        result.append(item)
    return tuple(result)


def _parse_positive_int(value: Any, field_name: str, config_file: Path, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"配置文件 {config_file} 中的 '{field_name}' 必须是大于 0 的整数。")
    return value


def load_config(config_path: Path | str | None = None) -> ProjectConfig:
    """加载配置：从 config.json 读取；不存在或缺少必填字段时直接抛出异常。"""
    config_file = Path(config_path) if config_path else Path(__file__).parent / "config.json"
    if not config_file.is_file():
        raise FileNotFoundError(
            f"未找到 Sentry 配置文件: {config_file}。请在 tools/sentry/ 目录下提供 config.json。"
        )

    with open(config_file, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {config_file} 内容格式错误，必须为 JSON 对象。")

    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ValueError(f"配置文件 {config_file} 缺少必填字段 'target' (例如 <org>/<project>)。")

    project_prefix = data.get("project_prefix")
    if not isinstance(project_prefix, str) or not project_prefix.strip():
        raise ValueError(f"配置文件 {config_file} 缺少必填字段 'project_prefix'。")

    release_pattern = data.get("release_pattern")
    if release_pattern is not None:
        if not isinstance(release_pattern, str) or not release_pattern.strip():
            raise ValueError(f"配置文件 {config_file} 中的 'release_pattern' 必须是非空字符串。")
        try:
            compiled = re.compile(release_pattern)
        except re.error as err:
            raise ValueError(f"配置文件 {config_file} 中的 'release_pattern' 正则表达式无效: {err}") from err
        if compiled.groups != 5:
            raise ValueError(
                f"配置文件 {config_file} 中的 'release_pattern' 必须包含恰好 5 个捕获组 "
                f"(major, minor, patch, prerelease, prerelease_number)，当前包含 {compiled.groups} 个。"
            )

    raw_labels = data.get("system_labels")
    if raw_labels is None:
        raw_labels = {}
    if not isinstance(raw_labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_labels.items()
    ):
        raise ValueError(f"配置文件 {config_file} 中的 'system_labels' 必须是「字符串 → 字符串」的对象。")

    return ProjectConfig(
        target=target.strip(),
        project_prefix=project_prefix.strip(),
        release_pattern=release_pattern,
        task_run_spans=_parse_str_list(data.get("task_run_spans"), "task_run_spans", config_file),
        ignored_prefixes=_parse_str_list(data.get("ignored_prefixes"), "ignored_prefixes", config_file),
        ignored_suffixes=_parse_str_list(data.get("ignored_suffixes"), "ignored_suffixes", config_file),
        ignored_descriptions=_parse_str_list(
            data.get("ignored_descriptions"), "ignored_descriptions", config_file
        ),
        system_labels=dict(raw_labels),
        min_release_unique_users=_parse_positive_int(
            data.get("min_release_unique_users"), "min_release_unique_users", config_file, 3
        ),
        min_samples=_parse_positive_int(data.get("min_samples"), "min_samples", config_file, 20),
    )


CONFIG = load_config()
