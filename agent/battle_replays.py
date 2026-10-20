from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import Context

from agentlog import log
from params import parse_params
from rect import DEFAULT_HIT_BOX

# 自律完成次数
_finish_count = 0
# 最大完成次数
_max_count = 5


# ---------- Reco ----------


# 判断是否满足结束次数
@AgentServer.custom_recognition("ReplaysStopTimesReco")
class ReplaysStopTimesReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if _finish_count >= _max_count:
            log.info(f"代理结束，已完成次数：{context.get_hit_count(argv.node_name)}")
            return DEFAULT_HIT_BOX


# ---------- Act ----------


# 初始化全局变量
@AgentServer.custom_action("ReplaysInitAct")
class ReplaysInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count, _max_count
        _finish_count = 0
        _max_count = 5

        try:
            p = parse_params(argv.custom_action_param, "times")
            _max_count = p["times"]
            log.debug(f"{argv.node_name} 设置 _max_count为{_max_count}")
        except Exception as e:
            log.warn(f"{argv.node_name} 解析param失败: {e}")

        log.info(f"代理次数: {_max_count}")

        return True


# 完成数自增
@AgentServer.custom_action("ReplaysLoopDefaultEndAct")
class ReplaysLoopDefaultEndAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global _finish_count
        _finish_count += 1
        log.info(f"第 {_finish_count} 次代理作战结束")
        return True
