# 使用问题记录（tools/drawing）

- `python tools/drawing/cli.py flow debug/maafw.log --list --from "2026-11-04 16:30" --to "2026-11-04 16:31"`：
  末尾几条 `提示：` 打印的是 `Note(text='…', kind='stale')` 的 repr 而不是正文。原因在
  `lib/flowlog/render.py` 的 `list_only` 分支：`lines.append(f"提示：{note}")` 应取 `note.text`
  （非 `--list` 路径打印的是 `result.notes` 的 `text`，所以只有 `--list` 受影响）。本轮没改。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。
