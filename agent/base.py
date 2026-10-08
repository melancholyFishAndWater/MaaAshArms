import json, logging, os, sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, TypeVar

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import Context, JRecognitionType, RecognitionDetail
from maa.pipeline import JTemplateMatch

from parent_tracker import father_name

T = TypeVar("T")

# 默认hit box 仅用于不需要坐标的节点
DEFAULT_HIT_BOX = [0] * 4


def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])


def is_hit(detail: RecognitionDetail | None) -> bool:
    return detail is not None and detail.hit


# TODO 全部替换
def parse_params(raw: str | None, *required_keys: str) -> dict[str, Any]:
    """解析 MaaFW 传入的 JSON 参数字符串。

    处理各种"空/异常"输入，并可选校验必填字段。

    Args:
        raw: 原始参数字符串。可能为 None 或空串（表示未传参）。
             MaaFW 在节点未写 custom_*_param 时，也可能传入字面量 "null"。
        *required_keys: 需要校验存在的必填字段名。若提供，则缺少时抛异常。

    Returns:
        解析后的参数字典。若 raw 为空且无必填字段，返回空字典 {}。

    Raises:
        ValueError: 以下任一情况：
            - raw 为空但存在必填字段
            - JSON 解析失败
            - 解析结果为 null 但存在必填字段
            - 解析结果不是对象(dict)
            - 缺少必填字段
    """
    if not raw:
        if required_keys:
            raise ValueError(f"参数为空，需要字段: {list(required_keys)}")
        return {}
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON解析失败: {e}") from e
    if params is None:
        # MaaFW 在节点未写 custom_*_param 时传的是字面量 "null" 而非空串，视同缺省
        if required_keys:
            raise ValueError(f"参数为空，需要字段: {list(required_keys)}")
        return {}
    if not isinstance(params, dict):
        raise ValueError(f"参数必须是对象，得到: {type(params).__name__}")
    if required_keys:
        missing = [k for k in required_keys if k not in params]
        if missing:
            raise ValueError(f"缺少必填字段: {missing}")
    return params


# ---------- Log ----------


# PERF 待拆分
# 面板可见性由前缀决定：MFA 只放行以 trace: / debug: / info: / success: / critical: /
# warn: / error: 开头的行（Extensions/MaaFW/MaaProcessor.cs:251-297），面板自身没有等级过滤。
# 所以照 M9A 的做法分两个 handler（agent/utils/logger.py:131-181）：
#   console → INFO 及以上，带 level 前缀 → 进面板
#   file    → TRACE 及以上全量       → 落 <安装根>/debug/agent/agent.log
TRACE = 5
SUCCESS = 25
logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(SUCCESS, "SUCCESS")

# MFA 认的前缀名；WARNING / ERROR 必须写成 warn / err
_LEVEL_SHORT = {
    "TRACE": "trace",
    "DEBUG": "debug",
    "INFO": "info",
    "SUCCESS": "success",
    "WARNING": "warn",
    "ERROR": "err",
    "CRITICAL": "critical",
}

_FILE_FORMAT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s"
)


class _PanelFormatter(logging.Formatter):
    """面板格式：level_short:message，前缀决定这一行能不能上面板"""

    def format(self, record: logging.LogRecord) -> str:
        short = _LEVEL_SHORT.get(record.levelname, record.levelname.lower())
        return f"{short}:{record.getMessage()}"


_console_handler: logging.Handler | None = None


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("maasharms")
    logger.setLevel(TRACE)
    logger.propagate = False

    # 面板：stdout（本机验证过的通道）+ INFO 门槛
    global _console_handler
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(_PanelFormatter())
    logger.addHandler(_console_handler)

    # 文件：debug/trace 全量。目录与框架的 log_dir 同一个（agent/main.py:30-32），
    # 框架启动时会清掉该目录下 7 天以上的 .log，所以 backupCount 也按 7 天
    try:
        log_dir = Path(__file__).resolve().parents[1] / "debug" / "agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_dir / "agent.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(TRACE)
        file_handler.setFormatter(_FILE_FORMAT)
        logger.addHandler(file_handler)
    except Exception as e:
        # 目录不可写不能拖垮 agent；这行不带前缀，只落 MFA 的 stdout 通道
        print(f"agent 日志文件初始化失败，仅输出到面板: {e!r}")

    return logger


_logger = _build_logger()


def change_console_level(level: str | int = "DEBUG"):
    """临时改面板等级：change_console_level("DEBUG") 会把 debug 也显示到面板上"""
    if _console_handler is None:
        return
    _console_handler.setLevel(
        level
        if isinstance(level, int)
        else getattr(logging, str(level).upper(), logging.INFO)
    )


# 日志接口
class _Log:
    __instance = None

    def __new__(cls):
        if cls.__instance is None:
            cls.__instance = super().__new__(cls)
        return cls.__instance

    def trace(self, msg: str):
        _logger.log(TRACE, msg)

    def debug(self, msg: str):
        _logger.debug(msg)

    def info(self, msg: str):
        _logger.info(msg)

    def success(self, msg: str):
        _logger.log(SUCCESS, msg)

    def warn(self, msg: str):
        _logger.warning(msg)

    def error(self, msg: str):
        _logger.error(msg)

    def critical(self, msg: str):
        _logger.critical(msg)

    def ferror(self, msg: str = ""):
        _logger.error("内部错误。" + msg)


