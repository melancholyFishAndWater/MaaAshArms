# 依赖与已知坑

## 依赖与前置

- **Pillow**：画图要它，而**发布包自带的解释器不含 Pillow**（`.python-runtime/` 与包内
  `python/` 都 `import PIL` 失败）。用装了 Pillow 的系统 Python 跑；绘图不进发布包。
- 无 Pillow 时 `import` 仍可用（Pillow 只在画图、加载字体时才惰性导入），只有真画图会报"需要 Pillow"。

## 已知坑

- **产出目录靠上溯级数算**：`REPO_ROOT` 在 `lib/flowlog/paths.py`，是 `tools/drawing/lib/flowlog/`
  上溯四级。再改目录层级必须同步改，否则图会静默落到 `tools/drawing/lib/flowlog/temp/flow/`。
  `tools/validation/cli.py check-python` 的 `repo-root` 检查拦这个。
- **`--no-thumbs` 只影响"贴不贴图"，不改变框高**（`slot_h` 与 `thumbs` 无关，
  `box_height_for` 只看该框有没有图）。别拿产物尺寸判断贴图是否生效——用 sha256 或直接看图。
- **贴图失败会打警告，但有上限**：一次几百张图读不了时只打前 5 条 + 一条汇总。读不出来的
  常见原因是分卷缺卷、或图片已被框架的 7 天清理删掉。接住的是一组"字节坏了"的异常：
  `OSError`（含 `PIL.UnidentifiedImageError` 与图片数据截断）、`ValueError`（偏移为负）、
  `zipfile.BadZipFile`（坏 CRC）、`zlib.error`（deflate 流坏）、`EOFError`（压缩流比记录的短）。
  后三个不是 `OSError` 子类，漏一个就会让一张坏图把整张图掀掉。
- **同时间戳的 vision 图要给 `sort` 加次键**：文件名毫秒段位数不固定（`.51` 与 `.510` 解析成同一个
  `datetime`），同一时刻有多张图时，`VisionIndex` 里裸 `list.sort()` 会继续比较 `MediaEntry` 抛
  `TypeError`（实测 37803 张里有 26 组）。两处 `sort` 都用 `key=lambda …: (…, entry.member)`，
  `find()` 是线性扫取第一个进 tol 的，所以次键只决定"同时间戳时哪张胜出"，给个稳定顺序就够。
- **副标题里不要用 `⚠`**：msyh 没有该字形，会画成方框。同一类坑实测还有 `✓`、`↩`、`▶`、`☑`、`↺`：
  都会画成方框（`getbbox` 看着有值也照样是方框）。候选那一行用的是 `√` 与"弹回"两个字。
- **分组框与角标是在框/连线之前画的**，它们只是背景；角标不透明且压在第一格左上，所以那一格的上边框
  入口会从角标**右边**的中点进（`in_port()` 里 `badge_boxes` 那段），否则箭头看着像被角标切断。
- **候选那一行比明细小一档（`font_size - 5`）**：实测最长的组合 `父 BattleWinSuccess：√TrainingBattleWin`
  在 13px 下要 255px，而带图槽的框只有 216px 可写；11px 下 219px 刚好放得下（现在角标接管了派发者名字，
  这一行只剩 `候选：…`，更宽松）。缩略图那张图槽只占右半边，所以这一行是从图槽左边过字的（与明细行一样），
  量宽度时用 `row_w` 而不是 `text_w`。
- **分组框要预留净增高**（`column_geo()`）：角标画在"第一格上边界的上方"，而上一格与本格之间只有十几像素，
  所以每组要预留 `group_extra_one = 角标高 + 2×内边距 + group_gap`——**不预留角标会压住上一格的文字**
  （踩过：`column_geo` 只留了角标高 + 内边距，角标吃掉上一格的下半行）。
  相邻两组之间另留 `group_gap_between`（26px）：上一组的框底自带 `group_pad` 内边距，只留 `group_gap`
  会让两个分组框贴到一起。组内相邻两步的间隔收成 `group_gap`（12px），否则默认 36px 叠上内边距太空。
- **一份日志装了多次运行是常态**：`reco_id` / `action_id` 是**进程内**计数，派发者
  （`[cur_node_=X]`）也按进程分开记，所以同一份 `maafw.log` 里的多次运行不会互相串台；
  判"哪一次"用 `--task-id` + `--from/--to`。Explore CSV 还原出来的日志天然跨多次运行
  （实测两个进程、三个任务），画图时必须给 `--task-id`。
- **父节点的归属按 `task_id` 而不是按进程**：`Node.NextList` 事件由 MFA 进程发出（带 `task_id`）、
  `PipelineTask node done` 由 agent 进程写（带 `[task_id_=N]`），两者天然不同进程，按进程匹配会一条都对不上。
  只有 agent 进程那种 `[message=Node.NextList.X] [details_json={…}]` 形态**没有 `name` 与 `task_id`**
  （实测 16 条全是这样），判不了父节点，工具直接跳过它而不是猜。
- **同一份日志里可能一条 `task start:` 都没有**（实测 Sentry 拉回来的 PYTHON-16 只有 `task end:`），
  所以"解析到某一行时正在跑哪个 task"是以最近一条 `node done` 为准的；只有入口名退回会因此落空。
- **日志只剩尾巴时图上会写"注意：日志仅含尾部"**：遥测附件只带 `maafw.log` 的 512 KB 尾部，
  没有这行标注的话读图的人会以为任务从日志第一行开始。
- **整份日志会读进内存**：为检测时间回跳必须读完（本机 10.9 MB 的 `maafw.log` 约 2.4 s、十几 MB 内存）。
  框架自己会轮转日志（`debug/` 下有 `maafw.bak.<日期>.log`），不需要为它做流式裁剪。
- **`RENDERER_VERSION`**：改动会影响产物内容时（版面、注记、解析口径）必须 +1，
  否则 `cases` 会命中旧的 `manifest.json` 直接"跳过（已画过）"，渲染改了却不重画。现在是 **23**
  （定义在 `lib/flowlog/constants.py`）。
- **合并与贴图解析的先后**：`overlay_repeats()` 的 `has_thumb` 问的是"第 i 轮实际贴到图没有"，
  所以只能在 `thumb_of` 算完之后调用；按"传没传 `--vision`"提前判断会把其实有图的轮折掉。
  折叠后 `thumb_of` 的键（旧列索引）必须重排，否则 `--no-thumbs` 与 on_error 回贴会把图贴到别的列。
- **合并列的列头要留角标走廊**：父节点角标压在第一格左上，首格若开着一组却没预留走廊，角标会盖住
  「第 a–b 轮 · ×N」——合并列的身份信息全在列头里。只有合并列补这段预留，非合并列维持原样，
  这是 `--no-overlay` 仍能逐字节还原旧图的前提。
- **一份证据包里的日志常覆盖整次运行**（多个任务）：`evidence` 拉下来的 `maafw.log` 就是这样，
  必须配 `--task-id`；同一条 trace 上的多个 issue 会共用同一份日志。
