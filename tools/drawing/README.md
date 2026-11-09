# tools/drawing —— 绘图工具

把**一次运行**的日志（外加可选的识别帧、失败现场图）还原成一张「横向 = 轮次、纵向 = 轮内顺序」的
PNG；也能把白板目录里的图片统一成 png 并改写引用。

只读本地证据：不联网、不驱动设备、不改日志与图片。产物默认落在**仓库根的 `temp/flow/`**。

入口只有一个：

```bash
python tools/drawing/cli.py <flow|cases|whiteboard> [参数...]
```

| 子命令 | 干什么 | 典型输入 |
| --- | --- | --- |
| `flow` | 一段日志 → 一张图（每次运行单独画） | `maafw.log`、MFA 导出 zip 里的那份 |
| `cases` | 一个证据包 → 图，带 `manifest.json` 幂等；也能按遥测库批量出图 | `debug/` 目录、导出包（可多卷）、单份日志 |
| `whiteboard` | 白板图片统一成 png 并改写 `.canvas` / `.md` 里的引用 | `docs/白板/` |

## `flow`：一段日志 → 一张图

```bash
# 本机安装根的一段运行
python tools/drawing/cli.py flow <安装根>/debug/maafw.log \
    --vision <安装根>/debug/vision \
    --from "2026-10-11 21:39:56" --to "2026-10-11 21:43:29"

# 只解析、不画图（父节点那一行也给全，末尾附「父节点评估」账）
python tools/drawing/cli.py flow <安装根>/debug/maafw.log --task-id 200000013 --list

# 同一份日志里多次运行：先看任务清单，再按进程 + 任务号定位
python tools/drawing/cli.py flow <安装根>/debug/maafw.log --list-tasks
python tools/drawing/cli.py flow <安装根>/debug/maafw.log --pid Px6448 --task-id 200000001

# 重复轮次逐轮展开（默认把连续同形、无截图无失败框的轮次并成一列）
python tools/drawing/cli.py flow <安装根>/debug/maafw.log --no-overlay
```

### 参数

输入与窗口：

| 参数 | 说明 |
| --- | --- |
| `log`（位置） | `maafw.log` 路径；也可以是 zip 里的那份，见 `--vision` |
| `--vision` | 识别帧目录，或导出包（zip / 多卷）、或它们的父目录（如 `debug/`） |
| `--on-error` | 失败现场图目录或导出包；默认取 `--vision` 同级的 `on_error/` |
| `--from` / `--to` | 时间窗；支持 `HH:MM:SS` 或完整时间戳 |
| `--task-id` / `--pid` | 只看某个任务 / 某个进程（`Px<pid>`）；任务号在各进程内复用，定位唯一一段要两个一起给 |

切分与折叠：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--anchor` | 自动 | 手动指定循环锚点（只影响分轮） |
| `--cut-on` | 自动 | 手动指定分轮节点（逗号分隔） |
| `--collapse` | 3 | 连续同节点折叠为「未命中 ×N」；`1` = 逐次各占一列 |
| `--repeat-threshold` | 2 | 出现次数 ≥ N 视为重要节点（图例里的深色） |
| `--min-round-len` | 2 | 一轮最少几步 |
| `--miss-min` / `--miss-max` | 5 / 8 | 连续未命中聚合成框的阈值 / 每列最多几个聚合框（`0` = 关掉上限） |
| `--max-rounds` | 不限 | 最多画几轮 |

版面：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--ports` | `pos` | 连线端口规则：`pos` 按相邻方向；`role` 是旧的按角色规则 |
| `--cols-per-band` | 0 | 一行放几列；`0` = 不换行，一直往右延伸 |
| `--no-thumbs` | — | 不贴缩略图 |
| `--no-parents` | — | 不写父节点那一行、不画分组框 |
| `--no-overlay` / `--overlay-min` | 开 / 2 | 关掉重复轮合并 / 连续同形轮 ≥ N 才合并 |
| `--thumb-height` `--box-width` `--box-height` `--gap-y` `--gap-error` `--gap-x` `--font-size` `--scale` `--title` | 各有默认 | 尺寸与标题 |

内容与外部：

