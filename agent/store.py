"""
持久化小变量的模块

含每日一次模块
"""

from copy import deepcopy
import json
import os
import tempfile
from pathlib import Path
from datetime import timedelta, datetime
from typing import (
    Callable,
    Generic,
    Mapping,
    Optional,
    Sequence,
    TypeAlias,
    TypeVar,
    Union,
)

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.context import Context

from agentlog import log
from params import parse_params
from rect import DEFAULT_HIT_BOX

import factory
import battle_training


def _data_dir() -> Path:
    """状态文件目录，与框架的 log_dir 同一基准。

    抽成函数只为让测试能重定向；生产路径不变。
    """
    return Path(__file__).resolve().parents[1] / "debug"


# JSON 标量：null / bool / number / string
JsonScalar: TypeAlias = Union[None, bool, int, float, str]

# JSON 值：标量 | 数组 | 对象（递归）
JsonValue: TypeAlias = Union[
    JsonScalar,
    Sequence["JsonValue"],
    Mapping[str, "JsonValue"],
]

JSON_VAR = TypeVar("JSON_VAR", bound=JsonValue)


class Store(Generic[JSON_VAR]):
    def __init__(
        self,
        state: str,
        default: JSON_VAR,
    ) -> None:
        """
        持久化管理一个数据

        使用set函数以触发持久化, 高频和大量写入数据时慎用

        Args:
            state (str): 存储文件名.
            default (T): 默认值.
        """
        # 原始 default 对象
        self.__org_default = default
        # 存储文件名
        self.__state = state
        # 持久化的数据
        self.__value = self.__load_data()

    @property
    def __default(self) -> JSON_VAR:
        """
        使用深拷贝复制默认数据

        Returns:
            JsonScalar: 新的默认值对象
        """
        return deepcopy(self.__org_default)

    @property
    def value(self) -> JSON_VAR:
        return self.__value

    def __load_data(self) -> JSON_VAR:
        """
        加载数据
        """
        path = _data_dir() / self.__state
        try:
            data_ = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else self.__default
            )
            if not type(data_) == type(self.__org_default):
                raise ValueError(f"类型错误, type={type(data_)}")
            return data_
        except Exception as e:
            log.warn(f"读取数据失败: path={path} err={e!r}")
            return self.__default

    def __cleanup_tmp(self, tmp_path: str):
        """尽力删除临时文件，失败只警告，不影响调用方抛出的原始异常。"""
        try:
            os.unlink(tmp_path)
        except OSError as e:
            log.warn(f"清理临时文件失败: tmp={tmp_path}, err={e!r}")
        else:
            log.debug(f"已清理临时文件: tmp={tmp_path}")

    def __save(self):
        """
        持久化数据（原子写）

        先写临时文件，再原子替换；
        失败时会清理临时文件。

        Raises:
            Exception:
                - 创建临时文件失败
                - 写入临时文件失败
                - 原子替换失败
        """
        path = _data_dir() / self.__state
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = json.dumps(self.__value, allow_nan=False)
        size = len(payload.encode("utf-8"))

        log.debug(f"开始保存数据: target={path}, state={self.__state}")

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
            log.warn(
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
            log.warn(f"写入临时文件失败: tmp={tmp_path}, err={e!r}")
            self.__cleanup_tmp(tmp_path)
            raise

        log.debug(f"临时文件写入完成: tmp={tmp_path}, bytes={size}")

        # 3) 原子替换。失败时临时文件仍完整存在
        try:
            os.replace(tmp_path, path)
        except Exception as e:
            log.warn(f"原子替换失败: tmp={tmp_path}, target={path}, err={e!r}")
            self.__cleanup_tmp(tmp_path)
            raise

        log.debug(f"保存成功: target={path}, bytes={size}")

    def set(self, value: JSON_VAR):
        """
        设置数据并触发写入

        如果写入失败会弹出日志并复原内存中数据

        Args:
            value (JSON_VAR): Json可接受类型
        """
        temp = self.__value
        try:
            self.__value = value
            self.__save()
        except Exception as e:
            self.__value = temp
            log.warn(f"持久化数据失败 err={e!r}")


class Daily(Generic[JSON_VAR]):
    def __init__(
        self,
        state: str,
        default: JSON_VAR,
    ):
        """
        单个每日一次管理类

        对外主要功能分为两部分:

        ### reco:
            返回 box == 今日执行过，否则禁用此节点
            可通过 custom_recognition_param.enable = false 强制不返回（取消每日一次）

        ### act:
            将数据存入 __data 并持久化

        Args:
            state (str): 存储文件名.
            default (JSON_VAR): 默认值, 必须是 json 可接受的值.
        """
        # 原始默认值
        self.__org_default = default
        # 持久化的数据 dict[时间字符串, 数据]
        self.__data = Store[dict[str, JSON_VAR]](state, {self.__now: default})

    @property
    def __now(self) -> str:
        """
        返回当前时间的字符串
        """
        # 凌晨 5 点跨天
        return (datetime.now() - timedelta(hours=5)).date().isoformat()

    @property
    def __default(self) -> JSON_VAR:
        """
        使用深拷贝复制默认数据

        Returns:
            JSON_VAR: 新的默认值对象
        """

        return deepcopy(self.__org_default)

    @property
    def value(self) -> JSON_VAR:
        """
        返回今日的值

        当过了一天, 若没 set 过, 返回的值则为默认值

        Returns:
            JSON_VAR: 今日值
        """
        if self.__now in self.__data.value:
            return self.__data.value[self.__now]
        return self.__default

    def __set(self, value: JSON_VAR):
        """
        设置今日值

        Args:
            value (JSON_VAR): 今日值
        """
        self.__data.set({self.__now: value})

    def reco(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
        func: Callable[[JSON_VAR], bool] = bool,
    ) -> Optional[list[int]]:
        """
        若今日执行过，则返回 box

        通过 custom_recognition_param.enable = false 强制不返回

        Args:
            context (Context): 上下文
            argv (CustomRecognition.AnalyzeArg): reco的argv
            func (Callable[[JSON_VAR], bool], optional): 传入今日值, 判断今日是否执行过. Defaults to bool.

        Returns:
            Optional[list[int]]: 今日执行过标志
        """

        # 提取变量
        p = (
            parse_params(argv.custom_recognition_param)
            if argv.custom_recognition_param
            else {}
        )
        # 是否启用每日一次
        bool_ = bool(p.get("enable", True))

        # 判断是否跳过此次任务
        if bool_ and func(self.value):
            log.info("今日已执行过")
            return DEFAULT_HIT_BOX
        else:
            b = context.override_pipeline({argv.node_name: {"enabled": False}})
            if b is None or not b:
                log.debug(f"禁用节点 {argv.node_name} 失败")
            return None

    def act(self, argv: CustomAction.RunArg, value: JSON_VAR) -> bool:
        """
        更新并写入数据

        Args:
            argv (CustomAction.RunArg): act的argv
            value (JSON_VAR): 数据

        Returns:
            bool: 始终返回 True, 会捕获并打印错误日志
        """
        try:
            self.__set(value)
            log.debug(f"{argv.node_name} 更新每日一次数据为 {value}")
        except Exception as e:
            log.error(f"更新每日一次数据失败: {e}")
        return True


# ----- 每日赠礼 -----

_gift = Daily[bool]("daily_gift.json", False)


@AgentServer.custom_recognition("GiftStopDailyReco")
class GiftStopDailyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return _gift.reco(context, argv)


@AgentServer.custom_action("GiftDailyUpdateAct")
class GiftDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return _gift.act(argv, True)


# ----- 订单工厂 -----

_factory = Daily[list[int]]("daily_factory.json", default=[])


@AgentServer.custom_recognition("FactoryStopDailyReco")
class FactoryStopDailyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return _factory.reco(context, argv, func=lambda x: len(x) >= 3)


@AgentServer.custom_action("FactoryDailyUpdateAct")
class FactoryDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        set_ = set(_factory.value)
        set_.add(factory.build_times_now)
        return _factory.act(argv, list(set_))


# ----- 训练所 -----


def _training_func(x: list[str]) -> bool:
    return len(x) > 0 and all(i in x for i in battle_training.battle_targets.keys())


_training = Daily[list[str]]("daily_training.json", default=[])


@AgentServer.custom_recognition("TrainingStopDailyReco")
class TrainingStopDailyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return _training.reco(context, argv, func=_training_func)


@AgentServer.custom_action("TrainingDailyUpdateAct")
class TrainingDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        try:
            # 提取参数
            p = parse_params(argv.custom_action_param, "name")
            name = p.get("name")
            if type(name) != list:
                log.error(f"{argv.node_name} 错误的关卡名参数: {name}{(type(name))}")
                return False

            return _training.act(argv, sorted(set(_training.value) | {*name}))
        except Exception as e:
            log.error(f"{argv.node_name} 参数解析失败: {e}")
            return False


# ----- 委派 -----

_entrust = Daily[bool]("daily_entrust.json", False)


@AgentServer.custom_recognition("EntrustStopDailyReco")
class EntrustStopDailyReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        return _entrust.reco(context, argv)


@AgentServer.custom_action("EntrustDailyUpdateAct")
class EntrustDailyUpdateAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return _entrust.act(argv, True)
