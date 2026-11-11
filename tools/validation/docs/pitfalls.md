# tools/validation —— 已知坑（pitfalls）

跑校验时最容易误判的几类现场。用法与验收见 [`../README.md`](../README.md)，规则目录见
[`rules.md`](rules.md)。

## 两处会制造"假红"的坑（2026-10-20 实测）

- **多行 `except (` 后面挂抑制注释**：ruff 0.17 起会把抑制判成"未使用"（`RUF100`）并连带报 `S110`/`BLE001`。
  用 `contextlib.suppress(Exception)`，或把抑制留在**单行** `except` 上（理由另起一行注释）。
  格式化工具会把长行折行，别依赖"正好没折"——那次"规范化代码格式"提交就让 8 条假红回归过。
- **`validate-schema` 跳过资源目录顶层的 `config/`**：那是 MaaCore 的运行时选项
  （`maa_option.json` 是标量键值），且该目录在 `.gitignore` 里；不跳过时本机会多 5 条
  `is not of type 'object'` 假 error。判据只看顶层，任务片段里真叫 `config` 的子目录照常校验。

## 一处会漏检的盲区：第三方库的类型错误（2026-10-20 实测）

pyright 在本机拿不到 `site-packages`（仓库里没有 venv），配置又把 `reportMissingImports` 降成 warning，
所以**装在本机解释器里的第三方库不参与类型检查**。2026-10 那条 `reportAssignmentType`
（`type[UnidentifiedImageError]` 不可分配给 `type[_UnidentifiedImage]`）就是这样漏掉的：编辑器能看到
已装的 Pillow、因此报红，`check_pyright.mjs` 却是 0 error。

要查这一层，临时往 `tools/validation/pyright.config.json` 的 `extraPaths` 里加本机的
`...\Lib\site-packages`（Windows 全局安装形如
`C:/Users/<用户>/AppData/Local/Programs/Python/Python3xx/Lib/site-packages`），跑一次
`node tools/validation/check_pyright.mjs`，**看完把那一行删掉**（本机路径不能入库）。
判据同第 4 步：0 error；`Import "PIL" could not be resolved` 那几条 warning 消失，说明这次真解析上了。

## 按 Pylance 的口径查一遍（2026-10-22 加）

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

## 其他已知坑

- **`*.new.py` 临时拷贝会让全量校验假红，已跳过**（2026-11-11）：仓库约定改脚本要先拷成
  同目录的 `<原名>.new.py` 再改、验证通过后原子替换；改到一半时那份拷贝语法不全，而
  `check-python` 的 `rglob("*.py")` 与 pyright 的 `include` 都会扫到它，于是全量报
  `[ERROR] syntax …logparse.new.py:234 invalid syntax`——正式文件当时是完好的（实测一次）。
  现在 `check_python.iter_py_files` / `iter_text_files` 与 `pyright.config.json` 的 `exclude`
  都跳过 `*.new.py`：改脚本期间跑校验不再被自己的半成品打断。
  **pyright 的 `exclude` 要写相对配置目录的路径**（`../../tools/**/*.new.py`）——只写
  `**/*.new.py` 匹配不到 `include` 里的 `../../tools`（2026-11-11 两次实测：带前缀的那次
  0 error，只有裸模式的那次把测试用的坏文件报了 6 条 error）。
- **`validate-schema` / `validate-refs` 的输出编码**：两者把 stdout 固定成
  UTF-8，`check-python` 与 `check-sentry-offline` 只把编不出的字符换成替代字符。所以控制台不是
  UTF-8 时校验不会中断，但中文与 `✓` / `❌` 会显示成乱码——先设 `[Console]::OutputEncoding` 或
  `PYTHONIOENCODING=utf-8` 再看回显（2026-10-26 核对）。
- **pyright 会报一批 `reportMissingImports` warning**（PIL / maa / jsonschema）。那是解析环境
  的事，不是代码问题——`pyright.config.json` 里已把该规则降成 warning，error 计数不受影响。
  `agent/` 的兄弟式 import（`from rect import ...`）靠配置文件里的 `extraPaths` 才解析得到，
  **`agent/` 与 `agent/shared/` 两个目录都要列**（`shared/` 下的模块按顶层名互相导入）；
  少了会误报 `reportMissingImports`，`agent/store.py` 的 `Generic[T]` 也会跟着报错。
- **`validate-refs` 的 agent 发现是递归的**：`rglob` 找 `<agent 目录>/**/*.py`，模块名按
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
