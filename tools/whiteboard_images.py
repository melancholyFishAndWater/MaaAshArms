#!/usr/bin/env python3
"""把白板目录里的图片统一成指定格式（默认 png），并改写 .canvas / .md 里的引用。

为什么默认 png：本仓库用的 VS Code 插件只认 png。
历史背景：曾经把白板图统一成 webp/jpeg，是为了躲开 MaaFramework 的 log_dir 清理——设置
log_dir 会递归删掉该目录下 mtime 超过 7 天的 `*.png` / `*.jpg` / `*.log`（MaaUtils
`source/Logger/Logger.cpp` 的 `remove_old_files`，`kMaxAge = 24*7h`），而 maa-tools 1.0.24
及以前把 log_dir 默认设成 `cwd`（= 仓库根）。maatools 1.0.25 起默认改成 `<cwd>/debug`，
本仓库已钉 1.1.3，所以仓库里的 png 不再被它影响。

残留风险：只要有人把 `maaLogDir` 写成 `"."`、或手跑旧版 maa-tools、或写调试脚本时把
`maa.Global.log_dir` 指到仓库根，7 天以上的 png 仍会被删；而 `docs/白板` 在 .gitignore 里，
删了没有版本历史可恢复。真要换回去用 `--format webp`（无损，体积约为 png 的 1/2–1/4）。

用法：
    python tools/whiteboard_images.py            # 转换 + 改写引用（输出 png）
    python tools/whiteboard_images.py --check    # 只报告，发现非目标格式即 exit 1
    python tools/whiteboard_images.py --dry-run  # 报告将要做什么，不改动
其他参数：
    --dir      # 扫描范围，默认 docs/白板（含各子白板文件夹）
    --format   # 目标格式：png（默认）/ webp / jpeg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 会被当成图片处理的扩展名（收集候选、统计、改写引用都用它）
ALL_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
    ".tif",
    ".tiff",
}

# 目标格式 -> 它接受的扩展名。按「族」判定：jpeg 族里 .jpg 与 .jpeg 等价，
# 所以选 jpeg 时不会把已有的 .jpg 无意义地改名成 .jpeg。
FORMAT_EXTS = {
    "png": {".png"},
    "webp": {".webp"},
    "jpeg": {".jpg", ".jpeg"},
}

# 非族内文件转换时写出的后缀
CANONICAL_EXT = {"png": ".png", "webp": ".webp", "jpeg": ".jpeg"}

# 会被改写引用的文本文件类型
TEXT_EXTS = {".canvas", ".md"}


def all_images(scan_dir: Path) -> list[Path]:
    return sorted(
        p for p in scan_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALL_EXTS
    )


def collect_targets(scan_dir: Path, fmt: str) -> list[Path]:
    """收集需要转换的图片：族内已合规的不动，其余全转。"""
    accepted = FORMAT_EXTS[fmt]
    return [p for p in all_images(scan_dir) if p.suffix.lower() not in accepted]


def convert(src: Path, dst: Path, fmt: str) -> None:
    from PIL import Image

    with Image.open(src) as im:
        im.load()
        if fmt == "png":
            # PNG 不支持 CMYK，遇到就转 RGB（白板截图不会是 CMYK，纯属兜底）
            if im.mode == "CMYK":
                im = im.convert("RGB")
            im.save(dst, "PNG", optimize=True)
        elif fmt == "webp":
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


def rewrite_refs(scan_dir: Path, fmt: str, dry_run: bool) -> list[str]:
    """把引用里的图片文件名改成该图片在磁盘上的真实文件名。

    不依赖"本次转换了哪些"：直接看磁盘现状，把同一个 stem + 任意图片扩展名的写法
    换成真实文件名。中途被打断、分几次转换也能自愈，可重复执行。
    同一个 stem 存在多份时优先选目标格式那份（否则按路径排序取第一份），保证结果确定。

    引用改写与图片扫描用同一个目录边界（默认整棵 `docs/白板/`），子白板文件夹里的图
    与引用一并覆盖。
    """
    by_stem: dict[str, list[Path]] = {}
    for path in all_images(scan_dir):
        by_stem.setdefault(path.stem, []).append(path)

    accepted = FORMAT_EXTS[fmt]
    substitutions: dict[str, str] = {}
    for stem, paths in by_stem.items():
        preferred = sorted(p for p in paths if p.suffix.lower() in accepted) or sorted(
            paths
        )
        for ext in ALL_EXTS:
            substitutions[stem + ext] = preferred[0].name

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
        default="png",
        choices=sorted(FORMAT_EXTS),
        help="目标格式（默认 png）",
    )
    parser.add_argument(
        "--check", action="store_true", help="只报告，发现非目标格式就 exit 1"
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

    images = all_images(scan_dir)
    targets = collect_targets(scan_dir, args.format)

    if not targets:
        print(
            f"OK: {scan_dir.relative_to(vault_root)} 下 {len(images)} 个图片文件都已是 {args.format}"
        )
        return 0

    if args.check:
        print(f"发现 {len(targets)} 个非 {args.format} 的图片：")
        for p in targets:
            print(f"  {p.relative_to(vault_root)}")
        print(
            f"跑一次 python tools/whiteboard_images.py --format {args.format} 即可转换。"
        )
        return 1

    size_before = 0
    size_after = 0
    converted = 0
    for src in targets:
        dst = src.with_suffix(CANONICAL_EXT[args.format])
        if dst.exists() and src != dst:
            print(f"目标已存在，跳过转换: {dst.relative_to(vault_root)}")
            continue
        if args.dry_run:
            print(f"[dry-run] {src.relative_to(vault_root)} -> {dst.name}")
            continue
        convert(src, dst, args.format)
        size_before += src.stat().st_size
        src.unlink()
        size_after += dst.stat().st_size
        converted += 1
        print(f"{src.relative_to(vault_root)} -> {dst.name}  ({dst.stat().st_size} B)")

    if not args.dry_run and converted:
        print(
            f"转换 {converted} 个：{size_before / 1048576:.1f} MB -> {size_after / 1048576:.1f} MB"
        )

    # 引用改写按磁盘现状做，不依赖本次转了哪些，可重复执行
    for t in rewrite_refs(scan_dir, args.format, args.dry_run):
        print(f"引用已更新: {t}")

    remaining = collect_targets(scan_dir, args.format)
    if remaining:
        print(f"仍有 {len(remaining)} 个非 {args.format} 的图片，未处理完。")
        return 1
    print(
        f"完成：{scan_dir.relative_to(vault_root)} 下 {len(all_images(scan_dir))} 个图片文件都已是 {args.format}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
