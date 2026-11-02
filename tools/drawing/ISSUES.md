# 使用问题记录（tools/drawing）

## 「由识别记录还原」模式下 `--task-id` 不过滤，别的任务混进图里

- 命令：`python -m tools.sentry.cli flow --case temp/evidence/PYTHON-2P/debug --task-id 200000058`
- 现象：该 task 全程未命中、日志里没有一条 `PipelineTask node done`，渲染器退到「由识别记录还原」；而 `reco` 行不带 `task_id`，于是 `--task-id` 在这条路径上形同不存在——同一份日志里 08:41 的 `FeiduGetMetal`（收取资源）等其它任务一起被画进来，出图 **26 轮 / 13048×8084**。图上第二行只提示"识别记录里没有 task_id，同窗口其他任务可能混入"，不改口径。
- 绕过：先在该 task 的日志里找出带 `task_id` 的事件区间，再用 `--from "2026-11-02 08:44:45" --to "2026-11-02 08:45:30"` 一并传；同一份证据重画为 9 轮 / 4788×1446，内容只含 `Entrust*` 节点。
- 待办口径：这种"窗口夹不住"的情况应改成在图上按失败标记硬标一行（"本图由整段日志还原，已混入其它 task"），而不是只在 stderr 提一句；或支持按该 task 的事件区间自动收紧窗口。

## 任务以 `Node.PipelineNode.Failed` 结束时不出失败框，`on_error` 现场图跟着丢

- 命令：`python -m tools.sentry.cli flow --case temp/evidence/PYTHON-2C/debug --task-id 200000201 --from "2026-10-29 23:10:12" --to "2026-10-29 23:13:04"`
- 现象：图最后一步停在 `23:13:01.046 BaseProbeConfirm ×7 未命中`，之后什么都没有。日志里本任务真正的失败是 `23:13:02.416 [msg=Node.PipelineNode.Failed] [name=MaintenanceBranchAble]` 加 `23:13:02.526 [msg=Tasker.Task.Failed] [entry=TaskMaintenance]`；但整份日志里既没有属于本任务的 `Task timeout [pretask.name=…]` / `invalid node id` / `node not completed`，也没有 `completed=false` 的 `PipelineTask node done`（实测 0 条）。渲染器的失败框只认这四种来源，于是真正的失败节点 `MaintenanceBranchAble` 没有框。`debug/on_error/2026.10.29-23.13.02.422_MaintenanceBranchAble.png` 存在，也因为没有失败框可挂而被丢掉——图上没有任何现场图，看不出"最后是怎么死的"。
- 绕过：`--list` 末尾的「父节点评估」账里能读到 `MaintenanceBranchAble 未命中 5 轮`，再手工打开 `debug/on_error/*_MaintenanceBranchAble.png`；有本机完整 `debug/`（带该节点的 `PipelineTask node done`）时会出 `completed=false` 框。
- 待办口径：把 `Node.PipelineNode.Failed` 作为失败框的第四类来源（事件总线行的例外，与 `Node.NextList` 同等对待），并把同时间戳的 `on_error` 图挂到该框上。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。