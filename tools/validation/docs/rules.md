# tools/validation —— 规则目录（rules）

`validate-refs` / `check-python` / `validate-schema` 各自查什么、规则怎么分组。
用法与验收见 [`../README.md`](../README.md)，踩过的坑见 [`pitfalls.md`](pitfalls.md)。

## `check-python` 查什么

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

## 规范版本（`E` 系）

**本脚本按两份规范的各自版本实现**：`NAMING_SPEC_VERSION`（`node-naming.md`）与
`STRUCT_SPEC_VERSION`（`pipeline-structure.md`）。运行时只在输出里声明命名规范那一版（console 头部、
`--format github` 的摘要、`--format json` 的 `spec_version` 字段）。命名类判据（`A13-*` / `A19-*` …）
按命名文档实现，结构类判据（A4/A5/A7/A8/A14…）按结构文档实现，所以两份常量都要跟着对齐，
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

## A 系：pipeline / agent 侧（`rules_pipeline_a.py`、`rules_pipeline_b.py`）

| 组 | 规则 | 管什么 |
| --- | --- | --- |
| 解析 | `A0-parse-error` / `A0-duplicate-node` | pipeline 文件 JSONC 解析失败、节点重名 |
| 引用 | `A1-dangling-node`、`A2-unknown-custom` / `A2-unused-registration`、`A3-custom-name-mapping`、`A31-unreferenced-node` | `next` / `on_error` 悬空引用；自定义识别/动作没被任何节点引用或 agent 侧没有；注册名 = 节点名 + `Reco`/`Act`；节点自己没人引用（探针 warn、动作/分支 error、`base.json` 一律 warn） |
| 结构 | `A4-shadowed-candidate`、`A5-spin-trap`、`A6-complement-unreachable`、`A7-jumpback-target`、`A8-loop-guard`、`A10-maxhit-in-loop`、`A11-bad-regex` / `A11-bounded-quantifier`、`A12-bad-focus-key` | 必然命中挡路、`[JumpBack]` 空转陷阱、取反 cut 不可达、`[JumpBack]` 落点、`Loop` 写法、`max_hit` 预算、`expected` 正则质量、`focus` 键名 |
| 命名 | `A13-*`（`branch-action` / `branch-next` / `branch-recognition` / `click-synonym` / `legacy-prefix` / `loop-next` / `missing-prefix` / `probe-action` / `probe-next` / `verb` / `file-case` / `key-case`）、`A27-click-ok-banned` | 节点命名与分类、同义写法、参数键 snake_case、片段文件名；`Ok` 结尾禁用（确认统一 `*Confirm`） |
| 语义 | `A14-fallback-same-probe`、`A15-error-branch-jumpback` / `A15-error-branch-shadowed` / `A15-error-branch-conflict`、`A17-guard-in-next`、`A18-next-length`、`A28-next-list-cap` | `Loop` 末尾兜底同探针；通用错误分支位次（shadowed 提示潜在遮蔽，conflict 为 error）；守卫探针重复识别；候选数量复核 |
| `desc` 标记 | `A19-desc-marker-unknown` / `A19-desc-marker-mismatch` / `A19-desc-marker-end` | 方括号标记（`[end]` / `[jp]` / `[once]` / `[max]` / `[Loop]`）与字段对不上 |
| 自环与兜底 | `A20-jp-self-loop`（error）、`A21-self-loop-without-error-next`（warn）、`A22-error-next-not-first`（warn） | `[jp]` 不许自环；自环默认配 `BaseErrorNext`（A21 查项目约定，不证明无限循环），引用时仍须排 `next[0]`（A22） |
| 可读性 | `A23-section-comment-probe`、`A24-node-comment-missing` | 分节注释下紧跟的节点类型；主干节点缺注释 |
| 停用与内联 | `A25-ensure-banned`、`A26-single-consumer-probe`、`A30-and-inline-sub` | `Ensure` 类别停用；单探针内联；多条件 `And` 内联子识别 |
| 识别预算 | `A29-and-box-index`、`A30` | `And` 组合多条件要显式 `box_index`（只报带 `action` 的节点） |

### Confirm 按钮识别（A33）

