# 节点命名与分类

版本号: v1.10

本文约束 pipeline 节点叫什么名、属于哪一类（探测 / 分支 / 就位 / 工作 / 动作），以及 agent 侧注册名、
参数键、文件名与目录的写法。改节点名、加新节点、拆分子任务时按本文执行。

**v1.9 新增**：`Branch` 是页面导航的默认写法，`Ensure` 嵌套深度上限 2 层、首项探针按救回率取舍。
判据与实测数据见 [任务链结构](./pipeline-structure.md) 第十节「识别预算」。

**字段怎么配不在本文**：`recognition` / `next` / `pre_wait_freezes` / `rate_limit` / `max_hit` /
`desc` 标记这些"写法"以编辑器里在用的那套模板为准——`tools/vscode/pipeline.snippets.schema.json`
（它的 `$comment` 上写着对应的规范版本）。**页面类节点（`Probe` / `Branch` / `Ensure` / `Loop`（废弃））放在哪个位置、
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
| 就位节点 | `<任务前缀>Ensure<页面>` | `TrainingEnsureHome`、`EnsureMain` |
| 分支节点 | `<任务前缀>Branch<状态/对象>` | `GiftBranchSortPage`、`BattleBranchClickTap` |
| 工作循环（废弃） | `<任务前缀>Loop` | `TrainingLoop`、`MaintenanceLoop`；新代码用 `Branch` 平铺 |
| 子过程入口 | `<任务前缀>Init` | `TrainingInit`、`EntrustInit` |
| 出口节点 | `<任务前缀><动词>…Success/Failed` | `EscortClickOkEnd`、`FactoryGetRewardFailed` |
| 日常门控 | `<任务前缀>ProbeDaily` / `<任务前缀>DailyUpdate` | `EntrustProbeDaily`、`GiftDailyUpdate` |

## 三、节点分类（探测 / 分支 / 就位 / 工作 / 动作）

按"这个节点做什么"分五类，每类一个词，别的词不要引入：

| 类别 | 命名 | 这一层有没有行为 | 做什么 |
| --- | --- | --- | --- |
| 探测 | `<前缀>Probe<页面/状态/元素>` | 无 | 只回答"现在是不是这样"，命中即终点 |
| 分支 | `<前缀>Branch<状态/对象>` | 无 | 状态成立就把 `next` 交出去，一次就走 |
| 就位 | `<前缀>Ensure<页面>` | 有 | 保证"当前处于该页面"，不在就点入口、被弹走就回 |
| 工作（废弃） | `<前缀>Loop<宾语/动词?>` | 可有 | 在该页面里干活；新代码用 `Branch` 代替 |
| 动作 | `<前缀><动词><宾语>` | 有 | 点一下、滑一下、领一次 |

这一列说的是"这一层有没有行为"，**不是"节点上有没有 `action` 字段"**：就位节点实测都不写 `action`
（`Ensure[A-Z]` 8/8），它把"探针 → 进入动作 → 上一级 `Ensure`"排在自己的 `next` 里，行为落在被引用的
动作节点上。

以 `Probe` / `Branch` / `Ensure` / `Loop`（废弃）开头的节点分别固定属于探测、分支、就位、工作层，其余前缀按第四节选词。

**分支与就位的分工**（v1.9）：`Branch` 命中即转、不保证页面，成本 1 次守卫；`Ensure` 保证在页面、不在就修，成本 5 次/层。页面导航默认用 `Branch` 平铺，只在需要「从任意页面保证到达」时用 `Ensure`。

本节只定"叫什么、属于哪一层"。**每个类别该配哪些字段、`next` 怎么排、限速用哪个键**，照
`tools/vscode/pipeline.snippets.schema.json` 里的七个骨架（空节点 / Probe / Branch / Ensure / Loop（废弃）/ 动作 / Task）
写——那是编辑器里实际在用的模板，改写法改那里，不在本文复述。

### 探测 `Probe`

探测节点只回答"现在是不是这样"：没有 `action`、没有 `next`。它只读、幂等，命中即终点，所以可以安全地并排放进任何 `next`。

识别之后还要继续走时，不要给它加 `next`，按目的换写法：

- **接着点一下**：把探测条件并进动作节点的 `recognition`（`And` + `all_of: [<探针>]` + `action` + `target`），一个节点同时判和做。
- **到某页再决定干什么**：那是守卫的职责，用 `Ensure` / `Branch`（自带 `recognition` 判页面），见 [任务链结构](./pipeline-structure.md) 第三节。

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

