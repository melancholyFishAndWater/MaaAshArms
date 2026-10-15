"""Sentry 报告脚本共用的查询、解析与输出工具。

包含四类内容：

- Sentry CLI 探测调用、游标分页、并发查询（照 M9A `tools/sentry/report_common.py`）；
- release 字符串解析与版本排序（**本项目与 M9A 的关键差异见 `release_match()`**）；
- 遥测工件的落盘与读回（`save_artifact` / `load_artifact`），供 `fetch` 与各报告共用；
- 终端表格、Wilson 区间等输出辅助。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections.abc import Generator, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

try:
    from .config import CONFIG
except ImportError:
    from config import CONFIG

EXPLORE_LIMIT = 1_000
DEFAULT_SENTRY_TIMEOUT_SECONDS = 120.0
# 报告默认查询窗口。超过 MAX_RELIABLE_SPAN_PERIOD_DAYS 时 Sentry 只返回截断样本，
# 绝对计数不可用，因此默认停在可靠窗口内。
DEFAULT_SPAN_PERIOD = "30d"
# 仅用于按独立用户数发现 release 的兜底查询：该路径只比较比率，放宽窗口不影响结论。
DEFAULT_RELEASE_DISCOVERY_PERIOD = "90d"
SENTRY_RELEASE_API_LIMIT = 100
# 独立查询的并发上限。Sentry 的瓶颈是单次请求延迟而非吞吐，少量并发即可显著提速。
MAX_EXPLORE_WORKERS = 4

# Sentry explore 的时间窗口超过约 30 天时不再做真实聚合，而是返回一批固定的
# 截断样本（不同 period 拿到完全相同的结果），绝对计数完全不可用。
MAX_RELIABLE_SPAN_PERIOD_DAYS = 30
PERIOD_UNIT_DAYS = {"m": 1 / 1_440, "h": 1 / 24, "d": 1.0, "w": 7.0}

ARTIFACT_SCHEMA = 1
DEFAULT_ARTIFACT_DIR = "temp/telemetry"

# Sentry 查询字段 → 工件里的中性字段名。fetch 用它给字段改名，读回时按中性名喂给分析函数。
FIELD_MAP: dict[str, str] = {
    "span.description": "description",
    "span.status": "status",
    "span.op": "op",
    "release": "release",
    "environment": "environment",
    "os.name": "os_name",
    "user.id": "user_id",
    "count_unique(trace)": "_count",
    "count()": "_events",
    "count_unique(user)": "_count_user",
}
# 同一中性名可能对应多个聚合写法（历史遗留），读回时两种写法都接受。
COUNT_ALIASES: dict[str, tuple[str, ...]] = {
    "_count": ("_count", "count_unique(trace)"),
    "_events": ("_events", "count()"),
    "_count_user": ("_count_user", "count_unique(user)"),
}


def module_summary(doc: str | None) -> str:
    """取模块 docstring 的首行当 argparse 的 description。

    `__doc__` 在 `python -O -OO`、或模块被以某种方式执行时可以是 None，
    直接 `__doc__.splitlines()[0]` 会抛 AttributeError；这里给个兜底。
    """
    lines = (doc or "").strip().splitlines()
    return lines[0].strip() if lines and lines[0].strip() else "（无描述）"


def field_alias(field: str) -> str:
    """把 Sentry 字段名换成工件里的中性名；未知字段原样保留（去掉点号）。"""
    if field in FIELD_MAP:
        return FIELD_MAP[field]
    return field.replace(".", "_")


def period_days(period: str) -> float | None:
    """把 Sentry 的 period 表达式换算成天数；无法识别的写法返回 None。

    同时支持 ``7d`` / ``24h`` / ``30m`` / ``2w`` 这类相对窗口，以及
    ``2026-08-23..2026-08-24`` 显式区间。
    """
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", period, re.IGNORECASE)
    if match is not None:
        return int(match.group(1)) * PERIOD_UNIT_DAYS[match.group(2).lower()]

    range_match = re.fullmatch(r"\s*([^\s.]+)\s*\.\.\s*([^\s.]+)\s*", period)
    if range_match is None:
        return None
    try:
        # Python 3.11+ 的 fromisoformat 原生认 `Z`（实测与手写 `.replace("Z", "+00:00")`
        # 结果逐字段相同），所以不必再手工替换。
        start = datetime.fromisoformat(range_match.group(1))
        end = datetime.fromisoformat(range_match.group(2))
        span = end - start
    except (TypeError, ValueError):
        return None
    return span.total_seconds() / 86_400


def warn_on_truncated_period(period: str) -> bool:
    """period 超出可靠窗口时向 stderr 提示一次，返回本次是否已提示。

    报告入口在发起查询前调用一次即可，避免每次 `explore()` 都重复刷屏。
    """
    days = period_days(period)
    if days is None or days <= MAX_RELIABLE_SPAN_PERIOD_DAYS:
        return False
    print(
        f"[警告] period={period} 超过 {MAX_RELIABLE_SPAN_PERIOD_DAYS} 天：Sentry explore 对长窗口返回截断样本，"
        f"绝对计数不可用（比率趋势仍可参考）。需要精确计数时请改用 {MAX_RELIABLE_SPAN_PERIOD_DAYS}d 以内的窗口。",
        file=sys.stderr,
        flush=True,
    )
    return True


REPORT_LOCK_NAME = "report.lock"
# 报告最长会跑几分钟；超过这个时长仍存在的锁视为上一轮异常退出留下的残留。
REPORT_LOCK_STALE_SECONDS = 900.0


def report_lock_path() -> Path:
    """报告建议性锁的位置（工作区临时目录，跨报告实例共享）。

    刻意不走系统临时目录：DSH 沙箱下工作区外写入会被拒，锁会变成噪音。
    """
    return repo_root() / DEFAULT_ARTIFACT_DIR / REPORT_LOCK_NAME


def _pid_is_running(pid: int) -> bool | None:
    """进程还活着吗：True 活着 / False 已退出 / None 没权限、问不出来。

    Windows 上不能用 `os.kill(pid, 0)`——实测对**已经退出**的进程它同样不报错（进程对象还在时不回收 pid），
    拿它做判据会把"刚跑完"永远当成"正在跑"，等于没修。所以这里读退出码：
    `GetExitCodeProcess` 给 `STILL_ACTIVE`（259）才算活着。
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 进程不存在时 OpenProcess 就失败；权限不足则存在但问不出来
        return None if ctypes.get_last_error() == error_access_denied else False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def lock_owner_alive(lock_path: Path) -> bool | None:
    """锁文件里记的进程还活着吗：True 活着 / False 已退出 / None 读不出 pid。

    锁文件里写的是写入者的 pid（见 `report_run_guard`）。**光看 mtime 分不出"正在跑"和"刚跑完"**——
    前一条报告刚退出时锁文件的 mtime 还是新的，于是下一条会收到"已进行约 0s"的假并发提示。
    """
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.isdigit():
        return None
    return _pid_is_running(int(raw))


