import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必须在 import maa 之前：maa 在导入时读 MAAFW_BINARY_PATH 把原生库目录定死
# （maa/__init__.py:6-9）。发行包里指向客户端那份 runtimes/<os>-<arch>/native，
# 开发态找不到就不设变量、继续用 wheel 自带的 site-packages/maa/bin。
import maafw_paths

maafw_paths.ensure_maafw_binary_path()

from maa.agent.agent_server import AgentServer
from maa.tasker import Tasker

from base import pi_check_maafw_version, pi_log_snapshot
import battle_training
import factory_fast
import factory
import startup
import store
import train


def main():
    # 日志目录固定成 <安装根>/debug/agent，不要用 "./"（= 安装根）：设置 log_dir 会让框架在
    # 后台线程里删掉该目录下 mtime 超过 7 天的 png/jpg/log，而安装根里有 resource/image/** 的
    # 识别模板。用绝对路径，不受 CWD 影响（M9A 也是把日志下沉到 ./debug/agent）。
    log_dir = Path(__file__).resolve().parents[1] / "debug" / "agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(log_dir)
    pi_log_snapshot()
    pi_check_maafw_version()

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        # 不走解释器关闭流程：libzmq 的 signaler 线程在 Windows 上会 assert 并挂住进程。
        os._exit(1)

    socket_id = sys.argv[-1]

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
