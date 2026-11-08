# tools/perf —— 已知坑（pitfalls）

踩过的现场记录。用法见 [`../README.md`](../README.md)，口径见 [`methodology.md`](methodology.md)。

## bench（合成基准）

- **本机 wheel 没有 `MaaDbgControlUnit.dll`**：`DbgController` 创建会直接失败
  （`Failed to load library MaaDbgControlUnit`）。`bench` 用 `CustomController`。
- **纯色模板会让 NCC 退化**：`TM_CCOEFF_NORMED` 对零方差模板失去区分度，本机实测
  40×40 纯红模板在白图上 33 个位置全判 `1.0`。模板必须有结构。
- **`timeout` 在派发者上**：MaaFW 的 `run_next` 用当前节点的 `reco_timeout` 卡下一轮的派发。
- **首遍偏冷**：同一模式第一次约 43 ms、热身后中位数约 29 ms；偶发 60 ms 的离群值。

## rounds（真实日志）

- **`NextList.Failed` 含 `need_to_stop`**：任务被停止时也会发这个事件，别全当超时。
- **桶不保证单调**：2 个失败候选的派发可能比 1 个的快（命中的候选是便宜 OCR）。
- **整轮空转样本很少**：短日志里常常是 0 个，别拿它当主口径。
- **`.bak` 名字不是内容时间**：改名发生在下一次启动，日志内容的起止要读行首时间戳。
- **会话内资源版本会变**：MFA 更新资源后会再写一条 `资源版本`；基线取会话起点那条，输出里标「会话内变过 N 次」。

## 通用

- **控制台编码**：脚本把 stdout/stderr 固定成 UTF-8；Windows 的 GBK 终端可能显示乱码。
