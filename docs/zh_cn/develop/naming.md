# 命名规范

本文约束 pipeline 节点名、agent 侧注册名与参数键的写法。改节点名、加新节点、拆分子任务时按本文执行。

本文只讲命名。节点该配哪些字段请看 [MaaFramework 文档](https://maafw.com/docs/1.1-QuickStarted)；文中引用的 `.agents/` 路径是本机开发笔记（该目录不入库），没有这些文件时按源码与本文为准。

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
| 状态/判据节点 | `<任务前缀><系动词/状态词>` | `EscortInHome`、`TrainIsAcceptable`、`TrainingHasHomeName` |
| 子过程入口 | `<任务前缀>Enter<页面>` / `<任务前缀>Init` | `EnterMaintenance`、`EntrustInit` |
| 出口节点 | `<任务前缀><Exit/End/Success/Failed>` | `EscortClickOkEnd`、`FactoryGetRewardFailed` |
| 日常门控 | `<任务前缀>CheckDaily` / `<任务前缀>DailyUpdate` | `EntrustCheckDaily`、`GiftDailyUpdate` |

## 三、动词表

### `Click`

点一个按钮或区域，触发一次动作或提交。**页面有明显变化**。

例：`EscortClickOk`（确认）、`FactoryClickCancel`（取消建造）、`FactoryClickStart`（开始建造）。

### `Choose`

从一组同类可选项里挑一个，选项之间可以反复换。**页面无明显变化**。

例：`FactoryChooseNumber`（建几个）、`GiftChooseFirstGift`（第一个礼物）、`EntrustChooseLevel`（小关）。

### `Enter`

进/回一个页面。宾语写页面名。

例：`EntrustEnterMainLine`（主线页面）、`EntrustEnterRewardMainLine`（领奖页面）、`BattleEnterMainPage`等。

反例：页内点一个按钮切到本页的另一块内容，不算 `Enter`。

### 其他动词

| 动词 | 用途 | 示例 |
| --- | --- | --- |
| `Start` | 启动一条正式流程 | `EntrustStart`（点出击开始委派） |
| `Get` | 领取类动作 | `FactoryGetReward`、`TrainGetReward`、`FeiduGetMetal` |
| `TouchDown` / `TouchUp` | 按住、松手 | `EntrustTouchDown`、`EntrustTouchUp`、`EntrustTouchUp2` |
| `Move` | 滑动、移动列表 | `MoveUp`、`MoveRight`、`TrainInfoMover` |
| `Close` | 关掉弹窗/页面 | `NoticeClose`、`TrainCloseStatusArrow` |

## 四、术语分层

一条链会穿过多个层级时，给每一层一个专用词，不要让同一个词兼两层。

**委派链的层级**：

```text
主线篇章（EntrustEnterMainLine，OCR"主线篇章"）
└── 章节（EntrustNextChapter，点"下一章"翻页）
    └── 大关（EntrustChooseStage，识别大关名，如"结晶废墟"）
        └── 小关（EntrustChooseLevel，匹配两位数字）
```

| 层级 | 节点名用 | 参数键用 |
| --- | --- | --- |
| 章节 | `Chapter`（`EntrustNextChapter`） | — |
| 大关 | `Stage`（`EntrustChooseStage`） | `stage_name` |
| 小关 | `Level`（`EntrustChooseLevel`） | `level` |

新任务出现类似层级时沿用这套词：`Chapter` / `Stage` / `Level` 分别指章节、大关、小关，不要写 `Section`、`Part`、`SubLevel` 之类的同义词。

## 五、动作节点

- 名字里的动词按第三节选，宾语与界面文字对齐（`确认` → `ClickOk`、`取消` → `ClickCancel`、`完成` → `ClickFinish`）。
- 同一个按钮在不同任务里叫法保持一致：`ClickOk`、`EscortClickOk`、`FactoryClickSkipOk`；不要出现 `ClickYes` 这类同义写法。
- 循环重试节点，靠 `desc` 里的 `[Loop]` 标记，例如 `"desc": "[Loop] 点击出击按钮 即使是灰色的"`。
- 出口/收尾节点在名字里标出性质：`*End`（链结束）、`*Success`（动作生效）、`*Failed`（失败分支）。成功探针节点用 `And all_of [<被探针节点>] + inverse` 时，名字取 `<被探针节点>Success`，例如 `EntrustClickFinishSuccess`。失败分支统一 `*Failed`（历史遗留的 `EscortClickOkFail` 可择机改）。

## 六、状态判据节点

判据节点自身一般不做动作，名字用系动词或状态词起头。

| 前缀 | 含义 | 动作/next |
| --- | --- | --- |
| `Has` | 页面上存在某个元素 | 无 |
| `In` | 当前在某个页面 | 有动作 |
| `Is` | 布尔状态 | 都可能 |
| `Able` / `Unable` | 可操作 / 不可操作 | 条件动作/next |

## 七、agent 侧命名

### 注册名与类名

- 注册名 = 节点名 + 后缀：识别用 `<节点名>Reco`，动作用 `<节点名>Act`。
- 类名一般与注册名一致。

### 参数键

- `custom_recognition_param` / `custom_action_param` 必须使用字典。键使用 `snake_case`，值是描述真实对象的词。
- 与层级相关的键按第四节命名。
- 同名键在不同任务里含义必须相同；含义不同就换名字（曾经的 `chapter_name` 实际指大关，已改成 `stage_name`）。
- agent 侧建议使用 `parse_params(..., "<键>")` 读取，不推荐使用默认值，否则可能静默报错。

### 模块全局变量

- 模块级可变状态用 `_` 前缀（`_train_list`、`_finish_count`、`_max_number`），导出的配置用无前缀名（`stage_name`、`level`、`battle_targets`）。
- 跨模块读这些变量必须写 `<模块名>.<变量名>`。

## 九、文件名与目录

- pipeline 片段：任务名全小写 snake_case，一个任务一个文件，公共节点进 `base.json`。
- 选项片段：与任务同名，放 `assets/resource/tasks/option/`。
- 识别模板：`assets/resource/image/<任务目录>/<元素名>.png`，元素名用英文 CamelCase（`Battle/BattleButton.png`、`Train/GoTrainLight.png`）。新任务沿用已有目录，不要为同一个页面再建一个。