`A33-confirm-template`（error）：名字以 `Confirm` 结尾的节点必须使用
`recognition: TemplateMatch`、`template: "Base/Confirm.png"`、`method: 10001`，
并显式填写整数矩形 `roi: [x, y, w, h]`（x/y ≥ 0、w/h > 0），减少模态窗口遮挡导致的误识别与点击异常。
单元素模板列表 `["Base/Confirm.png"]` 也有效；识别配置支持平铺字段和 `recognition.type/param`。

- `And` 可以引用公共探针或内联识别：至少一个必选条件满足上述配置即可；`Or` 的每个分支都必须满足。
- 取反的确认条件不算满足；循环引用、悬空引用也不算满足。
- 无 `action` 的 `Custom` 节点只用于日志/诊断（如 `BaseProbeErrorConfirm`），不按确认按钮检查；有动作则仍检查。
- 本规则只检查 pipeline 定义，不推演 interface/tasks 覆盖后的最终状态；不修改现有任务数据。

### 分组细则（判据原文与实测）

`A13-*` 里属于**护栏**（今天一条都不报，为的是以后别写错）的几条：探测节点不该有 `next`、
工作循环必须有 `next`、参数键 snake_case、pipeline 片段文件名 snake_case、不用 `ClickYes` 这类同义写法。
（v3.1 起 `Ensure` 停用，原来两条按 `Ensure` 名判的护栏随 `A13-ensure-*` 一起删掉。）

`A15-error-branch-*` 管通用错误分支 `BaseBranchError`（`base.json`）的引用与位次：引用必须带 `[JumpBack]`；
排在取反守卫（顶层 `inverse: true`，或识别配置里带 `inverse`）之后时，`A15-error-branch-shadowed` 按 **warn** 提示潜在遮蔽。
只有原识别受遮挡而失败时，取反候选才可能先命中；须实测遮挡对原识别的影响，并确认额度耗尽后的接管与业务优先级，不能据列表顺序断言永久不可达或批量前移错误分支。
排在自己会点「确认」的候选之前仍由 conflict 按 **error** 报——同一个确认会被当成错误弹窗点掉（v3.2 起）。
只判同一列表内的先后，不推演现场画面；「压暗后哪个模板仍会命中」静态判不了，按人工。
结构规范 v3.11 只澄清提示与复核边界，触发条件和级别均不变；见 `docs/zh_cn/develop/pipeline-structure.md` §9。
确认候选由外层 `action` + 确认标记判定：`*Confirm` 名字、`Base/Confirm.png` 模板或 OCR `expected` 含“确认”；
支持平铺与 `recognition.type/param`、And/Or 的引用和内联子识别。子探针不要求 `action`，引用按路径防环，
不再以固定三层截断；不存在/停用的引用不参与。此判定不依赖 A33 通过，漏写 `method` 等字段仍会报告位次冲突。

`A19-desc-marker-*` 管 `desc` 里的方括号标记（`node-naming.md` §6：一共五个，含义固定）：

| 规则 | 级别 | 判据 | 2026-10-31 实测 |
| --- | --- | --- | --- |
| `A19-desc-marker-unknown` | warn | 用了 `[end]` / `[jp]` / `[once]` / `[max]` / `[Loop]` 之外的写法（含大小写不符） | 0 |
| `A19-desc-marker-mismatch` | warn | `[max]` 没有 `max_hit`；`[once]` 的 `max_hit` 不是 1；`[Loop]` 标在名字不含 `Loop` 的节点上 | 0 |
| `A19-desc-marker-end` | info | `[end]` 却有 `next`，且不是只收 `BaseTaskStop` / `BaseTaskStopError` | 0 |

**只查"标了标记、字段对不上"，不查"该标没标"**：`desc` 由模板生成，而 `[jp]` 按 §3 只是提示标记——
今天 65 个带 `[jp]` 的节点里有 25 个还带 `next`，标不标都不构成判据。标记按**开头连续**的那几个算
（`[once][jp] …`），正文里当括号用的字（"点击[确认]按钮"）不算，所以那类不会误报。三条都能 `--ignore` 单独放行。

`A20` / `A21` / `A22` 管**自环动作与 `BaseErrorNext`**（`pipeline-structure.md` §0，v3.0 新增）：

| 规则 | 级别 | 判据 | 2026-10-31 实测 |
| --- | --- | --- | --- |
| `A20-jp-self-loop` | error | `desc` 带 `[jp]`（只补一次）的节点，`next` 里含自己 | 0（已清零：`TrainingEnterHome`、`TrainCloseStatusArrow` 两处已按 v3.0 改写） |
| `A21-self-loop-without-error-next` | warn | `next` 含自己的节点没有引用 `BaseErrorNext`；只查项目约定，不自动推演替代保障 | 15（当日存量） |
| `A22-error-next-not-first` | warn | 引用了 `BaseErrorNext` 但不在 `next[0]` | 0（护栏） |

