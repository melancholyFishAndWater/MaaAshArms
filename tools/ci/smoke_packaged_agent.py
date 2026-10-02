#!/usr/bin/env python3
"""冒烟：用包内解释器跑一次 agent 入口，确认它的 import 链能通。

为什么不写成 `python/<exe> -c "import maa"`：那只验证 maa 能导入，抓不到两类真实故障 ——
  * embeddable Python 的 ._pth 让解释器进 isolated 模式，脚本目录不进 sys.path，
    agent 的兄弟模块 import 全失败（Windows 专有）；
  * agent/train.py 里 `list[_Train]` 这种前置引用注解在 Python <= 3.13 会在 import 阶段 NameError。
只有跑 agent 入口本身才覆盖得到。

没有 socket id 时 main() 打印用法后 sys.exit(1)；libzmq 在解释器关闭阶段可能 assert 并挂住
（既存问题，与打包无关），所以这里带超时：只要用法打印出来就算通过，挂住的进程会被 kill。

用法：
    python tools/install.py v0.0.0-test win x86_64 --python-runtime .python-runtime
    python tools/ci/smoke_packaged_agent.py
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

# Windows runner 的控制台编码是 cp1252，print 中文会 UnicodeEncodeError。
# 只调 errors 不动 encoding：本机保持原编码、不乱码；CI 侧另有 job 级 PYTHONUTF8=1。
for _stream in (sys.stdout, sys.stderr):
    # reconfigure() 定义在 io.TextIOWrapper 上，而 sys.stdout 的静态类型是 typing.TextIO，
    # 直接调用会被 Pylance 报 reportAttributeAccessIssue；isinstance 收窄后静态与运行时都成立。
    if isinstance(_stream, io.TextIOWrapper):
        try:
            _stream.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass

EXPECTED = "Usage: python main.py"
TIMEOUT_SECONDS = 60


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    exe = (
        root
        / "install"
        / "python"
        / ("python.exe" if sys.platform == "win32" else "bin/python3")
    )
    agent = root / "install" / "agent" / "main.py"

    if not exe.is_file():
        sys.exit(
            f"找不到包内解释器：{exe}（先跑 tools/install.py --python-runtime <dir>）"
        )
    if not agent.is_file():
        sys.exit(f"找不到包内 agent：{agent}")

    # -u 很关键：不加它的话，agent 一旦在启动阶段崩溃或挂住，traceback / 用法输出会留在
    # 子进程的块缓冲里，随 kill 一起丢掉，日志里只剩一句"没有打印用法"，等于没有诊断信息。
    proc = subprocess.Popen(
        [str(exe), "-B", "-u", str(agent)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        output, _ = proc.communicate()

    state = (
        f"{TIMEOUT_SECONDS} 秒未退出已 kill"
        if timed_out
        else f"已退出 exit={proc.returncode}"
    )
    sys.stdout.write(output)
    if output and not output.endswith("\n"):
        print()
    print(f"[smoke] 子进程状态：{state}；捕获输出 {len(output)} 字符")

    if EXPECTED not in output:
        sys.exit("[smoke] agent 入口没有打印用法，判失败（import 链有问题，看不出原因就看上面的输出）")
    if "Traceback" in output:
        sys.exit("[smoke] agent 入口有 traceback，判失败")
    print(f"[smoke] agent 入口 OK（imports 通过；进程{state}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
