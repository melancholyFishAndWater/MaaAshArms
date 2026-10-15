# tools/validation —— 校验工具

本仓库所有"只读、判对错"的脚本：读文件、报问题、给退出码，**不改文件、不格式化、不产生产物**。
与 `tools/drawing/`（产出图片）、`tools/sentry/`（遥测分析）分开归置；构建与发布链路用的脚本
仍在 `tools/` 根与 `tools/ci/`。

## 功能取舍

| 不做 | 为什么 | 要它怎么办 |
|---|---|---|
| 格式化、自动修 | 目的只是"判对错"；会改文件的工具不该混进 CI 的校验步骤 | 各自编辑器/formatter，或单独提一个 PR |
| 把 `ruff` 默认规则集当门槛 | 默认集里大量是本仓库从未采纳的风格偏好（`TRY004`、`PERF402`、`SIM102` 等 178 条），当门槛只会训练人无视它 | 只把"真实缺陷"类当门槛（`F401`/`F841`/`E722`/`FURB162`/`S110`/`BLE001`/`PLW1510`/`RUF100`），现为零；要全量看就 `uvx ruff check tools`（受限沙箱下 `uv` 自己的三个目录都在工作区外、会被拒写，先 `UV_CACHE_DIR` / `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` 指进 `temp/`；`--no-cache` 不够，装工具那一步仍要写 `%APPDATA%\uv\tools`） |
| 把 `pyright` 写进 `package.json` | 两份 lockfile（`package-lock.json` 与 `pnpm-lock.yaml`）都是入库文件，加一个带平台二进制的依赖要重新生成两份 | 走 `npx`（`check_pyright.mjs`），首次需要网络 |
| 扫 vendored 目录 | `.python-runtime*/`、`install/`、`build/`、`node_modules/` 里有几千个第三方文件，查它们只会淹没真问题 | 只扫 `tools/**`、`agent/**`、`scripts/**` |
| 校验资源语义（数值范围、图片内容） | 那是 schema 与 maa-tools 的职责，本目录只查"引用与命名对不对得上" | `validate_schema.py`、`npx @nekosu/maa-tools check` |
| 不查需要语义判断的命名规则 | 只做结构层能判的：把"层级同义词"套成关键字会误报（`FeiduGetPart` 里的 `Part` 是拿"部件"这个宾语，不是层级）；"模块级可变状态"与"导出的配置"在源码里长得一样（都没有 `_` 前缀），静态判不出；"就位节点该不该有 `action`"实测 8 个就位节点都没有 `action`，规则与数据不符 | 人来判，写进评审意见 |
| 不写回文件头部的规范版本注解 | 校验脚本只读；写文件会让 CI 跑完工作区变脏 | 自己按 E3 的提示把 `使用命名规范版本 vX.Y` 改成当前版本 |
| 联网 | 除 pyright 首次拉取与 CI 里装 `jsonschema` 外不联网 | — |

## 怎么跑

按这个顺序，前面的失败会掩盖后面的结论：

```bash
# 1. 静态检查（零依赖、秒级）—— CI 里跑的就是它
python tools/validation/check_python.py
python tools/validation/check_python.py --format github     # CI 注解格式
python tools/validation/check_python.py --root agent        # 只查某个根

# 2. 跨文件名字与约定（纯标准库）
python tools/validation/validate_refs.py
python tools/validation/validate_refs.py --format github
python tools/validation/validate_refs.py --pipeline         # 只查某一域，可选 --tasks/--agent/--image/--interface
python tools/validation/validate_refs.py --self-test        # 只跑内置断言（26 项纯函数），不读资源

# 3. JSON Schema 校验（需要额外装包，见下）
python tools/validation/validate_schema.py \
    --schema-dir deps/tools --resource-dirs assets/resource \
    --exclude-dirs assets/resource/announcement assets/resource/tasks \
    --interface-files assets/interface.json --task-dirs assets/resource/tasks

# 4. Sentry 遥测工具的离线自检（零网络、零凭证）
python tools/validation/check_sentry_offline.py

# 5. 类型检查（需网络首次拉取 pyright）
node tools/validation/check_pyright.mjs
```

`npm run check:refs` / `check:refs:github` 是第 2 步的快捷方式。

## `check_python.py` 查什么

四类，都是**别的工具查不出来**的仓库特有约定：

| 检查 | 拦的是什么 |
|---|---|
| `syntax` | `ast.parse` 失败的文件 |
| `tool-path` | 文档/工作流/脚本里写了不存在的 `tools/...` 路径。2026-10 把脚本收进子目录时，靠它兜住了十几处漏改的引用 |
| `doc-access` | `__doc__.splitlines()` 这种直取：`__doc__` 可为 `None`，直接用抛 `AttributeError`；用 `report_common.module_summary(__doc__)` |
| `repo-root` | 声明的上溯级数与文件实际深度不符。**改了目录层级最容易静默搞错的就是它**——默认产出目录会落到 `tools/<子目录>/temp/...` 而不是仓库根 `temp/...` |