| 参数 | 说明 |
| --- | --- |
| `--keep-internal` | 保留 agent 内部识别（`recognition/OCR/<uuid>`） |
| `--list` / `--list-tasks` | 只打印步骤 / 只列日志里的任务 |
| `--mermaid` | 另出同名 `.mmd`（Mermaid 文本图） |
| `--no-stale-check` / `--version` | 关掉「疑似过时」检查 / 手动给基线版本（`vX.Y.Z`） |
| `--telemetry` `--no-telemetry` `--telemetry-offline` `--telemetry-issue` `--telemetry-days` `--telemetry-gap` `--telemetry-only` | 遥测关联开关，见 [docs/telemetry.md](./docs/telemetry.md) |
| `--log-level` / `--log-file` | 工具日志（默认 `temp/logs/flow_from_log_<时间戳>_<pid>.log`；`none` = 只走控制台） |

产物：`temp/flow/<日志名>_<时间戳>.png`；`--out` / `--outdir` 改路径。版面与端口规则见
[docs/layout.md](./docs/layout.md)；读图前该知道的口径见 [docs/parsing.md](./docs/parsing.md)。

## `cases`：证据包 → 图（带 manifest 幂等）

```bash
# 一个 debug 目录
python tools/drawing/cli.py cases --case D:/MaaAshArms/debug --task-id 200000118

# 导出包（多卷一起给）
python tools/drawing/cli.py cases --case log_20261014-part01.zip log_20261014-part02.zip --task-id 200000118

# 只列出发现的证据包
python tools/drawing/cli.py cases --case D:/MaaAshArms/debug --list-cases

# 按遥测库批量出图（有证据包出完整流程，没有的出概览卡）
# 默认增量：manifest 里分步摘要一致就跳过，画完只清掉不属于本次 issue 集合的旧产物
python tools/drawing/cli.py cases --telemetry-bulk
python tools/drawing/cli.py cases --telemetry-bulk --telemetry-offline --no-fetch-evidence
python tools/drawing/cli.py cases --telemetry-bulk \
    --evidence-dir D:/Downloads/evidence --evidence-dir D:/MaaAshArms/evidence

# 整目录清空后全量重画（大改版面、想彻底重来一遍时用）；--no-prune 是"增量但不删"
python tools/drawing/cli.py cases --telemetry-bulk --clean-flow
python tools/drawing/cli.py cases --telemetry-bulk --no-prune

# 只画某一段版本的 issue（条件可重复，按「且」叠加）
python tools/drawing/cli.py cases --telemetry-bulk \
    --telemetry-version ">=v1.0.0-beta.55" --telemetry-version "<=v1.0.0-beta.60"
python tools/drawing/cli.py cases --telemetry-bulk --telemetry-version "==v1.0.0-beta.52"
```

### cases 参数

