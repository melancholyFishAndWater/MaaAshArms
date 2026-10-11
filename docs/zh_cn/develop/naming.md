# 命名规范

本文约束 pipeline 节点名、agent 侧注册名与参数键的写法。改节点名、加新节点、拆分子任务时按本文执行；改名后必须跑一遍文末的「改名检查清单」。

本文只讲命名。节点该配哪些字段、`next` 怎么分发、`timeout` / `max_hit` 怎么算，看 [MaaFramework 文档](https://maafw.com/docs/1.1-QuickStarted)；文中引用的 `.agents/` 路径是本机开发笔记（该目录不入库），没有这些文件时按源码与本文为准。

## 一、总则

- 名字描述**这一次交互做什么**，不描述用了哪个动作字段。`action: Click` 的节点不一定叫 `Click*`，但叫 `Choose*` 的节点必须是从一组同类可选项里挑一个。
- 一个词只表示一个含义。同一个词在不同任务里指不同对象时，改成分层的专用词（见第四节）。
- 名字用英文、CamelCase、拼写完整。不要用缩写、拼音，也不要沿用错误的拼法。
- 任务内的节点统一带任务前缀（`Entrust*` / `Escort*` / `Factory*` / `Train*` / `Training*` / `Replays*` / `Battle*` / `Gift*` / `Maintenance*` / `StartUp*`），前缀只表示归属，不表示层级。
- 通用节点放 `assets/resource/pipeline/base.json`，名字不带任务前缀（`ReturnMain`、`ClickConfirm`、`TaskStop`）。

## 二、节点名骨架

| 类型 | 骨架 | 示例 |
|---|---|---|
| 任务入口 | `Task<任务名>` | `TaskEntrust`、`TaskFactory`、`TaskReplays` |
| 动作节点 | `<任务前缀><动词><宾语>` | `EscortClickOk`、`FactoryChooseNumber` |
| 状态/判据节点 | `<任务前缀><系动词/状态词>` | `EscortInHome`、`TrainIsAcceptable`、`TrainingHasHomeName` |
| 子过程入口 | `<任务前缀>Enter<页面>` / `<任务前缀>Init` | `EnterMaintenance`、`EntrustInit` |
| 出口节点 | `<任务前缀><Exit/End/Success/Failed>` | `EscortClickOkEnd`、`FactoryGetRewardFailed` |
| 日常门控 | `<任务前缀>CheckDaily` / `<任务前缀>DailyUpdate` | `EntrustCheckDaily`、`GiftDailyUpdate`（仓库里仍有 `TaskTrainingCheckDaily` 这类混合写法，新增节点用上面的形式） |

## 三、动词表

### `Click`

点一个按钮或区域，触发一次动作或提交。宾语写这个按钮在界面上叫什么，不要写它点完会发生什么。

例：`EscortClickOk`（确认）、`FactoryClickCancel`（取消建造）、`FactoryClickStart`（开始建造）、`FactoryClickSkipOk`（确认跳过）、`TrainClickArrow`、`TrainClickAutoButton`、`MaintenanceClickFinish`、`ReplaysClickFinish`、`BattleClickAttack`。

### `Choose`

从一组同类可选项里挑一个，选项之间可以反复换、点完不提交。

例：`FactoryChooseNumber`（建几个）、`FactoryChooseCharacter`（选角色）、`GiftChooseFirst`（第一个角色）、`GiftChooseFirstGift`（第一个礼物）、`EntrustChooseLevel`（小关）、`EntrustChooseStage`（大关）。

**边界：一次点击既是"选"又是"提交"时用 `Click`。** `TrainAcceptable` 点的是列表里的可发车路线（像 `Choose`），但它同时提交发车，所以名字用状态词 + 默认 `Click`，不叫 `TrainChooseRoute`。

### `Enter`

进/回一个页面。宾语写页面名。

例：`EntrustEnterMainLine`（主线页面）、`EntrustEnterRewardMainLine`（领奖页面）、`BattleEnterMainPage`、`FactoryEnterHome`、`TrainingEnterHome`、`EnterMaintenance`、`EnterFriend`、`GiftEnterTavern`、`GiftEnterHome`。

反例：页内点一个按钮切到本页的另一块内容，不算 `Enter`。

### 其他动词

| 动词 | 用途 | 示例 |
|---|---|---|
| `Start` | 启动一条正式流程 | `EntrustStart`（点出击开始委派） |
| `Get` | 领取类动作 | `FactoryGetReward`、`TrainGetReward`、`FeiduGetMetal` |
| `TouchDown` / `TouchUp` | 按住、松手 | `EntrustTouchDown`、`EntrustTouchUp`、`EntrustTouchUp2` |
| `Move` | 滑动、移动列表 | `MoveUp`、`MoveRight`、`TrainInfoMover`、`EntrustMoveToFind` |
| `Close` | 关掉弹窗/页面 | `NoticeClose`、`TrainCloseStatusArrow` |
| `Give` | 重复给予 | `GiftGiveThree` |

## 四、术语分层

一条链会穿过多个层级时，给每一层一个专用词，不要让同一个词兼两层。

**委派链的层级**（2026-10 确认）：

```text
主线篇章（EntrustEnterMainLine，OCR"主线篇章"）
└── 章节（EntrustNextChapter，点"下一章"翻页）
    └── 大关（EntrustChooseStage，识别大关名，如"结晶废墟"）
        └── 小关（EntrustChooseLevel，匹配两位数字）
```

| 层级 | 节点名用 | 参数键用 |
|---|---|---|
| 章节 | `Chapter`（`EntrustNextChapter`） | — |
| 大关 | `Stage`（`EntrustChooseStage`） | `stage_name` |
| 小关 | `Level`（`EntrustChooseLevel`） | `level` |

新任务出现类似层级时沿用这套词：`Chapter` / `Stage` / `Level` 分别指章节、大关、小关，不要写 `Section`、`Part`、`SubLevel` 之类的同义词。

## 五、动作节点

- 名字里的动词按第三节选，宾语与界面文字对齐（`确认` → `ClickOk`、`取消` → `ClickCancel`、`完成` → `ClickFinish`）。
- 同一个按钮在不同任务里叫法保持一致：通用确认框用 `ClickConfirm`（`base.json`），任务自己的确认按钮用 `ClickOk`（`EscortClickOk`、`FactoryClickSkipOk`）；不要出现 `ClickYes` 这类同义写法。
- 循环重试点（`next` 里包含自身的那些）名字不变，靠 `desc` 里的 `[Loop]` 标记，例如 `"desc": "[Loop] 点击出击按钮 即使是灰色的"`。
- 出口/收尾节点在名字里标出性质：`*End`（链结束）、`*Success`（动作生效）、`*Failed`（失败分支）。成功探针节点用 `And all_of [<被探针节点>] + inverse` 时，名字取 `<被探针节点>Success`，例如 `EntrustClickFinishSuccess`。失败分支统一 `*Failed`（历史遗留的 `EscortClickOkFail` 可择机改）。

## 六、状态判据节点

判据节点自身不做动作，名字用系动词或状态词起头，不要用动词。

| 前缀 | 含义 | 示例 |
|---|---|---|
| `In` | 当前在某个页面 | `EscortInHome`、`TrainInAcceptPage`、`EscortInChoose` |
| `Is` | 布尔状态 | `TrainIsAcceptable`、`BattleIsLoading`、`FactoryIsNoMetal` |
| `Has` | 页面上存在某个元素 | `HasHelpButton`、`TrainingHasHomeName` |
| `Able` / `Unable` | 可操作 / 不可操作 | `EntrustAble`、`EntrustUnable` |
| `At` | 位于某个具体页面（与 `In` 同义时的备选） | `TrainAtHome`、`TrainAtFormation` |

同一个仓库里 `In` 与 `At` 只保留一种用法；当前两套并存（`EscortInHome` 与 `TrainAtHome`），新增节点统一用 `In`。

## 七、任务入口

- 入口节点名固定 `Task<任务名>`，同时是 `assets/interface.json` 里 `task[].entry` 的值。
- 每个要求「从面板启动才完整」的任务，其入口节点名也出现在每日记录（`debug/daily_*.json`）的键里 —— 键是 `task_detail.entry`，改名会让历史记录读不到。
- 内部节点也可以用 `Task` 前缀（`TaskFactoryNext` 是订单工厂的 driver、`TaskTrainingCheckDaily` 是训练所门控）；但面板可启动的任务名与内部节点名不得重复，两者的区别是 `interface.json` 的 `task[].entry` 指哪一个。

## 八、agent 侧命名

### 注册名与类名

- 注册名 = 节点名 + 后缀：识别用 `<节点名>Reco`，动作用 `<节点名>Act`。
- 类名一般与注册名一致；若一个节点同时有识别和动作两个类，各自加后缀即可（`XxxReco` / `XxxAct`），不要用 `Handler` / `Impl` 之类的词。
- 注册名与 pipeline 里的 `custom_recognition` / `custom_action` 字符串必须逐字相同。名字不一致时框架只会打一行 `Recognition is null` / `Action is null`，该节点按未命中处理，属静默故障。
- 历史遗留的短名（`TaskFactoryReco`、`TaskGiftReco` 这类不带 Reco 前的全名）不必为对齐而改名；新注册的按其所属节点起名。
- 仅当动作语义是「清理本次大循环的计数」这类没有对应节点的工具动作时，才允许起抽象名（`LoopErrorByTimesInitAct`、`EscortInChooseAct`）。

### 参数键

- `custom_recognition_param` / `custom_action_param` 的键用 `snake_case`，值是描述真实对象的词。
- 与层级相关的键按第四节：`stage_name`、`level`。
- 同名键在不同任务里含义必须相同；含义不同就换名字（曾经的 `chapter_name` 实际指大关，已改成 `stage_name`）。
- 必填键要与 agent 侧 `parse_params(..., "<键>")` 的必填列表一致：pipeline 里删掉某个键而 agent 仍要求它，动作会直接返回 False。

### 模块全局变量

- 模块级可变状态用 `_` 前缀（`_train_list`、`_finish_count`、`_max_number`），导出的配置用无前缀名（`stage_name`、`level`、`battle_targets`、`build_times`）。
- 跨模块读这些变量必须写 `<模块名>.<变量名>`。`from <模块> import <变量>` 在对方重绑后永远拿到导入时的旧值。

## 九、文件名与目录

- pipeline 片段：任务名全小写 snake_case（`battle_training.json`、`feidu_gift.json`），一个任务一个文件，公共节点进 `base.json`。
- 选项片段：与任务同名，放 `assets/resource/tasks/option/`（`battle_training.json`、`factory.json`），一次改一个任务的面板选项。
- 识别模板：`assets/resource/image/<任务目录>/<元素名>.png`，元素名用英文 CamelCase（`Battle/BattleButton.png`、`Train/GoTrainLight.png`）。任务目录目前有 `Entrust`、`Escort`、`Factory`、`Train`、`Battle`、`Maintenance`、`Reward`、`StartUp`、`Feidu`（赠礼、废都共用），新任务沿用已有目录，不要为同一个页面再建一个。中文名只允许出现在 `docs/白板/image/` 这类文档资源里。

## 十、改名检查清单

改一个节点名，至少同步这些位置，漏一处就是静默故障：

1. 同文件的节点定义、`next` / `on_error` 数组。
2. `target: "<节点名>"`、`roi: "<节点名>"`、`roi_offset` 之类引用其它节点识别框的字段。
3. `assets/interface.json` 的任务级 `pipeline_override` 键（例：`IsFreezes`、`BattleWinSuccess`、`MoveRight`）。
4. `assets/resource/tasks/option/*.json` 与 `assets/resource/tasks/preset/*.json` 里的 `pipeline_override` 键。
5. `agent/*.py` 里按字符串取用节点名的地方：`override_pipeline({...})`、`override_next(...)`、`get_hit_count(...)`、`clear_hit_count(...)`、`run_action("...")`、`focus` 文案。
6. `custom_action` / `custom_recognition` 的注册名字符串与 `@AgentServer.*("<名>")` 装饰器。
7. 旧名留在仓库内的开发笔记里（`.agents/`）作为对照，不要直接删。

改完后跑一遍校验：

- 解析全部 `assets/resource/pipeline/*.json`（读文件用 UTF-8；用 `Get-Content -Raw` 会把中文按 GBK 解码，导致所有文件假报 JSON 错）。
- 断言「被 `next` / `on_error` 引用但未定义」的节点为 0（比较时去掉 `[JumpBack]` 前缀）。
- 断言每个 `custom_action` / `custom_recognition` 名字都能在 `agent/*.py` 里搜到字符串字面量。
- `python -m py_compile agent/*.py`。

## 十一、常见错误

| 错误写法 | 问题 | 正确写法 |
|---|---|---|
| `FactoryChooseStart` | 点的是提交按钮，不是选项 | `FactoryClickStart` |
| `EntrustEnterChapter`（实际选大关） | `Enter` 表示换页，这里是同页选目标 | `EntrustChooseStage` |
| `chapter_name`（实际是大关名） | 词与对象不符，层级冲突 | `stage_name` |
| `GiftChooseFrist` / `StartUpClickIco` / `FactoryGetRewardFaild` | 拼写错误 | `GiftChooseFirst` / `StartUpClickIcon` / `FactoryGetRewardFailed` |
| `FactoryChooseCharater` | 拼写错误，且与 `EscortChooseCharacter` 不一致 | `FactoryChooseCharacter` |
| `TaskTaskFactoryRecoAct` | 注册名里重复出现节点名 | `TaskFactoryRecoAct` |
| `End`（无前缀） | 通用词会与其它任务的节点撞名 | `EscortClickOkEnd` |