只在 `tools/**`、`agent/**`、`scripts/**` 里扫，不动 `.python-runtime*/`、`install/`、`build/`、
`node_modules/`（那里有几千个 vendored 文件）。

退出码 0 = 无 error；1 = 有 error。`warn` 不影响退出码。

## 命名规范版本（`validate_refs.py`）

**本脚本按 `NAMING_SPEC_VERSION` 那一版规范实现**，运行时会在输出里声明（console 头部、
`--format github` 的摘要、`--format json` 的 `spec_version` 字段）。

规范和脚本必须对齐，否则"这几条没查"会和"查了没问题"看起来一样：

| 发现 | 什么时候报 | 怎么办 |
|---|---|---|
| `E1-spec-missing`（error） | 找不到 `docs/zh_cn/develop/node-naming.md`，或文档里没有「版本号: vX.Y」 | 检查文档路径与头部版本号；改名要同步脚本里的 `NAMING_SPEC` |
| `E2-spec-version`（warn） | 文档的版本号与 `NAMING_SPEC_VERSION` 不一致（两个方向都报）；写法模板 `tools/vscode/pipeline.snippets.schema.json` 的 `$comment` 与它不一致也报 | 复核新规则要不要收进脚本，收完把 `NAMING_SPEC_VERSION` 与文档版本一起改 |
| `E3-stale-annotation`（info） | 某个 pipeline/tasks/agent 文件头部写着「使用命名规范版本 vX.Y 校验年月日 YY/MM/DD」，且 vX.Y 低于当前版本 | 按当前规范重看那个文件，然后把版本号改成当前值 |

**改命名规范（`node-naming.md`）的规则就要同步 +0.1 并复核脚本**：文档版本号、`NAMING_SPEC_VERSION`、
`tools/vscode/pipeline.snippets.schema.json` 的 `$comment` 三处是对齐的。文件没写注解的不报 E3
（老文件太多，报了等于噪音），只报"看过的"。

`A13-*` 里属于**护栏**（今天一条都不报，为的是以后别写错）的几条：引用就位节点必须带 `[JumpBack]`、
就位节点的 `next` 里不放业务 `Loop`（`LoopErrorByTimes` 属骨架，豁免）、探测节点不该有 `next`、
工作循环必须有 `next`、参数键 snake_case、pipeline 片段文件名 snake_case、不用 `ClickYes` 这类同义写法。

## 依赖与前置

- `check_python.py`、`validate_refs.py`、`check_sentry_offline.py`：**零第三方依赖**（纯标准库）。
- `validate_schema.py`：需要 `jsonschema==4.26.0` + `referencing==0.37.0`（CI 里现装）。`referencing`
  可选——没有就退回 `jsonschema` 自带的 `RefResolver`。
- `check_pyright.mjs`：需要 `npx`（首次要网络拉 pyright）。它把 npm 缓存指到 `temp/npm-cache`，
  跑完可删。**刻意不把 pyright 写进 `package.json`**：两份 lockfile（`package-lock.json` 与
  `pnpm-lock.yaml`）都是入库文件，加一个带平台二进制的依赖要重新生成两份 lock，收益不抵成本。

## 已知坑

- **本机装不上 `jsonschema`**：写沙箱临时目录报 `Permission denied`，改 `--target` 后两个镜像
  双双超时。所以本机只能核对 `validate_schema.py` 的参数与文件集，跑不了实校验。详见
  `.agents/evidence/project.md` 的「本机跑校验的环境坑」。
- **pyright 会报一批 `reportMissingImports` warning**（PIL / maa / jsonschema）。那是解析环境
  的事，不是代码问题——`pyright.config.json` 里已把该规则降成 warning，error 计数不受影响。
  `agent/` 的兄弟式 import（`from base import ...`）靠配置文件里的 `extraPaths` 才解析得到；
  少了它 `Generic[T]` 里的 `T` 会被当成 `Unknown` 并误报 `reportInvalidTypeForm`。
- **`uvx ruff` 能跑但要注意目录**：受限沙箱下 `UV_TOOL_DIR` / `UV_CACHE_DIR` 要指到工作区内，
  否则报 `failed to create directory ... os error 5`。
- **`ruff` 的默认规则集里有大量本仓库从未采纳的风格偏好**（`TRY004` 异常类型判定、`PERF402`
  列表拷贝、`SIM102` 嵌套 if 合并等）。本仓库只把"真实缺陷"类当门槛：
  `F401`（未使用导入）、`F841`（未使用变量）、`E722`（裸 except）、`FURB162`、`S110`、`BLE001`、
  `PLW1510`、`RUF100`。这些已清零：

  ```bash
  uvx ruff check tools --select F401,F841,E722,FURB162,S110,PLW1510,BLE001,RUF100
  ```
