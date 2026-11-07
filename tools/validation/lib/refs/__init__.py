"""跨文件一致性校验的实现包；入口仍是 tools/validation/lib/validate_refs.py。

模块分工：
  * model            数据结构与通用小工具
  * jsonc            JSONC 读取
  * loaders          读 pipeline / tasks / agent
  * rules_pipeline_a A1–A18
  * rules_pipeline_b A19–A30
  * rules_misc       B / C / D / E 域
  * report           命令行与输出
  * selftest         --self-test
"""

from __future__ import annotations

from .report import main

__all__ = ["main"]
