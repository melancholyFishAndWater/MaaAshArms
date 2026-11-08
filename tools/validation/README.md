# tools/validation —— 校验工具

本仓库所有"只读、判对错"的脚本：读文件、报问题、给退出码，**不改文件、不格式化、不产生产物**。
与 `tools/drawing/`（产出图片）、`tools/sentry/`（遥测分析）分开归置；构建与发布链路用的脚本
仍在 `tools/` 根与 `tools/ci/`。

规则目录见 [`docs/rules.md`](docs/rules.md)，已知坑见 [`docs/pitfalls.md`](docs/pitfalls.md)，
模块分布与改法见 [`AGENTS.md`](AGENTS.md)。

**改完 `tools/` 下的脚本要按下面「[改完脚本的验收](#改完脚本的验收)」跑一遍并修到没报错才结束。**

## 怎么跑

本目录四条，加上 CI 的 `check` 工作流另外跑的两条，按这个顺序跑：前面的失败会掩盖后面的结论。

```bash
# 1. 静态检查（零依赖、秒级）—— CI 里跑的就是它
python tools/validation/cli.py check-python                     # 不传 --root 时扫 tools / agent / scripts / tests
python tools/validation/cli.py check-python --format github     # CI 注解格式
python tools/validation/cli.py check-python --root agent        # 只查某个根（可重复）

# 2. 跨文件名字与约定（纯标准库）—— 不传域参数就是全量
python tools/validation/cli.py validate-refs
python tools/validation/cli.py validate-refs --format github
python tools/validation/cli.py validate-refs --pipeline assets/resource/pipeline   # 只查某一域，路径可重复
python tools/validation/cli.py validate-refs --tasks assets/resource/tasks         # 另有 --agent / --image / --interface
python tools/validation/cli.py validate-refs --self-test        # 只跑内置断言（纯函数），不读资源
python tools/validation/cli.py validate-refs --ignore A17-guard-in-next   # 放行指定 id（逗号分隔，可重复）
python tools/validation/cli.py validate-refs --fail-on warn     # 把 warn 也算失败（默认只算 error；当前仓库 warn 非零，这条会 exit 1）

# 3. JSON Schema 校验
python tools/validation/cli.py validate-schema \
    --schema-dir deps/tools --resource-dirs assets/resource \
    --exclude-dirs assets/resource/announcement assets/resource/tasks \
    --interface-files assets/interface.json --task-dirs assets/resource/tasks

# 4. Sentry 遥测工具的离线自检（零网络、零凭证；没有参数，跑起来就是自检）
python tools/validation/cli.py check-sentry-offline

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

`validate-schema` 会跳过资源目录**顶层**的 `config/`：那是 MaaCore 的运行时选项（`maa_option.json`：
`draw_quality` / `logging` / `save_draw` …，标量键值），且该目录在 `.gitignore` 里，CI 干净检出时不存在。
不跳过的话本机每次都会多 5 条 "is not of type 'object'" 的假 error。

第 6 步那两条不在本目录，但 CI 的 `check` 工作流会跑，跑不了就照实说明：`npx @nekosu/maa-tools check`
要先 `npm ci` 且首次要网络；`python -m pytest -q tests` 要装 `pytest`，本机是全局解释器、没装就用
`uv run --with pytest --no-project python -m pytest -q tests`（`maa` 由 `maafw` 提供，本机已有）。
2026-10-26 本机实测：`npx @nekosu/maa-tools check` 只打 npm notice、exit 1，直接
`node node_modules/@nekosu/maa-tools/bin/maa-tools check` 也是 exit 1 且没有输出——这一条以 CI 为准。

## 改完脚本的验收

**改完 `tools/` 下任何脚本（新建、改逻辑、改注释、改依赖、改文档里的命令都算）都要跑校验并修到没报错才结束。**
按这个顺序跑，全部 exit 0 才算完；有报错就修，修完从头再跑一遍。

```bash
python tools/validation/cli.py check-python --root tools      # 1. 语法 / 悬空工具路径 / __doc__ 直取 / 仓库根级数
python tools/validation/cli.py validate-refs --self-test      # 2. 跨文件引用与规范版本（纯函数断言）
python tools/validation/cli.py validate-refs                  #    全量：必须是 error 0
uvx ruff check tools --select F401,F841,E722,FURB162,S110,PLW1510,BLE001,RUF100   # 3. 真实缺陷类 lint
node tools/validation/check_pyright.mjs                   # 4. 类型检查（需 npx，首次要网络）

# 5. 改到哪个脚本，就跑它自己的 CLI 与自检
python -m tools.sentry.cli selftest                       # 遥测工具：21 项
python tools/validation/cli.py check-sentry-offline
python tools/drawing/cli.py flow --help
python tools/drawing/cli.py cases --help
python tools/perf/cli.py bench --help
python tools/perf/cli.py bench --selftest                     # 性能基准（合成）：18 项（不建资源）
python tools/perf/cli.py rounds --help
python tools/perf/cli.py rounds --selftest                    # 性能基准（真实日志）：25 项
```

**通过判据**  

- `check-python`、`ruff`、`check_pyright.mjs` 必须 **0 error / 0 命中**；`validate-refs` 必须 `error 0`。
  它的 `warn` / `info` 不算失败，但要么是仓库既有的已知项（旧 `Is` 前缀、非 CamelCase 图片名、
  `max_hit` 预算、过期头部注解），要么在结论里说清为什么留下。
- 脚本有自己的自检（`--self-test`、`selftest` 子命令）时**必须全过**；新加的可复用逻辑要补进自检。
- 某个校验**跑不了**（需要网络、缺包、缺设备）时，在结论里写明"没跑 + 为什么"，不能默认通过。
- 改的是 CI 会调的入口（`cli.py check-python` / `validate-refs` / `validate-schema`），再跑一遍 CI
  实际用的命令（见上「怎么跑」第 1–3 步）；同时改了 `agent/` 或 pipeline 数据的，第 6 步那两条也要跑。
- 改了渲染口径的（`tools/drawing/lib/flow_from_log.py`）还要按 [`tools/drawing` 的已知坑](../drawing/docs/pitfalls.md)
  「已知坑」bump `RENDERER_VERSION` 并重画一张核对。

**会制造"假红"、以及会漏检的坑**（ruff 抑制被折行、`validate-schema` 的 `config/` 跳过、pyright 看不见
`site-packages`、Pylance 复现配方）见 [`docs/pitfalls.md`](docs/pitfalls.md)。

## 依赖与前置

- `check-python` / `validate-refs` / `check-sentry-offline`：**零第三方依赖**（纯标准库）。
- `validate-schema`：需要 `jsonschema==4.26.0` + `referencing==0.37.0`。CI 里现装，本机全局解释器
  里这两个版本也已就位（2026-10-26 实测）。`referencing` 可选——没有就退回 `jsonschema` 自带的
  `RefResolver`。
- `check_pyright.mjs`：需要 `npx`（首次要网络拉 pyright）。它把 npm 缓存指到 `temp/npm-cache`，
  跑完可删。**刻意不把 pyright 写进 `package.json`**：两份 lockfile（`package-lock.json` 与
  `pnpm-lock.yaml`）都是入库文件，加一个带平台二进制的依赖要重新生成两份 lock，收益不抵成本。
