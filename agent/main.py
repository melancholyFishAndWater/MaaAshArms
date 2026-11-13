# ruff: noqa: F401
# 业务模块通过导入执行装饰器注册；未直接引用不代表可以删除。
import io
import os
import sys
from pathlib import Path

# Client 按 UTF-8 读取 agent 日志；uv 不会自动替 Python 设置输出编码。
for _stream in (sys.stdout, sys.stderr):
    if isinstance(_stream, io.TextIOWrapper):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 两个目录都按顶层模块导入，避免 agentlog、store 等状态因混用包名而重复实例化。
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_AGENT_DIR, "shared"))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from runtime_check import ensure_runtime_ready

# 在加载 maa 前报告环境权限问题，避免进入原生库后才触发 socket 断言。
ensure_runtime_ready()

# maa 导入时确定原生库路径：发布态复用客户端库，开发态回退到 wheel 自带库。
# 必须在导入任何依赖 maa 的业务模块之前设置。
import maafw_paths

maafw_paths.ensure_maafw_binary_path()

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

from pienv import pi_check_maafw_version, pi_log_snapshot
import battle_replays
import battle_training
import entrust
import factory_fast
import factory
import generic
import maintenance
import parent_tracker
import startup
import store
import train


def main():
    # 用入口位置定位独立日志目录，不依赖 CWD。
    # 框架会清理 log_dir 下的过期日志和图片，不可指向含资源模板的项目根。
    log_dir = Path(__file__).resolve().parents[1] / "debug" / "agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(log_dir)
    pi_log_snapshot()
    pi_check_maafw_version()

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        # 避免 Windows 原生库在解释器清理阶段触发已知 ZeroMQ 断言。
        os._exit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
