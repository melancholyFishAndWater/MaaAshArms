# 使用问题记录（tools/validation）

## Loop 类别退役后，5 条按名字判据会变死规则

- 命令：`python tools/validation/validate_refs.py`
- 现象：`feidu_gift.json` 已无 `Loop` 结尾节点（只剩 `desc` 里两处 `[Loop]` 标记，已删）；其余 10 个文件仍有 21 个 `*Loop*` 节点。规范方向是 Loop 让位给 Branch，但以下判据仍按 `endswith("Loop")` / `"Loop" in name` 识别目标类别，节点改名或删除后它们常年 0 命中、与文档脱节：
  - `A8-loop-guard`（`check_a8_loop_guard`，:929）
  - `A10-maxhit-in-loop`（`check_a10_maxhit_in_loop`，:997）
  - `A13-loop-next`（`check_a13_naming`，:1162）
  - `A13-ensure-loop-next`（`check_a13_references`，:1209）
  - `A14-fallback-same-probe`（`check_a14_loop_fallback`，:1268）
- 连带：`A7-jumpback-target` 的 `elif "Loop" in name` 分支（:902）、`A18-next-length` 的 Loop 档（`MAX_NEXT_LOOP`，:1520）、`CLASS_SUFFIXES` 里的 `"Loop"`（:131）。
- 绕过：暂不改脚本，按现状报。等 `TrainLoop`/`TrainingLoop` 等剩余 21 个 `*Loop*` 节点退役时，一并把上面 5 条判据改成按语义（守卫 + next 含该守卫探针族）而不是名字后缀，或直接删掉。

## `Ensure` 类别停用后，按名字识别 `Ensure` 的 7 条判据会变死规则

- 命令：`python tools/validation/validate_refs.py`
- 现象：2026-10-30 起约定 `Ensure` 不允许再用、现存 9 个节点待删除，但下列判据仍把 `Ensure` 当正式类别（行号按 `44b472d`）：
  - `A9-ensure-order`（`check_a9_ensure`，:943-992）
  - `A16-ensure-depth`（`check_a16_ensure_depth`，:1458-1475；自测 :2813）
  - `A13-ensure-jumpback`（:1194-1203）、`A13-ensure-loop-next`（:1210-1216）
  - `check_a13_naming` 的 Ensure 跳过（:1174）
  - `A18-next-length` 的 Task/Ensure 档（`MAX_NEXT_TASK_ENSURE`，:151 与 :1533）
  - `check_a7_jumpback` 的 Ensure 分支（:885 起）、`_is_inverse_guard`（:1320，按 `Ensure` 名前缀判 inverse 守卫，牵连 `A15` 与 `check_a14_loop_fallback` :1281）
  - `ENSURE_RE`（:257）、`MAX_ENSURE_DEPTH`（:149）
- 连带：9 个节点定义（`battle.json:146`、`battle_training.json:67`、`entrust.json:194,226,276`、`entrust_reward.json:10,46`、`feidu.json:34`、`maintenance.json:100`）与 13 处 `[JumpBack]…Ensure…` 引用（`entrust` 7、`battle_training` 2、`entrust_reward` 1、`feidu` 1、`feidu_gift` 1、`maintenance` 1）要同批删；删完的悬空引用会被 `A1-dangling-node` 抓到，可当回归判据。
- 绕过：暂不改脚本、按现状报。删节点时把上面判据改成按语义判（"逐级收缩到枢纽的就位链"现由 `Branch` 平铺 + 入口动作 + 调用方 `[JumpBack]` 兜底表达）或直接删掉，并补 `--self-test`。
- 出处：本会话用户裁定"不允许 `Ensure`，其将在未来删除"（2026-10-30）；节点与引用分布为 `validate_refs.py` 按名扫描的结果。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。
