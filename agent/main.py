import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

import base
import battle_training
import daily
import factory_fast
import factory
import train


def main():
    Toolkit.init_option("./")

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
