"""本地 release（安装目录，默认 `D:/MaaAshArms`）的文件辅助绘图：只按**内容身份**认运行。

为什么不用时间认：同一个时刻可能有**不同的用户**都在跑同一个任务，遥测里一个事件只属于一个
证据包；本地轮转日志的文件名又是"轮转时刻"、不是内容区间
（实测 `maafw.bak.2026.11.08-13.22.36.708.log` 的内容是 11-07 20:59 → 11-08 13:22）。
按时间挑就会挑到别人的运行。

所以这里只认**内容身份**：

0. 遥测事件的 `event_id` 出现在本机面板日志（`logs/log-*.log`）的
   `[Telemetry] 失败事件已加入发送队列：event_id=…` 里 → 这次运行确实在本机
   （只有产生它的那台机器会打这一行）；它只用来确认"该在本机找"，不能代替下面的对账。
1. 从证据包的日志里取几行"带完整时间戳 + task_id"的独特行（`PipelineTask node done`、
   `Task timeout`、`invalid node id`、`node not completed`、`reco [result=`，优先长行）；
2. 在本地各份 `debug/maafw*.log` 里找这些行**逐字出现**的那一份；
3. 再用 `task_id` 复核：那份日志里必须真有这个 task 的归属记录。
   命中唯一 → 用它（比证据包完整得多的同一份日志 + 同目录的 `vision/`、`on_error/`）；
   命中多份或零份 → **不用**，并在日志里说清，不猜。

时间只用来**缩小候选范围**（没拿到 `event_id` 时，先看哪些日志的首/末行时间区间盖住这个包），
不单独作判据。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from tools.drawing.lib.flow_from_log import _open_log_lines

from .pack import Case

log = logging.getLogger("tools.flow_cases")

DEFAULT_LOCAL_ROOT = "D:/MaaAshArms"
ENV_LOCAL_ROOT = "MAA_DRAWING_LOCAL_ROOT"
# 候选日志：当前日志 + 轮转日志（`maafw.bak.2026.11.04-00.50.15.156.log` 这类）
LOCAL_LOG_RE = re.compile(r"^(?:maafw|maa)[\w.\-]*\.log$", re.IGNORECASE)
LINE_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3,6})\]")
# 挑"独特行"用的标记：这些行带完整时间戳与 task_id / 节点名，逐字比对最可靠。
# 只做字符串判断、不重复解析——挑的是日志里现成的文本。
NEEDLE_MARKS = (
    "PipelineTask node done",
    "Task timeout",
    "invalid node id",
    "node not completed",
    "reco [result=",
)
# 失败标记：优先用来"锚定"是哪一份日志（一次运行可能跨日志轮转，失败那一段在最新的那份里）
FAIL_MARKS = ("Task timeout", "invalid node id", "node not completed")
# 事件总线那两种行**不能当对账依据**：同一次识别会被两个进程各写一份、格式还不一样
# （`[INF][Px..]...!!!OnEventNotify!!! [msg=..][details=..]` 与
# `[TRC][Px..]...ctx_event_sink [message=..][details_json=..]`），拿它逐字比对必然对不上。
NEEDLE_SKIP = ("!!!OnEventNotify!!!", "ctx_event_sink")
MIN_NEEDLE_LEN = 60
MAX_NEEDLES = 6
# 至少对上几行才算同一份运行（1 行 + 同 task 也认，见 `find`）
MIN_HITS = 2


def default_root() -> str:
    """本地 release 根：环境变量优先，其次 `D:/MaaAshArms`。"""
    return os.environ.get(ENV_LOCAL_ROOT) or DEFAULT_LOCAL_ROOT


def _say(quiet: bool, level: int, message: str, *args: object) -> None:
    """`find()` 的提示语：`LocalReleases` 汇总时逐根静音，只由它打一行结论。"""
    if not quiet:
        log.log(level, message, *args)


@dataclass(frozen=True)
class LocalRun:
    """本地 release 里与证据包**同一次运行**的那份证据。"""

    log_path: str
    debug_dir: str
    vision_dir: str | None
    on_error_dir: str | None
    # 短，上图：10 行逐字相同、task 200000007、本机事件
    evidence: str
    matched_lines: int
    # 终端补充：本机面板日志里记着这个事件的文件名
    panel_log: str = ""

    def note_lines(self) -> list[str]:
        """图上用的两行：注记是逐行截断的，一行太长会把后半句切掉。"""
        return [
            f"本地 release 辅助：{os.path.basename(self.log_path)}",
            self.evidence,
        ]


@dataclass
class LocalRelease:
    """一个本地 release 安装目录（只读；目录不完整时 `discover()` 返回 None）。"""

    root: str
    _logs: list[str] | None = field(default=None, repr=False)
    _ranges: dict[str, tuple[datetime | None, datetime | None]] = field(
        default_factory=dict, repr=False
    )
    _events: dict[str, tuple[datetime | None, str] | None] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def discover(cls, root: str | None = None) -> LocalRelease | None:
        """按目录建一个；`debug/` 不存在或没有日志时返回 None（静默关闭）。"""
        base = os.path.abspath(os.path.expanduser(root or default_root()))
        if not os.path.isdir(os.path.join(base, "debug")):
            return None
        instance = cls(root=base)
        if not instance.logs():
            return None
        return instance

    def logs(self) -> list[str]:
        """`debug/` 下的本机日志（当前 + 轮转），按**内容**起始时间排序。"""
        if self._logs is None:
            debug_dir = os.path.join(self.root, "debug")
            try:
                names = sorted(os.listdir(debug_dir))
            except OSError:
                names = []
            paths = [
                os.path.join(debug_dir, name)
                for name in names
                if LOCAL_LOG_RE.match(name)
                and os.path.isfile(os.path.join(debug_dir, name))
            ]
            self._logs = sorted(paths, key=lambda p: self._start_ts(p) or datetime.min)
        return self._logs

    def time_range(self, path: str) -> tuple[datetime | None, datetime | None]:
        """日志的**内容**时间区间（读首/末行，不按文件名）。"""
        if path not in self._ranges:
            self._ranges[path] = (self._start_ts(path), self._end_ts(path))
        return self._ranges[path]

    @staticmethod
    def _start_ts(path: str) -> datetime | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for _ in range(3):
                    line = fh.readline()
                    if not line:
                        break
                    m = LINE_TS_RE.match(line)
                    if m:
                        return _parse_ts(m.group(1))
        except OSError:
            return None
        return None

    @staticmethod
    def _end_ts(path: str) -> datetime | None:
        """末行时间：从文件尾部按块回溯，找第一条带时间戳的**完整**行。"""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                chunk = 256 * 1024
                end = size
                while end > 0:
                    start = max(0, end - chunk)
                    fh.seek(start)
                    lines = fh.read(end - start).splitlines()
                    if start > 0 and lines:
                        lines = lines[1:]  # 第一段可能是半行
                    for raw in reversed(lines):
                        m = LINE_TS_RE.match(raw.decode("utf-8", "replace"))
                        if m:
                            return _parse_ts(m.group(1))
                    end = start
        except OSError:
            return None
        return None

    def find(
        self,
        case: Case,
        task_id: int | None = None,
        label: str = "",
        event_id: str = "",
        quiet: bool = False,
    ) -> LocalRun | None:
        """在本地 release 里找**同一次运行**的那份日志；认不出就返回 None（不猜）。

        `task_id` 给了就**只认**真有这个 task 的日志（`task_id_=<id>` / `"task_id":<id>` /
        `task_id=<id>`，后面不能再接数字）。原因：一条 trace 上常挂着好几个 issue（实测
        `PYTHON-1E` 与训练/委托那几个共用一条），包的日志切片里失败行属于**另一个**任务，
        按失败行锚定会挑到别的运行那一份，而真正跑过这个 task 的是轮转掉的另一份日志。

        因此失败行在"给了 task"时只当**优先**（同名候选里优先挑锚得上那份），不再是门槛：
        选定后若没有锚上行，会打一行说明。`task_id` 没给时仍然要求锚上失败行——
        那种情况下没有更强的身份信号，宁可不用。

        `quiet=True` 时不打"没对上"这类提示：`LocalReleases`（多个根）逐根扫的时候会问
        每一份日志，逐根打就是刷屏，它自己汇总成一行。
        """
        name = label or case.label
        fails, others = _needles(case)
        needles = fails + others
        if not needles:
            _say(
                quiet,
                logging.INFO,
                "提示：%s 的日志里没有可用于对账的独特行，本地 release 不参与（不猜）",
                name,
            )
            return None
        anchor = fails[-1] if fails else ""
        hint = self.panel_event(event_id) if event_id else None
        candidates = self.logs()
        if hint is None:
            # 没有事件号就只能用"内容区间是否盖住这个包"缩范围——只是省时间，
            # 判身份仍然是下面逐字对账；一份都不盖住时不缩，交给内容判定
            pack_span = _pack_span(case)
            covering = [
                path for path in candidates if _overlaps(self.time_range(path), pack_span)
            ]
            if covering:
                candidates = covering
        hits: list[tuple[bool, int, bool, str]] = []
        for path in candidates:
            count, task_ok, anchor_ok = _scan(path, needles, task_id, anchor)
            if task_id is not None:
                if not task_ok:
                    continue
            elif anchor and not anchor_ok:
                continue
            if count >= MIN_HITS or (count >= 1 and task_ok):
                hits.append((anchor_ok, count, task_ok, path))
        if not hits:
            if hint is not None:
                _say(
                    quiet,
                    logging.WARNING,
                    "提示：%s 的事件 %s… 记在本地面板日志（%s），但本地 debug 日志里没有这次"
                    "运行的记录（可能已被轮转覆盖），不参与辅助",
                    name,
                    (event_id or "")[:8],
                    hint[1],
                )
            elif task_id is not None:
                _say(
                    quiet,
                    logging.INFO,
                    "提示：%s 的日志行在本地 release 的 %d 份日志里都没有同时对上 task %s，"
                    "不参与辅助（不按时间猜）",
                    name,
                    len(candidates),
                    task_id,
                )
            else:
                _say(
                    quiet,
                    logging.INFO,
                    "提示：%s 的日志行在本地 release 的 %d 份日志里都没逐字对上，"
                    "不参与辅助（不按时间猜）",
                    name,
                    len(candidates),
                )
            return None
        hits.sort(key=lambda item: (item[0], item[2], item[1]), reverse=True)
        best = hits[0]
        if len(hits) > 1 and hits[1][:3] == best[:3]:
            _say(
                quiet,
                logging.WARNING,
                "本地 release 有 %d 份日志都与 %s 对上了同样多的行（%s），"
                "认不出是哪一次，不参与辅助",
                len(hits),
                name,
                "、".join(os.path.basename(hit[3]) for hit in hits[:4]),
            )
            return None
        _anchor_ok, count, task_ok, path = best
        if anchor and not _anchor_ok:
            _say(
                quiet,
                logging.INFO,
                "提示：%s 按 task %s 定位到 %s（包里的失败行不在这份里：它属于这条 trace 上的"
                "别的任务）",
                name,
                task_id,
                os.path.basename(path),
            )
        debug_dir = os.path.dirname(path)
        vision = os.path.join(debug_dir, "vision")
        on_error = os.path.join(debug_dir, "on_error")
        evidence = f"{count} 行逐字相同" + (f"、task {task_id}" if task_ok else "")
        if hint is not None:
            evidence += "、本机事件"
        return LocalRun(
            log_path=path,
            debug_dir=debug_dir,
            vision_dir=vision if os.path.isdir(vision) else None,
            on_error_dir=on_error if os.path.isdir(on_error) else None,
            evidence=evidence,
            matched_lines=count,
            panel_log=hint[1] if hint is not None else "",
        )

    def panel_event(self, event_id: str) -> tuple[datetime | None, str] | None:
        """本机面板日志（`logs/log-*.log`）里记着这个 event_id 吗？返回 `(时刻, 文件名)`。

        面板日志在失败时会打一行 `[Telemetry] 失败事件已加入发送队列：event_id=<32 位>`，
        只有**产生这个事件的那台机器**会打。所以它命中就等于"这次运行在本机"，
        比任何时间推断都强；没命中也不代表不是本机（面板日志会被轮转清掉）。
        """
        wanted = event_id.strip().lower()
        if not wanted:
            return None
        if wanted in self._events:
            return self._events[wanted]
        found: tuple[datetime | None, str] | None = None
        panel_dir = os.path.join(self.root, "logs")
        try:
            names = sorted(os.listdir(panel_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith(".log"):
                continue
            path = os.path.join(panel_dir, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        if "event_id=" not in raw:
                            continue
                        if f"event_id={wanted}" not in raw.lower():
                            continue
                        m = LINE_TS_RE.match(raw)
                        found = (_parse_ts(m.group(1)) if m else None, name)
                        break
            except OSError:
                continue
            if found is not None:
                break
        self._events[wanted] = found
        return found


@dataclass
class LocalReleases:
    """多个本地 release 根一起参与对账：`--local-release` 可重复，默认根永远兜底。

    一个根只装得下一台机器的 `debug/`，而"这次运行"的日志可能在另一个导出目录里（用户把
    几次导出的 `debug/` 各放一处）。根之间**没有先后**：仍然只按内容认；两个根装着同一份
    运行（文件名与对上的行数都一样）算同一份，各装一份不同的运行则都不采用（不猜）。
    """

    releases: list[LocalRelease] = field(default_factory=list)

    @classmethod
    def discover(cls, roots: list[str]) -> LocalReleases | None:
        """按给的根建一组；一个可用的都没有就返回 None（静默关闭）。"""
        found: list[LocalRelease] = []
        for root in roots:
            one = LocalRelease.discover(root)
            if one is None:
                log.info("提示：本地 release 根 %s 没有 debug/ 日志，跳过", root)
                continue
            if any(
                os.path.normcase(one.root) == os.path.normcase(item.root)
                for item in found
            ):
                continue
            found.append(one)
        return cls(releases=found) if found else None

    def logs(self) -> list[str]:
        """所有根的日志（按根的顺序拼接）。"""
        return [path for one in self.releases for path in one.logs()]

    def describe(self) -> str:
        return "、".join(one.root for one in self.releases)

    def panel_event(self, event_id: str) -> tuple[datetime | None, str] | None:
        """本机面板日志里记着这个事件吗：任一根命中就算（同一次运行只可能在一台机器上）。"""
        for one in self.releases:
            hit = one.panel_event(event_id)
            if hit is not None:
                return hit
        return None

    def find(
        self,
        case: Case,
        task_id: int | None = None,
        label: str = "",
        event_id: str = "",
    ) -> LocalRun | None:
        """在各根里找同一次运行：唯一命中才用；多个根给出不同的运行时不猜。"""
        name = label or case.label
        runs: list[LocalRun] = []
        for one in self.releases:
            run = one.find(
                case, task_id, label=label, event_id=event_id, quiet=True
            )
            if run is not None:
                runs.append(run)
        if not runs:
            log.info(
                "提示：%s 的日志行在本地 release 的 %d 个根 / %d 份日志里都没逐字对上，"
                "不参与辅助（不按时间猜）",
                name,
                len(self.releases),
                len(self.logs()),
            )
            return None
        if len(runs) > 1:
            same = {(os.path.basename(run.log_path), run.matched_lines) for run in runs}
            if len(same) > 1:
                log.warning(
                    "本地 release 有 %d 个根都认出了 %s、但不是同一份运行（%s），"
                    "认不出用哪个，不参与辅助",
                    len(runs),
                    name,
                    "、".join(os.path.basename(run.log_path) for run in runs),
                )
                return None
        return runs[0]


def _parse_ts(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _needle_line(raw: str) -> str:
    """统一行尾：证据包与本地日志可能一份 CRLF、一份 LF。"""
    return raw.rstrip("\r\n")


def _needles(case: Case) -> tuple[list[str], list[str]]:
    """证据包日志里的"独特行"：`(失败行, 其他独特行)`。

    失败行（`Task timeout` / `invalid node id` / `node not completed`）用来**锚定**是哪一份
    日志——一次运行可能跨日志轮转，失败那一段落在哪份里就用哪份；其他行（node done / reco）
    用来计数。只做文本挑选（不解析），按出现位置均匀取几行。
    """
    fails: list[str] = []
    others: list[str] = []
    try:
        for raw in _open_log_lines(case.log_source()):
            line = _needle_line(raw)
            if len(line) < MIN_NEEDLE_LEN or not LINE_TS_RE.match(line):
                continue
            if any(skip in line for skip in NEEDLE_SKIP):
                continue
            if not any(mark in line for mark in NEEDLE_MARKS):
                continue
            (fails if any(mark in line for mark in FAIL_MARKS) else others).append(line)
    except (OSError, TypeError, ValueError):
        return ([], [])

    def sample(lines: list[str]) -> list[str]:
        if len(lines) <= MAX_NEEDLES:
            return lines
        step = len(lines) / MAX_NEEDLES
        return [lines[int(i * step)] for i in range(MAX_NEEDLES)]

    return (sample(fails), sample(others))


def _pack_span(case: Case) -> tuple[datetime | None, datetime | None]:
    """证据包日志的首/末行时间（只用来缩小候选日志范围）。"""
    first: datetime | None = None
    last: datetime | None = None
    try:
        for raw in _open_log_lines(case.log_source()):
            m = LINE_TS_RE.match(_needle_line(raw))
            if not m:
                continue
            ts = _parse_ts(m.group(1))
            if ts is None:
                continue
            if first is None:
                first = ts
            last = ts
    except (OSError, TypeError, ValueError):
        return (None, None)
    return (first, last)


def _overlaps(
    log_range: tuple[datetime | None, datetime | None],
    pack_range: tuple[datetime | None, datetime | None],
) -> bool:
    lo, hi = log_range
    plo, phi = pack_range
    if plo is None or phi is None:
        return True
    if hi is not None and hi < plo:
        return False
    if lo is not None and lo > phi:
        return False
    return True


def _scan(
    path: str, needles: list[str], task_id: int | None, anchor: str = ""
) -> tuple[int, bool, bool]:
    """一份日志里：逐字对上几行、有没有这个 task、锚定的失败行在不在。

    整份读成文本再逐条 `in`：比逐行 Python 循环快一个量级（17 MB 的日志约 20 ms/次），
    而且对行尾的差异更宽容（本地行可能比证据包那份多一段后缀）。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return (0, False, False)
    found = sum(1 for needle in needles if needle in text)
    # 后面不能接数字：`task_id=2000000041` 里也含 `task_id=200000004`，不加边界会误判成有 task
    task_ok = task_id is None or re.search(
        rf'(?:task_id_=|"task_id":|task_id=){task_id}(?!\d)', text
    ) is not None
    anchor_ok = not anchor or anchor in text
    return (found, task_ok, anchor_ok)