@contextmanager
def report_run_guard(lock_path: Path | None = None) -> Generator[None]:
    """报告运行期间持有一个建议性锁，并在检测到并发报告时提醒一次。

    Sentry explore 的瓶颈是单次请求延迟而非吞吐，并发跑多个报告只会互相拖慢，
    因此这里不做互斥，只在发现另一个**活着**的报告时向 stderr 提示。
    持锁进程已退出（含 mtime 未过期的残留）时不提示、顺手把残留锁删掉，免得下一条报告又白提示一次。
    """
    lock = lock_path or report_lock_path()
    try:
        age_seconds = time.time() - lock.stat().st_mtime
    except OSError:
        age_seconds = None
    if age_seconds is not None and age_seconds < REPORT_LOCK_STALE_SECONDS:
        alive = lock_owner_alive(lock)
        if alive is False:
            try:
                lock.unlink()
            except OSError:
                pass
        else:
            # 活着，或读不出 pid（旧版本工具写的锁）：照旧提示
            print(
                f"[提示] 检测到另一个 Sentry 报告正在运行（已进行约 {age_seconds:.0f}s，"
                f"锁文件 {lock.name} 里的进程还活着）。并发运行多个报告会互相拖慢，建议等它结束。",
                file=sys.stderr,
                flush=True,
            )

    owner = str(os.getpid())
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(owner, encoding="utf-8")
    except OSError:
        # 锁只是建议性的，建不出来不该让报告失败（但在 finally 里不能有 yield）。
        pass
    try:
        yield
    finally:
        try:
            # 只清理本次运行写入的锁，避免把后来者的锁删掉。
            if lock.read_text(encoding="utf-8") == owner:
                lock.unlink()
        except (OSError, UnicodeDecodeError):
            pass


