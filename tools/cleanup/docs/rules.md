# 清理规则：删什么、为什么、边界在哪

## pytest 缓存为什么会留在仓库根

pytest 9 的 `_pytest/cacheprovider.py:_make_cachedir` 不是直接建 `.pytest_cache`：它先在旁边
`tempfile.mkdtemp(prefix="pytest-cache-files-")` 建临时目录，写 `README.md` / `.gitignore` /
`CACHEDIR.TAG`，再 rename 成 `.pytest_cache`。rename 被拒（父目录没有删除权限、目录被杀软或
索引占用）时临时目录会留下；收尾用的是 `shutil.rmtree(path, ignore_errors=True)`，连失败也吞掉。
两个名字都在 `.gitignore` 里（第 471–472 行附近），`git status` 看不到，文件系统里却一直在。

`pytest` 子命令只删这两个名字，扫描时跳过 `node_modules`、`deps`、`build`、`install`、`temp`、
`debug`、`.ruff_cache` 和 `.python-runtime*`（见 `lib/pytest_cache.py` 的 `SKIP_DIR_NAMES`）。

## `temp/` 的保留策略

- 只按**文件 mtime** 判断：早于 `now - days` 的删。目录不按年龄删，只在清完文件后自底向上删空目录。
- `temp/` 根目录本身永远不删；被忽略目录的整棵子树不进入（既不删文件，也不删空目录）。
- 不跟随符号链接与 Windows junction：链接本身可以被删，指向的内容不动。
- 只读位先清再删；`拒绝访问`（ACL 不属于当前用户）会汇总提示 `takeown` / `icacls`，
  `文件被占用`（winerror 32）只提示关掉占用程序。
- `--dry-run` 只打印计划：文件按当前 mtime 判定，空目录按当前状态判定，不落盘。

## 忽略项的理由

`config.json` 的默认 `ignore` 都是"删了要重新下载 / 重新登录 / 重建很慢"的目录：

| 名字 | 里面是什么 | 删掉的代价 |
| --- | --- | --- |
| `flow` | `temp/flow/` 流程图产物与 `manifest.json` | 重画要重新解析证据包 |
| `M9A` | 参考仓库副本 | 重新 clone / 解压 |
| `telemetry` | `temp/telemetry/` 遥测工件 | 重新落盘要联网 |
| `sentry-cfg` | Sentry CLI 凭据副本（`cli.db`） | 重新 `sentry auth login` + 复制 |
| `uvcache` / `uvtools` | `uvx` 包缓存与工具环境 | 下次 `uvx` 要重新下载 |

匹配规则是"目录名在 `temp/` 下任意层级出现即忽略"，所以嵌套目录里叫 `flow` 的也会被跳过。

## 和 `pytest` 子命令的分工

`temp` 管 `temp/` 下的年龄；`pytest` 管仓库根按名字出现的 pytest 缓存。两者互不覆盖：
`pytest` 的扫描跳过整个 `temp/`，`temp` 不按名字删 `.pytest_cache`。`all` 依次跑两者，
`.vscode/tasks.json` 用的是它。
