from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from base import log, parse_params


@AgentServer.custom_action("StartUpFailedAct")
class StartUpFailedAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 提取变量
        p = parse_params(argv.custom_action_param)
        node_name = p.get("node_name", "StartUpAshArms")

        # 覆写：关掉启动节点，本次任务内不再重试启动
        try:
            if not context.override_pipeline({node_name: {"enabled": False}}):
                log.warn(f"关闭节点失败：{node_name}")
        except Exception as e:
            log.warn(f"关闭节点异常：{node_name} ({e!r})")

        log.error("启动游戏失败，请检查是否正确选择游戏资源")
        return True