def repo_root() -> Path:
    """仓库根目录（tools/sentry/report_common.py 上溯三级）。"""
    return Path(__file__).resolve().parents[2]


def get_release_pattern() -> re.Pattern[str]:
    """获取项目 Release 匹配正则。"""
    if CONFIG.release_pattern:
        return re.compile(CONFIG.release_pattern)
    prefix = CONFIG.project_prefix
    return re.compile(
        rf"(?:^|[+/]){re.escape(prefix)}@v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?$",
        re.IGNORECASE,
    )


RELEASE_PATTERN = get_release_pattern()


def release_match(release: str) -> re.Match[str] | None:
    """在 release 字符串里定位**本项目版本号**，返回最后一个匹配。

    与 M9A 的关键差异：本项目的 release 是双 ``@`` 拼接
    （``MFA@v2.16.1+MaaAshArms@v1.0.0-beta.38``），字符串里存在多个
    ``<name>@vX.Y.Z`` 片段。M9A 的 ``search()`` 取第一个匹配，在这里会命中
    前面的 ``MFA@v2.16.1``，把「资源版本对比」变成「客户端版本对比」——结论是错的。
    取**最后一个**匹配才对：拼接格式下它永远是资源版本，裸 release 下也是唯一匹配。
    """
    if not isinstance(release, str):
        return None
    last: re.Match[str] | None = None
    for match in RELEASE_PATTERN.finditer(release):
        last = match
    return last


PRERELEASE_RANKS: dict[str, int] = {
    "alpha": -1,
    "beta": 0,
    "rc": 1,
}
STABLE_RELEASE_RANK = 2


def release_version_key(release: str) -> tuple[int, int, int, int, int] | None:
    """解析 release 字符串内嵌的版本排序键，不匹配或预发布类型未知时返回 None。"""
    match = release_match(release)
    if match is None:
        return None

    major, minor, patch, prerelease, prerelease_number = match.groups()
    if prerelease is None or prerelease_number is None:
        prerelease_rank = STABLE_RELEASE_RANK
        prerelease_number = "0"
    else:
        rank = PRERELEASE_RANKS.get(prerelease.lower())
        if rank is None:
            return None
        prerelease_rank = rank

    return (
        int(major),
        int(minor),
        int(patch),
        prerelease_rank,
        int(prerelease_number),
    )


def version_label(release: str) -> str | None:
    """从 release 字符串中提取内嵌的版本标签，例如 "v1.0.0-beta.38"。"""
    match = release_match(release)
    if match is None:
        return None
    major, minor, patch, prerelease, prerelease_number = match.groups()
    label = f"v{major}.{minor}.{patch}"
    if prerelease is not None and prerelease_number is not None:
        label += f"-{prerelease.lower()}.{prerelease_number}"
    return label


def resolve_sentry_command() -> str:
    """查找当前系统中可执行的 sentry 命令。"""
    candidates = ("sentry.cmd", "sentry.exe", "sentry") if os.name == "nt" else ("sentry",)
    for candidate in candidates:
        command = shutil.which(candidate)
        if command:
            return command
    raise RuntimeError(
        "未找到 sentry 命令。请先安装 Sentry CLI（pnpm add -g sentry 或 npm install -g sentry），"
        "确认 sentry --version 可运行后执行一次 sentry auth login。"
    )


def run_sentry_json_value(
    sentry_command: str,
    arguments: Sequence[str],
    *,
    verbose: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> Any:
    """执行 Sentry CLI 并解析其 JSON 输出。"""
    if verbose:
        print(f"+ sentry {' '.join(arguments)}", file=sys.stderr)

    try:
        process = subprocess.run(
            [sentry_command, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"sentry {' '.join(arguments)} 执行超过 {timeout_seconds:g} 秒，请检查网络、认证状态或缩短查询范围。"
        ) from error
    if process.returncode != 0:
        diagnostic = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"sentry {' '.join(arguments)} 执行失败（退出码 {process.returncode}）：\n{diagnostic}")

    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"sentry CLI 未返回有效 JSON：\nstdout：\n{process.stdout}\nstderr：\n{process.stderr}"
        ) from error
    return result


