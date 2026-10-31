# 节点命名与分类

版本号: v3.1

本文约束 pipeline 节点叫什么名、属于哪一类（探测 / 分支 / 工作 / 动作），以及 agent 侧注册名、
参数键、文件名与目录的写法。改节点名、加新节点、拆分子任务时按本文执行。

**v3.1 变更**：停用就位 `Ensure` 类别（改由调用方 `Branch` 平铺 + 入口动作 +
`[JumpBack]BaseBranchError` 表达「保证当前处于某页」）；单探针内联；行内注释 4~6 字。
判据见 §3「分支」「单探针内联」与 [任务链结构](./pipeline-structure.md) 第十节。

**v3.0 变更**：动作节点不再分「主动作 / 兜底动作」，改为按**有没有自环**看：`next` 含自己的是**自环动作**，
必须配 `BaseErrorNext`；`desc` 带 `[jp]` 的节点**不许**自环。判据见 §3 动作节与
[任务链结构](./pipeline-structure.md)「〇」节。

**v1.9 新增**：`Branch` 成为页面导航的默认写法。

**字段怎么配不在本文**：`recognition` / `next` / `pre_wait_freezes` / `rate_limit` / `max_hit` /
`desc` 标记这些"写法"以编辑器里在用的那套模板为准——`tools/vscode/pipeline.snippets.schema.json`
（它的 `$comment` 上写着对应的规范版本）。**页面类节点（`Probe` / `Branch` / `Loop`（废弃））放在哪个位置、
`[JumpBack]` 和 `max_hit` 怎么算** 见 [任务链结构](./pipeline-structure.md)；节点该配哪些字段见
[MaaFramework 文档](https://maafw.com/docs/1.1-QuickStarted)。

文中引用的 `.agents/` 路径是本机开发笔记（该目录不入库），没有这些文件时按源码与本文为准。

**版本号**：改本文的规则就 +0.1。各文件头部注释里的「使用命名规范版本 vX.Y 校验年月日 YY/MM/DD」
说的就是本文——版本低于本文的文件表示还没按当前规则重看过，`tools/validation/validate_refs.py`
会把它们列出来。

## 一、总则

- 名字描述**这一次交互做什么**，不描述用了哪个动作字段。
- 一个词只表示一个含义。同一个词在不同任务里指不同对象时，改成分层的专用词。
- 名字用英文。
- 任务内的节点统一带任务前缀，前缀只表示归属，不表示层级。
- 通用节点放 `assets/resource/pipeline/base.json`，名字不带任务前缀。

## 二、节点名

| 类型 | 骨架 | 示例 |
| --- | --- | --- |
| 任务入口 | `Task<任务名>` | `TaskEntrust`、`TaskFactory`、`TaskReplays` |
| 动作节点 | `<任务前缀><动词><宾语>` | `EscortClickOk`、`FactoryChooseNumber` |
| 探测节点 | `<任务前缀>Probe<页面/状态/元素>` | `EscortProbeHome`、`TrainProbeAcceptable`、`TrainingProbeHomeName` |
| 分支节点 | `<任务前缀>Branch<状态/对象>` | `GiftBranchSortPage`、`BattleBranchClickTap` |
| 工作循环（废弃） | `<任务前缀>Loop` | `TrainingLoop`、`MaintenanceLoop`；新代码用 `Branch` 平铺 |
| 子过程入口 | `<任务前缀>Init` | `TrainingInit`、`EntrustInit` |
| 出口节点 | `<任务前缀><动词>…Success/Failed` | `EscortClickOkEnd`、`FactoryGetRewardFailed` |
| 日常门控 | `<任务前缀>ProbeDaily` / `<任务前缀>DailyUpdate` | `EntrustProbeDaily`、`GiftDailyUpdate` |

## 三、节点分类（探测 / 分支 / 工作 / 动作）

按"这个节点做什么"分四类，每类一个词，别的词不要引入：

| 类别 | 命名 | 这一层有没有行为 | 做什么 |
| --- | --- | --- | --- |
| 探测 | `<前缀>Probe<页面/状态/元素>` | 无 | 只回答"现在是不是这样"，命中即终点 |
| 分支 | `<前缀>Branch<状态/对象>` | 无 | 状态成立就把 `next` 交出去，一次就走 |
| 工作（废弃） | `<前缀>Loop<宾语/动词?>` | 可有 | 在该页面里干活；新代码用 `Branch` 代替 |
| 动作 | `<前缀><动词><宾语>` | 有 | 点一下、滑一下、领一次 |

以 `Probe` / `Branch` / `Loop`（废弃）开头的节点分别固定属于探测、分支、工作层，其余前缀按第四节选词。

**分支的分工**（v3.1）：`Branch` 命中即转、不保证页面，成本 1 次守卫。页面导航默认用 `Branch` 平铺到调用方 `next`，由调用方决定下一步；「保证当前处于某页」不再有专门类别，改由调用方 `next` 里的 `Branch`（自带 `recognition`）+ 入口动作 + `[JumpBack]BaseBranchError` 兜底表达。

本节只定"叫什么、属于哪一层"。**每个类别该配哪些字段、`next` 怎么排、限速用哪个键**，照
`tools/vscode/pipeline.snippets.schema.json` 里的七个骨架（空节点 / Probe / Branch / 动作 / 自环动作 / `[jp]` 一次性动作 / Task）
写——那是编辑器里实际在用的模板，改写法改那里，不在本文复述。

### 探测 `Probe`

探测节点只回答"现在是不是这样"：没有 `action`、没有 `next`。它只读、幂等，命中即终点，所以可以安全地并排放进任何 `next`。

识别之后还要继续走时，不要给它加 `next`，按目的换写法：

- **接着点一下**：把探测条件并进动作节点的 `recognition`（单探针按下面「单探针内联」直接搬识别字段；多消费者或要组合条件时用 `And` + `all_of: [<探针>]`），一个节点同时判和做。
- **到某页再决定干什么**：那是守卫的职责，用 `Branch`（自带 `recognition` 判页面），见 [任务链结构](./pipeline-structure.md) 第十节。

名字统一用 `Probe` 作前缀，后缀写它在观察什么。

| 观察对象 | 例 |
| --- | --- |
| 页面 | `EscortProbeHome`、`BattleProbeBlack`、`FactoryProbeHome` |
| 布尔状态 | `TrainProbeAcceptable`、`BattleProbeLoading`、`BattleProbeAutoLight` |
| 页面元素 | `TrainingProbeHomeName`、`ProbeProbeHelpButton`（通用元素，放 `base.json`） |
| 可操作性 | `EntrustProbeAble`、`MaintenanceProbeRecyclable` |
| 每日门控 | `EntrustProbeDaily`、`TrainingProbeDaily` |

旧写法都并入 `Probe` + 状态词：`In`、`Is`、`Has`、`At`、`Able` / `Unable`、`Can`，以及没有前缀的状态词（`TrainFormation` 这类）。

### 分支 `Branch`

只回答"这个状态成不成立"，成立就把 `next` 里的候选交出去：不写 `action`、不保证页面、不回到自己。

**它也是页面导航的默认写法**（v1.9 新增，v3.1 起是唯一写法）：把每个页面的守卫各写成一个 `Branch`，并列排进调用方的 `next`。`Branch` 每次只要 1 次守卫；判据与示例见 [任务链结构](./pipeline-structure.md) 第十节。

用在"一个入口下按状态分流"的地方（点了按钮之后看是哪种结果）：

| 想做的事 | 用 |
| --- | --- |
| 读状态，命中即终点 | `Probe` |
| 读到状态后转到别处（可能多个候选） | `Branch` |
| 按页面分流（在 A 页做 A、在 B 页做 B） | `Branch` 平铺到调用方 |
| 在这一页反复干到有出口 | `Branch`（`Loop` 已废弃） |

必须写 `recognition`：没有识别的分支等于无条件跳转，把 `next` 写在上一个节点里就行。`next` 至少一项。

### 单探针内联

只被**一处**引用的探针不再单独留节点：把它的识别字段写进引用它的节点 `recognition`，删掉那个 `Probe` 节点。

判据：

- 全库只有一处引用。`And` + `all_of: [<探针>]` 的单探针包装也算一处引用——包装节点就是引用方，识别直接落到它身上。
- 引用方不是通用节点：`base.json` 里的通用探针会被多个任务共用，保留。
- 引用方要的是"识别"本身，不是"命中即终点"的独立出口。

做法：把探针的 `recognition` / `expected` / `template` / `roi` 等识别字段原样搬到引用方，引用方原有的识别条件用 `And` 合并；搬完删掉 `Probe` 节点。`Branch` 的单探针同样内联（`Branch` 自带 `recognition`，包装层没有额外语义）。

多消费者（两处以上引用）保留 `Probe`：它承担的是复用，不是一次性判据。

### 工作 `Loop`（v1.10 废弃）

`Loop` 已废弃，职责由 `Branch` 承担（见 §3 分支）：宽守卫圈住页面、收窄候选、守卫失效交还上层。既有 `*Loop*` 节点逐步退役，校验器仍按旧规则报（`A8`/`A10`/`A13-loop-next`/`A14`），退役时间线见 `tools/validation/ISSUES.md`。

新代码不要新建 `Loop`。页面内多段独立工作拆成多个 `Branch`，由 `Task.next` 或父 `Branch.next` 用 `[JumpBack]` 串联。

### 动作

其余节点是动作节点，名字按第四节选动词 + 宾语。想让一个节点既判又做时不要合并：拆成探测节点加
动作节点，探测节点只探、`next` 指向动作节点。因条件结束任务的情形用 `Stop` 动作节点，不要写进
探测节点名字里（`Stop` 已在第四节动词表里）。

动作节点按**有没有自环**分两类（v3.0）：

| 类别 | `next` 含自己 | `desc` 标记 | 出口 | 干什么 |
| --- | --- | --- | --- | --- |
| 自环动作 | 是 | 无 | 自己不再命中 + `BaseErrorNext` | 反复做同一步直到它不再命中 |
| 一次性动作 | 否 | 无，或 `[jp]` | 交给 `next` 里的下一项 | 做一次就往下走 |

**自环动作必须配 `BaseErrorNext`**：`timeout` 只限制「`next` 里一直找不到命中」的轮询，候选一直在命中
（含 `[JumpBack]` 弹回的 jp 节点反复命中）时它永远不触发，任务既不失败也不结束。`BaseErrorNext` 数的是
「这张表连续几轮没有别的节点成功」，满 10 轮就 `BaseTaskStopError`。判据与写法见
[任务链结构](./pipeline-structure.md)「〇」节。

**`desc` 带 `[jp]` 的节点，`next` 里不许出现自己**：`[jp]` 的语义是「只补一次」，与「反复重试」冲突，
两种语义在日志里分不开，而且会绕过调用方。要重复补就去掉 `[jp]`、按自环动作写；要只补一次就
`next` 里不放自己。

`[jp]` 只是**提示标记**，不是角色的判据：本仓库多数一次性动作没带 `[jp]`（`BaseClickTapBox`、`BaseClickBackButton`、
`BaseTaskStop` 这些通用节点一个都没带），而 v3.1 起停用的 9 个 `Ensure*` 节点（2026-10-31 已全部删除，`A25` 归零）全带 `[jp]`、却不是动作节点。
写新节点时按 `action` / `next` 两个字段定角色，`[jp]` 标不标都不影响它成立。

一次性动作的名字按同一套动词表写；与自环动作的区别是**宾语要能看出是同一个东西的另一种点法**：
本仓库的惯例是在宾语后加 `Button`（点按钮本体）或 `Box`（点按钮所在的框/区域），
如 `EscortEnterHome` 配 `EscortClickHomeButton`、`ShopClickTap` 配 `ShopClickTapBox`。

- 一次性动作**不加 `Button` / `Box` 后缀**时，多半是"这一步走不通就换个方式"（如 `BaseMoveUp` 滑动列表），不是"再点一次"——写错成重试会一直点同一个东西。
- 一次性动作**整个不带识别**（无条件点一次）时，`desc` 里写清点的是哪个区域的哪一边（"点击tap区域"），否则排查时无从判断它会点到哪。
- 一次性动作**不写 `timeout`**：框架的 `timeout` 判的是"本节点的候选表一直不命中"，这类节点没有候选表，写了也永远不触发。
- 一次性动作被别的节点引用时**一律用 `[JumpBack]`**，见 [任务链结构](./pipeline-structure.md)「〇」节与第四节。

`Init` 类节点也有两类：真初始化（写全局变量、读参数、清计数）用 `Init`；"先取一次状态再写全局"的
本质是探测，按 `Probe` 命名。判据见 [任务链结构](./pipeline-structure.md) 第八节。

## 四、动词表（动作节点）

动作节点名必须以本节列出的动词之一开头，宾语写界面上那个东西叫什么。`Click` / `Choose` / `Enter` 三者互斥，判定按下表从上往下走，先命中先算：

| 顺序 | 判据 | 用 |
| --- | --- | --- |
| 1 | 点完**离开当前页面**（`next` 里出现新页面的探针，如 `*BaseProbeHome`） | `Enter` |
| 2 | 点完留在本页，只改变选中态（数量、排序、选了哪一项），可反复换 | `Choose` |
| 3 | 其余：提交、执行、领取、关闭弹窗 | `Click` |

第 1 条之所以落到"`next` 里有没有新页面判据"，是因为"变化是否明显"只看截图容易各说各话；页面内的显隐、展开、切页签都不算离开本页，页内翻页（点"下一章"、列表翻页）也不算——页还是那一页，只是内容换了，按第 3 条算 `Click`。

### `Enter`

离开当前页面，进入另一个页面。宾语必须是页面名。

例：`EntrustEnterMainLine`（主线页面）、`EntrustEnterRewardStage`（领奖页面）、`BattleEnterMainPage`（出击页面）。

不算 `Enter`：页内点按钮切到本页的另一块内容、展开折叠、切页签。

### `Choose`

不离开当前页，只改变选中态。宾语是要选的那个东西。

例：`GiftChooseFirstGift`（第一个礼物）、`FactoryChooseNumber`（建造个数）。

### `Click`

点一个按钮或区域，提交或执行一次动作，通常伴随本页的显隐变化。宾语写界面上的按钮名。

例：`EscortClickOk`（确认）、`FactoryClickStart`（开始建造）、`FactoryClickCancel`（取消订单）、`GiftClickSortConfirm`（确认排序）。

### 其他动词

| 动词 | 用途 | 示例 |
| --- | --- | --- |
| `Start` | 启动一条正式流程 | `EntrustStart`（点出击开始委派） |
| `Get` | 领取类动作 | `FactoryGetReward`、`TrainGetReward`、`FeiduGetMetal` |
| `TouchDown` / `TouchUp` | 按住、松手 | `EntrustTouchDown`、`EntrustTouchUp`、`EntrustTouchUp2` |
| `Move` | 滑动、移动列表 | `BaseMoveUp`、`BaseMoveRight`、`TrainInfoMover` |
| `Close` | 关掉弹窗/页面 | `NoticeClose`、`TrainCloseStatusArrow` |
| `Stop` | 因某个条件结束任务（`action: StopTask`，提示写 `focus`） | `StopNoMetal` |
| `Init` | 进入某页后的初始化（清计数、读参数） | `TrainingInit`、`ReplaysInit` |
| `Enable` | 不点界面，开关另一条任务链的节点（`override_pipeline`） | `EntrustEnableTaskReward`（启用领奖任务） |

## 五、术语分层

一条链会穿过多个层级时，给每一层一个专用词，不要让同一个词兼两层。

**委派链的层级**：

```text
主线篇章（EntrustEnterMainLine，OCR"主线篇章"）
└── 章节（EntrustNextChapter，点"下一章"翻页）
    └── 大关（EntrustProbeStage，如"结晶废墟"）
        └── 小关（EntrustProbeLevel，匹配数字）
```

| 层级 | 节点名用 | 参数键用 |
| --- | --- | --- |
| 章节 | `Chapter`（`EntrustNextChapter`） | — |
| 大关 | `Stage`（`EntrustProbeStage`） | `stage_name` |
| 小关 | `Level`（`EntrustProbeLevel`） | `level` |

新任务出现类似层级时沿用这套词：`Chapter` / `Stage` / `Level` 分别指章节、大关、小关，不要写 `Section`、`Part`、`SubLevel` 之类的同义词。

## 六、动作节点细节

- 宾语与界面文字对齐（`确认` → `ClickOk`、`取消` → `BaseClickCancel`、`完成` → `ClickFinish`）。
- 同一个按钮在不同任务里叫法保持一致：`ClickOk`、`EscortClickOk`、`FactoryClickSkipOk`；不要出现 `ClickYes` 这类同义写法。
- 出口/收尾节点在名字里标出性质：`*End`（链结束）、`*Success`（动作生效）、`*Failed`（失败分支）。成功探针节点用 `And all_of [<被探针节点>] + inverse` 时，名字取 `<被探针节点>Success`，例如 `EntrustClickFinishSuccess`。
- `desc` 由模板生成（`[end]`、`[Loop]` 这些前缀见 `tools/vscode/pipeline.snippets.schema.json`），名字里不重复这些标记。
- `desc` 的方括号标记一共五个，含义固定：

  | 标记 | 含义 | 用在 |
  | --- | --- | --- |
  | `[end]` | 命中即终点，不往下走 | 探测节点、出口节点 |
  | `[jp]` | 兜底动作，只补一次（**该节点不许自环**） | 一次性动作节点（见 §3 动作节） |
  | `[once]` | 一个 task 内只做一次 | 每日门控、`Init` 类节点 |
  | `[max]` | 命中次数有上限 | 限次动作节点 |
  | `[Loop]` | 工作循环 | 工作层节点（已废弃，新代码用 `Branch`） |

## 七、agent 侧命名

### 注册名与类名

- 注册名 = 节点名 + 后缀：识别用 `<节点名>Reco`，动作用 `<节点名>Act`。
- 类名一般与注册名一致。

### 参数键

- `custom_recognition_param` / `custom_action_param` 必须使用字典，键使用 `snake_case`，值是描述真实对象的词
  （怎么读见 `agent/shared/params.py` 的 `parse_params` 与 [MaaFramework 文档](https://maafw.com/docs/1.1-QuickStarted)）。
- 与层级相关的键按第五节命名。
- 同名键在不同任务里含义必须相同；含义不同就换名字。

### 模块全局变量

- 模块级可变状态用 `_` 前缀（`_train_list`、`_finish_count`、`_max_number`），导出的配置用无前缀名（`stage_name`、`level`、`battle_targets`）。
- 跨模块读这些变量必须写 `<模块名>.<变量名>`。

## 八、文件名与目录

- pipeline 片段：任务名全小写 snake_case，一个任务一个文件，公共节点进 `base.json`。
- 选项片段：与任务同名，放 `assets/resource/tasks/option/`。
- 识别模板：`assets/resource/image/<任务目录>/<元素名>.png`，元素名用英文 CamelCase（`Battle/BattleButton.png`、`Train/GoTrainLight.png`）。新任务沿用已有目录，不要为同一个页面再建一个。
