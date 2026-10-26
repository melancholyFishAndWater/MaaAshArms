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

## `B7-default-case` 对 `checkbox` 误报（`default_case` 是数组）

- 命令：`python tools/validation/validate_refs.py`
- 现象：`assets/resource/tasks/option/reward.json` 的 `reward` 是 `checkbox`，按协议 `default_case` 就是数组（`["siku_reward_switch"]`）；`check_option_shape` 用 `cases[0].get("name") != default_case` 做标量比较，字符串永远不等于数组 → 恒报 `option reward 的 default_case=['siku_reward_switch'] 与 cases[0]='siku_reward_switch' 不一致`。
- 绕过：按现状报（是误报，不动 option 文件——改成字符串反而违反 checkbox 语义）。要修就改 `check_option_shape`：`default_case` 是 list 时判断 `cases[0].get("name") in default_case`，并补进 `--self-test`（「checkbox 的数组 default_case 不报 B7」）。
- 出处：`tools/validation/validate_refs.py:1662-1676`、`.agents/interface.md` 的「switch / select 的默认项必须放 `cases[0]`」（checkbox 走 `SelectedCases`，与 `Index` 无关）

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。
