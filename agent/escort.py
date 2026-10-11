from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
from base import log


# 清空大循环 EscortChooseCharacter - EscortClickOk 的 max_hit
@AgentServer.custom_action("EscortInChooseAct")
class EscortInChooseAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        log.debug(f"{argv.node_name} 清空 EscortChooseCharacter 的 hit")
        return context.clear_hit_count("EscortChooseCharacter")
