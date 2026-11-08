"""清理工具：pytest 缓存目录与 `temp/` 里过期的东西。

    python tools/cleanup/cli.py pytest [--root DIR] [--dry-run]
    python tools/cleanup/cli.py temp [--root DIR] [--days N] [--ignore NAME]... [--dry-run]
    python tools/cleanup/cli.py all [--dry-run]

`pytest` 删仓库里的 `.pytest_cache` / `pytest-cache-files-*`（为什么会有见 `lib/pytest_cache.py`）。
`temp` 删 `temp/` 下超过 N 天（默认 7）的文件，再清掉空目录；忽略目录的默认值与来源见
`lib/config.py`，可用 `tools/cleanup/config.json` 或 `--ignore` 覆盖。
`all` = 两者依次跑，`.vscode/tasks.json` 调的是它。

退出码：0 = 没有可删的或全部删掉；1 = 有东西删不掉（输出里有原因与提权修法）；2 = 参数/根目录不对。
"""

from __future__ import annotations

import argparse
import errno
import io
import sys
from pathlib import Path

try:  # 作为包导入（tools.cleanup.cli）
    from .lib import config, pytest_cache, temp_clean
except ImportError:  # 直接跑脚本：python tools/cleanup/cli.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib import config, pytest_cache, temp_clean  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMP = REPO_ROOT / "temp"


def _is_permission_error(exc: OSError) -> bool:
    """拒绝访问类错误：ACL 不归当前用户，普通清理改不动，要靠提权收回。

    Windows 上按 `winerror` 判：5 = 拒绝访问；32（文件被占用）也是 OSError，
    但那是"关掉占用的程序再来一次"就能解决的事，不该给提权建议。
    """
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror == 5
    return exc.errno in (errno.EACCES, errno.EPERM)


def _print_failures(failed: list[tuple[Path, OSError]], total_ok: int) -> None:
    print(f"\n{len(failed)} 个删不掉（{total_ok} 个已处理）：")
    for rel, exc in failed:
        print(f"  {rel}：{exc}")
    if any(_is_permission_error(exc) for _, exc in failed):
        print(
            "\n上面报「拒绝访问」的那些，ACL 不归当前用户（多见于沙箱或别的账户跑过），"
            "普通清理改不动。在管理员 PowerShell 里对每个目录执行一次即可收回：\n"
            '  takeown /f "<目录>" /r /d y ; icacls "<目录>" /reset /t /c'
        )


def cmd_pytest(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else REPO_ROOT
    if not root.is_dir():
        print(f"扫描起点不是目录：{root}")
        return 2
    removed, failed = pytest_cache.clean(root, args.dry_run)
    if not removed and not failed:
        print(f"没有 pytest 缓存目录：{root}")
        return 0
    for rel in removed:
        print(f"{'将删除' if args.dry_run else '已删除'} {rel}")
    if args.dry_run:
        print(f"共 {len(removed)} 个；去掉 --dry-run 再跑一次才真的删")
        return 0
    if failed:
        _print_failures(failed, len(removed))
        return 1
    print(f"共删除 {len(removed)} 个 pytest 缓存目录")
    return 0


def cmd_temp(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else DEFAULT_TEMP
    if not root.is_dir():
        print(f"要清理的目录不存在：{root}")
        return 2
    default_days, default_ignore = config.load_config()
    days = args.days if args.days is not None else default_days
    ignore = set(default_ignore) | set(args.ignore)
    report = temp_clean.clean(root, days, ignore, args.dry_run)
    action = "将删除" if args.dry_run else "已删除"
    for rel in report.removed_files:
        print(f"{action} 文件 {rel}")
    for rel in report.removed_dirs:
        print(f"{action} 空目录 {rel}")
    print(
        f"{action} {len(report.removed_files)} 个文件（{report.freed_text}）、"
        f"{len(report.removed_dirs)} 个空目录；保留 {days} 天；"
        f"忽略：{'、'.join(sorted(ignore))}"
    )
    if args.dry_run:
        print("去掉 --dry-run 再跑一次才真的删")
    if report.failed:
        _print_failures(report.failed, len(report.removed_files))
        return 1
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    days, ignores = config.load_config()
    code = 0
    action = "将删除" if args.dry_run else "已删除"
    if DEFAULT_TEMP.is_dir():
        report = temp_clean.clean(DEFAULT_TEMP, days, set(ignores), args.dry_run)
        print(
            f"temp：{action} {len(report.removed_files)} 个文件（{report.freed_text}）、"
            f"{len(report.removed_dirs)} 个空目录"
        )
        if report.failed:
            _print_failures(report.failed, len(report.removed_files))
            code = 1
    removed, failed = pytest_cache.clean(REPO_ROOT, args.dry_run)
    print(f"pytest：{action} {len(removed)} 个缓存目录")
    if failed:
        _print_failures(failed, len(removed))
        code = 1
    if args.dry_run:
        print("去掉 --dry-run 再跑一次才真的删")
    return code


def create_argument_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="清理 pytest 缓存目录，以及 temp/ 下超过保留期的文件与空目录。",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    pytest_parser = sub.add_parser("pytest", help="删 .pytest_cache / pytest-cache-files-*")
    pytest_parser.add_argument("--root", default=None, help=f"扫描起点（默认 {REPO_ROOT}）")
    pytest_parser.add_argument("--dry-run", action="store_true", help="只列出，不删")
    pytest_parser.set_defaults(func=cmd_pytest)

    temp_parser = sub.add_parser("temp", help="删 temp/ 下超过保留期的文件与空目录")
    temp_parser.add_argument(
        "--root", default=None, help=f"要清理的目录（默认 {DEFAULT_TEMP}）"
    )
    temp_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"保留天数（默认取 config.json，缺省 {config.DEFAULT_DAYS}）",
    )
    temp_parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="NAME",
        help="额外忽略的目录名（可重复；默认忽略项见 config.json）",
    )
    temp_parser.add_argument("--dry-run", action="store_true", help="只列出，不删")
    temp_parser.set_defaults(func=cmd_temp)

    all_parser = sub.add_parser("all", help="先清 temp 过期文件，再删 pytest 缓存")
    all_parser.add_argument("--dry-run", action="store_true", help="只列出，不删")
    all_parser.set_defaults(func=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    parser = create_argument_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    if getattr(args, "days", None) is not None and args.days < 1:
        print("--days 必须 >= 1")
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
