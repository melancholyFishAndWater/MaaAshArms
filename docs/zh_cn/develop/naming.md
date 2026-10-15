# 命名规范

版本号: v1.3

本文约束 pipeline 节点名、agent 侧注册名与参数键的写法。改节点名、加新节点、拆分子任务时按本文执行；命名规则看第三节（节点分类）与第四节（动作节点）。

本文只讲命名。节点该配哪些字段请看 [MaaFramework 文档](https://maafw.com/docs/1.1-QuickStarted)；文中引用的 `.agents/` 路径是本机开发笔记（该目录不入库），没有这些文件时按源码与本文为准。

页面类节点（`Probe` / `Ensure` / `Loop`）放在哪个位置、`[JumpBack]` 和 `max_hit` 怎么算，见 [任务链结构](./pipeline-structure.md)。

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
| 工作循环 | `<任务前缀>Loop` | `TrainingLoop`、`MaintenanceLoop` |
| 子过程入口 | `<任务前缀>Init` | `TrainingInit`、`EntrustInit` |
| 出口节点 | `<任务前缀><动词>…Success/Failed` | `EscortClickOkEnd`、`FactoryGetRewardFailed` |
| 日常门控 | `<任务前缀>ProbeDaily` / `<任务前缀>DailyUpdate` | `EntrustProbeDaily`、`GiftDailyUpdate` |

## 三、节点分类（探测 / 就位 / 工作 / 动作）

按"这个节点做什么"分四类，每类一个词，别的词不要引入：

| 类别 | 命名 | 有无 `action` | 做什么 |
| --- | --- | --- | --- |
| 探测 | `<前缀>Probe<页面/状态/元素>` | 无 | 只回答"现在是不是这样" |
| 就位 | `<前缀>Ensure<页面>` | 有 | 保证"当前处于该页面"，不在就点入口、被弹走就回 |
| 工作 | `<前缀>Loop<宾语/动词?>` | 可有 | 在该页面里干活 |
| 动作 | `<前缀><动词><宾语>` | 有 | 点一下、滑一下、领一次 |

以 `Probe` 开头的节点永远是探测层，以 `Ensure` 开头的永远是就位层，其余前缀按第四节选词。

### 探测 `Probe`

探测节点只回答"现在是不是这样"：没有 `action` 字段。

其他节点应该使用
`"recognition": "And"` 或 `"recognition": "Or"` 来引用。

名字统一用 `Probe` 作前缀，后缀写它在观察什么。

| 观察对象 | 例 |
| --- | --- |
| 页面 | `EscortProbeHome`、`BattleProbeBlack`、`FactoryProbeHome` |
| 布尔状态 | `TrainProbeAcceptable`、`BattleProbeLoading`、`BattleProbeAutoLight` |
| 页面元素 | `TrainingProbeHomeName`、`ProbeHasHelpButton`（通用元素，放 `base.json`） |
| 可操作性 | `EntrustProbeAble`、`MaintenanceProbeRecyclable` |
| 每日门控 | `EntrustProbeDaily`、`TrainingProbeDaily` |

因为探测节点没有 `next` 字段（或为空），它不会进行任何行为，适合作为终点节点。

旧写法都并入 `Probe` + 状态词：`In`、`Is`、`Has`、`At`、`Able` / `Unable`、`Can`，以及没有前缀的状态词（`TrainFormation` 这类）。

### 就位 `Ensure`

此节点必须使用 `[JumpBack]`。

此节点用于保证页面正常，不允许在next中加入 `Loop`。`Loop` 应该在 `Task*` 入口命中。

它内部按"目标页面的探针 → 进入动作 → 上一级 `Ensure` → `LoopErrorByTimes`"排候选，降级过程封在节点内部，调用方只看到"保证在训练所"这一个词。

允许自循环（点了没进去），用 `pre_wait_freezes` + `rate_limit` 限速，不要用 `max_hit` 卡。它不写业务候选——业务属于工作循环。

允许嵌套齐套 `Ensure`，来确保页面层层推进。

next写法应准守：

```json
"next": [
    "出口/工作节点",
    "[jp]行为节点",
    "[jp]异常弹窗",
    "[jp]Ensure节点"
]
```

出口节点置于最上。虽然第一轮浪费一次扫描，但在页面有明显变化不够明显，所有节点均可命中时，出口/工作节点命中优先。

参照 `GiftEnsureRules` 和 `GiftClickSortRule`，排序页面不全覆盖，还是能匹配到排序按钮，但此时又能正常操作。

不过总得来说，还是不提倡，建议把自愈节点和出口/工作节点写得更严谨。

### 工作 `Loop`

页面内的工作循环统一用 `Loop` 结尾。

`recognition` 判断是否在此页面上。业务候选排在里面。

### 动作

其余节点是动作节点，名字按第四节选动词 + 宾语。想让一个节点既判又做时不要合并：拆成探测节点加动作节点，探测节点只探、用 `next` 指向动作节点。因条件结束任务的情形用 `Stop` 动作节点（`action: StopTask`，提示写 `focus`），不要写进探测节点名字里。

## 四、动词表（动作节点）

动作节点名必须以本节列出的动词之一开头，宾语写界面上那个东西叫什么。`Click` / `Choose` / `Enter` 三者互斥，判定按下表从上往下走，先命中先算：

| 顺序 | 判据 | 用 |
| --- | --- | --- |
| 1 | 点完**离开当前页面**（`next` 里出现新页面的探针，如 `*ProbeHome`） | `Enter` |
| 2 | 点完留在本页，只改变选中态（数量、排序、选了哪一项），可反复换 | `Choose` |
| 3 | 其余：提交、执行、领取、关闭弹窗 | `Click` |

第 1 条之所以落到"`next` 里有没有新页面判据"，是因为"变化是否明显"只看截图容易各说各话；页面内的显隐、展开、切页签都不算离开本页，页内翻页（点"下一章"、列表翻页）也不算——页还是那一页，只是内容换了，按第 3 条算 `Click`。

### `Enter`

离开当前页面，进入另一个页面。宾语必须是页面名。

例：`EntrustEnterMainLine`（主线页面）、`EntrustEnterRewardMainLine`（领奖页面）、`BattleEnterMainPage`（出击页面）。

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
- 循环重试节点，靠 `desc` 里的 `[Loop]` 标记，例如 `"desc": "[Loop] 点击出击按钮 即使是灰色的"`。
- 出口/收尾节点在名字里标出性质：`*End`（链结束）、`*Success`（动作生效）、`*Failed`（失败分支）。成功探针节点用 `And all_of [<被探针节点>] + inverse` 时，名字取 `<被探针节点>Success`，例如 `EntrustClickFinishSuccess`。

## 七、agent 侧命名

### 注册名与类名

- 注册名 = 节点名 + 后缀：识别用 `<节点名>Reco`，动作用 `<节点名>Act`。
- 类名一般与注册名一致。

### 参数键

- `custom_recognition_param` / `custom_action_param` 必须使用字典。键使用 `snake_case`，值是描述真实对象的词。
- 与层级相关的键按第五节命名。
- 同名键在不同任务里含义必须相同；含义不同就换名字。
- agent 侧建议使用 `parse_params(..., "<键>")` 读取，不推荐使用默认值，否则可能静默报错。

### 模块全局变量

- 模块级可变状态用 `_` 前缀（`_train_list`、`_finish_count`、`_max_number`），导出的配置用无前缀名（`stage_name`、`level`、`battle_targets`）。
- 跨模块读这些变量必须写 `<模块名>.<变量名>`。

## 八、文件名与目录

- pipeline 片段：任务名全小写 snake_case，一个任务一个文件，公共节点进 `base.json`。
- 选项片段：与任务同名，放 `assets/resource/tasks/option/`。
- 识别模板：`assets/resource/image/<任务目录>/<元素名>.png`，元素名用英文 CamelCase（`Battle/BattleButton.png`、`Train/GoTrainLight.png`）。新任务沿用已有目录，不要为同一个页面再建一个。