# 日志
log = _Log()

# ---------- PI 环境变量 ----------


# MFA 注入给 agent 的 PI_* 全集（Extensions/MaaFW/AgentHelper.cs:581-590）。
# 分工：这里只有"客户端 / 资源 / 控制器"的身份与上下文；用户在面板上选的选项不走环境变量，
# 而是开始任务时变成 pipeline_override 落到节点上（读它用 context.get_node_data()）。
PI_ENV_KEYS = (
    "PI_INTERFACE_VERSION",  # PI 规范版本
    "PI_CLIENT_NAME",  # 客户端名，如 MFAAvalonia
    "PI_CLIENT_VERSION",  # 客户端版本
    "PI_CLIENT_LANGUAGE",  # 界面语言，如 zh_cn
    "PI_CLIENT_MAAFW_VERSION",  # 客户端链接的 MaaFramework 版本，如 v5.12.2
    "PI_VERSION",  # 项目版本（interface.json 的 version）
    "PI_CONTROLLER",  # 选中的 controller[] 条目，JSON 文本
    "PI_RESOURCE",  # 选中的 resource[] 条目，JSON 文本
)

_pi_env: dict[str, str] | None = None


def pi_env(force: bool = False) -> dict[str, str]:
    """读取 PI_* 环境变量（进程内只读一次；缺的键给空串）。

    环境变量在 agent 生命周期里不会变，所以缓存；force 留给测试。
    返回的字典视为只读，不要就地改。
    """
    global _pi_env
    if force or _pi_env is None:
        _pi_env = {k: os.environ.get(k, "") for k in PI_ENV_KEYS}
    return _pi_env


def _as_string(value: Any) -> str:
    return "" if value is None else str(value)


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_as_string(item) for item in value if item is not None]


def _pi_json(key: str) -> dict[str, Any] | None:
    """把 JSON 文本型的环境变量解析成 dict。

    解析失败只记日志并返回 None —— 客户端没传或传坏了都不该让 agent 停摆。
    """
    raw = pi_env().get(key, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warn(f"{key} 不是合法 JSON: {e} | 原文前 200 字: {raw[:200]}")
        return None
    if not isinstance(value, dict):
        log.warn(f"{key} 应为对象，得到 {type(value).__name__}")
        return None
    return value


def pi_controller() -> dict[str, Any] | None:
    """选中的 controller[] 条目。"""
    return _pi_json("PI_CONTROLLER")


def pi_resource() -> dict[str, Any] | None:
    """选中的 resource[] 条目。"""
    return _pi_json("PI_RESOURCE")


def pi_client_language() -> str:
    return pi_env()["PI_CLIENT_LANGUAGE"]


def pi_controller_type() -> str:
    c = pi_controller()
    return _as_string(c.get("type")) if c else ""


def pi_controller_name() -> str:
    c = pi_controller()
    return _as_string(c.get("name")) if c else ""


def pi_resource_name() -> str:
    r = pi_resource()
    return _as_string(r.get("name")) if r else ""


def pi_resource_label() -> str:
    """资源显示名；取不到 label 就回落 name。"""
    r = pi_resource()
    if not r:
        return ""
    return _as_string(r.get("label")) or _as_string(r.get("name"))


def pi_resource_paths() -> list[str]:
    r = pi_resource()
    return _as_string_list(r.get("path")) if r else []


def pi_check_maafw_version() -> bool:
    """比对客户端链接的框架版本与本包 MaaFw 版本；不一致只警告（握手会给出更准确的报错）。

    两处来源的写法不同：PI_CLIENT_MAAFW_VERSION 形如 v5.12.2，包元数据是 5.12.2。
    """
    from importlib.metadata import PackageNotFoundError, version

    client = pi_env()["PI_CLIENT_MAAFW_VERSION"].lstrip("v")
    if not client:
        return True  # 不是 MFA 拉起的（VS Code 调试 / 本机冒烟），不判
    try:
        ours = version("maafw")
    except PackageNotFoundError:
        return True
    if client != ours:
        log.warn(
            f"客户端 MaaFramework {client} 与包内 MaaFw {ours} 不一致，"
            f"可能以 Protocol version mismatch 握手失败"
        )
        return False
    return True


def pi_log_snapshot() -> None:
    """启动时打一行汇总：客户端到底传没传 PI_*、传了什么。

    对齐 M9A 的 log_pi_environment()：出问题时不用让用户去 dump 环境变量。
    非 MFA 拉起时各字段都是空，会打成 '-'，这是预期的。
    """
    env = pi_env()
    log.debug(
        f"PI: interface={env['PI_INTERFACE_VERSION'] or '-'} "
        f"client={env['PI_CLIENT_NAME'] or '-'}/{env['PI_CLIENT_VERSION'] or '-'} "
        f"lang={env['PI_CLIENT_LANGUAGE'] or '-'} "
        f"maafw={env['PI_CLIENT_MAAFW_VERSION'] or '-'} "
        f"project={env['PI_VERSION'] or '-'} "
        f"controller={pi_controller_type() or '-'} "
        f"controller_ok={pi_controller() is not None} "
        f"resource_ok={pi_resource() is not None}"
    )


# ---------- Reco ----------


# 如果连续k次画面不变 则返回roi box 一般和move系列搭配判断移动前后是否有明显变化
@AgentServer.custom_recognition("IsFreezesReco")
class IsFreezesReco(CustomRecognition):
    __freezes_dict: dict[tuple[int, str], int] = {}

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):

        # 提取变量
        p = (
            json.loads(argv.custom_recognition_param)
            if argv.custom_recognition_param
            else {}
        )
        from_ = p.get("from_", "unknow")  # 父节点
        thr = float(p.get("threshold", 0.99))  # 识别阈值
        k = p.get("k", 3)  # 连续不变次数
        name = f"probe_{from_}"  # 图片名字
        key = (argv.task_detail.task_id, from_)
        x, y, w, h = argv.roi

        # 保证画面处于静止
        context.wait_freezes(2000, box=(x, y, w, h))

        # 有上一张图像，则识别
        r = None
        if key in self.__freezes_dict.keys():
            r = context.run_recognition_direct(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(
                    template=[name], roi=(x, y, w, h), threshold=[thr], method=5
                ),
                argv.image,
            )
        else:
            log.debug(f"{argv.node_name} 第一次识别冻结，跳过识别")

        # 判断是否冻结
        same = bool(r and r.hit)
        if same:
            log.debug(f"{argv.node_name} 检测到画面冻结")

        # 存储本帧识别
        if not context.override_image(name, argv.image[y : y + h, x : x + w]):
            print(f"override_image failed: {name}")
            same = False
            log.warn(f"{argv.node_name} 覆写图片失败，冻结次数重置")

        # 存储连续识别成功次数
        self.__freezes_dict[key] = (self.__freezes_dict.get(key, 0) + 1) if same else 0

        # 条件返回识别成功
        if self.__freezes_dict[key] >= k:
            del self.__freezes_dict[key]
            log.debug(f"{argv.node_name} 画面冻结次数超过{k}，返回识别区域box")
            return (x, y, w, h)