def run_sentry_json(
    sentry_command: str,
    arguments: Sequence[str],
    *,
    verbose: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """执行 Sentry CLI，并要求其返回 JSON 对象。"""
    result = run_sentry_json_value(
        sentry_command,
        arguments,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(result, dict):
        raise RuntimeError("sentry CLI 返回了非对象 JSON。")
    return result


def authority_error_hint(error: RuntimeError) -> str:
    """把 Sentry CLI 的常见报错补一句可执行的下一步。"""
    text = str(error)
    lowered = text.lower()
    hints: list[str] = []
    if "not authenticated" in lowered or "auth login" in lowered:
        hints.append("尚未登录 Sentry：先执行一次 `sentry auth login`（浏览器授权）。")
    if "unable to open database file" in lowered:
        hints.append(
            "Sentry CLI 打不开自己的缓存库（受限沙箱下 ~/.config/sentry/cli.db 在工作区外只读）。"
            "CLI 每次 explore 都要写这个库，所以 --no-fresh 也绕不开。"
            "把凭据复制进可写目录再把 SENTRY_CONFIG_DIR 指过去：\n"
            '    copy "$env:USERPROFILE\\.config\\sentry\\cli.db" <可写目录>\\cli.db\n'
            "    $env:SENTRY_CONFIG_DIR = '<可写目录>'\n"
            "OAuth 的 token 与 refresh_token 都在 cli.db 的 auth 表里（host 列写 https://sentry.io），"
            "令牌未过期时复制后直接可用，不需要重新登录。"
        )
    if "project" in lowered and ("not found" in lowered or "does not exist" in lowered):
        hints.append(
            f"target 可能写错（当前 config.json 的 target={CONFIG.target}）："
            f"用 `sentry project list {CONFIG.target.partition('/')[0]}/` 核对 slug 后改 tools/sentry/config.json。"
        )
    return ("\n".join(hints) + "\n" + text) if hints else text


def explore(
    sentry_command: str,
    *,
    target: str,
    period: str,
    fields: Sequence[str],
    query: str,
    sort: str | None = None,
    fresh: bool = True,
    verbose: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """查询 Sentry spans，并跟随游标返回全部分页结果。

    ``fresh=False`` 允许 Sentry CLI 复用本地缓存：同一查询只需约三分之一的往返时间，
    代价是结果可能略滞后于最新遥测。
    """
    base_arguments = ["explore", target, "--dataset", "spans"]
    for field in fields:
        base_arguments.extend(("--field", field))
    if query:
        base_arguments.extend(("--query", query))
    if sort:
        base_arguments.extend(("--sort", sort))
    base_arguments.extend(("--period", period, "--limit", str(EXPLORE_LIMIT)))
    if fresh:
        base_arguments.append("--fresh")
    base_arguments.append("--json")

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        arguments = [*base_arguments]
        if cursor:
            arguments.extend(("--cursor", cursor))
        result = run_sentry_json(
            sentry_command,
            arguments,
            verbose=verbose,
            timeout_seconds=timeout_seconds,
        )
        data = result.get("data", [])
        if not isinstance(data, list):
            raise RuntimeError("sentry explore 返回的 data 不是数组。")
        rows.extend(data)
        if not result.get("hasMore"):
            return rows

        next_cursor = result.get("nextCursor") or (result.get("meta") or {}).get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError("sentry explore 声明存在下一页，但未返回有效游标。")
        if next_cursor in seen_cursors:
            raise RuntimeError(f"sentry explore 返回了重复分页游标：{next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


@dataclass(frozen=True)
class SpanQuery:
    """一次 spans 聚合查询的字段、过滤与排序。"""

    fields: tuple[str, ...]
    query: str = ""
    sort: str | None = None


def explore_all(
    sentry_command: str,
    *,
    target: str,
    period: str,
    queries: Sequence[SpanQuery],
    fresh: bool = True,
    verbose: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> list[list[dict[str, Any]]]:
    """并发执行互不依赖的 spans 查询，并按输入顺序返回各自结果。

    单个 explore 内部必须串行跟随游标分页，而 Sentry explore 的瓶颈是单次请求延迟
    而非吞吐；因此在查询之间并发能成倍缩短报告耗时，又不会把请求数放大。
    """
    if not queries:
        return []
    if len(queries) == 1:
        only = queries[0]
        return [
            explore(
                sentry_command,
                target=target,
                period=period,
                fields=only.fields,
                query=only.query,
                sort=only.sort,
                fresh=fresh,
                verbose=verbose,
                timeout_seconds=timeout_seconds,
            )
        ]

    with ThreadPoolExecutor(max_workers=min(len(queries), MAX_EXPLORE_WORKERS)) as executor:
        futures = [
            executor.submit(
                explore,
                sentry_command,
                target=target,
                period=period,
                fields=query.fields,
                query=query.query,
                sort=query.sort,
                fresh=fresh,
                verbose=verbose,
                timeout_seconds=timeout_seconds,
            )
            for query in queries
        ]
        return [future.result() for future in futures]


# ---------------------------------------------------------------- 工件读写


@dataclass(frozen=True)
class Measurement:
    """一个具名聚合结果（一次 explore 查询的全部行）。"""

    name: str
    rows: list[dict[str, Any]]
    fields: tuple[str, ...] = ()
    query: str = ""
    sort: str | None = None


@dataclass(frozen=True)
class Artifact:
    """一份下载下来的遥测数据工件。"""

    target: str
    period: str
    measurements: dict[str, Measurement]
    generated_at: str
    path: Path | None = None

    def get(self, name: str, required_by: str = "") -> list[dict[str, Any]]:
        """取一个 measurement 的行；缺失时报出缺哪个、被谁需要。"""
        measurement = self.measurements.get(name)
        if measurement is None:
            available = "、".join(sorted(self.measurements)) or "（空）"
            need = f"（{required_by} 需要它）" if required_by else ""
            raise ValueError(f"工件里没有 measurement '{name}'{need}。现有: {available}")
        return measurement.rows


def to_neutral_rows(rows: Iterable[dict[str, Any]], fields: Sequence[str]) -> list[dict[str, Any]]:
    """把 Sentry 返回的行改成中性字段名（带 ``_count`` 计数列）。"""
    mapping = {field: field_alias(field) for field in fields}
    neutral: list[dict[str, Any]] = []
    for row in rows:
        converted: dict[str, Any] = {}
        for key, value in row.items():
            converted[mapping.get(key, key.replace(".", "_"))] = value
        neutral.append(converted)
    return neutral


def measurement_to_dict(measurement: Measurement) -> dict[str, Any]:
    """把 measurement 序列化成工件的 JSON 形状。"""
    return {
        "fields": list(measurement.fields),
        "query": measurement.query,
        "sort": measurement.sort,
        "rows": measurement.rows,
    }


def artifact_to_dict(artifact: Artifact) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_SCHEMA,
        "generated_at": artifact.generated_at,
        "target": artifact.target,
        "period": artifact.period,
        "measurements": {
            name: measurement_to_dict(measurement) for name, measurement in artifact.measurements.items()
        },
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_artifact_path(target: str, period: str, now: datetime | None = None) -> Path:
    """默认落盘路径 temp/telemetry/<project>-<period>-<时间戳>.json。"""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    project = target.rpartition("/")[2] or "project"
    safe_period = re.sub(r"[^0-9A-Za-z]+", "-", period).strip("-") or "period"
    return repo_root() / DEFAULT_ARTIFACT_DIR / f"{project}-{safe_period}-{stamp}.json"


def save_artifact(artifact: Artifact, path: Path) -> Path:
    """把工件写到磁盘（UTF-8，两个空格缩进），返回实际路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = artifact_to_dict(artifact)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_artifact(path: Path) -> Artifact:
    """读回一份工件；schema 不认识时直接报错，不猜。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"未找到遥测工件: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"遥测工件 {path} 不是合法 JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"遥测工件 {path} 顶层必须是 JSON 对象。")

    schema = data.get("schema")
    if schema != ARTIFACT_SCHEMA:
        raise ValueError(f"遥测工件 {path} 的 schema={schema!r}，本工具只认 schema={ARTIFACT_SCHEMA}。")

    raw_measurements = data.get("measurements")
    if not isinstance(raw_measurements, dict):
        raise ValueError(f"遥测工件 {path} 缺少 'measurements' 对象。")

    measurements: dict[str, Measurement] = {}
    for name, raw in raw_measurements.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("rows"), list):
            raise ValueError(f"遥测工件 {path} 的 measurement '{name}' 结构不对（缺少 rows 数组）。")
        measurements[name] = Measurement(
            name=name,
            rows=[row for row in raw["rows"] if isinstance(row, dict)],
            fields=tuple(raw.get("fields") or ()),
            query=str(raw.get("query") or ""),
            sort=raw.get("sort"),
        )

    return Artifact(
        target=str(data.get("target") or ""),
        period=str(data.get("period") or ""),
        measurements=measurements,
        generated_at=str(data.get("generated_at") or ""),
        path=path,
    )


def row_count(row: dict[str, Any], key: str = "_count") -> int | None:
    """从中性行里取计数列；接受历史/别名写法，取不到或非整数时返回 None。"""
    for alias in COUNT_ALIASES.get(key, (key,)):
        value = row.get(alias)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


# ---------------------------------------------------------------- 统计辅助


def wilson_interval(failures: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """失败率的 Wilson 95% 置信区间（闭区间，单位与失败率同为小数）。

    比「失败数/总数」多告诉一件事：这个比率在当前样本量下能漂多远。
    样本数为 0 时返回 None。
    """
    if total <= 0:
        return None
    phat = failures / total
    denominator = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def format_rate(rate: float | None) -> str:
    """把小数失败率格式化为百分比。"""
    return "暂无样本" if rate is None else f"{rate:.1%}"


def format_rate_with_ci(rate: float | None, total: int, min_samples: int) -> str:
    """失败率 + Wilson 区间；样本不足时改为显式标注。"""
    if rate is None:
        return "暂无样本"
    if total < min_samples:
        return f"{rate:.1%}（样本不足 n={total}）"
    interval = wilson_interval(round(rate * total), total)
    if interval is None:
        return f"{rate:.1%}"
    low, high = interval
    return f"{rate:.1%}（95%CI {low:.1%}–{high:.1%}）"


def format_delta(delta_pp: float | None) -> str:
    """格式化百分点变化量，例如 '+1.4pp' 或 '-2.0pp'。"""
    if delta_pp is None:
        return ""
    sign = "+" if delta_pp > 0 else ""
    return f"{sign}{delta_pp:.1f}pp"


def show_progress(message: str, *, quiet: bool) -> None:
    """向标准错误输出阶段进度，不污染报告正文。"""
    if not quiet:
        print(message, file=sys.stderr, flush=True)


def display_width(value: str) -> int:
    """计算终端中的 Unicode 显示宽度，中文等宽字符按两列计算。"""
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def pad_display(value: str, width: int, *, align_right: bool = False) -> str:
    """按照终端显示宽度填充文本。"""
    padding = " " * (width - display_width(value))
    return f"{padding}{value}" if align_right else f"{value}{padding}"


def write_console_table(
    headers: Sequence[str],
    values: Sequence[Sequence[str]],
    output: TextIO,
    *,
    right_aligned: set[int] | None = None,
) -> None:
    """输出处理中日韩宽字符的 Unicode 框线表格。"""
    if not values:
        print("（无数据）", file=output)
        return
    alignments = right_aligned or set()
    widths = [
        max(display_width(value) for value in (header, *(row[index] for row in values)))
        for index, header in enumerate(headers)
    ]

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (width + 2) for width in widths) + right

    def table_row(row: Sequence[str], *, header: bool = False) -> str:
        cells = [
            pad_display(
                value,
                widths[index],
                align_right=index in alignments and not header,
            )
            for index, value in enumerate(row)
        ]
        return "│ " + " │ ".join(cells) + " │"

    print(border("┌", "┬", "┐"), file=output)
    print(table_row(headers, header=True), file=output)
    print(border("├", "┼", "┤"), file=output)
    for row in values:
        print(table_row(row), file=output)
    print(border("└", "┴", "┘"), file=output)


def provenance_line(artifact: Artifact | None, period: str) -> str:
    """报告页脚的数据来源说明，顺带钉住「这份结论出自哪次抓取」。"""
    if artifact is None:
        return f"数据来源: Sentry 实时查询（period={period}）"
    location = f"文件 {artifact.path}" if artifact.path else "工件"
    return f"数据来源: 下载工件（{location}，抓取于 {artifact.generated_at}，period={artifact.period}）"


# ---------------------------------------------------------------- Sentry release API


def select_latest_reported_release(
    rows: Sequence[dict[str, Any]],
    min_users: int = 0,
) -> str | None:
    """选择发布流程已 finalize 并记录 deploy 的最新 release。

    本项目不跑 Sentry 的发布/deploy 上报，这个通道大概率返回 None，
    于是自动选版本会落到 `select_latest_release()` 的 spans 样本通道上。
    两条通道都保留，是为了让将来接上官方发布上报时自动生效。
    """
    del min_users  # release API 通道不按用户数过滤，保留参数仅为调用签名一致
    candidates: list[tuple[datetime, tuple[int, int, int, int, int], str]] = []
    for row in rows:
        release = row.get("version")
        released_at = row.get("dateReleased")
        deploy_count = row.get("deployCount")
        if (
            not isinstance(release, str)
            or not isinstance(released_at, str)
            or not isinstance(deploy_count, int)
            or deploy_count < 1
        ):
            continue

        version = release_version_key(release)
        if version is None:
            continue
        try:
            release_time = datetime.fromisoformat(released_at)
        except ValueError:
            continue
        candidates.append((release_time, version, release))

    return max(candidates)[2] if candidates else None


def select_latest_release(
    rows: Sequence[dict[str, Any]],
    min_users: int = 3,
    *,
    quiet: bool = False,
) -> str:
    """从 spans 中选择最新 release；用户数不达标时放宽门槛并提示。

    与 M9A 的差异：M9A 找不到达标版本时直接抛错。本项目装机量小，
    硬门槛会让命令在第一条就失败，所以这里逐级放宽并明确告知放宽到了多少。
    """
    for threshold in (min_users, 0):
        candidates: list[tuple[tuple[int, int, int, int, int], int, int, str]] = []
        for row in rows:
            release = row.get("release")
            user_count = row.get("count_unique(user)")
            trace_count = row.get("count_unique(trace)")
            if not isinstance(release, str) or not isinstance(trace_count, int):
                continue
            users = user_count if isinstance(user_count, int) else 0
            if users < threshold:
                continue
            version = release_version_key(release)
            if version is None:
                continue
            candidates.append((version, users, trace_count, release))
        if candidates:
            if threshold != min_users:
                show_progress(
                    f"[提示] {DEFAULT_RELEASE_DISCOVERY_PERIOD} 内没有达到 {min_users} 位独立用户的版本，"
                    f"已放宽到 {threshold} 位；可用 --release 显式指定。",
                    quiet=quiet,
                )
            return max(candidates)[3]

    prefix_desc = f"{CONFIG.project_prefix}@" if CONFIG.project_prefix else ""
    raise RuntimeError(
        f"Sentry 中找不到格式为 {prefix_desc}vX.Y.Z 或 {prefix_desc}vX.Y.Z-beta.N 的 release，"
        "请通过 --release 显式指定。"
    )


def query_sentry_releases(
    sentry_command: str,
    *,
    target: str,
    verbose: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """查询项目最新一页的 Sentry release 元数据。"""
    organization, separator, project = target.partition("/")
    if not separator or not organization or not project or "/" in project:
        raise ValueError(f"无效的 Sentry target：{target!r}，应为 <org>/<project>。")

    endpoint = (
        f"projects/{quote(organization, safe='')}/{quote(project, safe='')}/"
        f"releases/?per_page={SENTRY_RELEASE_API_LIMIT}"
    )
    result = run_sentry_json_value(
        sentry_command,
        ("api", endpoint, "--json"),
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(result, dict) and isinstance(result.get("body"), list):
        result = result["body"]
    if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
        raise RuntimeError("Sentry release API 返回的 JSON 不是对象数组。")
    return result


def resolve_latest_release(
    sentry_command: str,
    *,
    target: str,
    min_users: int = 3,
    fresh: bool = True,
    verbose: bool = False,
    quiet: bool = False,
    timeout_seconds: float = DEFAULT_SENTRY_TIMEOUT_SECONDS,
) -> str:
    """优先使用发布流程上报的 release，本项目下通常回退到 spans 样本。"""
    try:
        release_rows = query_sentry_releases(
            sentry_command,
            target=target,
            verbose=verbose,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeError as error:
        show_progress(f"[提示] 无法读取 Sentry release 列表，改用 spans 样本推断版本：{error.args[0][:120]}", quiet=quiet)
        release_rows = []

    reported_release = select_latest_reported_release(release_rows, min_users)
    if reported_release is not None:
        return reported_release

    rows = explore(
        sentry_command,
        target=target,
        period=DEFAULT_RELEASE_DISCOVERY_PERIOD,
        fields=("release", "count_unique(user)", "count_unique(trace)"),
        query="",
        sort="-count_unique(user)",
        fresh=fresh,
        verbose=verbose,
        timeout_seconds=timeout_seconds,
    )
    return select_latest_release(rows, min_users, quiet=quiet)
