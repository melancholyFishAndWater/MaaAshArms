// 对资源图片做无损压缩：只重编码压缩流与去元数据，**不改像素、不改文件格式**。
//
// 为什么需要它：assets/resource/image 下的 PNG 是识别模板，会随发布包与资源更新一起分发，
// 字节数直接进包体积；而"无损压 PNG"没有一次性的开关——要逐文件跑、要对比前后、
// 要知道哪些文件其实没变（避免反复改写已压过的文件、污染 git 历史）。
//
// 为什么两遍都带 `--nc --np --ng`：oxipng 默认做有损的"颜色空间精简"——alpha 全不透明就
// 把 RGBA 降成 RGB，全灰的图降成灰度，能索引化就转 PLTE。模板的 colorType 交给框架处理，
// 压缩工具不该动它；带上这三个开关后 `git diff` 里只有 IDAT 变，解压后的像素与调色板逐字节
// 相同（实测 3 个样本省 44%–49%，不加开关是 55%–67%，差的那部分就是被砍掉的通道）。
// 注意 `-a`（--alpha）不是这个用途：它是"允许改全透明像素的颜色值"的额外有损优化。
//
// 依赖外部 oxipng：https://github.com/shssoichiro/oxipng
//
// 用法：
//   npm run optimize:images                      # 默认压 assets/resource/image
//   node tools/optimize-images.mjs --dry-run     # 只列出会被处理的文件
//   node tools/optimize-images.mjs <路径...>     # 指定文件或目录
//   OPTIMIZE_IMAGE_PATHS=a.png,b/ node tools/optimize-images.mjs
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { relative, resolve } from "node:path";

const DEFAULT_TARGETS = ["assets/resource/image"];

const EXCLUDED_DIRECTORIES = new Set([
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".temp",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "out",
    "release-assets",
    "vend",
]);

try {
    main();
} catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
}

function main() {
    if (process.argv.includes("--help") || process.argv.includes("-h")) {
        printUsage();
        return;
    }

    const targets = resolveTargets();
    // 先确认目标存在，再检查 oxipng：目标写错时报错才指得准。
    const files = collectPngFiles(targets);

    if (process.argv.includes("--dry-run")) {
        for (const file of files) console.log(displayPath(file));
        console.log(`\n${files.length} PNG image(s) would be processed.`);
        return;
    }

    ensureOxipng();
    if (files.length === 0) {
        console.log("No PNG images found.");
        return;
    }

    let changed = 0;
    let totalBefore = 0;
    let totalAfter = 0;

    for (const file of files) {
        const beforeHash = sha256(file);
        const beforeSize = statSync(file).size;
        // `--nc --np --ng`：禁止 oxipng 改 colorType / 调色板 / 灰度。默认行为会把"alpha 全不
        // 透明"的 RGBA 降成 RGB、把全灰的图降成灰度（实测 65/65 张都被改，其中两张变成 PLTE
        // 或灰度）——那是识别模板的文件格式，不该由压缩工具决定。加上这三个后只重编码压缩流，
        // 解压后的像素与调色板逐字节不变。
        runOxipng(["-o", "max", "--fast", "-Z", "-s", "--nc", "--np", "--ng", file]);
        runOxipng(["-o", "2", "-s", "--nc", "--np", "--ng", file]);
        const afterSize = statSync(file).size;
        const afterHash = sha256(file);

        totalBefore += beforeSize;
        totalAfter += afterSize;
        if (beforeHash !== afterHash) {
            changed += 1;
            console.log(`${displayPath(file)}: ${formatBytes(beforeSize)} -> ${formatBytes(afterSize)}`);
        }
    }

    console.log(
        `Optimized ${changed}/${files.length} PNG image(s), saved ${formatBytes(totalBefore - totalAfter)}.`,
    );
}

function printUsage() {
    console.log(`Usage: node tools/optimize-images.mjs [--dry-run] [file-or-directory ...]

When no arguments are provided, the default target is scanned:
${DEFAULT_TARGETS.join("\n")}
Set OPTIMIZE_IMAGE_PATHS to pass comma/space-separated targets instead.
--dry-run lists the files that would be processed without running oxipng.`);
}

function ensureOxipng() {
    const result = spawnSync("oxipng", ["--version"], { encoding: "utf8" });
    if (result.error) {
        if (result.error.code === "ENOENT") {
            throw new Error(
                "找不到 oxipng 命令。请先安装：https://github.com/shssoichiro/oxipng",
            );
        }
        throw new Error(`oxipng --version 启动失败：${result.error.message}`);
    }
    if (result.status !== 0) {
        throw new Error(
            `oxipng --version 退出码 ${result.status}：${(result.stderr || result.stdout || "").trim()}`,
        );
    }
}

function resolveTargets() {
    const args = process.argv.slice(2).filter((arg) => !arg.startsWith("-"));
    if (args.length > 0) return args;

    const envTargets = process.env.OPTIMIZE_IMAGE_PATHS?.trim();
    if (!envTargets) return DEFAULT_TARGETS;

    return envTargets
        .split(/[\r\n,\s]+/)
        .map((part) => part.trim())
        .filter(Boolean);
}

function collectPngFiles(targets) {
    const files = new Map();
    for (const target of targets) {
        const absoluteTarget = resolve(process.cwd(), target);
        if (!existsSync(absoluteTarget)) {
            throw new Error(`目标不存在：${target}`);
        }
        for (const file of collectTargetFiles(absoluteTarget)) {
            files.set(file, file);
        }
    }
    return [...files.keys()].sort((left, right) => displayPath(left).localeCompare(displayPath(right)));
}

function collectTargetFiles(target) {
    const stats = statSync(target);
    if (stats.isFile()) return isPng(target) ? [target] : [];
    if (!stats.isDirectory()) return [];
    return walkDirectory(target);
}

function walkDirectory(directory) {
    const files = [];
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = resolve(directory, entry.name);
        // 跳过符号链接：assets/resource/image/image.lnk 这类快捷方式不该被当成图片处理。
        if (entry.isSymbolicLink()) continue;
        if (entry.isDirectory()) {
            if (!EXCLUDED_DIRECTORIES.has(entry.name)) {
                files.push(...walkDirectory(path));
            }
            continue;
        }
        if (entry.isFile() && isPng(path)) files.push(path);
    }
    return files;
}

function isPng(path) {
    return path.toLowerCase().endsWith(".png");
}

function runOxipng(args) {
    const result = spawnSync("oxipng", args, { stdio: "inherit" });
    if (result.error) {
        throw new Error(`oxipng 启动失败：${result.error.message}`);
    }
    if (result.status !== 0) {
        throw new Error(`oxipng 退出码 ${result.status}`);
    }
}

function sha256(path) {
    return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function displayPath(path) {
    return relative(process.cwd(), path).replaceAll("\\", "/");
}

function formatBytes(bytes) {
    const sign = bytes < 0 ? "-" : "";
    const value = Math.abs(bytes);
    if (value < 1024) return `${sign}${value} B`;
    if (value < 1024 * 1024) return `${sign}${(value / 1024).toFixed(2)} KiB`;
    return `${sign}${(value / 1024 / 1024).toFixed(2)} MiB`;
}