A21 查**项目默认约定**，不是完整终止性分析。框架的 `timeout` 只限制当前一次 `next` 轮询一直找不到命中的等待；
自环或 `[JumpBack]` 子链持续命中时，不能靠它限制循环总时长，但缺少 `BaseErrorNext` 不足以证明无限循环。
替代限次（包括 `max_hit`）须确认额度耗尽后由哪个候选接管、如何到达终态；Agent 预算或显式持续任务须人工评审范围、停止条件与业务意图。
这些机制不自动豁免 A21，触发条件与 warn 级别不变，也不放宽其他预算规则。

`BaseErrorNext` 按派发者节点名计数，由 Agent 识别事件策略清零；满 10 轮合成 `<父节点名>ErrorNext`，覆写父节点的 `next` 后由合成节点执行失败动作，保留父节点名供故障定位与 Sentry 归组。
不是 `BaseErrorNext` 自己命中后失败；见 `agent/shared/generic.py` 的 `BaseErrorNextReco` 与 `_BaseErrorNextSink`。
默认仍配 `BaseErrorNext`；`A22` 仍要求它排 `next[0]`：每轮先参与计数，再由后续命中事件决定是否清零，避免前面的持续命中候选阻止其被评估。

`A23-section-comment-probe` 管**分节注释的可读性**（warn）：`/* ---- 探测 ---- */` 这种分节注释下面
**必须是动作 / 分支 / 任务入口**；`/* ---- 通用探测 ---- */` 下面允许是 `Probe`；非通用 `Probe` 要放到
用到它的节点后面。实测 8 条（`battle.json` 的 `BattleProbeHome`、`entrust.json` 的 `EntrustProbeDaily` 等）。
只查带横线的分节注释，文件头那种多行 INFO 注释（写版本注解的）不在此列。

`A24-node-comment-missing` 管**主干节点注释缺失**（warn）：**主干节点**（有 `next` 的：动作、分支、任务入口）
前面要写注释，说明这个节点做什么。纯辅助节点（没有 `next`，如 `EscortClickDeliveryButton` / `BaseClickHomeButton` /
`BaseMoveUp`）不要求写注释——它们是跟着引用它们的节点一起读的，逐个加注释只会把文件撑长。
实测 26 条主干节点缺注释（见 `validate-refs` 输出，集中在 `factory.json` 7 条、`reward_shop.json` 6 条）。

`A17` / `A18` 管**识别预算**（`pipeline-structure.md` §10）：

| 规则 | 级别 | 判据 | 2026-10-31 实测 |
| --- | --- | --- | --- |
| `A17-guard-in-next` | info | 节点 `next` 里含本节点 `recognition` 已用过的探针（同一轮识别两遍） | 14 |
| `A18-next-length` | warn | `Loop`（废弃）的 `next` > 5 项；**`Task*` 入口完全不判**（v3.8，入口的候选表由人工把关） | 0（存量 `*Loop` 里没有超限的） |

`A17` 只报「同一轮会问两遍」这个事实，**留删由人按实测救回率定**（<25% 该删）——救回率要日志实测，
静态判不了。两条都可以 `--ignore` 单独放行。

`A25` / `A26` 是 v3.1 新增的两条（`A25` 管 `Ensure` 停用，`A26` 管单探针内联，见 `node-naming.md` §3）：

| 规则 | 级别 | 判据 | 2026-10-31 实测 |
| --- | --- | --- | --- |
| `A25-ensure-banned` | warn | 节点名匹配 `Ensure[A-Z]`：v3.1 起 `Ensure` 类别停用，此节点待删；改用调用方 `Branch` + 入口动作 + `[JumpBack]BaseBranchError`。**故意用 warn 不用 error**：留作回归护栏；存量 9 个节点已于 2026-10-31 全部删除 | 0（2026-10-31 已删净；原 9 个分布在 `battle.json`、`battle_training.json`、`entrust.json` ×3、`entrust_reward.json` ×2、`feidu.json`、`maintenance.json`） |
| `A26-single-consumer-probe` | info | 纯探针只被一处引用（`next` / `on_error` / `roi` / `target` / `all_of` 都算）且唯一引用是**单探针包装**（`And all_of` 恰好一项，或引用方是没有 recognition / action、`next` 只有这一项的纯路由）：可内联进引用它的节点 `recognition`。多条件 `And` 的子识别不算（v3.9，改由 `A30` 管）。多消费者、自定义识别、`[JumpBack]`、`Task` / `Ensure` 引用方、interface / tasks / agent 提到的一律不报 | 6 |

