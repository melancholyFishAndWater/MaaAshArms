# 项目架构

> 本文描述**入库的项目结构**与各层之间的调用关系，不含本机运行产物、构建产物、
> 缓存与临时目录（见末尾「不计入架构的本机目录」）。数值按 2026-11-09 工作区核对。
>
> 面向开发者；用户侧的使用说明在 [README.md](../../../README.md)，开发上手步骤在
> [如何开发](how_to_develop.md)。

## 1. 运行时链路

```text
MFAAvalonia v2.16.1（GUI，由 CI 下载进发布包）
    │ 读 assets/interface.json（ProjectInterface V2）
    │ 按 task[].entry 投递任务
    ▼
MaaFramework v5.12.2（Tasker，执行 pipeline 节点）
    │ 节点里 recognition/action 为 Custom 时，经 AgentServer 反向请求
    │ socket_id 由 MFA 通过 argv 传入
    ▼
agent/main.py（Python 子进程，AgentServer.start_up(socket_id)）
    │ 各任务模块用装饰器注册自定义识别/动作
    ▼
assets/resource/pipeline/*.json + image/ + model/
```

- GUI 与框架的版本由 `.github/workflows/install.yml` 顶部的 `MAAFW_VERSION` /
  `MFAA_VERSION` 固定，当前为 `v5.12.2` / `v2.16.1`。
- `assets/interface.json` 的 `agent.child_exec` 是 `python`，`child_args` 是
  `["./agent/main.py"]`；`controller` 为 `Adb`、`display_short_side: 720`。
- agent 侧把日志目录固定到 `<安装根>/debug/agent`（`agent/main.py` 的 `main()`），
  并做 `pi_log_snapshot()` / `pi_check_maafw_version()` 版本核对。
- 失败遥测由 MFA 上报 Sentry；`assets/interface.json` 的 `telemetry.sentry` 只有
  `dsn` 与 `failure_attachments_sample_rate` 两个键。

## 2. 入库的顶层目录

`git ls-tree -d HEAD` 得到 8 个目录：

| 目录 | 职责 |
| --- | --- |
| `assets/` | 资源与项目接口：`interface.json` + `resource/`（pipeline / 任务选项 / 图片 / 模型） |
| `agent/` | MaaFramework 的 Python agent 子进程，任务逻辑与共用件 |
| `tools/` | 发布打包、校验、遥测分析、绘图、性能、清理、编辑器 schema |
| `tests/` | agent 纯逻辑打桩测试（不连设备、不起 GUI） |
| `docs/` | 中文开发文档与白板 |
| `.github/` | CI 工作流、changelog 配置、Issue/PR 模板 |
| `deps/tools/` | MaaFramework 的 schema 快照（入库文件，不会自动同步） |
| `.vscode/` | 编辑器设置、任务、调试配置与 pipeline schema 引用 |

根目录的主要文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 面向用户的介绍、功能列表、下载方式 |
| `AGENTS.md` | 本仓库工作约定的索引（被 `.gitignore` 忽略，不入库） |
| `package.json` / `package-lock.json` / `pnpm-lock.yaml` | Node 工具链，钉住 `@nekosu/maa-tools` 1.1.3 |
| `requirements.in` / `requirements.txt` | Python 依赖，`maafw==5.12.2`（uv 编译产物） |
| `maatools.config.mts` | `npx @nekosu/maa-tools check` 的配置，必须在仓库根跑 |
| `.gitmodules` | `assets/MaaCommonAssets` 子模块指向 MaaXYZ/MaaCommonAssets |
| `LICENSE` / `logo.png` | MIT 许可与图标 |

## 3. 资源层 `assets/`

`assets/interface.json` 是 PI V2 的入口，`interface_version: 2`、
`version: "1.0.0-beta.70"`。当前声明 12 个任务、8 个 `import` 片段。

| 内容 | 位置 | 数量 / 说明 |
| --- | --- | --- |
| 任务入口 | `interface.json` 的 `task[].entry` | 12 个，如 `TaskStartUp` / `TaskEntrust` / `TaskFactory` |
| 任务选项片段 | `resource/tasks/option/` | 7 个 json，`import` 里逐项引用 |
| 预设片段 | `resource/tasks/preset/default.json` | 1 个 |
| 节点定义 | `resource/pipeline/*.json` | 18 个 json（`base.json` 为通用节点） |
| 识别模板图片 | `resource/image/` | 77 个文件 |
| OCR 模型 | `resource/model/ocr/` | 3 个文件（`det.onnx` / `rec.onnx` / `keys.txt`），不入库，CI 打包时下载 |
| 公告文本 | `resource/announcement/` | 5 个文件，`welcome` 指向 `0-welcome.md` |

## 4. agent 层 `agent/`

- 入口 `agent/main.py`：`Tasker.set_log_dir()` → 版本核对 →
  `AgentServer.start_up(socket_id)` → `join()` → `shut_down()`。
- 顶层 13 个模块：`main.py`、`maafw_paths.py`、`parent_tracker.py`、`store.py`
  与各任务模块（`battle_*.py` / `entrust.py` / `escort.py` 等）。
- 共用件在 `agent/shared/`：`agentlog.py`、`generic.py`、`params.py`、
  `pienv.py`、`rect.py`。
- 三条固定约定：新模块必须在 `agent/main.py` 导入（装饰器只在导入时注册）；
  两个目录都注入 `sys.path`，模块一律按顶层名互导；`agent/shared/` 不放
  `__init__.py`，避免同名模块被加载两份。

## 5. 工具层 `tools/`

根目录脚本属于构建与发布链路；各子目录是独立工具，入口都是 `cli.py`
（`ci/` 与 `vscode/` 除外）。

