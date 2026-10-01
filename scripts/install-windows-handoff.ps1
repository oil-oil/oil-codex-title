<#
.SYNOPSIS
  注册或卸载 oil-codex-title 的 Windows 沙箱移交 worker。

.DESCRIPTION
  Windows 上 Codex 以沙箱账户执行 Stop Hook，该身份对 ~/.codex 只有只读权限，
  Hook 里既起不了 app-server，也连不上真实用户 daemon 的 control socket。
  因此 Hook 只投递命名请求，由本脚本注册的**用户身份**计划任务周期性执行
  `oil_codex_title.py worker` 完成命名（详见 docs/Windows沙箱移交.md）。

  脚本只做两件事：
    1. 建立共享队列目录，并仅对该目录给沙箱账户授予修改权限（不触碰 .codex）；
    2. 注册以当前用户身份运行的计划任务。

  以当前用户身份运行即可；若提示权限不足，再用管理员 PowerShell 重跑。

.PARAMETER Uninstall
  删除计划任务并撤销队列目录上的沙箱权限。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-windows-handoff.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-windows-handoff.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$QueueDir = (Join-Path $env:ProgramData 'oil-codex-title\queue'),
    [string]$SandboxGroup = "$env:COMPUTERNAME\CodexSandboxUsers",
    [string]$TaskName = 'OilCodexTitleHandoff',
    [int]$IntervalMinutes = 5,
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $pluginRoot 'scripts\oil_codex_title.py'
$groupName = $SandboxGroup.Split('\')[-1]

if (-not $PythonPath) {
    # 优先 pythonw.exe：计划任务以交互身份运行时，控制台解释器会闪出一个窗口。
    $PythonPath = @(
        (Join-Path $env:APPDATA 'uv\python\cpython-3.13-windows-x86_64-none\pythonw.exe'),
        (Join-Path $env:APPDATA 'uv\python\cpython-3.13-windows-x86_64-none\python.exe'),
        (Get-Command pythonw -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $PythonPath) { throw '找不到 Python 解释器；请用 -PythonPath 指定（需 3.10 及以上）' }
if (-not (Test-Path $entry)) { throw "找不到入口脚本：$entry" }

if ($PSBoundParameters.ContainsKey('Uninstall')) {
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop }
    catch { & schtasks.exe /Delete /TN $TaskName /F 2>&1 | Out-Null }
    Write-Host "已删除计划任务 $TaskName"
    if (Test-Path $QueueDir) {
        & icacls $QueueDir /remove:g $SandboxGroup /T | Out-Null
        Write-Host "已撤销 $SandboxGroup 对 $QueueDir 的权限（目录保留，可手工删除）"
    }
    return
}

foreach ($sub in 'requests', 'done', 'failed') {
    New-Item -ItemType Directory -Force -Path (Join-Path $QueueDir $sub) | Out-Null
}
Write-Host "队列目录：$QueueDir"

$hasGroup = $false
try { $hasGroup = [bool](Get-LocalGroup -Name $groupName -ErrorAction Stop) } catch { $hasGroup = $false }
if ($hasGroup) {
    & icacls $QueueDir /grant "${SandboxGroup}:(OI)(CI)M" /T | Out-Null
    Write-Host "已授权 $SandboxGroup 修改该目录（仅此目录，未触碰 .codex）"
} else {
    Write-Warning "未找到本地组 $groupName；跳过授权。若 Hook 报无法写入队列，请确认 Codex 的 Windows 沙箱已初始化后重跑本脚本。"
}

$taskArgument = "-X utf8 `"$entry`" worker"
$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $PythonPath -Argument $taskArgument -WorkingDirectory $pluginRoot
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650))
    )
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Description '处理 oil-codex-title 沙箱 Hook 移交的命名请求' -Force | Out-Null
    $registered = $true
} catch {
    # 受限令牌（例如管理员组为 deny-only）下注册 Cmdlet 会被拒；schtasks.exe 走的是另一条路径。
    Write-Warning "计划任务 Cmdlet 注册失败（$($_.Exception.Message.Trim())）；改用 schtasks.exe 重试。"
}
if (-not $registered) {
    & schtasks.exe /Create /TN $TaskName /SC MINUTE /MO $IntervalMinutes /TR "`"$PythonPath`" $taskArgument" /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "计划任务注册失败。请手工执行：schtasks /Create /TN $TaskName /SC MINUTE /MO $IntervalMinutes /TR `"`"$PythonPath`" $taskArgument`" /F"
    }
}
Write-Host "已注册计划任务 $TaskName（每 $IntervalMinutes 分钟，以当前用户身份运行）"

Write-Host '自检：'
& $PythonPath -X utf8 $entry worker
Write-Host "验证：以上应输出 processed/renamed/kept/skipped/failed 统计；队列由 Hook 侧写入（默认 $QueueDir\requests）。"
