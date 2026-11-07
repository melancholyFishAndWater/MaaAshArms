#!/usr/bin/env python3
"""入口：实现分在 tools/validation/refs/ 下的八个模块。

  * model            数据结构与通用小工具（Ctx / Node / parse_ref / walk_key …）
  * jsonc            JSONC 读取（去注释与末尾逗号）
  * loaders          读 pipeline / tasks / agent
  * rules_pipeline_a pipeline 规则 A1–A18
  * rules_pipeline_b pipeline 规则 A19–A30
  * rules_misc       B / C / D / E 域（interface / tasks / image / agent / 规范版本）
  * report           命令行、按域跑规则、输出与退出码
  * selftest         --self-test

用法与判据不变；说明见 tools/validation/README.md 与 refs/__init__.py。
"""

from __future__ import annotations

from .refs import main

if __name__ == "__main__":
    raise SystemExit(main())
