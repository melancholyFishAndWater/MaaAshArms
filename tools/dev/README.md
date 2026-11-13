# Windows Python 完整性标签检查

本工具只用于 Windows 上已确认的 Low/Untrusted 可执行文件问题。正常开发只需 `uv sync --locked`，不需要运行修复。

## 默认检查（不需要管理员）

在仓库根执行：

```powershell
pwsh -File tools/dev/python_integrity.ps1
```

只读发现当前 PATH 的 `uv.exe`、项目 `.venv/Scripts/python.exe` / `pythonw.exe`，以及 `pyvenv.cfg` 的 `home` 对应的基础 Python。不会执行这些 Python 来寻找路径，也不扫描其他环境。显示路径、完整性标签和 SHA256。

## 显式修复

先核对预览中的目标，再打开管理员 PowerShell，在同一项目根执行：

```powershell
pwsh -File tools/dev/python_integrity.ps1 -Apply -BackupPath "$env:USERPROFILE/python-label-backup.json"
```

备份文件必须不存在，其父目录应是可信、持久、用户拥有的目录；不要把唯一备份放到会清理的 `temp`。脚本仅把发现目标中 Low/Untrusted 的可执行文件标为 Medium，修改前保存原始 `LABEL_SECURITY_INFORMATION`，并核对文件身份、SHA256 和原标签。已是 Medium 或更高的文件跳过。

不递归修改文件夹、不修改所有者或 DACL、不修改用户 Temp、不自动提权、不重启解释器、不修改 D 盘发布包。仅允许把 `pyvenv.cfg` 的基础 home 目录别名解析为实际目录，随后拒绝实际目标或父路径的其他联接/符号链接。Medium 文件可能无法再被 Low 沙箱更新，这是预期边界。

## 恢复原标签

从管理员 PowerShell 运行脚本输出的恢复命令，或执行：

```powershell
pwsh -File tools/dev/python_integrity.ps1 -Restore "$env:USERPROFILE/python-label-backup.json"
```

恢复必须使用同一项目和同一 PATH 解析结果。备份中的目标必须仍是当前发现的启动文件；身份和 SHA256 必须相同，当前标签必须是预期 Medium 或完全未变的原标签（后者跳过）。脚本拒绝任意目标、重复路径、已替换文件和其他标签格式。不应手工编辑备份。恢复重新写入原 mandatory label（含原策略与继承标志），不覆盖其他审计 SACL。`icacls /save` 不是完整性标签备份。

若途中某个目标失败，脚本立即停止，备份仍保留；已成功修改的文件不会静默自动回滚。恢复也逐个验证，失败立即停止，应保留输出和备份，不要扩大目录或递归操作。

## 验证与自检

应用后用普通权限终端重新启动 Python，检查实际进程为 Medium、临时目录创建成功、agent 完成握手。管理员进程成功不能替代普通权限验证。重建环境或更新解释器后需重新检查；文件 Medium 也不能提升原本 Low 的父进程。

```powershell
pwsh -File tools/dev/python_integrity.ps1 -SelfTest
Get-Help ./tools/dev/python_integrity.ps1 -Full
```

自检只测试标签白名单和文件身份判定，不调用权限修改 API。Apply/Restore 在此项目测试中不自动运行；真实修改需要用户明确执行。工具失败与未验证项见 [ISSUES.md](ISSUES.md)。