`A26` 只按引用计数报，**不报"`next` 里唯一候选但引用方另有 recognition"那类**：那种内联会把「命中就转」
改成「先满足守卫」，语义不等价，判不准就不报。

`A31-unreferenced-node` 是 2026-11-11 用户要求新增的一条（`rules_pipeline_b.py:check_a31_unreferenced_node`）：
节点自己**没有任何引用**时报出来。

| 级别 | 条件 |
| --- | --- |
| warn | `base.json` 里的节点没人引用（通用节点可能被别处按名调用，先提示） |
| warn | 其他文件里没人引用的**探针**（可能还没接上） |
| error | 其他文件里没人引用的**动作 / 分支**节点（写好了却没人调用，读图的人会以为流程里在用它） |

判据：

- 引用 = 别的节点 `next` / `on_error` / `all_of` / `any_of` 里出现，或别的节点数据里"整串等于这个名字"
  的字符串（`roi` / `target` / `anchor` 这类不走 `ref_lists`）；**自引用不算引用**（只指自己的节点仍然
  进不来，自环另有 `A20` / `A21`）；
- interface / option / preset / agent 源码里出现过这个名字也算被引用（保守，宁可少报）；
- 类别按结构判、不看名字：有 `action` = 动作，无 `action` 有 `next` = 分支，有 `recognition`、
  既无 `action` 又无 `next` = 探针；
- `Task*` 入口不在这里判：由 interface / agent 按名调用，拦它只会制造假红。

实测（2026-11-11）：4 条 error —— `BattleStopNoProps`（`battle.json:160`）、`BattleClickAttack`
（`battle.json:341`，只在注释里出现过）、`ExploreClickTap`（`explore.json:182`，只被自己 `next` 引用）、
`FeiduEnsureHome`（`feidu.json:133`，`Ensure` 遗留，`A25` 同时也在报）。都是存量死节点，接进流程或删掉即可。

`A27` 是 v3.2 新增的一条（`node-naming.md` §6），**error**：

| 规则 | 级别 | 判据 |
| --- | --- | --- |
| `A27-click-ok-banned` | error | 节点名以 `Ok` 结尾（含 `ClickOk`）：确认按钮统一 `*Confirm`，改用 `BaseClickConfirm` / `<前缀>ClickConfirm` |

确认按钮的**位次**不单列规则：`*Confirm` 必须排在 `BaseBranchError` 之前，由 `A15-error-branch-conflict` 管（v3.2 起 warn → **error**）。

`A28` 是 v3.3 新增的一条（`pipeline-structure.md` §10），**warn**：

| 规则 | 级别 | 判据 |
| --- | --- | --- |
| `A28-next-list-cap` | warn | 非任务入口的 `next` > 5 项（v3.10 起；v3.3–v3.9 为 7 项）；计长度时**不计** `BaseBranchError` / `BaseErrorNext`。`Task*` 入口完全不判（v3.8，也不归 `A18` 管了） |

结构规范 v3.11 明确：5 项是**人工复核阈值，不是性能分界或强制拆分条件**；触发条件与 warn 级别不变。
须结合日志评估候选识别成本、实际命中顺序与失败次数，并比较拆分后新增守卫、截图和调度轮次的开销；不能只为消除告警而拆 `Branch`。

`A29` 是 v3.5 新增的一条（`pipeline-structure.md` 第十一节），**warn**：

| 规则 | 级别 | 判据 |
| --- | --- | --- |
| `A29-and-box-index` | warn | 带 `action` 的节点里，`recognition: And` 且 `all_of` 多于 1 项却没写 `box_index`：输出框默认 `all_of[0]`，调序会悄悄换落点，要求显式写出。没 `action` 的（`Probe` / `Branch` / `Loop`）不报——框只进日志，换索引没有行为差别 |

`A30` 是 v3.9 新增的一条（`node-naming.md` §3「单探针内联」），**warn**：

