#!/usr/bin/env python3
"""把白板目录里的图片统一成 MaaFramework 的 log_dir 清理不会删的格式。

为什么需要：MaaFramework 的全局选项 log_dir（`MaaGlobalOption_LogDir`，绑定侧是
`maa.Global.log_dir = <目录>`）一设置，框架就把该目录当自己的 SaveDraw / 日志输出目录，
**异步递归删除其中的 `*.png` / `*.jpg` / `*.log`**。maa-tools 的 `check` 默认把这个目录
设成 `cwd`（`maatools.config.mts` 未写 `maaLogDir` 时 = 仓库根），所以随手跑一次
`npx @nekosu/maa-tools check` 就会删掉仓库里的图片（本机 2026-09-27 已发生过：
`assets/resource/image/**` 掉 62 张、`docs/白板/image/**` 掉 69 张，且不进回收站）。

白板图片位于被 .gitignore 忽略的 `docs/白板/` 下、没有版本历史，删了就找不回来，
所以这里把它们换成不在清理名单里的扩展名（默认 webp 无损，可用 --format jpeg）。

用法：
    python tools/whiteboard_images.py            # 转换 + 改写引用
    python tools/whiteboard_images.py --check    # 只报告，发现不安全格式即 exit 1
    python tools/whiteboard_images.py --dry-run  # 报告将要做什么，不改动
其他参数：
    --dir # 指定目录，默认 docs/白板
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# MaaFramework 5.14.0 实测：设置 log_dir 后消失的是这些扩展名，其余（.jpeg/.webp/.json/
# .txt/.md/.lnk 等）不受影响。名单随框架版本可能变化，换版本后请用 --check 复验。
DELETED_EXTS = {".png", ".jpg", ".log"}
SAFE_EXTS = {".webp", ".jpeg", ".gif", ".bmp", ".avif"}

# 会被改写引用的文本文件类型
TEXT_EXTS = {".canvas", ".md"}


def collect_targets(scan_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in scan_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in DELETED_EXTS
    )


def all_images(scan_dir: Path) -> list[Path]:
    """扫描范围内所有图片文件，用于统计。"""
    exts = DELETED_EXTS | SAFE_EXTS
    return sorted(
        p for p in scan_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts
    )


def convert(src: Path, dst: Path, fmt: str) -> None:
    from PIL import Image

    with Image.open(src) as im:
        im.load()
        if fmt == "webp":
            # method 越高越慢：6 在 1280×720 截图上约 15–20 s/张，4 约 1–3 s，体积差 <5%。
            im.save(dst, "WEBP", lossless=True, method=4)
        elif fmt == "jpeg":
            rgb = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            rgb.save(dst, "JPEG", quality=95, optimize=True)
        else:
            raise ValueError(f"unsupported format: {fmt}")

    # 落盘校验：文件存在、非空、能被解码，才允许删原件
    if not dst.is_file() or dst.stat().st_size == 0:
        raise RuntimeError(f"converted file missing or empty: {dst}")
    with Image.open(dst) as chk:
        chk.verify()


def rewrite_refs(scan_dir: Path, dry_run: bool) -> list[str]:
    """把引用里指向被清理格式的文件名换成实际存在的安全格式同名文件。

    不依赖"本次转换了哪些"：直接按磁盘上存在的安全格式文件，把它的 .png/.jpg/.log
    同名变体在文本里换掉。这样中途被打断、分几次转换也能自愈，可重复执行。

    引用改写与图片扫描用同一个目录边界（默认整棵 `docs/白板/`），子白板文件夹里的图
    与引用一并覆盖。
    """
    safe_files = [
        p for p in scan_dir.rglob("*") if p.is_file() and p.suffix.lower() in SAFE_EXTS
    ]
    substitutions: dict[str, str] = {}
    for safe in safe_files:
        for ext in DELETED_EXTS:
            substitutions[safe.stem + ext] = safe.name

    touched: list[str] = []
    for path in scan_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        original = text
        for old, new in substitutions.items():
            if old in text:
                text = text.replace(old, new)
        if text != original:
            touched.append(str(path))
            if not dry_run:
                path.write_text(text, encoding="utf-8")
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", default=None, help="vault 根目录（默认脚本所在仓库的根）"
    )
    parser.add_argument(
        "--dir",
        default="docs/白板",
        help="扫描范围，相对 --root 解析（默认整棵 docs/白板，含各子白板文件夹）",
    )
    parser.add_argument(
        "--format",
        default="webp",
        choices=["webp", "jpeg"],
        help="目标格式（默认 webp 无损）",
    )
    parser.add_argument(
        "--check", action="store_true", help="只报告，发现不安全格式就 exit 1"
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么")
    args = parser.parse_args()

    vault_root = (
        Path(args.root).resolve()
        if args.root
        else Path(__file__).resolve().parent.parent
    )
    scan_dir = (vault_root / args.dir).resolve()

    if not scan_dir.is_dir():
        print(f"scan dir not found: {scan_dir}")
        return 1

    targets = collect_targets(scan_dir)
    if not targets:
        print(
            f"OK: {scan_dir.relative_to(vault_root)} 下没有被 log_dir 清理的格式，共 {len(all_images(scan_dir))} 个图片文件"
        )
        return 0

    if args.check:
        print(
            f"发现 {len(targets)} 个会被删的图片格式（{', '.join(sorted(DELETED_EXTS))}）："
        )
        for p in targets:
            print(f"  {p.relative_to(vault_root)}")
        print("跑一次 python tools/whiteboard_images.py 即可转换。")
        return 1

    for src in targets:
        dst = src.with_suffix("." + args.format)
        if dst.exists() and src != dst:
            print(f"目标已存在，跳过转换: {dst.relative_to(vault_root)}")
            continue
        if args.dry_run:
            print(f"[dry-run] {src.relative_to(vault_root)} -> {dst.name}")
        else:
            convert(src, dst, args.format)
            src.unlink()
            print(
                f"{src.relative_to(vault_root)} -> {dst.name}  ({dst.stat().st_size} B)"
            )

    # 引用改写按磁盘现状做，不依赖本次转了哪些，可重复执行
    touched = rewrite_refs(scan_dir, args.dry_run)
    if touched:
        for t in touched:
            print(f"引用已更新: {t}")

    remaining = collect_targets(scan_dir)
    if remaining:
        print(f"仍有 {len(remaining)} 个不安全格式，未处理完。")
        return 1
    print(
        f"完成：{scan_dir.relative_to(vault_root)} 下已无 {', '.join(sorted(DELETED_EXTS))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
