# ruff: noqa: F401 —— 下面这批 import 是**注册入口**（装饰器在 import 时执行），
# 名字一个都不在本文件里用；删掉任何一个都会让对应模块的自定义识别/动作静默消失。
import os
import sys
from pathlib import Path

# 兄弟式 import：agent/ 与 agent/shared/ 都进 sys.path，两个目录下的模块都按顶层名导入
# （`from rect import ...`）。**只加 agent/ 不够**：shared/ 下的模块不在 agent/ 目录里。
# 反过来不要把 shared/ 做成带 __init__.py 的包 —— 那样 `rect` 与 `shared.rect` 会各成一份模块对象，
# agentlog.log 的单例、generic 的 _times_by_name / _move_entry 都会被复制成两份。
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_AGENT_DIR, "shared"))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# 必须在 import maa 之前：maa 在导入时读 MAAFW_BINARY_PATH 把原生库目录定死
# （maa/__init__.py:6-9）。发行包里指向客户端那份 runtimes/<os>-<arch>/native，
# 开发态找不到就不设变量、继续用 wheel 自带的 site-packages/maa/bin。
# 由此 agent/shared/*.py 一律不得 import maafw_paths：那会提前把 maa 拉进来，本行就白设了。
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
import parent_tracker
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
