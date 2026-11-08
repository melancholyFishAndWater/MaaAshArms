# tools/cleanup —— 清理工具

清理两类东西：pytest 留下的缓存目录，以及 `temp/` 下超过保留期的文件与空目录。

- 模块分布、编写约束、该工具的功能取舍：[`AGENTS.md`](AGENTS.md)
- 为什么会有 pytest 残留在仓库根、保留策略的边界：[`docs/rules.md`](docs/rules.md)
- 改代码前要跑的校验：[`tools/validation/README.md`](../validation/README.md)

## 用法

```bash
python tools/cleanup/cli.py pytest                 # 删 .pytest_cache / pytest-cache-files-*
python tools/cleanup/cli.py pytest --dry-run       # 只列出会删什么
python tools/cleanup/cli.py pytest --root D:/repo  # 换扫描起点（默认仓库根）

python tools/cleanup/cli.py temp                   # 删 temp/ 下超过 7 天的文件 + 空目录
python tools/cleanup/cli.py temp --dry-run
python tools/cleanup/cli.py temp --days 3          # 换保留天数
python tools/cleanup/cli.py temp --ignore logs     # 额外忽略一个目录名（可重复）

python tools/cleanup/cli.py all                    # 先清 temp，再删 pytest 缓存（VS Code 自动任务用这个）
```

`all` 只认 `tools/cleanup/config.json` 里的保留天数与忽略项，不接 `--days` / `--ignore`；需要细调时分开跑 `temp`。

## 配置

`tools/cleanup/config.json`：

```json
{
    "days": 7,
    "ignore": ["flow", "M9A", "telemetry", "sentry-cfg", "uvcache", "uvtools"]
}
```

- `days`：`temp` 的文件保留天数，`--days` 可临时覆盖。
- `ignore`：目录名（在 `temp/` 下任意层级按名字匹配），整棵子树都不进入。`--ignore NAME` 在它之上追加。
- 默认忽略的是"删了要重新下载/重登"的目录：`flow`（流程图产物）、`M9A`（参考仓库）、`telemetry`（遥测工件缓存）、`sentry-cfg`（Sentry 凭据副本）、`uvcache` / `uvtools`（`uvx` 包缓存）。

## 行为

- `temp` 只删**文件**（mtime 早于保留期），然后自底向上删**空目录**；`temp/` 根目录本身不删。
- 不跟随符号链接 / 目录联接；被忽略的子树整个跳过。
- 只读位的文件/目录会先清只读位再删；`拒绝访问`（ACL 不归当前用户）删不掉的会列出来并给提权修法。
- `--dry-run` 只打印计划，不改磁盘。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 没有可删的，或全部删掉 |
| 1 | 有东西删不掉（输出里有原因；`拒绝访问` 会附 `takeown` / `icacls` 修法） |
| 2 | 参数不对，或 `--root` 指向的目录不存在 |
