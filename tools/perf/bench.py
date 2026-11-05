#!/usr/bin/env python3
"""单轮派发性能基准：用合成 pipeline 测三种情况的整条任务耗时，按 20/30/50 加权。

    python tools/perf/bench.py 0                 # 三种都跑，出完整时间
    python tools/perf/bench.py 1                 # 只测「必定命中且瞬间触发」
    python tools/perf/bench.py 2 3 --x 4         # 只测空转；模式 3 空转 4 轮
    python tools/perf/bench.py --selftest        # 只跑纯逻辑断言

不需要设备：`CustomController` 按序返回内存里的帧；资源与 pipeline 建在
`temp/perf/<时间戳>-<pid>/` 下，模板用 `Resource.override_image` 注入，不写图片文件。

三种模式用同一条两节点任务：

    BenchStart（DirectHit，next = BenchHit）
        → BenchHit（TemplateMatch）首轮命中 / 空转 k 轮后命中

口径：时间 = `Tasker.post_task()` 到 `job.wait()` 返回。含一次截图（numpy→ImageBuffer
拷贝）、一次 TemplateMatch、一次 DoNothing 动作与事件通知；不含资源加载、Tasker 绑定、
控制器连接、预热。默认把 `rate_limit` / `pre_delay` / `post_delay` 置 0，否则每个节点
的默认 1.4 s 延迟会把三种模式的差异完全淹没。

完整时间 = t1×20% + t2×30% + t3×50%（三项的中位数，只有 1/2/3 都测过才输出）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    from maa.controller import CustomController
    from maa.define import LoggingLevelEnum
    from maa.library import Library
    from maa.resource import Resource
    from maa.tasker import Tasker
except ImportError as exc:  # pragma: no cover - 环境缺依赖时给一行可读的报错
    raise SystemExit(f"需要 maafw（当前解释器 import maa 失败）：{exc}") from exc

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._logging import configure_tool_logging

log = logging.getLogger("tools.perf")

SIZE = (720, 1280)                       # (宽, 高)，对齐 interface.json 的 display_short_side
TEMPLATE_BOX = (100, 100, 140, 140)      # 模板在命中帧里的位置（x0, y0, x1, y1）
TEMPLATE_ROI = (0, 0, 200, 200)          # 模板匹配的 ROI
TEMPLATE_SIZE = 40
THRESHOLD = 0.8
TEMPLATE_NAME = "bench_hit.png"
NODE_START = "BenchStart"
NODE_HIT = "BenchHit"

DEFAULT_X = 2
DEFAULT_REPEAT = 5
WEIGHTS = {1: 0.20, 2: 0.30, 3: 0.50}


# ---------------------------------------------------------------- 纯逻辑（可自检）


def expand_modes(raw: Sequence[str]) -> list[int]:
    """把位置参数展开成去重、升序的模式列表；`0` 表示全部（1/2/3）。"""
    modes: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError(f"模式必须是 0/1/2/3 的数字：{item!r}") from exc
        if value == 0:
            return [1, 2, 3]
        if value not in (1, 2, 3):
            raise ValueError(f"模式只能是 0/1/2/3：{value}")
        modes.add(value)
    return sorted(modes) or [1, 2, 3]


def idle_rounds(mode: int, x: int) -> int:
    """模式对应的空转轮数：1 → 0，2 → 1，3 → x。"""
    if mode == 1:
        return 0
    if mode == 2:
        return 1
    if mode == 3:
        return x
    raise ValueError(f"未知模式：{mode}")


def mode_label(mode: int, x: int) -> str:
    if mode == 1:
        return "必定命中且瞬间触发"
    if mode == 2:
        return "next 空转 1 轮"
    if mode == 3:
        return f"next 空转 {x} 轮"
    raise ValueError(f"未知模式：{mode}")


def frame_count(k: int) -> int:
    """一次任务消耗的截图数：第一张给 BenchStart，之后 k 张空白 + 1 张命中。"""
    return k + 2


def frame_sequence(blank: np.ndarray, hit: np.ndarray, k: int) -> list[np.ndarray]:
    """按 screencap 顺序排好的帧：空白 ×（k+1）再命中。"""
    return [blank] * (k + 1) + [hit]


def weighted(t1: float, t2: float, t3: float) -> float:
    return t1 * WEIGHTS[1] + t2 * WEIGHTS[2] + t3 * WEIGHTS[3]


def per_round(t1: float, t3: float, x: int) -> float | None:
    """x 轮空转摊到每轮的平均增量；x <= 0 时没有这个量。"""
    if x <= 0:
        return None
    return (t3 - t1) / x


def timeout_ms(k: int, rate_limit: int) -> int:
    """派发者上的兜底超时：够跑完 k 轮，但模板失效时不用等默认的 20 s。"""
    return max(3000, (k + 2) * max(rate_limit, 100) + 3000)


# ---------------------------------------------------------------- 合成材料


def make_frames() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """全白底图、命中图与 40×40 双色模板（模板必须有结构，纯色会让 NCC 退化）。"""
    width, height = SIZE
    blank = np.full((height, width, 3), 255, dtype=np.uint8)

    template = np.full((TEMPLATE_SIZE, TEMPLATE_SIZE, 3), 255, dtype=np.uint8)
    template[:, : TEMPLATE_SIZE // 2] = (0, 0, 255)      # BGR 红
    template[:, TEMPLATE_SIZE // 2 :] = (0, 255, 255)    # BGR 黄

    hit = blank.copy()
    x0, y0, x1, y1 = TEMPLATE_BOX
    hit[y0:y1, x0:x1] = template
    return blank, hit, template


def write_resource(
    work: Path,
    *,
    rate_limit: int,
    pre_delay: int,
    post_delay: int,
    timeout: int,
) -> Path:
    res = work / "res"
    (res / "pipeline").mkdir(parents=True, exist_ok=True)
    (res / "image").mkdir(parents=True, exist_ok=True)

    (res / "default_pipeline.json").write_text(
        json.dumps(
            {
                "Default": {
                    "rate_limit": rate_limit,
                    "pre_delay": pre_delay,
                    "post_delay": post_delay,
                }
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )
    (res / "pipeline" / "bench.json").write_text(
        json.dumps(
            {
                NODE_START: {
                    "recognition": "DirectHit",
                    "next": [NODE_HIT],
                    "timeout": timeout,
                },
                NODE_HIT: {
                    "recognition": "TemplateMatch",
                    "template": [TEMPLATE_NAME],
                    "roi": list(TEMPLATE_ROI),
                    "threshold": THRESHOLD,
                },
            },
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )
    return res


class FrameController(CustomController):
    """按序返回内存帧；用尽后停在最后一帧。输入类回调只返回成功。"""

    def __init__(self, frames: list[np.ndarray]) -> None:
        super().__init__()
        self._frames = frames
        self.calls = 0

    def connect(self) -> bool:
        return True

    def request_uuid(self) -> str:
        return "perf-bench"

    def screencap(self) -> np.ndarray:
        index = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return self._frames[index]

    def click(self, x: int, y: int) -> bool:
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        return True

    def touch_down(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_move(self, contact: int, x: int, y: int, pressure: int) -> bool:
        return True

    def touch_up(self, contact: int) -> bool:
        return True

    def click_key(self, keycode: int) -> bool:
        return True

    def input_text(self, text: str) -> bool:
        return True

    def key_down(self, keycode: int) -> bool:
        return True

    def key_up(self, keycode: int) -> bool:
        return True

    def start_app(self, intent: str) -> bool:
        return True

    def stop_app(self, intent: str) -> bool:
        return True


# ---------------------------------------------------------------- 测量


@dataclass
class ModeResult:
    mode: int
    label: str
    idle_rounds: int
    median_ms: float
    min_ms: float
    max_ms: float
    runs_ms: list[float]
    screencaps: int


def prepare(
    work: Path,
    *,
    rate_limit: int,
    pre_delay: int,
    post_delay: int,
    timeout: int,
) -> tuple[Resource, Tasker, np.ndarray, np.ndarray]:
    res_dir = write_resource(
        work,
        rate_limit=rate_limit,
        pre_delay=pre_delay,
        post_delay=post_delay,
        timeout=timeout,
    )
    log_dir = work / "maafw"
    log_dir.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(log_dir)
    Tasker.set_stdout_level(LoggingLevelEnum.Off)

    blank, hit, template = make_frames()
    resource = Resource()
    if not resource.override_image(TEMPLATE_NAME, template):
        raise RuntimeError("override_image 失败")
    if not resource.post_bundle(res_dir).wait().succeeded:
        raise RuntimeError(f"资源加载失败：{res_dir}")
    missing = {NODE_START, NODE_HIT} - set(resource.node_list)
    if missing:
        raise RuntimeError(f"资源里缺节点：{'、'.join(sorted(missing))}")

    tasker = Tasker()
    return resource, tasker, blank, hit


def run_once(
    tasker: Tasker,
    resource: Resource,
    blank: np.ndarray,
    hit: np.ndarray,
    k: int,
) -> tuple[float, int]:
    controller = FrameController(frame_sequence(blank, hit, k))
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("控制器连接失败")
    if not tasker.bind(resource, controller):
        raise RuntimeError("Tasker.bind 失败")

    start = time.perf_counter()
    status = tasker.post_task(NODE_START).wait()
    elapsed_ms = (time.perf_counter() - start) * 1000

    if not status.succeeded:
        raise RuntimeError(
            f"任务失败（k={k}，截图 {controller.calls} 次）；看产物目录的 maafw/maafw.log"
        )
    expected = frame_count(k)
    if controller.calls < expected:
        raise RuntimeError(
            f"截图只有 {controller.calls} 次，少于预期的 {expected} 次：空白帧被误判命中，"
            "本次计时无效"
        )
    return elapsed_ms, controller.calls


def measure_mode(
    tasker: Tasker,
    resource: Resource,
    blank: np.ndarray,
    hit: np.ndarray,
    *,
    mode: int,
    x: int,
    repeat: int,
) -> ModeResult:
    k = idle_rounds(mode, x)
    run_once(tasker, resource, blank, hit, k)  # 预热：模板/线程池/事件缓存

    times: list[float] = []
    counts: list[int] = []
    for _ in range(repeat):
        elapsed, calls = run_once(tasker, resource, blank, hit, k)
        times.append(elapsed)
        counts.append(calls)
    if len(set(counts)) > 1:
        log.warning("模式 %d 的截图次数不一致：%s", mode, counts)

    return ModeResult(
        mode=mode,
        label=mode_label(mode, x),
        idle_rounds=k,
        median_ms=round(statistics.median(times), 3),
        min_ms=round(min(times), 3),
        max_ms=round(max(times), 3),
        runs_ms=[round(value, 3) for value in times],
        screencaps=counts[-1],
    )


# ---------------------------------------------------------------- 报告


def display_path(path: Path) -> str:
    try:
        return f"{path.relative_to(REPO_ROOT).as_posix()}/"
    except ValueError:
        return f"{path.as_posix()}/"


def print_report(
    header: str,
    results: list[ModeResult],
    *,
    x: int,
    work: Path,
) -> dict[str, float | None]:
    print(header)
    print()
    for result in results:
        print(
            f"模式 {result.mode}  {result.label}  "
            f"t{result.mode} = {result.median_ms:6.1f} ms  "
            f"[{result.min_ms:.1f}, {result.max_ms:.1f}]  "
            f"n={len(result.runs_ms)}  帧 {result.screencaps}"
        )
    print()

    by_mode = {result.mode: result.median_ms for result in results}
    parts: list[str] = []
    one_round: float | None = None
    x_round: float | None = None
    if 1 in by_mode and 2 in by_mode:
        one_round = by_mode[2] - by_mode[1]
        parts.append(f"t2 − t1 = {one_round:.1f} ms")
    if 1 in by_mode and 3 in by_mode and x > 0:
        delta = by_mode[3] - by_mode[1]
        x_round = per_round(by_mode[1], by_mode[3], x)
        parts.append(f"t3 − t1 = {delta:.1f} ms ÷ {x} = {x_round:.1f} ms/轮")
    if parts:
        print("单轮空转：" + "；".join(parts))

    if all(mode in by_mode for mode in (1, 2, 3)):
        total = weighted(by_mode[1], by_mode[2], by_mode[3])
        print(
            f"完整时间 = t1×20% + t2×30% + t3×50% = {total:.1f} ms"
        )
    else:
        missing = "、".join(str(mode) for mode in (1, 2, 3) if mode not in by_mode)
        print(f"完整时间：缺模式 {missing}，不算（用 0 或 1 2 3 一起跑）")

    print(f"产物：{display_path(work)}")
    return {"one_round": one_round, "per_round": x_round}


def write_json(
    work: Path,
    *,
    maafw: str,
    rate_limit: int,
    pre_delay: int,
    post_delay: int,
    x: int,
    repeat: int,
    results: list[ModeResult],
    per_round_ms: dict[str, float | None],
) -> Path:
    by_mode = {result.mode: result.median_ms for result in results}
    total = (
        weighted(by_mode[1], by_mode[2], by_mode[3])
        if all(mode in by_mode for mode in (1, 2, 3))
        else None
    )
    payload = {
        "tool": "tools/perf/bench.py",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "maafw": maafw,
        "size": list(SIZE),
        "delays_ms": {
            "rate_limit": rate_limit,
            "pre_delay": pre_delay,
            "post_delay": post_delay,
        },
        "x": x,
        "repeat": repeat,
        "modes": [asdict(result) for result in results],
        "per_round_ms": {
            key: (round(value, 3) if value is not None else None)
            for key, value in per_round_ms.items()
        },
        "weighted_ms": round(total, 3) if total is not None else None,
    }
    path = work / "bench.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------- 自检


def run_selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def check(name: str, condition: bool) -> None:
        checks.append((name, condition))

    check("模式 0 展开为全部", expand_modes(["0"]) == [1, 2, 3])
    check("空参数默认全部", expand_modes([]) == [1, 2, 3])
    check("去重并升序", expand_modes(["3", "1", "1"]) == [1, 3])
    check("0 压过其它模式", expand_modes(["2", "0"]) == [1, 2, 3])
    try:
        expand_modes(["4"])
        check("非法模式报错", False)
    except ValueError:
        check("非法模式报错", True)

    check("模式 1 空转 0 轮", idle_rounds(1, 2) == 0)
    check("模式 2 空转 1 轮", idle_rounds(2, 2) == 1)
    check("模式 3 空转 x 轮", idle_rounds(3, 2) == 2)
    check("模式 3 的 x=0", idle_rounds(3, 0) == 0)
    check("帧数 = k+2", frame_count(0) == 2 and frame_count(3) == 5)

    blank = np.zeros((1, 1, 3), dtype=np.uint8)
    hit = np.ones((1, 1, 3), dtype=np.uint8)
    sequence = frame_sequence(blank, hit, 2)
    check("帧序列长度", len(sequence) == 4)
    check("帧序列末帧命中", sequence[-1] is hit)
    check("帧序列前面全空白", all(item is blank for item in sequence[:-1]))

    check("20/30/50 加权", abs(weighted(10.0, 20.0, 30.0) - 23.0) < 1e-9)
    check("x 轮摊每轮", per_round(10.0, 50.0, 2) == 20.0)
    check("x=0 不摊", per_round(10.0, 10.0, 0) is None)
    check("超时下限", timeout_ms(0, 0) >= 3000)
    check("超时随 k 增", timeout_ms(4, 0) > timeout_ms(1, 0))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'}  {name}")
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 1


# ---------------------------------------------------------------- 入口


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="单轮派发性能基准：合成 pipeline + 内存帧，不需要设备",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "模式：0 = 全部；1 = 必定命中且瞬间触发；2 = next 空转 1 轮；\n"
            "      3 = next 空转 x 轮（--x，默认 2）\n"
            "完整时间 = t1×20% + t2×30% + t3×50%（三项中位数，1/2/3 都测过才算）"
        ),
    )
    parser.add_argument(
        "modes",
        nargs="*",
        default=["0"],
        help="要跑的模式，可给多个；0 = 全部（默认）",
    )
    parser.add_argument("--x", type=int, default=DEFAULT_X, help="模式 3 的空转轮数（默认 2）")
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help=f"每个模式的测量次数（默认 {DEFAULT_REPEAT}；另加 1 次预热）",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=0,
        help="合成 Default.rate_limit，单位 ms（默认 0）",
    )
    parser.add_argument(
        "--pre-delay",
        type=int,
        default=0,
        help="合成 Default.pre_delay，单位 ms（默认 0）",
    )
    parser.add_argument(
        "--post-delay",
        type=int,
        default=0,
        help="合成 Default.post_delay，单位 ms（默认 0）",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="资源与产物的落点（默认 temp/perf/<时间戳>-<pid>）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="工具日志级别（默认 INFO）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="工具日志文件（默认 temp/logs/perf_<时间戳>_<pid>.log）；传 none 只走控制台",
    )
    parser.add_argument("--selftest", action="store_true", help="只跑纯逻辑自检")
    return parser


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = create_argument_parser(prog).parse_args(argv)
    if args.selftest:
        return run_selftest()

    _, log_path = configure_tool_logging(
        "perf", log_file=args.log_file, level=args.log_level
    )
    if log_path:
        log.info("日志 → %s", log_path)

    try:
        modes = expand_modes(args.modes)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if args.x < 0:
        log.error("--x 必须 >= 0（收到 %d）", args.x)
        return 2
    if args.repeat < 1:
        log.error("--repeat 必须 >= 1（收到 %d）", args.repeat)
        return 2
    for name in ("rate_limit", "pre_delay", "post_delay"):
        if getattr(args, name) < 0:
            log.error("--%s 必须 >= 0", name.replace("_", "-"))
            return 2

    work = (
        Path(args.work_dir)
        if args.work_dir
        else REPO_ROOT / "temp" / "perf" / f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
    )
    work.mkdir(parents=True, exist_ok=True)

    maafw = "?"
    with contextlib.suppress(Exception):
        maafw = Library.version()

    header = (
        f"MaaFW {maafw} · {SIZE[0]}×{SIZE[1]} · "
        f"rate_limit={args.rate_limit} pre_delay={args.pre_delay} "
        f"post_delay={args.post_delay} · x={args.x} · "
        f"重复 {args.repeat} 次（中位数）"
    )

    max_k = max(idle_rounds(mode, args.x) for mode in modes)
    try:
        resource, tasker, blank, hit = prepare(
            work,
            rate_limit=args.rate_limit,
            pre_delay=args.pre_delay,
            post_delay=args.post_delay,
            timeout=timeout_ms(max_k, args.rate_limit),
        )
        results: list[ModeResult] = []
        for mode in modes:
            result = measure_mode(
                tasker,
                resource,
                blank,
                hit,
                mode=mode,
                x=args.x,
                repeat=args.repeat,
            )
            results.append(result)
            log.info("模式 %d 完成：中位数 %.1f ms", mode, result.median_ms)
    except (RuntimeError, OSError) as exc:
        log.error("%s", exc)
        log.error("产物：%s", display_path(work))
        return 1

    if args.x == 0 and 3 in modes:
        print("提示：--x 0 时模式 3 与模式 1 等价，t3 会与 t1 相同。\n")

    by_mode = {result.mode: result.median_ms for result in results}
    if 1 in by_mode and 2 in by_mode and by_mode[2] <= by_mode[1]:
        log.warning("t2 不大于 t1（机器有负载或预热不足），先看 min/max 再下结论")

    per_round_ms = print_report(header, results, x=args.x, work=work)
    path = write_json(
        work,
        maafw=maafw,
        rate_limit=args.rate_limit,
        pre_delay=args.pre_delay,
        post_delay=args.post_delay,
        x=args.x,
        repeat=args.repeat,
        results=results,
        per_round_ms=per_round_ms,
    )
    log.info("JSON → %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
