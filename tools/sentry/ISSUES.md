# 使用问题记录（tools/sentry）

## `evidence` 下载附件遇 SSL 中断时整包不落地（连已取到的日志也没了）

- 命令：`python -m tools.sentry.cli evidence --issue PYTHON-2M --force`
- 报错：`错误：下载附件失败（网络）：[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1077)`，随后直接 `跳过：没有 evidence.json`。连重试两次都失败，而该 issue 的日志本身是能取到的（`logs captured`）——附件失败把整包一起废掉了。
- 绕过：`python -m tools.sentry.cli evidence --issue PYTHON-2M --force --no-images` 只拉日志，再用 `flow --case …/debug --task-id …`（图上画面格为空）。`--timeout 300` 没能救回附件。
- 待办口径：附件失败时不该丢掉已下成功的日志——按"日志已 captured"继续落盘，并在 `evidence.json` 里记 `attachment_status=failed`，让 `flow` 照常画（只是没现场图）。

## 复用旧证据包时不报旧包属于哪个事件，容易照旧包出图

- 命令：`python -m tools.sentry.cli evidence --issue PYTHON-2F`（该 issue 已有 9 个事件）
- 现象：打 `提示: 已存在（--force 重新下载）` 就复用了 10-31 那份包（`event.id=c7d66270…`，beta.57 / 13:45 那次），随后 `flow --case` 画出的图是**第 1 个事件**的形状；而 issue 当时的最新事件已是 11-01 12:27（beta.58，另一个失败节点）。表格里的 `次数=9` 是唯一线索，不复用包自己的 event id/时间都不回显。
- 绕过：画图前先 `sentry issue view <SHORT-ID>` 对时间，或直接 `evidence --issue <ID> --force` 重拉最新事件；`--event <event-id>` 可以指定某一次。
- 待办口径：复用路径也应回显旧包的 `event.id / received / release`，并在"旧包的 event ≠ issue 最新 event"时给一条明确提示。

---

有新问题时一行一条追加，写清**什么命令、什么报错、怎么绕过的**；处理完就删掉那一条——已修的不留历史
（改了哪个函数、为什么这么改写在代码注释里），决定"不做"的写进 [README 的「功能取舍」](README.md#功能取舍)。