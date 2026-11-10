# 使用问题记录（tools/drawing）

- `python tools/drawing/cli.py flow debug/maafw.log --list --from "2026-11-04 16:30" --to "2026-11-04 16:31"`：
  末尾几条 `提示：` 打印的是 `Note(text='…', kind='stale')` 的 repr 而不是正文。原因在
  `lib/flowlog/render.py` 的 `list_only` 分支：`lines.append(f"提示：{note}")` 应取 `note.text`
  （非 `--list` 路径打印的是 `result.notes` 的 `text`，所以只有 `--list` 受影响）。本轮没改。
- `python tools/drawing/cli.py cases --telemetry-bulk --telemetry-version ">=v1.0.0-beta.69"`（2026-11-10 12:54，联网，`SENTRY_CONFIG_DIR=temp/sentry-cfg`）：`sentry issue list 5e4d413b64a7/python --period 30d --limit 200 --json` 与 `sentry issue view PYTHON-1X / 1N / 3E --json` 均报「Error: Not authenticated. Run 'sentry auth login' first.」（退出码 10），本地遥测库改用旧数据、`PYTHON-1N` 与 `PYTHON-3E` 拉取证据包失败退概览卡。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。
