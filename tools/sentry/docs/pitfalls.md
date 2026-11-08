# 日志、验证与读报告时的坑

改 `lib/report_common.py`（日志/锁/提示）或给报告加字段前先读这份；「功能取舍」在
[AGENTS.md](../AGENTS.md)，口径在 [methodology.md](methodology.md)。

## 日志（`--log-level` / `--log-file`）

`telemetry`（以及 `flow` 转发的绘图入口）每次运行会在 `temp/logs/` 下按时间戳建一个日志文件
（`<tool>_<YYYYMMDD-HHMMSS>_<pid>.log`），带时间戳与级别，**独立于** `temp/flow/` 与
`temp/telemetry/`。控制台仍只走 stderr，stdout 留给机器可读的结果。

`--log-level DEBUG|INFO|WARNING|ERROR` 过滤级别；`--log-file <路径>` 改路径，传 `none`
只走控制台、不写文件。

## 本地验证

零网络、零凭证，校验 release 解析、任务表与失败标记分流、Δpp 方向、工件往返、三种渲染：

```bash
python -m tools.sentry.cli selftest
python tools/validation/cli.py check-sentry-offline    # 同一套断言 + 渲染一份合成报告方便人眼看
```

自检覆盖 release 解析、任务表与失败标记分流、Δpp 方向、工件往返、三种渲染，以及
**流程图解析器的文本入参**与**诊断行为**（时间戳回跳重排、只有识别事件时的合成、导出截断的行、
空窗口的三种说法、`--task-id` 严格过滤与任务判定窗口），再加 **Explore CSV 提取**（块边界接续、
五分类计数、任务可画性、截断按类型拆、入口报错）、**失败证据的纯逻辑**（解包白名单、附件名取 task_id、
去重、扁平 context 键、失败标记、可复制命令）、**证据清单与磁盘一致**（`--force --no-log` 留下的
日志/现场图仍进 `written` 与 `hints`）与**并发锁的持有者判据**（残留锁不提示且自动清掉、真并发仍提示）——
远程日志没有本地文件、只有正文，这几条都在遥测路径上。
它不需要 Pillow——画图才需要；也不需要网络与登录。

> 画图（`flow`）依赖 Pillow，而**发布包自带的解释器不含它**（`.python-runtime/` 与包内
> `python/` 都 `import PIL` 失败）。用装了 Pillow 的系统 Python 跑即可；绘图不进发布包。

联网后建议按这个顺序确认打通（第一条能同时验证登录、target 与 release 格式）：

```bash
python -m tools.sentry.cli fetch --name probe \
    --field release --field "count_unique(user)" --period 30d
```

工件里应能看到 `MaaAshArms@v1.0.0-beta.<N>` 形式的 release。若 `total_rows == 0`，
说明窗口内没有上报数据，或 `target` / `period` 写错。

## 读报告时的注意点

- **时间窗口受 Sentry 限制**：超过约 30 天绝对计数不可用；更早的数据由 Sentry 端保留策略决定，
  脚本不缓存历史（AGENTS.md「功能取舍」里有对应条目）。
- **`avg(span.duration)` 不能当耗时用**：存在"挂着不动几小时再继续"的运行（实测某版本 `Friend` 的
  p95 = 9.9e6 ms，合出来的 `avg` 是 21.3 分钟而 p50 只有 6.4 秒）。全表 `avg/p50` 的中位数是
  1.0x 但**最大 2845x**——中位数正常只是因为大多数版本没有长尾跑。**只看 p50 / p95**，
  或取数时加 `span.duration:<1800000` 排除挂机跑（`methodology.md` 里那条 `fetch` 命令已经带了）。
- **`--query` 按 release 过滤必须带通配符**：`release:MaaAshArms@v1.0.0-beta.37`、`release:beta.37`、
  裸写 `MaaAshArms@v1.0.0-beta.37` **都返回 0 行且不报错**，只有 `release:*beta.37` 命中（同窗口实测 83 行）。
  按版本切数据前先用 `--field release` 核对写法，否则会把"过滤没生效"读成"该版本没数据"。
- **配额**：失败附件实测 0.97 / 1.24 MB（`failure_attachments_sample_rate: 1`），12 个 issue 里只有 2 个带附件。
  读报告时若看到某天失败数暴增，先确认是不是一次启动失败后队列继续跑出一串失败。
- **受限沙箱下 `sentry` 一律 `Fatal: Error: unable to open database file`**（连 `--version` 也一样）：
  照 [README 的「前置条件」](../README.md#前置条件) 把 `cli.db` 复制到可写目录并设 `SENTRY_CONFIG_DIR`。
  `evidence` 走的是同一个 CLI，同样需要它。
- **并发提示看锁文件里的 pid 还活不活**（`report_common.lock_owner_alive()`）：报告会往
  `temp/telemetry/report.lock` 写自己的 pid，下一条报告只在那个进程**还在**时才提示并发。
  持锁进程已退出（或锁是旧版本工具写的、读不出 pid）时：前者顺手删掉残留锁，后者照旧提示——
  这条锁只是建议性的，不做互斥，也不会拦截任何命令。
