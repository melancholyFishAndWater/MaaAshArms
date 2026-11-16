# tools/logs —— 本地日志筛选

只读本地数据，提供日志文件概览、任务索引和带源文件行号的原文检索，输出 JSON。无需 drawing 或 sentry，不联网、不驱动设备、不修改日志，也不写缓存。

## 入口

从仓库根执行：

```powershell
python tools/logs/cli.py inspect PATH [PATH ...] [参数...]
python tools/logs/cli.py inspect --help
```

必须写 `inspect` 子命令，不支持在 CLI 后直接传裸路径。PATH 可为单份日志或已解压目录；目录递归读取全部 `.log`，包括轮转和 agent 日志，重叠输入路径会去重。不直接读取压缩包。

## 常用命令

```powershell
# 默认 inventory：先确认文件、行数、内容时间范围和级别分布
python tools/logs/cli.py inspect D:/Downloads/logs D:/Downloads/debug

# tasks：列框架任务，选择源文件、进程和任务号
python tools/logs/cli.py inspect D:/Downloads/debug --view tasks --limit 30

# search：提取失败回显与前后各两行；面板任务失败可能是 INF
python tools/logs/cli.py inspect D:/Downloads/logs --view search --contains "任务失败" --context 2

# 精确任务号与进程，不把 7 匹配到 71
python tools/logs/cli.py inspect D:/Downloads/debug --view search --task-id 200000052 --pid Px25000 --limit 20

# 节点、级别、时间窗及分页
python tools/logs/cli.py inspect D:/Downloads/debug --view search --node StartUpEnterLogin --limit 10
python tools/logs/cli.py inspect D:/Downloads/debug --view search --level ERR --from "2026-11-16 19:45:00" --to "2026-11-16 20:00:00" --limit 10 --offset 10
```

## 输出与下一步

标准输出为单个 JSON 对象，包含 `schema_version`、`view`、`total`、`offset`、`limit`、`has_more`、`items`、`warnings`。默认每页 50 条；原文默认最多 2000 字符，可用 `--max-chars` 调整。无命中返回空数组及退出码 0；错误参数或不可读输入退出码 2。

`search` 保留源文件和 1 起算行号、原文截断标志；`tasks` 的 `framework_ret` 仅表示框架返回，不保证业务完成。同一任务跨轮转文件的记录分别保留，不自动合并身份。

筛选及证据边界详见 [inspect 用法](docs/inspect.md)。需要绘图时，选中实际日志文件后使用 [drawing](../drawing/README.md)；drawing 消费日志规则并绘图，现有 `flow` / `cases` / `whiteboard` 行为不变。本次职责拆分未将历史 cases 遥测编排全部迁移。
