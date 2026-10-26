# tools/validation —— 校验工具

本仓库所有"只读、判对错"的脚本：读文件、报问题、给退出码，**不改文件、不格式化、不产生产物**。
与 `tools/drawing/`（产出图片）、`tools/sentry/`（遥测分析）分开归置；构建与发布链路用的脚本
仍在 `tools/` 根与 `tools/ci/`。

**改完 `tools/` 下的脚本要按下面「[改完脚本的验收](#改完脚本的验收)」跑一遍并修到没报错才结束。**

## 功能取舍

| 不做 | 为什么 | 要它怎么办 |
| --- | --- | --- |
| 格式化、自动修 | 目的只是"判对错"；会改文件的工具不该混进 CI 的校验步骤 | 各自编辑器/formatter，或单独提一个 PR |
| 把 `ruff` 默认规则集当门槛 | 默认集里大量是本仓库从未采纳的风格偏好（2026-10-26 实测 47 条：`ISC004` 13、`TRY004` 11、`PERF402` 3 等），当门槛只会训练人无视它 | 只把"真实缺陷"类当门槛（`F401`/`F841`/`E722`/`FURB162`/`S110`/`BLE001`/`PLW1510`/`RUF100`），现为零；要全量看就 `uvx ruff check tools`（沙箱下 `uv` 的目录要指进工作区，见「已知坑」） |
| 把 `pyright` 写进 `package.json` | 两份 lockfile（`package-lock.json` 与 `pnpm-lock.yaml`）都是入库文件，加一个带平台二进制的依赖要重新生成两份 | 走 `npx`（`check_pyright.mjs`），首次需要网络 |
| 扫 vendored 目录 | `.python-runtime*/`、`install/`、`build/`、`node_modules/` 里有几千个第三方文件，查它们只会淹没真问题 | 只扫 `tools/**`、`agent/**`、`scripts/**` |
| 校验资源语义（数值范围、图片内容） | 那是 schema 与 maa-tools 的职责，本目录只查"引用与命名对不对得上" | `validate_schema.py`、`npx @nekosu/maa-tools check` |
| 不查需要语义判断的命名规则 | 只做结构层能判的：把"层级同义词"套成关键字会误报（`FeiduGetPart` 里的 `Part` 是拿"部件"这个宾语，不是层级）；"模块级可变状态"与"导出的配置"在源码里长得一样（都没有 `_` 前缀），静态判不出；"就位节点该不该有 `action`"实测 8 个就位节点都没有 `action`，规则与数据不符 | 人来判，写进评审意见 |
| 不写回文件头部的规范版本注解 | 校验脚本只读；写文件会让 CI 跑完工作区变脏 | 自己按 E3 的提示改注解版本（三种写法都认，见「规范版本」一节） |
| 查 `desc` 该标没标标记 | 标记是给人读的提示，不是字段：`desc` 由模板生成，`[jp]` 按 `node-naming.md` §3 不标也成立（今天 83 个带 `[jp]` 的节点里 29 个还带 `next`） | 评审时人工看；机器能判的是"标了但字段对不上"，即 `A19-desc-marker-*` 那三条 |
| 联网 | 除 pyright 首次拉取与 CI 里装 `jsonschema` 外不联网 | — |

## 怎么跑

本目录五条，加上 CI 的 `check` 工作流另外跑的两条，按这个顺序跑：前面的失败会掩盖后面的结论。

```bash
# 1. 静态检查（零依赖、秒级）—— CI 里跑的就是它
python tools/validation/check_python.py                     # 不传 --root 时扫 tools / agent / scripts / tests
python tools/validation/check_python.py --format github     # CI 注解格式
python tools/validation/check_python.py --root agent        # 只查某个根（可重复）

# 2. 跨文件名字与约定（纯标准库）—— 不传域参数就是全量
python tools/validation/validate_refs.py
python tools/validation/validate_refs.py --format github
python tools/validation/validate_refs.py --pipeline assets/resource/pipeline   # 只查某一域，路径可重复
python tools/validation/validate_refs.py --tasks assets/resource/tasks         # 另有 --agent / --image / --interface
python tools/validation/validate_refs.py --self-test        # 只跑内置断言（纯函数），不读资源
python tools/validation/validate_refs.py --ignore A17-guard-in-next   # 放行指定 id（逗号分隔，可重复）
python tools/validation/validate_refs.py --fail-on warn     # 把 warn 也算失败（默认只算 error；当前仓库 warn 非零，这条会 exit 1）

# 3. JSON Schema 校验（要 jsonschema + referencing；本机已装，见「依赖与前置」）
python tools/validation/validate_schema.py \
    --schema-dir deps/tools --resource-dirs assets/resource \
    --exclude-dirs assets/resource/announcement assets/resource/tasks \
    --interface-files assets/interface.json --task-dirs assets/resource/tasks
# 资源目录**顶层**的 config/ 会被自动跳过：那是 MaaCore 的运行时选项（maa_option.json：
# draw_quality / logging / save_draw …，标量键值），且该目录在 .gitignore 里，CI 干净检出时不存在。
# 不跳过的话本机每次都会多 5 条 "is not of type 'object'" 的假 error。
# 控制台编码不用先设：脚本自己把输出固定成 UTF-8，回显乱码见「已知坑」。

# 4. Sentry 遥测工具的离线自检（零网络、零凭证；没有参数，跑起来就是自检）
python tools/validation/check_sentry_offline.py

# 5. 类型检查（需网络首次拉取 pyright）
node tools/validation/check_pyright.mjs

# 6. CI 的 check 工作流还跑两条本目录之外的
npx @nekosu/maa-tools check     # 资源结构与数值；先 npm ci，首次要网络
python -m pytest -q tests       # agent 纯逻辑打桩，不连设备
```

三条使用要点：

- **域参数都带路径**：`--pipeline` / `--tasks` / `--agent` / `--image` / `--interface` 各收一个路径、可重复，
  漏写路径 argparse 直接报 `expected one argument`。`E` 类判据（文档版本、文件头注解）不分域，每次都会一起报。
- **放宽或放行**：`--fail-on warn` 把 warn 也算失败（默认只算 error）；`--ignore <id>` 放行单条判据，可重复、逗号分隔。
- **快捷方式只覆盖 agent 域**：`npm run check:refs` / `check:refs:github` 就是 `--agent agent`
  （agent 域 + `E` 类注解），不是全量；要全量直接跑第 2 步第一条。

第 6 步那两条不在本目录，但 CI 的 `check` 工作流会跑，跑不了就照实说明：`npx @nekosu/maa-tools check`
要先 `npm ci` 且首次要网络；`python -m pytest -q tests` 要装 `pytest`，本机是全局解释器、没装就用
`uv run --with pytest --no-project python -m pytest -q tests`（`maa` 由 `maafw` 提供，本机已有）。
2026-10-26 本机实测：`npx @nekosu/maa-tools check` 只打 npm notice、exit 1，直接
`node node_modules/@nekosu/maa-tools/bin/maa-tools check` 也是 exit 1 且没有输出——这一条以 CI 为准。

## 改完脚本的验收

**改完 `tools/` 下任何脚本（新建、改逻辑、改注释、改依赖、改文档里的命令都算）都要跑校验并修到没报错才结束。**
按这个顺序跑，全部 exit 0 才算完；有报错就修，修完从头再跑一遍。

```bash
python tools/validation/check_python.py --root tools      # 1. 语法 / 悬空工具路径 / __doc__ 直取 / 仓库根级数
python tools/validation/validate_refs.py --self-test      # 2. 跨文件引用与规范版本（纯函数断言）
python tools/validation/validate_refs.py                  #    全量：必须是 error 0
uvx ruff check tools --select F401,F841,E722,FURB162,S110,PLW1510,BLE001,RUF100   # 3. 真实缺陷类 lint
node tools/validation/check_pyright.mjs                   # 4. 类型检查（需 npx，首次要网络）

# 5. 改到哪个脚本，就跑它自己的 CLI 与自检
python -m tools.sentry.cli selftest                       # 遥测工具：19 项
python tools/validation/check_sentry_offline.py
python tools/drawing/flow_from_log.py --help
python tools/drawing/flow_cases.py --help
```

**通过判据**  

- `check_python.py`、`ruff`、`check_pyright.mjs` 必须 **0 error / 0 命中**；`validate_refs.py` 必须 `error 0`。
  它的 `warn` / `info` 不算失败，但要么是仓库既有的已知项（旧 `Is` 前缀、非 CamelCase 图片名、
  `max_hit` 预算、过期头部注解），要么在结论里说清为什么留下。
- 脚本有自己的自检（`--self-test`、`selftest` 子命令）时**必须全过**；新加的可复用逻辑要补进自检。
- 某个校验**跑不了**（需要网络、缺包、缺设备）时，在结论里写明"没跑 + 为什么"，不能默认通过。
- 改的是 CI 会调的入口（本目录三个 `.py`），再跑一遍 CI 实际用的命令（见上「怎么跑」第 1–3 步）；
  同时改了 `agent/` 或 pipeline 数据的，第 6 步那两条也要跑。
- 改了渲染口径的（`tools/drawing/flow_from_log.py`）还要按 [`tools/drawing`](../drawing/README.md) 的
  「已知坑」bump `RENDERER_VERSION` 并重画一张核对。

**两处会制造"假红"的坑**（2026-10-20 实测）

- **多行 `except (` 后面挂抑制注释**：ruff 0.17 起会把抑制判成"未使用"（`RUF100`）并连带报 `S110`/`BLE001`。
  用 `contextlib.suppress(Exception)`，或把抑制留在**单行** `except` 上（理由另起一行注释）。
  格式化工具会把长行折行，别依赖"正好没折"——那次"规范化代码格式"提交就让 8 条假红回归过。
- **`validate_schema.py` 跳过资源目录顶层的 `config/`**：那是 MaaCore 的运行时选项
  （`maa_option.json` 是标量键值），且该目录在 `.gitignore` 里；不跳过时本机会多 5 条
  `is not of type 'object'` 假 error。判据只看顶层，任务片段里真叫 `config` 的子目录照常校验。

**一处会漏检的盲区：第三方库的类型错误**（2026-10-20 实测）

pyright 在本机拿不到 `site-packages`（仓库里没有 venv），配置又把 `reportMissingImports` 降成 warning，
所以**装在本机解释器里的第三方库不参与类型检查**。2026-10 那条 `reportAssignmentType`
（`type[UnidentifiedImageError]` 不可分配给 `type[_UnidentifiedImage]`）就是这样漏掉的：编辑器能看到
已装的 Pillow、因此报红，`check_pyright.mjs` 却是 0 error。

要查这一层，临时往 `tools/validation/pyright.config.json` 的 `extraPaths` 里加本机的
`...\Lib\site-packages`（Windows 全局安装形如
`C:/Users/<用户>/AppData/Local/Programs/Python/Python3xx/Lib/site-packages`），跑一次
`node tools/validation/check_pyright.mjs`，**看完把那一行删掉**（本机路径不能入库）。
判据同第 4 步：0 error；`Import "PIL" could not be resolved` 那几条 warning 消失，说明这次真解析上了。

**按 Pylance 的口径查一遍**（2026-10-22 加）

Pylance 是 VS Code 内的 LSP，没有命令行入口（`dist/server.bundle.js` 只在插件宿主里启动），
所以"用 Pylance 检查"只能拿**同一个引擎 + 同一套设置**复现：`pyright.config.json` 的项目根是
`tools/validation/`，`tools.*` 这种从仓库根起算的导入在里面解析不到；Pylance 的 rootUri 是仓库根。
要复现，把下面这份临时配置**放在仓库根**（文件名随便，例如 `pyright.pylance.json`，用完删掉——
里面的 `site-packages` 是本机路径）：

```json
{
    "include": ["tools"],
    "exclude": ["**/node_modules", "**/__pycache__", "**/build", "**/install", "**/temp", "**/deps"],
    "extraPaths": [
        "agent/shared",
        "C:/Users/<用户>/AppData/Local/Programs/Python/Python3xx/Lib/site-packages"
    ],
    "typeCheckingMode": "basic",
    "reportMissingImports": "warning",
    "reportMissingTypeStubs": "none",
    "reportDeprecation": "warning"
}
```

`typeCheckingMode` / `reportMissingImports` 抄的是 VS Code 用户设置里的
`python.analysis.*`（本机是 `basic`）；`extraPaths` 抄工作区 `.vscode/settings.json`
（那里只有 `./agent/shared`），再补一条解释器的 `site-packages` —— Pylance 用
`python.defaultInterpreterPath` 选的那个解释器，所以它能看见 Pillow，pyright 裸跑看不见。
然后 `npx --yes pyright --project pyright.pylance.json`。

2026-10-22 用这份配置查出 14 条 error，全是 `check_pyright.mjs` 两侧都看不见的真问题：
`steps` 与局部函数 `step` 同名（5 条）、`jsonschema.RefResolver` 是动态属性（9 条）。
**判据 0 error / 0 warning**；警告数会随 `site-packages` 解析情况变化，只有 error 算失败。

## `check_python.py` 查什么

四类，都是**别的工具查不出来**的仓库特有约定：

| 检查 | 拦的是什么 |
| --- | --- |
| `syntax` | `ast.parse` 失败的文件 |
| `tool-path` | 文档/工作流/脚本里写了不存在的 `tools/...` 路径。2026-10 把脚本收进子目录时，靠它兜住了十几处漏改的引用 |
| `doc-access` | `__doc__.splitlines()` 这种直取：`__doc__` 可为 `None`，直接用抛 `AttributeError`；用 `report_common.module_summary(__doc__)` |
| `repo-root` | 声明的上溯级数与文件实际深度不符。**改了目录层级最容易静默搞错的就是它**——默认产出目录会落到 `tools/<子目录>/temp/...` 而不是仓库根 `temp/...` |

默认扫 `tools/`、`agent/`、`scripts/`、`tests/` 四个根（`--root` 可换、可重复），不动
`.python-runtime*/`、`install/`、`build/`、`node_modules/`（那里有几千个 vendored 文件）。

退出码 0 = 无 error；1 = 有 error。`warn` 不影响退出码。

## 规范版本（`validate_refs.py`）

**本脚本按两份规范的各自版本实现**：`NAMING_SPEC_VERSION`（`node-naming.md`）与
`STRUCT_SPEC_VERSION`（`pipeline-structure.md`）。运行时只在输出里声明命名规范那一版（console 头部、
`--format github` 的摘要、`--format json` 的 `spec_version` 字段）。命名类判据（`A13-*` / `A19-*` …）
按命名文档实现，结构类判据（A4/A5/A7/A8/A9/A14/A16…）按结构文档实现，所以两份常量都要跟着对齐，
哪一份落后都在 `E2` 里报。

规范、模板、脚本必须对齐，否则"这几条没查"会和"查了没问题"看起来一样：

| 发现 | 什么时候报 | 怎么办 |
| --- | --- | --- |
| `E1-spec-missing`（error） | 找不到 `node-naming.md` / `pipeline-structure.md`，或文档里没有「版本号: vX.Y」 | 检查文档路径与头部版本号；改名要同步脚本里的 `NAMING_SPEC` / `STRUCT_SPEC` |
| `E2-spec-version`（warn） | 某份文档的版本号与对应常量不一致（两个方向都报）；写法模板 `tools/vscode/pipeline.snippets.schema.json` 的 `$comment` 里两份规范的版本也逐份比 | 复核新规则要不要收进脚本，收完把对应常量（`NAMING_SPEC_VERSION` / `STRUCT_SPEC_VERSION`）与文档版本一起改 |
| `E3-stale-annotation`（info） | 某个 pipeline/tasks/agent 文件头部注解低于对应文档的当前版本。**三种写法都认**：`使用命名规范版本 vX.Y 校验年月日 …`（旧）、`使用节点命名与分类 vX.Y …` + `使用任务链结构 vX.Y …`（新，一行一份）、带不带校验日期都行 | 按当前规范重看那个文件，然后把版本号改成当前值 |

**改规范的规则就要同步 +0.1 并复核脚本**：文档版本号、对应常量、
`tools/vscode/pipeline.snippets.schema.json` 的 `$comment` 三处是对齐的。文件没写注解的不报 E3
（老文件太多，报了等于噪音），只报"看过的"。一份文件可以同时挂两份规范的注解，落后的那份会被单独报出来。

`A13-*` 里属于**护栏**（今天一条都不报，为的是以后别写错）的几条：引用就位节点必须带 `[JumpBack]`、
就位节点的 `next` 里不放业务 `Loop`、探测节点不该有 `next`、
工作循环必须有 `next`、参数键 snake_case、pipeline 片段文件名 snake_case、不用 `ClickYes` 这类同义写法。

`A15-error-branch-*` 是同一类护栏，管通用错误分支 `BaseBranchError`（`base.json`）挂在哪：引用必须带
`[JumpBack]`；不能排在取反守卫（`Ensure*` / 顶层 `inverse`）之后——弹窗挡住页面时那些守卫必然命中，错误分支
永远轮不到；也不能排在自己会点「确认」的候选之前——同一个确认会被当成错误弹窗点掉。只判同一列表内的先后，
"哪个模板在弹窗下仍会命中"静态判不了，按人工。判据见 `docs/zh_cn/develop/pipeline-structure.md` §9。

`A19-desc-marker-*` 管 `desc` 里的方括号标记（`node-naming.md` §6：一共五个，含义固定）：

| 规则 | 级别 | 判据 | 2026-10-26 实测 |
| --- | --- | --- | --- |
| `A19-desc-marker-unknown` | warn | 用了 `[end]` / `[jp]` / `[once]` / `[max]` / `[Loop]` 之外的写法（含大小写不符） | 0 |
| `A19-desc-marker-mismatch` | warn | `[max]` 没有 `max_hit`；`[once]` 的 `max_hit` 不是 1；`[Loop]` 标在名字不含 `Loop` 的节点上 | 1（`EntrustClickNextChapter` 标了 `[max]` 却没有 `max_hit`） |
| `A19-desc-marker-end` | info | `[end]` 却有 `next`，且不是只收 `TaskStop` / `TaskStopError` | 2（`EscortClickCancel`、`GiftClickFirstcharacter`） |

**只查"标了标记、字段对不上"，不查"该标没标"**：`desc` 由模板生成，而 `[jp]` 按 §3 只是提示标记——
今天 83 个带 `[jp]` 的节点里有 29 个还带 `next`，标不标都不构成判据。标记按**开头连续**的那几个算
（`[once][jp] …`），正文里当括号用的字（"点击[确认]按钮"）不算，所以那类不会误报。三条都能 `--ignore` 单独放行。

`A16` / `A17` / `A18` 管**识别预算**（`pipeline-structure.md` §10）：

| 规则 | 级别 | 判据 | 2026-10-26 实测 |
| --- | --- | --- | --- |
| `A16-ensure-depth` | warn | `Ensure` 嵌套深度 > 2（单层 5 次识别，每多一层再付 5 次） | 2（都在 `entrust`：`EntrustEnsureStage` 深度 3、`EntrustEnsureLevelInfo` 深度 4） |
| `A17-guard-in-next` | info | 节点 `next` 里含本节点 `recognition` 已用过的探针（同一轮识别两遍） | 14 |
| `A18-next-length` | warn | `Task` / `Ensure` 的 `next` > 4 项，`Loop` > 5 项 | 10（全是 Task/Ensure 档，没有 `Loop` 档） |

`A17` 只报「同一轮会问两遍」这个事实，**留删由人按实测救回率定**（<25% 该删）——救回率要日志实测，
静态判不了。`A16` 的修法是改用 `Branch` 平铺到调用方 `next`（同样效果只要 1 次守卫），不是把
`Ensure` 删掉换名字。三条都可以 `--ignore` 单独放行。

## 依赖与前置

- `check_python.py`、`validate_refs.py`、`check_sentry_offline.py`：**零第三方依赖**（纯标准库）。
- `validate_schema.py`：需要 `jsonschema==4.26.0` + `referencing==0.37.0`。CI 里现装，本机全局解释器
  里这两个版本也已就位（2026-10-26 实测）。`referencing` 可选——没有就退回 `jsonschema` 自带的
  `RefResolver`。
- `check_pyright.mjs`：需要 `npx`（首次要网络拉 pyright）。它把 npm 缓存指到 `temp/npm-cache`，
  跑完可删。**刻意不把 pyright 写进 `package.json`**：两份 lockfile（`package-lock.json` 与
  `pnpm-lock.yaml`）都是入库文件，加一个带平台二进制的依赖要重新生成两份 lock，收益不抵成本。

## 已知坑

- **`validate_schema.py` 的输出编码**：`validate_refs.py` 与 `validate_schema.py` 把 stdout 固定成
  UTF-8，`check_python.py` 与 `check_sentry_offline.py` 只把编不出的字符换成替代字符。所以控制台不是
  UTF-8 时校验不会中断，但中文与 `✓` / `❌` 会显示成乱码——先设 `[Console]::OutputEncoding` 或
  `PYTHONIOENCODING=utf-8` 再看回显（2026-10-26 核对）。
- **pyright 会报一批 `reportMissingImports` warning**（PIL / maa / jsonschema）。那是解析环境
  的事，不是代码问题——`pyright.config.json` 里已把该规则降成 warning，error 计数不受影响。
  `agent/` 的兄弟式 import（`from rect import ...`）靠配置文件里的 `extraPaths` 才解析得到，
  **`agent/` 与 `agent/shared/` 两个目录都要列**（`shared/` 下的模块按顶层名互相导入）；
  少了会误报 `reportMissingImports`，`agent/store.py` 的 `Generic[T]` 也会跟着报错。
- **`validate_refs.py` 的 agent 发现是递归的**：`rglob` 找 `<agent 目录>/**/*.py`，模块名按
  "相对 agent 根的点分名"建图（`agent/shared/rect.py` → `shared.rect`），`--agent` 传目录时同样递归。
  `D1-unimported-module` 因此能追查子目录里的模块（`agent/main.py` 的 import 图是唯一入口）；
  反过来，**把注册类挪进子目录时不要只 `glob` 顶层**，否则 `A2` / `A3` / `D2` 与"注册名必须能在
  agent 里搜到"会静默漏掉它们。`--agent` 传子目录只收窄**报告范围**（`report().in_scope`），
  载入集取并集——传单个文件也不会让同一棵树里注册的自定义名变成 `A2-unknown-custom`
  （早先的实现是替换载入集，`--agent agent/entrust.py` 会误报 3 条 error）。
- **`uvx ruff` 能跑但要注意目录**：受限沙箱下 `UV_CACHE_DIR` 与 `UV_TOOL_DIR` 都要指到工作区内
  （`temp/uvcache`、`temp/uvtools`）。少了缓存目录报 `failed to create directory ... os error 5`；
  只设缓存目录、工具目录仍留在 `%APPDATA%\uv\tools` 时，报的是
  `failed to open file ...\uv\tools\.lock: 拒绝访问`（2026-10-26 两次实测）。
- **`ruff` 的默认规则集里有大量本仓库从未采纳的风格偏好**（`TRY004` 异常类型判定、`PERF402`
  列表拷贝、`SIM102` 嵌套 if 合并等）。本仓库只把"真实缺陷"类当门槛：
  `F401`（未使用导入）、`F841`（未使用变量）、`E722`（裸 except）、`FURB162`、`S110`、`BLE001`、
  `PLW1510`、`RUF100`。门槛**只覆盖 `tools/`**（`tools/sentry` + `tools/validation` 是常查对象）；
  `agent/` 不进这个门槛：`agent/main.py` 的那批 import 是注册入口（名字一个都不在本文件里用，
  删掉就静默丢注册，已在该文件加 `# ruff: noqa: F401` 说明），`agent/**` 里 `ble001` 的广捕获是
  "回调异常别让任务崩"的既定写法且都打了日志。要查 `agent/` 就去掉 `BLE001` 单独跑。这些已清零：

  ```bash
  uvx ruff check tools --select F401,F841,E722,FURB162,S110,PLW1510,BLE001,RUF100
  ```

- **抑制注释会被格式化工具"折"失效**：`except (\n Exception\n):  # noqa: BLE001, S110` 这种多行写法
  在 ruff 0.17 起会被判成"抑制未使用"（`RUF100`）并连带把 `S110`/`BLE001` 报出来——2026-10-20 的
  "规范化代码格式"提交就把上一版**单行**写法折成了多行，8 条又回来了。`validate_refs.py` 现在用
  `contextlib.suppress(Exception)`（不是 try/except，两个版本都不报、也不怕折行）；确实要 `as error`
  取异常对象的地方（`selftest.py`）把抑制留在**单行** `except` 上，理由另起一行注释。
