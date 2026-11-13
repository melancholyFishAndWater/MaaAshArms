<#
.SYNOPSIS
检查 Windows 开发启动文件的完整性标签；显式应用或恢复时要求管理员。
.DESCRIPTION
默认只读。仅发现 PATH 上 uv.exe、项目 .venv 的 python.exe/pythonw.exe，
以及 pyvenv.cfg 指定的基础 python.exe/pythonw.exe。不会执行 Python 来发现路径。
不递归修改目录，不修改所有者或 DACL。备份的是 LABEL_SECURITY_INFORMATION，
不是 icacls /save。Apply/Restore 不用于日常 agent 自动启动。
.PARAMETER ProjectRoot
项目根目录，默认由本脚本位置推导。
.PARAMETER Apply
仅将已发现的 Low/Untrusted 可执行文件设为 Medium。
.PARAMETER BackupPath
Apply 必填，新建的 JSON 备份文件；不得覆盖既有文件。
.PARAMETER Restore
显式指定本工具生成的备份文件，恢复原始完整性标签。
.PARAMETER SelfTest
只执行纯逻辑自检，不读写文件权限。
.EXAMPLE
pwsh -File tools/dev/python_integrity.ps1
.EXAMPLE
pwsh -File tools/dev/python_integrity.ps1 -Apply -BackupPath C:\Users\me\python-label-backup.json
.EXAMPLE
pwsh -File tools/dev/python_integrity.ps1 -Restore C:\Users\me\python-label-backup.json
#>
[CmdletBinding(DefaultParameterSetName='Check')]
param(
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [Parameter(Mandatory, ParameterSetName='Apply')][switch]$Apply,
    [Parameter(Mandatory, ParameterSetName='Apply')][string]$BackupPath,
    [Parameter(Mandatory, ParameterSetName='Restore')][string]$Restore,
    [Parameter(Mandatory, ParameterSetName='Test')][switch]$SelfTest
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-LabelRid([string]$Sddl) {
    if ($Sddl -match '^S:(?:AI|AR|P)*\(ML;[A-Z]*;(?:NW|0x1);;;(LW|UN|ME)\)$') {
        return @{UN=0;LW=4096;ME=8192}[$Matches[1]]
    }
    throw "不支持的完整性标签格式，拒绝修改：$Sddl"
}
function Assert-Record($Record, $Current, [string]$ExpectedPath) {
    if ($Record.path -cne $ExpectedPath -or $Record.sha256 -cne $Current.sha256 -or
        $Record.identity -cne $Current.identity) { throw '目标路径、文件身份或 SHA256 已改变，拒绝操作。' }
}
if ($SelfTest) {
    $count = 0
    foreach ($pair in @(@('S:(ML;ID;NW;;;LW)',4096),@('S:(ML;;NW;;;UN)',0),@('S:(ML;;NW;;;ME)',8192),@('S:AI(ML;ID;NW;;;LW)',4096),@('S:AI(ML;;NW;;;ME)',8192))) {
        if ((Get-LabelRid $pair[0]) -ne $pair[1]) { throw '标签解析自检失败' }; $count++
    }
    foreach ($bad in @('D:(A;;FA;;;WD)','S:(ML;;NW;;;HI)','S:(ML;;NW;;;LW)(AU;;FA;;;WD)')) {
        $rejected = $false; try { Get-LabelRid $bad | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { throw '标签白名单自检失败' }; $count++
    }
    $r = @{path='x';sha256='a';identity='b'}
    Assert-Record $r $r 'x'; $count++
    foreach ($bad in @(@{sha256='z';identity='b'},@{sha256='a';identity='z'})) {
        $rejected=$false;try { Assert-Record $r $bad 'x' } catch { $rejected=$true }
        if (-not $rejected) { throw '文件身份检查自检失败' }; $count++
    }
    Write-Output "SelfTest: $count passed (no permission mutation)"; exit 0
}
if ($env:OS -ne 'Windows_NT') { throw '本工具仅支持 Windows。' }

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class PythonIntegrityNative {
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] static extern uint GetNamedSecurityInfo(string n,int t,uint i,out IntPtr o,out IntPtr g,out IntPtr d,out IntPtr s,out IntPtr sd);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] static extern bool ConvertSecurityDescriptorToStringSecurityDescriptor(IntPtr sd,uint r,uint i,out IntPtr str,out uint len);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string s,uint r,out IntPtr sd,out uint len);
 [DllImport("advapi32.dll")] static extern bool GetSecurityDescriptorSacl(IntPtr sd,out bool present,out IntPtr acl,out bool def);
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode)] static extern uint SetNamedSecurityInfo(string n,int t,uint i,IntPtr o,IntPtr g,IntPtr d,IntPtr s);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
 [StructLayout(LayoutKind.Sequential)] struct Info { public uint attr; public System.Runtime.InteropServices.ComTypes.FILETIME c,a,w; public uint vol,hi,lo,links,idhi,idlo; }
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle f,out Info i);
 public static string Identity(SafeFileHandle f) { Info i; if(!GetFileInformationByHandle(f,out i)) throw new Win32Exception(); return i.vol.ToString("X8")+":"+i.idhi.ToString("X8")+i.idlo.ToString("X8"); }
 public static string Read(string path) {
  IntPtr o,g,d,s,sd,text; uint len; uint rc=GetNamedSecurityInfo(path,1,0x10,out o,out g,out d,out s,out sd);
  if(rc!=0) throw new Win32Exception((int)rc);
  try { if(!ConvertSecurityDescriptorToStringSecurityDescriptor(sd,1,0x10,out text,out len)) throw new Win32Exception(); try{return Marshal.PtrToStringUni(text);}finally{LocalFree(text);} } finally{LocalFree(sd);}
 }
 // LABEL_SECURITY_INFORMATION changes mandatory labels only, preserving audit SACL and DACL.
 public static void Write(string path,string label) {
  IntPtr sd; uint len; if(!ConvertStringSecurityDescriptorToSecurityDescriptor(label,1,out sd,out len)) throw new Win32Exception();
  try { bool p,def; IntPtr acl; if(!GetSecurityDescriptorSacl(sd,out p,out acl,out def)||!p) throw new Exception("Missing mandatory label");
   uint rc=SetNamedSecurityInfo(path,1,0x10,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,acl); if(rc!=0) throw new Win32Exception((int)rc);
  } finally {LocalFree(sd);}
 }
}
'@
function Get-SafeFile([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or $item.Extension -ine '.exe') { throw "目标不是 exe 文件：$Path" }
    $cursor = $item
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "拒绝联接/符号链接路径：$($cursor.FullName)" }
        $cursor = if ($cursor -is [IO.FileInfo]) { $cursor.Directory } else { $cursor.Parent }
    }
    return $item.FullName
}
function Get-Snapshot([string]$Path) {
    $p=Get-SafeFile $Path
    $f=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        $sha=[Security.Cryptography.SHA256]::Create()
        try { $hash=([BitConverter]::ToString($sha.ComputeHash($f))).Replace('-','') } finally { $sha.Dispose() }
        return [pscustomobject]@{path=$p;identity=[PythonIntegrityNative]::Identity($f.SafeFileHandle);sha256=$hash;label=[PythonIntegrityNative]::Read($p)}
    } finally { $f.Dispose() }
}
function Get-Targets {
    $root=(Get-Item -LiteralPath $ProjectRoot).FullName
    if (-not (Test-Path -LiteralPath (Join-Path $root 'pyproject.toml') -PathType Leaf)) { throw '项目根缺少 pyproject.toml。' }
    $cfg=Join-Path $root '.venv\pyvenv.cfg'
    $homes=@(Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^home\s*=\s*(.+)$' } | ForEach-Object { ($_ -split '=',2)[1].Trim() })
    if ($homes.Count -ne 1 -or -not [IO.Path]::IsPathRooted($homes[0])) { throw 'pyvenv.cfg 必须恰有一个绝对 home 路径。' }
    $baseHome=Get-Item -LiteralPath $homes[0] -Force
    if (($baseHome.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        $resolved=$baseHome.ResolveLinkTarget($true)
        if ($null -eq $resolved -or -not ($resolved -is [IO.DirectoryInfo])) { throw '基础 Python home 别名无法解析到真实目录。' }
        $baseHome=$resolved
    }
    $uv=Get-Command uv.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $candidates=@($uv.Source)
    foreach ($dir in @((Join-Path $root '.venv\Scripts'),$baseHome.FullName)) {
        foreach ($name in @('python.exe','pythonw.exe')) { $p=Join-Path $dir $name; if (Test-Path -LiteralPath $p -PathType Leaf) { $candidates += $p } }
    }
    return @($candidates | ForEach-Object { Get-SafeFile $_ } | Select-Object -Unique)
}
$targets=Get-Targets
$current=@($targets | ForEach-Object { Get-Snapshot $_ })
$current | Select-Object path,label,sha256 | Format-List | Out-Host
if (-not $Apply -and -not $Restore) { Write-Output '只读检查完成；未修改任何权限。'; exit 0 }
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Apply/Restore 必须从管理员 PowerShell 显式运行；脚本不会自动提权。' }

if ($Apply) {
    $records=@($current | Where-Object { $_.label -match ';;;(?:LW|UN)\)' })
    foreach($r in $records) { if ((Get-LabelRid $r.label) -ge 8192) { throw '拒绝非 Low/Untrusted 目标' } }
    if ($records.Count -eq 0) { Write-Output '没有 Low/Untrusted 启动文件，无需修改。'; exit 0 }
    $full=[IO.Path]::GetFullPath($BackupPath)
    $json=@{schema='maaasharms-python-integrity-v1';project=[IO.Path]::GetFullPath($ProjectRoot);records=$records} | ConvertTo-Json -Depth 5
    $backup=[IO.File]::Open($full,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json);$backup.Write($bytes,0,$bytes.Length) } finally { $backup.Dispose() }
    Write-Output "原始 LABEL_SECURITY_INFORMATION 已保存：$full"
    Write-Output "恢复命令：pwsh -File `"$PSCommandPath`" -ProjectRoot `"$ProjectRoot`" -Restore `"$full`""
    foreach ($r in $records) {
        $now=Get-Snapshot $r.path; Assert-Record $r $now $r.path
        if ($now.label -cne $r.label) { throw '标签在预览后改变，拒绝应用。' }
        [PythonIntegrityNative]::Write($r.path,'S:(ML;;NW;;;ME)')
        if ((Get-LabelRid ([PythonIntegrityNative]::Read($r.path))) -ne 8192) { throw "修改验证失败：$($r.path)；使用备份恢复。" }
        Write-Output "已验证 Medium：$($r.path)"
    }
    Write-Output "恢复命令：pwsh -File `"$PSCommandPath`" -ProjectRoot `"$ProjectRoot`" -Restore `"$full`""
} else {
    $data=Get-Content -LiteralPath $Restore -Raw | ConvertFrom-Json
    if ($data.schema -cne 'maaasharms-python-integrity-v1' -or $data.project -ine [IO.Path]::GetFullPath($ProjectRoot)) { throw '备份格式或项目不匹配。' }
    $seen=@{}
    foreach ($r in @($data.records)) {
        if ($targets -inotcontains $r.path -or $seen.ContainsKey($r.path)) { throw '备份含非发现目标或重复路径，拒绝恢复。' }
        $seen[$r.path]=$true
        if ((Get-LabelRid $r.label) -ge 8192) { throw '备份不是原始 Low/Untrusted 标签。' }
        $now=Get-Snapshot $r.path; Assert-Record $r $now $r.path
        if ($now.label -cne $r.label -and (Get-LabelRid $now.label) -ne 8192) { throw '当前不是原始或预期 Medium 状态，拒绝覆盖。' }
    }
    foreach ($r in @($data.records)) {
        $now=Get-Snapshot $r.path;Assert-Record $r $now $r.path
        if ($now.label -ceq $r.label) { Write-Output "原始标签未改变，跳过：$($r.path)"; continue }
        if ((Get-LabelRid $now.label) -ne 8192) { throw '恢复期间标签改变，拒绝覆盖。' }
        [PythonIntegrityNative]::Write($r.path,$r.label)
        if ([PythonIntegrityNative]::Read($r.path) -cne $r.label) { throw "恢复标签（含标志/策略）验证失败：$($r.path)" }
        Write-Output "已验证恢复：$($r.path)"
    }
}
