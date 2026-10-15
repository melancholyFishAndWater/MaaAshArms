#!/usr/bin/env node
/**
 * 对 tools/ 与 agent/ 下的 Python 做类型检查（pyright）。
 *
 *   node tools/validation/check_pyright.mjs
 *
 * 为什么不用 `python -m pyright`：pyright 是 TypeScript 写的，本仓库已有 Node 工具链
 * （`package.json` 里三个 .mjs 脚本），走 npx 不引入 Python 侧依赖。
 *
 * 两点与沙箱/环境有关：
 *   * npm 的缓存默认在 `%LOCALAPPDATA%`，受限沙箱下写不进去 —— 这里指到工作区内的
 *     `temp/npm-cache`，跑完可以删。
 *   * pyright 现在装不进 vendored 的发布包解释器，Pillow 也可能没装；所以配置里把
 *     `reportMissingImports` 降成 warning —— 缺库不是代码问题，不该让校验假红。
 *
 * 退出码：0 = 无 error；非 0 = 有 error 或没跑起来。
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..', '..');
const CACHE_DIR = join(REPO_ROOT, 'temp', 'npm-cache');
const CONFIG = join(HERE, 'pyright.config.json');

if (!existsSync(CONFIG)) {
    console.error(`找不到 ${CONFIG}`);
    process.exit(2);
}
mkdirSync(CACHE_DIR, { recursive: true });

const args = ['--yes', 'pyright', '--project', CONFIG, ...process.argv.slice(2)];

try {
    execFileSync('npx', args, {
        cwd: REPO_ROOT,
        stdio: 'inherit',
        env: { ...process.env, npm_config_cache: CACHE_DIR },
        shell: process.platform === 'win32',   // Windows 上 npx 是 .cmd，需要 shell 才能执行
    });
    process.exit(0);
} catch (error) {
    if (typeof error.status === 'number') {
        process.exit(error.status === 0 ? 0 : error.status);
    }
    console.error('pyright 没跑起来。首次运行需要网络拉取 pyright（npx --yes pyright）。');
    console.error(error.message);
    process.exit(2);
}