| 规则 | 级别 | 判据 |
| --- | --- | --- |
| `A30-and-inline-sub` | warn | 多条件 `And`（`all_of` 两项以上）里出现内联子识别对象：内联对象没有名字，注册名校验 / `box_index` 落点指认 / 单独复测三样都落空，应拆成独立 `Probe` 节点按名字引用。单探针包装（`all_of` 恰好一项）不算——那是 `A26` 认可的内联写法 |

## B 系：`interface.json` / tasks 片段（`rules_misc.py`）

| 规则 | 管什么 |
| --- | --- |
| `B1-bad-entry` | `task[].entry` 指向的节点不存在 |
| `B2-bad-group` | 任务分组结构不对 |
| `B3-bad-option` | `option` 引用或形状非法；不检查 `verify` 是否有锚点 |
| `B4-bad-import` | `import` 片段路径不存在或重复 |
| `B5-bad-override-target` | `pipeline_override` 的目标节点不在 pipeline 里定义 |
| `B6-bad-preset-task` | `preset` 里引用了不存在的 task |
| `B7-default-case` | checkbox 的 `default_case` 必须是 `cases[0]` |
| `B8-bad-path` | `interface` 里引用的资源路径 / `agent.child_args` 不存在 |
| `B9-custom-param-type`（error） | `pipeline_override` 中的 `custom_recognition_param` / `custom_action_param` 分别要求目标节点的有效识别 / 动作类型为 `Custom`；覆盖未指定类型时继承节点定义。 |

Pipeline 节点直接声明同类参数时由 `A32-custom-param-type` 检查。支持扁平写法及 `recognition` / `action` 对象的嵌套参数；普通 OCR、Click 等内置类型的普通参数不在此规则内。不同选项之间的覆盖组合无法仅凭单个覆盖判定，本规则按该覆盖与基础节点定义核对；动态创建或无法解析的目标不推断。

`B7-verify-anchor` 已移除：未锚定的 `verify` 不等于无法通过校验，`.+` 等非空/子串约束合法。
是否要求整串匹配由输入需求决定，不统一要求 `^...$`。本工具不新增 Python `re` 编译校验冒充客户端正则引擎；
现有 JSON/schema 校验保持不变。

## C 系：模板图片（`rules_misc.py`）

| 规则 | 管什么 |
| --- | --- |
| `C1-missing-template`（error） | pipeline `template` 引用的图片不存在（含 agent 里不在 `JTemplateMatch(...)` 内的字面量） |
| `C2-unused-image`（warn） | 没被任何 pipeline/agent 引用的图片 |
| `C3-non-png`（warn） | image 目录下有非 png 文件 |
| `C4-empty-folder-template`（error） | 目录模板（`xxx/`）里没有图片 |
| `C8-template-larger-than-roi`（error） | pipeline TemplateMatch 的 PNG 模板宽/高超过固定 ROI（含 roi_offset 后的尺寸），模板匹配无法执行；支持模板列表、目录、嵌套及内联 And/Or。动态/引用 ROI、非 PNG、宽高非正的框架边界语义不推断；不读取图片内容，仅使用 PNG IHDR。 |
| `C5-image-name`（warn） | 模板元素名不是英文 CamelCase |
| `C6-agent-template-missing`（error） | agent 里 `JTemplateMatch(...)` 的字面量模板在 `image/` 下不存在（消息带 `类.方法` 与行号） |
| `C7-agent-template-dynamic`（info） | 模板实参是变量 / f-string，静态查不出指向 |

`C6` / `C7` 的细节（为什么 C1 不重复报、为什么仍算"引用过"）见
[`../README.md`](../README.md) 的「怎么跑」第 2 步注释。

## D 系：agent 注册（`rules_misc.py`）

| 规则 | 管什么 |
| --- | --- |
| `D1-unimported-module`（error） | agent 模块没被 `main.py` 导入（装饰器不执行 → 注册不生效）。**只对带 `@AgentServer.custom_*` 的模块报**：没有注册副作用的工具模块还没接进来不算错（2026-11-11 修过 `agent/shared/direct.py` 的假红） |
| `D2-registration-suffix`（warn） | 注册名后缀不是 `Reco` / `Act` |
| `D2-class-name`（warn） | 注册名与类名不一致 |

agent 发现是 `rglob` 递归 + 点分模块名，`--agent` 传目录/文件只收窄**报告范围**，载入集取并集。