# ---------- Action ----------


# 始终执行失败
@AgentServer.custom_action("TaskStopErrorAct")
class TaskStopErrorAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        return False


# ----- LoopErrorByTimes -----

loop_times_by_name: dict[str, int] = {}


# 返回 box 以结束任务
@AgentServer.custom_recognition("LoopErrorByTimesReco")
class LoopErrorByTimesReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        node_name = argv.node_name
        name = father_name(argv.task_detail.task_id, node_name)
        if name is None:
            log.warn(f"{node_name} 获取父节点失败")
            return

        global loop_times_by_name
        loop_times = loop_times_by_name.get(name, 0) + 1
        loop_times_by_name[name] = loop_times

        times = 10
        try:
            p = parse_params(argv.custom_recognition_param, "times")
            times = p["times"]
        except Exception as e:
            log.warn(f"{argv.node_name} 解析param失败: {e}")
        if loop_times >= times:
            return DEFAULT_HIT_BOX


# LoopErrorByTimes 初始化
@AgentServer.custom_action("LoopErrorByTimesInitAct")
class LoopErrorByTimesInitAct(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        global loop_times_by_name
        loop_times_by_name.clear()
        return True


# ----- MoveUpDown -----

_move_up_down_dict: dict[tuple[int, str], int] = {}
_move_entry: str = "MoveUp"


# 若还有滑动次数 返回非 None
@AgentServer.custom_recognition("MoveUpDownReco")
class MoveUpDownReco(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> list[int] | None:
        # 提取变量
        s = argv.custom_recognition_param
        param: dict[str, str] = s and json.loads(s) or {}
        from_ = param.get("from_", "unknow")
        x = int(param.get("x", 10))
        n_name = argv.node_name

        # 合成key
        key = (argv.task_detail.task_id, from_)

        # 判断是否还有次数
        times = _move_up_down_dict.get(key, 0)
        if times >= x * 2:
            log.debug(f"{n_name} 移动次数耗尽，取消移动")
            return
        log.debug(f"{n_name} 当前移动次数: {times}")

        # 自增
        times += 1
        _move_up_down_dict[key] = times

        # 移动方向
        global _move_entry
        if times <= x:
            _move_entry = "MoveUp"
        else:
            _move_entry = "MoveDown"
        log.debug(f"{n_name} 当前移动方向: {_move_entry}")

        return DEFAULT_HIT_BOX


# 上滑动x次 下滑动x次
@AgentServer.custom_action("MoveUpDownAct")
class MoveUpDownAct(CustomAction):

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        # 移动
        r = context.run_action(_move_entry)
        log.debug(f"{argv.node_name} 执行移动: {_move_entry}")

        # 返回结果
        if r is not None:
            return r.success
        return False
