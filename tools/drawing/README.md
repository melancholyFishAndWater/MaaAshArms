# tools/drawing —— 绘图工具

把**一次运行**的日志（外加可选的识别帧、失败现场图）还原成一张「横向=轮次、纵向=轮内顺序」的图，
回答三个问题：这次跑到第几轮、卡在哪个节点、连续多少次没命中。

它只读本地证据：不联网、不驱动设备、不改日志与图片。产出是给人看的 PNG（`--list` 给纯文本），
统一落到**仓库根的 `temp/flow/`**（`--out` / `--outdir` 可覆盖）。

**边界**（后面「功能取舍」里的每一条都是这条边界的推论）：本工具只画单次执行的**过程**，
不统计多次运行、不判断任务好坏——那是 [`tools/sentry`](../sentry/README.md) 的事；
日志里没记的东西它不会补，也不会猜。

## 功能取舍

| 不做 | 为什么 | 要它怎么办 |
|---|---|---|
| 失败率、趋势、跨版本对比 | 目的只画单次执行顺序；把多次运行混进一张图，图的含义就变了 | `python -m tools.sentry.cli task-failure` / `task-trend` |
| 自己去 Sentry 下载日志与附件 | 画图必须有本地图片；而 Sentry 失败附件跳过 `vision/`、图片包上限 5 MiB，在那边画不出画面格 | `python -m tools.sentry.cli evidence --issue <ID>`：把现场图与那次运行的日志一起拉到 `temp/evidence/<ID>/debug/`，并打印 `flow --case` 命令 |
| 证据包没带 `vision/` 时"补"出画面格 | 只画日志与图片里确实有的东西；没有就留空，不合成 | 让对方手动导出（勾「包含 vision 图片」），把导出包喂 `--case` |
| 从事件总线行取数（`!!!OnEventNotify!!!`、`ctx_event_sink`、`Node.*`） | 同一次识别在日志里有两行副本，按行计数会翻倍；`reco [result=]` 已经带完整识别明细与 `reco_id` | 手工按行计数时按 `reco_id` 去重（注意它是**进程内**计数，一份装了多次运行的日志里会重复） |
| 还原被导出截断的行 | 截断的 JSON 猜出来的内容会变成图上的假事实 | 用完整 `maafw.log` / `debug` 目录；工具会统计并提示跳过了多少条 |
| 把 Explore 导出的 CSV 直接当输入 | 导出的正文按块切碎（每格约 3.3 KB、块首块尾丢头去尾），格子顺序也不是时间顺序，直接拼会拼出假行 | 先还原：`python -m tools.sentry.cli explore-csv <csv>`，再拿它产出的 `temp/flow/<csv 名>.log` 来画 |
| 把连续未命中折成一格后仍标注时间跨度 | 框文本经 `fit_text` 截断，先被切掉的正是末尾的数字 | `--collapse 1`：每次识别各占一列，逐列看时间 |
| 加 `--keep-order` 之类的开关保留原始行序 | 时间戳回跳是数据坏了，按时间重排是修数据而不是风格选择 | 要看原始顺序直接读日志文件 |

判据都写在代码里：`round_steps()`、`compose_columns()`、`describe_empty_window()` 的文档字符串。

## 怎么跑

```bash
# 1. 一段日志 → 执行流程图（本机安装根）
python tools/drawing/flow_from_log.py <安装根>/debug/maafw.log \
    --vision <安装根>/debug/vision \
    --from "2026-10-11 21:39:56" --to "2026-10-11 21:43:29"

# 只解析、不画图
python tools/drawing/flow_from_log.py <安装根>/debug/maafw.log --task-id 200000013 --list

# 2. 证据包（debug 目录 / MFA 导出 zip / 多卷）→ 流程图，带 manifest 幂等
python tools/drawing/flow_cases.py --case D:/MaaAshArms/debug --task-id 200000118
python tools/drawing/flow_cases.py --case log_20261014-part01.zip log_20261014-part02.zip --task-id 200000118
python tools/drawing/flow_cases.py --case D:/MaaAshArms/debug --list-cases

# 3. 白板图片转 png 并改写引用
python tools/drawing/whiteboard_images.py
python tools/drawing/whiteboard_images.py --check     # 只报告，发现非目标格式即 exit 1
python tools/drawing/whiteboard_images.py --dry-run   # 报告将要做什么，不改动
```

遥测侧的入口是 `python -m tools.sentry.cli flow`，它转发到 `flow_cases.py`，产出目录一致。

### 解析口径（读图前先看这里）

- **按时间重排**：日志时间戳回跳时（Explore 导出的 CSV 是分块拼接的）先按时间戳稳定排序再解析，
  图上印一行「日志时间戳有回跳，已按时间重排（N 行）」。不再有"遇到第一条晚于 `--to` 的行就停"这一步——
  它会把窗口内的记录整段丢掉，还报成"窗口内没有记录"。
- **仅事件模式**：日志里没有 `PipelineTask node done`（Sentry 导出就是这种）时，用识别记录
  （`reco [result=]`）合成步骤，图上印「由识别记录还原」。此时默认按 `--collapse` 把连续同节点的
  识别折成一格（`未命中 ×13`）；`--collapse 1` 保留每次识别各占一列。`reco` 行里没有 `task_id`，
  能取到该 task 的起始行就夹住范围，取不到会印一行提示。