| 位置 | 入口 | 职责 |
| --- | --- | --- |
| `tools/` 根 | `install.py` / `configure.py` | 按 tag 打包发布包、配置 OCR 资源 |
| `tools/` 根 | `sync-schema.mjs` / `optimize-images.mjs` / `check-png-lossless.mjs` | schema 快照同步、图片优化与无损检查 |
| `tools/ci/` | `setup_python_runtime.py` / `smoke_packaged_agent.py` | 给发布包准备随身解释器、包内冒烟 |
| `tools/validation/` | `cli.py` | `check-python` / `validate-refs` / `validate-schema` / `check-sentry-offline`；另有 `check_pyright.mjs` |
| `tools/sentry/` | `cli.py` | 遥测分析与出图（`fetch` / `task-failure` / `flow` / `selftest` 等），另有 `issue-resolve`（本地 `pre-push` 按提交正文关闭 issue） |
| `tools/drawing/` | `cli.py` | 由日志与证据包生成流程图、白板图 |
| `tools/perf/` | `cli.py` | 合成性能基准与真实日志轮次统计 |
| `tools/cleanup/` | `cli.py` | 清理 pytest / temp 产物 |
| `tools/vscode/` | — | 编辑器补全用的 pipeline 片段骨架与 `default_pipeline.json` 默认值 schema |

改完 `tools/` 下任何脚本要按 `tools/validation/README.md` 的「改完脚本的验收」
跑校验：`check-python`、`validate-refs`（含 `--self-test`）、ruff、
`check_pyright.mjs`，以及被改脚本自己的 `--help` 与自检。

## 6. CI / 发布

`.github/workflows/` 有 5 个工作流：

| 工作流 | 触发 | 主要 job / 作用 |
| --- | --- | --- |
| `check.yml` | push / PR / 手动 | `resource`：`check-python`、maa-tools check、`validate-schema`、`validate-refs`、`pytest` |
| `install.yml` | push tag `v*`、分支、PR、手动 | `meta` → `install` → `changelog` → `release` |
| `mirrorchyan_release.yml` / `_note.yml` | 手动 | 两个 job 当前 `if: false`，不发布到镜像 |
| `sync_schema_files.yml` | cron / 手动 | 只对上游 boilerplate 生效，本仓库空转 |

- `install.yml` 的 `meta` 用 `git describe --tags --match "v*"` 算 tag，
  产出 `is_release` / `is_prerelease`；`release` job 的 `if` 是
  `needs.meta.outputs.is_release == 'true'`。
- `install` job 下载 MaaFramework 与 MFAAvalonia 的 release、准备随身 Python、
  调 `tools/install.py`；`changelog` 用 `.github/cliff.toml` 生成 CHANGES.md；
  `release` 先建 draft 再用 `gh release edit --draft=false` 发布。
- `check.yml` 里 `validate-schema` 显式排除 `assets/resource/announcement` 与
  `assets/resource/tasks`（后者是 PI 片段，另用 `--task-dirs` 单查）。

## 7. 依赖固定点

| 依赖 | 固定方式 |
| --- | --- |
| MaaFramework | `requirements.txt` 的 `maafw==5.12.2` + `install.yml` 的 `MAAFW_VERSION` |
| MFAAvalonia | `install.yml` 的 `MFAA_VERSION: v2.16.1` |
| Node 工具 | `package.json` 的 `@nekosu/maa-tools: 1.1.3` + 两份 lockfile |
| schema 快照 | `deps/tools/*.schema.json` 与 `schema-manifest.json`，由 `tools/sync-schema.mjs` 手动同步 |
| 通用资源 | `.gitmodules` 的 `assets/MaaCommonAssets` 子模块 |

## 8. 文档与约定

- `docs/zh_cn/develop/`：开发上手、节点命名与分类、任务链结构、agent、
  GUI 调试、个性化配置、FAQ、PR 规范，以及本文（项目架构）。
- `docs/.markdownlint.yaml`：`npx markdownlint-cli2 "docs/**/*.md"` 的规则。
  工作区里的 `docs/白板/` 是未入库的白板目录。
- `.agents/`：AI 助手的项目记忆，按「开工前先查项目记忆」使用，其中
  `.agents/<领域>.md` 放结论、`.agents/evidence/<同名>.md` 放论据。
  `AGENTS.md` 与 `.agents/` 都被 `.gitignore` 忽略，不属于交付内容。
- 规范文档与校验脚本的版本号成对：`node-naming.md` ↔ `NAMING_SPEC_VERSION`、
  `pipeline-structure.md` ↔ `STRUCT_SPEC_VERSION`，改规则要同步常量。

## 9. 不计入架构的本机目录

以下目录在工作区存在但被 `.gitignore` 忽略，或是运行/构建产物，
不属于项目结构（出处：`.gitignore:443-474`）：

- 构建与发布产物：`build/`、`install/`、`deps/*`（但 `deps/.gitkeep` 与
  `deps/tools/` 的 schema 入库）、`assets/agent`（`install.py` 拷出的副本）、
  `assets/config`、`assets/debug`、`assets/resource/config`、`assets/resource/debug`。
- 解释器与缓存：`.python-runtime*/`、`node_modules/`、`.pytest_cache/`、
  `pytest-cache-files-*`、`temp/`、`.ruff_cache/`（自带 `.gitignore`）、
  `.firecrawl/*`。
- 运行日志：`debug/`；`logs/` 只在发布包里出现，工作区不存在。
  素材与工具：`ffmpeg/*`。
- 编辑器与个人笔记：`.obsidian/`、`白板/`、`.agents/`、`AGENTS.md`、`scripts/`。
