# 本地日志筛选（面向 AI 调用）

入口是 `python tools/drawing/cli.py inspect PATH [PATH ...]`。只读已解压目录或单份 `.log`；不解压归档、不联网、不驱动设备、不产生缓存。

## 推荐调用顺序

1. 默认 `--view inventory`：先确认实际文件、日志内容的首尾时间、级别分布，不按轮转文件名猜内容范围。
2. `--view tasks`：列框架任务，选择源文件、进程和任务号。没有 node-done 的任务仍保留，避免漏掉初始化或投递异常。
3. `--view search`：提取原文，按任务、进程、节点、时间、级别、字面文本筛选。先用 `--contains "任务失败"` / `--contains "Traceback"`，不要只查 ERR；面板任务失败常写 INF。
4. 依据任务清单选中单份框架日志，交给 `flow --task-id ... --pid ... --list --no-telemetry` 获取结构化执行经过或绘图。

```powershell
python tools/drawing/cli.py inspect D:/Downloads/logs D:/Downloads/debug
python tools/drawing/cli.py inspect D:/Downloads/debug --view tasks --limit 30
python tools/drawing/cli.py inspect D:/Downloads/logs --view search --contains "event_id=" --context 2
python tools/drawing/cli.py inspect D:/Downloads/debug --view search --node StartUpEnterLogin --limit 10
python tools/drawing/cli.py inspect D:/Downloads/debug --view search --level ERR --from "2026-11-16 19:45:00" --to "2026-11-16 20:00:00"
# 下一页；offset 是命中记录序号，不是原文行号
python tools/drawing/cli.py inspect D:/Downloads/debug --view search --level ERR --limit 10 --offset 10
```

## 输出与筛选

标准输出为单个 JSON 对象：`schema_version`、`view`、`total`、`offset`、`limit`、`has_more`、`items`、`warnings`。`total` 是筛选后、分页前的条数；没有命中返回空数组和退出码 0，错误参数/不可读输入退出码 2。不要把空数组解释成任务正常。

- `inventory`：逐文件大小、行数、内容时间范围和级别分布。
- `tasks`：逐源文件、进程和任务号列入口、起止时间、框架返回值 `framework_ret` 与 `node_done_count`。起止缺失保留空值，不补造。时间筛选按任务观测区间与查询窗口是否相交判断，节点数仍统计该文件中的完整任务记录。`source_lines` 最多列出 20 条原文定位，超出时以 `source_lines_truncated` 标记。任务索引只读 task-start、task-end、node-done；只有事件总线的截断日志应另用 search 或 flow，不保证出现在任务清单。
- `search`：每项有源文件 `path`、1 起算 `line`、时间、级别、进程、原文 `text`、截断标志 `truncated`；`--context N` 附带前后 N 行及其真实行号。上下文不是筛选命中，不应据此给当前任务归属。

`--task-id` 是数值精确匹配，不会将 7 匹配到 71；搜索仅匹配行上明确出现的任务号，不把时间范围内所有识别/agent行都归给当前任务。`--node` 按完整 token 匹配，`Gift` 不匹配 `Gift2`；它是原文定位，不承诺该行代表节点执行。`--contains` 为区分大小写的字面子串。多个筛选按“且”叠加，多次 `--level` 按“或”。

时间使用日志原本的本地时间，必须给完整日期，不做跨时区换算。分页默认 50 条；原文默认最多 2000 字符，可用 `--max-chars` 调整；截断的原文需要回到对应文件行号读取，不猜测被截掉的 JSON。

目录递归读取所有 `.log`，输入文件按稳定路径顺序、文件内按原文行序输出；不会按时间重排。若要查某一个文件，直接将该文件作为 PATH。概览不支持任务/原文筛选；任务视图不支持节点/文本/级别/上下文筛选，不支持的组合报错而非静默忽略。

## 证据边界

- 同一任务跨轮转文件的记录分别保留；不把源文件间重复的任务号自动合并。PID 也可能被操作系统复用，跨文件合并前仍需核验运行身份。
- `framework_ret=true` 只是框架返回，不等于业务完成；停止任务也可能返回 true。业务结论还要核对面板停止记录、完成节点与截图。
- Node 事件、底层 reco 记录与 agent 转发可能是同一次操作的多份记录。搜索的 `total` 是原文命中行数，不是识别次数或失败次数；任务的节点数只统计现有解析器定义的 node-done 行。
- 面板日志通常没有原生任务号，按 `--task-id` 搜不到它是正常的。应另用文本和时间检索，并保留它自己的来源；时间相近不构成身份确认。
- 本工具帮助定位日志，不代替绘图器的任务窗口、候选表、识别去重或终止失败判断。证据包是否属于同一运行，继续按 [本地辅助的身份核验](packs.md#本地-release-辅助默认开) 处理。