- **`--task-id` 严格过滤 + 任务判定窗口**：`steps` 与事件都按 `task_id` 过滤；框架级事件
  （`Task timeout` / `invalid node id` / `node not completed`，没有 `task_id`）改用**判定窗口**
  归属——下界是本任务第一步，上界优先取**它自己的 `task end`**（任务失败后框架不再为它记东西），
  没有 `task end` 时退回"本任务最后一步之后、别的任务的第一步"。未命中聚合（`reco` 行同样没有
  `task_id`）用同一个窗口。这样一份装了多次运行的日志不会把 23:59 那趟训练的未命中画进 13:11 那趟维修的末轮，
  也不会把同一运行里后一个任务的超时画进前一个任务的图上；而"最后一步之后"的失败标记仍留在图上
  （任务失败恰恰发生在最后一步之后）。被排掉的条数会印出来。
- **诊断注记**：由上面这些事实构成的注记印在图第二行（琥珀色）并同时打到 stderr，别只看 stdout。

### 画不出来时的报错

`--from/--to/--task-id` 写错、路径写错、窗口落在日志时间范围外，报的话各不相同，且都会报出
**看到了什么**（行数、日志的时间范围、被过滤掉的是哪一类）：

```
错误：入参像路径但磁盘上没有这个文件：D:/MaaAshArms/debug/vision；它也不含任何一行日志正文
错误：窗口内没有日志行：日志时间范围 2026-10-13 14:52:37.678 ~ 2026-10-14 00:35:24.413；请求的窗口 2027-01-01 00:00:00.000 ~ 2027-01-01 01:00:00.000
错误：窗口内 20 行里属于本 task（--task-id=999999）的节点级记录一条都没有，有 1 行是其他 task_id 的
```

## 三个脚本的分工

| 文件 | 角色 |
|---|---|
| `flow_from_log.py` | **渲染器**：解析 + 布局，唯一定义处。也能 `import` 调用 —— `render_flow(log, vision=…, on_error=…, out=…, options=FlowOptions(…))` 返回 `FlowResult` |
| `flow_cases.py` | **编排**：找证据、跑批量、写 `temp/flow/manifest.json`、幂等跳过。**不复制渲染逻辑** |
| `whiteboard_images.py` | 白板体系的图片格式统一（见 `.agents/canvas.md`） |

`flow_from_log.py` 的日志入参接受**路径（含 `pathlib.Path`）/ 正文 / 文本流**三种；图片证据接受目录、zip、
多卷 zip、内存字节，都经 `MediaResolver`（zip 里的图**延迟解压**，避免整包读进内存）。

`FlowResult` 里有 `truncated_start`（日志只剩尾部）、`notes`（与图上第二行同一批注记）、
`derived_from`（非空 = 步骤由识别记录合成）、`out_of_order`（回跳行数）——库调用方不用去猜图是按什么口径画的。

## 依赖与前置

- **Pillow**：画图要它，而**发布包自带的解释器不含 Pillow**（`.python-runtime/` 与包内
  `python/` 都 `import PIL` 失败）。用装了 Pillow 的系统 Python 跑；绘图不进发布包。
- 无 Pillow 时 `import` 仍可用（异常别名做了 fallback），只有真正画图会报"需要 Pillow"。

## 已知坑

- **产出目录靠上溯级数算**：`REPO_ROOT` 是 `tools/drawing/` 上溯两级。再改目录层级必须同步改，
  否则图会静默落到 `tools/drawing/temp/flow/`。`check_python.py` 的 `repo-root` 检查拦这个。
- **`--no-thumbs` 只影响"贴不贴图"，不改变框高**（`slot_h` 与 `thumbs` 无关，
  `box_height_for` 只看该框有没有图）。别拿产物尺寸判断贴图是否生效——用 sha256 或直接看图。
- **贴图失败会打警告，但有上限**：一次几百张图读不了时只打前 5 条 + 一条汇总。读不出来的
  常见原因是分卷缺卷、或图片已被框架的 7 天清理删掉。
- **副标题里不要用 `⚠`**：msyh 没有该字形，会画成方框。
- **一份日志装了多次运行是常态**：`reco_id` / `action_id` 是**进程内**计数，派发者
  （`[cur_node_=X]`）也按进程分开记，所以同一份 `maafw.log` 里的多次运行不会互相串台；
  判"哪一次"用 `--task-id` + `--from/--to`。Explore CSV 还原出来的日志天然跨多次运行
  （实测两个进程、三个任务），画图时必须给 `--task-id`。
- **日志只剩尾巴时图上会写"注意：日志仅含尾部"**：遥测附件只带 `maafw.log` 的 512 KB 尾部，
  没有这行标注的话读图的人会以为任务从日志第一行开始。
- **整份日志会读进内存**：为检测时间回跳必须读完（本机 10.9 MB 的 `maafw.log` 约 2.4 s、十几 MB 内存）。
  框架自己会轮转日志（`debug/` 下有 `maafw.bak.<日期>.log`），不需要为它做流式裁剪。
- **`RENDERER_VERSION`**：改动会影响产物内容时（版面、注记、解析口径）必须 +1，
  否则 `flow_cases` 会命中旧的 `manifest.json` 直接"跳过（已画过）"，渲染改了却不重画。
- **一份证据包里的日志常覆盖整次运行**（多个任务）：`evidence` 拉下来的 `maafw.log` 就是这样，
  必须配 `--task-id`；同一条 trace 上的多个 issue 会共用同一份日志。
