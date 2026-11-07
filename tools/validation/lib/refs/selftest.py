"""--self-test 的入口：按顺序跑前两段用例、打印结果并给退出码。
不读仓库文件；用例细节在 selftest_pipeline / selftest_misc。
"""

from __future__ import annotations


from .selftest_misc import _checks_rest
from .selftest_pipeline import _checks_core

def self_test() -> int:
    """跑全部合成用例，返回 0/1；只打印失败项与总条数。"""
    checks = _checks_core()
    _checks_rest(checks)
    bad = [name for ok, name in checks if not ok]
    if bad:
        for name in bad:
            print(f"self-test FAIL: {name}")
        return 1
    print(f"self-test ok（{len(checks)} 项）")
    return 0
