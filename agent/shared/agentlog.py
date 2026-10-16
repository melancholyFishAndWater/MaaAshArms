"""agent 的日志设施：面板一级、文件一级。

面板可见性由前缀决定：MFA 只放行以 trace: / debug: / info: / success: / critical: /
warn: / error: 开头的行（Extensions/MaaFW/MaaProcessor.cs:251-297），面板自身没有等级过滤。
所以照 M9A 的做法分两个 handler（agent/utils/logger.py:131-181）：
  console → INFO 及以上，带 level 前缀 → 进面板
  file    → TRACE 及以上全量       → 落 <安装根>/debug/agent/agent.log

放在 agent/shared/ 下靠 agent/main.py 把它注入 sys.path 后按顶层名导入（`from agentlog import log`）。
模块名不叫 logging：那会遮蔽标准库，本模块自己要用它。
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# PERF 待拆分
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