| 参数 | 说明 |
| --- | --- |
| `--case`（可多个） | 一个 `debug` 目录、一组归档（多卷一起给），或一份日志文件；`--telemetry-bulk` 时可选 |
| `--vision` / `--on-error` | 同 `flow`，默认取 `<case>/vision`、`<case>/on_error`（单份日志时取日志所在目录） |
| `--outdir` | 产出目录，默认 `temp/flow` |
| `--from` / `--to` / `--task-id` / `--pid` | 同 `flow` |
| `--cut-on` / `--max-rounds` / `--collapse` | 同 `flow` |
| `--no-thumbs` / `--no-parents` / `--no-overlay` / `--overlay-min` / `--mermaid` | 同 `flow` |
| `--version` | 手动给「疑似过时」的基线版本 |
| 遥测组（`--telemetry` … `--telemetry-only`） | 同 `flow` |
| `--telemetry-version SPEC` | 只画版本满足条件的 issue，`>=` / `<=` / `==`（运算符可省，默认 `==`），版本写 `v1.0.0-beta.61` 或整串 release；可重复，多条按「且」叠加。口径与边界见 [docs/telemetry.md](./docs/telemetry.md) |
| `--local-release DIR` | 本地 release 安装目录（默认 `D:/MaaAshArms`，环境变量 `MAA_DRAWING_LOCAL_ROOT` 可改）：画证据包时用它的完整日志与 `vision/`、`on_error/` 辅助，**按内容认运行**（本机面板日志的 `event_id` + 证据包日志行在本地日志里逐字相同 + 同 task），认不出就不用。见 [docs/packs.md](./docs/packs.md) |
| `--no-local-release` | 不让本地 release 参与，只用证据包自己的文件 |
| `--force` | 忽略 manifest，强制重画 |
| `--clean-flow` | 批量出图（`--telemetry-bulk`）前先清空输出目录里的 `png`/`mmd` 与 `manifest.json`，再整批重画；同目录下 `explore-csv` 的 `.log` 与子目录不动 |
| `--no-prune` | 增量出图，但不删任何旧产物（目录里会留着上次的图、以及版本筛选范围外的图） |
| `--evidence-dir DIR` | 批量模式找证据包的根目录（可重复，先给的先找；`temp/evidence` 永远兜底） |
| `--telemetry-bulk` | 批量出图：遍历本地遥测库所有未解决 issue，每个尽量出一张。**默认增量**：分步摘要（输入/参数/解析/注记/疑似过时 + 两段代码摘要）一致就跳过，见 [docs/packs.md](./docs/packs.md#增量绘制与旧产物清理) |
| `--fetch-evidence` / `--no-fetch-evidence` | 本地缺证据包时是否自动 `evidence` 拉取（默认开，需联网） |
| `--list-cases` | 只列出发现的证据包 |
| `--all-tasks` | 按 `(进程, task_id)` 各出一张，文件名带 `_task<id>_px<进程>` |
| `--log-level` / `--log-file` | 同 `flow`（默认 `temp/logs/flow_cases_…`） |

产物：`temp/flow/<NAME>.png`（`--mermaid` 另出 `.mmd`），追加进 `temp/flow/manifest.json`；
同一份输入重跑会跳过（`--force` 强制重画），批量模式还会删掉不属于本次 issue 集合的旧图。
命名规则、增量判据、批量模式的取舍、证据包布局见 [docs/packs.md](./docs/packs.md)。

遥测侧的入口是 `python -m tools.sentry.cli flow`，它转发到 `cases`，产出目录一致。

## `whiteboard`：白板图片统一成 png

```bash
python tools/drawing/cli.py whiteboard            # 转换 + 改写引用（默认 png）
python tools/drawing/cli.py whiteboard --check    # 只报告，发现非目标格式即 exit 1
python tools/drawing/cli.py whiteboard --dry-run  # 报告将要做什么，不改动
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--root` | 仓库根 | vault 根目录 |
| `--dir` | `docs/白板` | 扫描范围（相对 `--root`） |
| `--format` | `png` | `png` / `webp` / `jpeg`；默认 png 是因为 VS Code 插件只认 png |
| `--check` / `--dry-run` | — | 只检查 / 只打印计划 |

## 日志

三者都支持 `--log-level DEBUG|INFO|WARNING|ERROR` 与 `--log-file <路径>`：每次运行在 `temp/logs/` 下
按时间戳建一个日志文件（`<tool>_<YYYYMMDD-HHMMSS>_<pid>.log`），独立于 `temp/flow/` 与
`temp/telemetry/`；控制台只走 stderr，stdout 留给结果。`--log-file none` 只走控制台。

## 环境

画图需要 **Pillow**；发布包自带的解释器不含 Pillow，用装了 Pillow 的系统 Python 跑。

## 作为库调用

```python
from tools.drawing.lib.flow_from_log import FlowOptions, MediaResolver, render_flow

res = render_flow(
    log_text,                                  # 路径 / 正文 / 文本流都行
    vision=MediaResolver(["log-part01.zip", "log-part02.zip"]),  # 目录 / zip / 多卷 / 内存字节
    options=FlowOptions(t_from=t0, t_to=t1, task_id=200000118),
)
print(res.path, res.width, res.height, res.steps)
```

`FlowResult` 里有 `truncated_start`（日志只剩尾部）、`notes`（与图上第二行同一批注记，元素是
`Note(text, kind)`，`kind` 决定颜色）、`derived_from`（非空 = 步骤由识别记录合成）、
`out_of_order`（回跳行数）、`stale`（疑似过时的节点清单，`None` = 没做这项检查）。

## 更多

- 模块分布、编写约束、本工具的功能取舍：[AGENTS.md](./AGENTS.md)
- 版面与端口：[docs/layout.md](./docs/layout.md)
- 解析口径（读图前先看）：[docs/parsing.md](./docs/parsing.md)
- 证据包、产物命名、批量模式：[docs/packs.md](./docs/packs.md)
- 遥测关联：[docs/telemetry.md](./docs/telemetry.md)
- 疑似过时：[docs/stale.md](./docs/stale.md)
- agent 缺位、失败框、角标、报错：[docs/signals.md](./docs/signals.md)
- 依赖与已知坑：[docs/pitfalls.md](./docs/pitfalls.md)
