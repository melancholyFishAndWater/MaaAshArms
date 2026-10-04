"""
存储一日一次的任务信息的模块
"""

import json
import os
import tempfile
from pathlib import Path
from datetime import timedelta, datetime

from typing import Callable, Generic

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import Context

from base import DEFAULT_HIT_BOX, log, T, parse_params
from factory import build_times
import battle_training


class Store(Generic[T]):
    def __init__(
        self,
        state: str = "store.json",
        default: T = False,
        func: Callable[[T], bool] = lambda x: bool(x),
    ):
        """
        存储实例

        Args:
            state (str, optional): 存储文件名. Defaults to "store.json".
            default (T, optional): 读取数据默认值. Defaults to False.
            func (Callable[[T], bool], optional): 判断函数. Defaults to lambda x: bool(x).
        """
        # 任务名: 数据
        self._data: dict[str, T] = {}
        self._state = state
        self._today = self._today_str()
        self._default = default
        self._func = func
        self._init_data()

    @staticmethod
    def _today_str() -> str:
        # 凌晨 5 点跨天
        return (datetime.now() - timedelta(hours=5)).date().isoformat()

    def _init_data(self):
        """
        初始化数据
        """
        path = Path(__file__).resolve().parents[1] / "debug" / self._state
        try:
            # 读取原始数据
            dict_ = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )

            # 提取今日数据，抛弃其他历史数据
            self._data = dict_.get(self._today, {})
        except Exception as e:
            self._data = {}
            log.warn(f"读取数据失败: {path} ({e})")

    def _save(self):
        """
        持久化数据（原子写）

        先写临时文件，再 os.replace 原子替换；
        失败时清理临时文件，但不掩盖原始异常。
        """
        path = Path(__file__).resolve().parents[1] / "debug" / self._state
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps({self._today: self._data})
        size = len(payload.encode("utf-8"))

        log.debug(
            f"开始保存数据: target={path}, state={self._state}, today={self._today}"
        )

        # 1) 创建临时文件。此阶段失败说明文件可能根本没建出来
        try:
            f = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
                delete=False,
            )
        except Exception as e:
            log.error(
                f"创建临时文件失败: dir={path.parent}, prefix={path.name}, err={e!r}"
            )
            raise

        tmp_path = f.name

        # 2) 写入临时文件
        try:
            with f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            log.error(f"写入临时文件失败: tmp={tmp_path}, err={e!r}")
            self._cleanup_tmp(tmp_path)
            raise

        log.debug(f"临时文件写入完成: tmp={tmp_path}, bytes={size}")

        # 3) 原子替换。失败时临时文件仍完整存在
        try:
            os.replace(tmp_path, path)
        except Exception as e:
            log.error(f"原子替换失败: tmp={tmp_path}, target={path}, err={e!r}")
            self._cleanup_tmp(tmp_path)
            raise

        log.debug(f"保存成功: target={path}, bytes={size}")

    def _cleanup_tmp(self, tmp_path: str):
        """尽力删除临时文件，失败只警告，不影响调用方抛出的原始异常。"""
        try:
            os.unlink(tmp_path)
        except OSError as e:
            log.warn(f"清理临时文件失败: tmp={tmp_path}, err={e!r}")
        else:
            log.debug(f"已清理临时文件: tmp={tmp_path}")

    def _ensure_today(self):
        """
        跨天则清空
        """
        today = self._today_str()
        if today != self._today:
            self._today = today
            self._data = {}
            self._save()

    def get(self, key) -> T:
        self._ensure_today()
        return self._data.get(key, self._default)

    def set(self, key, value: T):
        self._ensure_today()
        self._data[key] = value
        self._save()

    # TODO
    # def delete(self, key):
    #     self._ensure_today()
    #     if key in self._data:
    #         del self._data[key]
    #         self._save()

    def reco(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 校验日期
        self._ensure_today()

        # 提取变量
        p = (
            parse_params(argv.custom_recognition_param)
            if argv.custom_recognition_param
            else {}
        )
        # 是否启用每日一次
        bool_ = bool(p.get("enable", True))

        # 判断是否跳过此次任务
        if bool_ and self._func(self._data.get(argv.task_detail.entry, self._default)):
            s = "今日已执行过，跳过"
            log.info(s)
            b = context.run_action(
                "TaskStop",
                pipeline_override={"TaskStop": {"focus": {"Node.Action.Succeeded": s}}},
            )
            if b is None or not b.success:
                log.warn(f"{argv.node_name} 执行 TaskStop 失败")
            return None
        return DEFAULT_HIT_BOX

    def act(self, argv: CustomAction.RunArg, value: T) -> CustomAction.RunResult | bool:
        try:
            self.set(argv.task_detail.entry, value)
            log.debug(f"{argv.node_name} 更新每日一次数据为 {value}")
        except Exception as e:
            log.error(f"更新每日一次数据失败: {e}")
        return True


# ----- 每日赠礼 -----

gift = Store[bool]("daily_gift.json")


# 若为今日第一次或若未开启每日一次，则返回 BOX；
@AgentServer.custom_recognition("TaskGiftReco")
class TaskGiftReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return gift.reco(context, argv)


# 更新每日一次数据
@AgentServer.custom_action("GiftDailyUpdateAct")
class GiftDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return gift.act(argv, True)


# ----- 订单工厂 -----

factory = Store[int]("daily_factory.json", default=0, func=lambda x: x >= 3)


@AgentServer.custom_recognition("TaskFactoryReco")
class TaskFactoryReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return factory.reco(context, argv)


@AgentServer.custom_action("FactoryDailyUpdateAct")
class FactoryDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return factory.act(argv, factory.get(argv.task_detail.entry) + build_times)


# ----- 训练所 -----


def _training_func(x: list[str]) -> bool:
    return len(x) > 0 and all(i in x for i in battle_training.battle_targets.keys())


training = Store[list[str]]("daily_training.json", default=[], func=_training_func)


@AgentServer.custom_recognition("TaskTrainingReco")
class TaskTrainingReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return training.reco(context, argv)


@AgentServer.custom_action("TrainingDailyUpdateAct")
class TrainingDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        try:
            # 提取参数
            p = parse_params(argv.custom_action_param, "name")
            name = p.get("name")
            if type(name) != str:
                log.error(f"{argv.node_name} 错误的关卡名参数: {name}")
                return False

            return training.act(
                argv, sorted(set(training.get(argv.task_detail.entry)) | {name})
            )
        except Exception as e:
            log.error(f"{argv.node_name} 参数解析失败: {e}")
            return False
