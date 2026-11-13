# 使用问题记录（tests）

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里）。`tests/` 没有 README，「决定不做」的取舍写进 `tests/conftest.py` 的说明。

- `uv run --locked python -m pytest -q tests`：33 passed，但写 `.pytest_cache` 时出现 `PytestCacheWarning: [WinError 5]`；测试断言全部通过，缓存权限问题尚未处理。
- `.venv/Scripts/python.exe -m pytest -q tests/test_agent_launch.py`：受限运行 16 passed、2 个 `tmp_path` fixture 因系统临时目录 WinError 5 未执行；宽权限复测 18 passed，全量复测 56 passed，但退出阶段 ZeroMQ `signaler.cpp:120` 断言使进程 exit 1（与现有 maafw 退出问题一致，未修改框架）。