**它也是页面导航的默认写法**（v1.9 新增）：把每个页面的守卫各写成一个 `Branch`，并列排进调用方的 `next`，代替嵌套的 `Ensure`。`Ensure` 每层固定 5 次识别，`Branch` 每次只要 1 次守卫；判据与示例见 [任务链结构](./pipeline-structure.md) 第十节。

用在"一个入口下按状态分流"的地方（点了按钮之后看是哪种结果）：

| 想做的事 | 用 |
| --- | --- |
| 读状态，命中即终点 | `Probe` |
| 读到状态后转到别处（可能多个候选） | `Branch` |
| 按页面分流（在 A 页做 A、在 B 页做 B） | `Branch` 平铺到调用方 |
| 保证当前处于某页 | `Ensure` |
| 在这一页反复干到有出口 | `Branch`（`Loop` 已废弃） |

必须写 `recognition`：没有识别的分支等于无条件跳转，把 `next` 写在上一个节点里就行。`next` 至少一项。

### 就位 `Ensure`

被引用时必须带 `[JumpBack]`（怎么写见 [任务链结构](./pipeline-structure.md)与上面的模板）。

它保证页面正常，自己**不写业务候选**——业务属于 `Branch`，在 `Task*` 入口命中。它可以嵌套
（一层层推进页面），内部按"目标页面的探针 → 进入动作 → 上一级 `Ensure` → 最后一个兜底候选"排候选，
降级过程封在节点内部，调用方只看到"保证在训练所"这一个词。

**嵌套深度上限 2 层**（v1.9 新增）：单层 5 次识别、每多一层再付 5 次，深度 5 就是 24 次。需要第 3 层时改用 `Branch` 平铺，见 [任务链结构](./pipeline-structure.md) 第十节。

**首项探针只在守卫会误判时保留**（v1.9 新增）：`next` 首项通常是守卫里那个探针（"刚说不在，再问一遍"），救回率 < 25% 就删掉。实测数据与判据见 [任务链结构](./pipeline-structure.md) 第十节。

`next` 的末项按嵌套与否分两种：单层 `Ensure`（上面就是枢纽那一级）的末项是最后一个兜底候选，通常是
回主页/枢纽的那一级；嵌套 `Ensure` 的末项是上一级 `Ensure`。完整写法见 [任务链结构](./pipeline-structure.md)。

### 工作 `Loop`（v1.10 废弃）

`Loop` 已废弃，职责由 `Branch` 承担（见 §3 分支）：宽守卫圈住页面、收窄候选、守卫失效交还上层。既有 `*Loop*` 节点逐步退役，校验器仍按旧规则报（`A8`/`A10`/`A13-loop-next`/`A14`），退役时间线见 `tools/validation/ISSUES.md`。

新代码不要新建 `Loop`。页面内多段独立工作拆成多个 `Branch`，由 `Task.next` 或父 `Branch.next` 用 `[JumpBack]` 串联。

### 动作

其余节点是动作节点，名字按第四节选动词 + 宾语。想让一个节点既判又做时不要合并：拆成探测节点加
动作节点，探测节点只探、`next` 指向动作节点。因条件结束任务的情形用 `Stop` 动作节点，不要写进
探测节点名字里（`Stop` 已在第四节动词表里）。

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
| `Move` | 滑动、移动列表 | `MoveUp`、`MoveRight`、`TrainInfoMover` |
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

- 宾语与界面文字对齐（`确认` → `ClickOk`、`取消` → `ClickCancel`、`完成` → `ClickFinish`）。
- 同一个按钮在不同任务里叫法保持一致：`ClickOk`、`EscortClickOk`、`FactoryClickSkipOk`；不要出现 `ClickYes` 这类同义写法。
- 出口/收尾节点在名字里标出性质：`*End`（链结束）、`*Success`（动作生效）、`*Failed`（失败分支）。成功探针节点用 `And all_of [<被探针节点>] + inverse` 时，名字取 `<被探针节点>Success`，例如 `EntrustClickFinishSuccess`。
- `desc` 由模板生成（`[end]`、`[Loop]` 这些前缀见 `tools/vscode/pipeline.snippets.schema.json`），名字里不重复这些标记。

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
