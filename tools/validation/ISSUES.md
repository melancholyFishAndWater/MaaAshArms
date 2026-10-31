# 使用问题记录（tools/validation）

## Loop 类别退役后，4 条按名字判据会变死规则

- 命令：`python tools/validation/validate_refs.py`
- 现象：`feidu_gift.json` 已无 `Loop` 结尾节点（只剩 `desc` 里两处 `[Loop]` 标记，已删）；其余 10 个文件仍有 21 个 `*Loop*` 节点。规范方向是 Loop 让位给 Branch，但以下判据仍按 `endswith("Loop")` / `"Loop" in name` 识别目标类别，节点改名或删除后它们常年 0 命中、与文档脱节：
  - `A8-loop-guard`（`check_a8_loop_guard`，:930）
  - `A10-maxhit-in-loop`（`check_a10_maxhit_in_loop`，:942）
  - `A13-loop-next`（`check_a13_naming`，:1004）
  - `A14-fallback-same-probe`（`check_a14_loop_fallback`，:1176）
- 连带：`A7-jumpback-target` 的 `elif "Loop" in name` 分支（:905）、`A18-next-length` 的 Loop 档（`MAX_NEXT_LOOP`，:151）、`CLASS_SUFFIXES` 里的 `"Loop"`（:132）。
- 绕过：暂不改脚本，按现状报。等 `TrainLoop`/`TrainingLoop` 等剩余 21 个 `*Loop*` 节点退役时，一并把上面 4 条判据改成按语义（守卫 + next 含该守卫探针族）而不是名字后缀，或直接删掉。

## `Ensure` 类别停用（已处理完毕：2026-10-31 全部删除，`A25` = 0）

- 命令：`python tools/validation/validate_refs.py`
- 处理：2026-10-30 与作者定 `Ensure` 不允许再用、现存 9 个节点待删。规范与脚本升到 v3.1：
  - 删掉按名字识别 `Ensure` 的判据：`A9-ensure-order`、`A16-ensure-depth`、`A13-ensure-jumpback`、`A13-ensure-loop-next`、`check_a13_naming` 的 `Ensure` 跳过、`A18` 的 `Task/Ensure` 档与 `ENSURE_RE` / `MAX_ENSURE_DEPTH` / `MAX_NEXT_TASK_ENSURE`；
  - `check_a7_jumpback` 本来就没有按 `Ensure` 判的分支（旧记录写的“Ensure 分支”在本版不存在），只有“指向自己”与“指向 `Loop`”两支，无需改动；
  - `_is_inverse_guard` 改成按结构判（顶层 `inverse: true`，或识别配置里带 `inverse`），`A15-error-branch-*` 与 `A14-fallback-same-probe` 原语义不变；
  - 新增 `A25-ensure-banned`（warn）：节点名匹配 `Ensure[A-Z]` 就报，文案写明待删与替代写法。**故意 warn 不用 error**——留作回归护栏。
- 现状：2026-10-31 存量 9 个节点（`battle.json`、`battle_training.json`、`entrust.json` ×3、`entrust_reward.json` ×2、`feidu.json`、`maintenance.json`）与同批 13 处 `[JumpBack]…Ensure…` 引用已全部删除；全量实测 `A25` = 0、`A1-dangling-node` = 0、error 0。
- 出处：本会话用户裁定"不允许 `Ensure`，其将在未来删除"（2026-10-30）；节点与引用分布为 `validate_refs.py` 按名扫描的结果。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。
