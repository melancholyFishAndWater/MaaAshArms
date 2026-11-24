import factory
from typing import Any
from numpy import ndarray, dtype

from maa.agent.agent_server import AgentServer
from maa.pipeline import JTemplateMatch, JOCR
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import Context, JRecognitionType, Rect
from rect import DEFAULT_HIT_BOX


from agentlog import log

# 目标领取次数
target_reward_times: int = 1

# ---------- FastStartOrgTask ----------


# 领取成功一次 返回box
@AgentServer.custom_recognition("FastStartOrgTaskReco")
class FastStartOrgTaskReco(CustomRecognition):

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        if context.get_hit_count("FactoryGetRewardSuccess") >= target_reward_times:
            return DEFAULT_HIT_BOX


# 重新执行原始任务
@AgentServer.custom_action("FastStartOrgTaskAct")
class FastStartOrgTaskAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        d = context.run_task(argv.task_detail.entry)
        return d is not None


# ---------- reco ----------


# 领完角色则返回 box
@AgentServer.custom_recognition("FastProbeCreateOrderReco")
class FastProbeCreateOrderReco(CustomRecognition):
    @staticmethod
    def clear(context: Context, argv: CustomRecognition.AnalyzeArg):
        """
        清空 FactoryGetRewardSuccess 命中次数

        防止干扰建造
        """
        b = context.clear_hit_count("FactoryGetRewardSuccess")
        if not b:
            log.warn(f"{argv.node_name} 清空 FactoryGetRewardSuccess 命中次数失败")

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 领取三次 直接下一步
        if context.get_hit_count("FactoryGetRewardSuccess") >= 3:
            self.clear(context, argv)
            return DEFAULT_HIT_BOX
        # 滑动超过5次 条件下一步
        elif context.get_hit_count("BaseMoveUp") >= 5:
            d = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(["创建新的订单"], roi=(526, 468, 283, 97)),
                image=argv.image,
            )
            if d and d.hit:
                self.clear(context, argv)
                return DEFAULT_HIT_BOX


# ---------- action ----------


# 初始化覆写
@AgentServer.custom_action("TaskFastFactoryAct")
class TaskFastFactoryAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        log.debug(f"{argv.node_name} 执行初始化")
        return context.override_pipeline(
            {
                "FactoryClickSkip": {"enabled": True},
                "FactoryChooseNumber": {"index": 9},  # 一次造10个(0~9)
                "FactoryClickCreateOrder": {
                    "roi": [560, 347, 255, 246]  # 调整roi 防止点到其他创建
                },
            }
        )
