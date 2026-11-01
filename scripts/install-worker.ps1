param(
    [Parameter(Mandatory=$true)][string]$QueueDir,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RunnerPath,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$queueFull = [IO.Path]::GetFullPath($QueueDir)
$queuePrefix = [IO.Path]::GetFullPath((Join-Path $env:PROGRAMDATA 'MogooOilCodexTitle')) + [IO.Path]::DirectorySeparatorChar
if (-not $queueFull.StartsWith($queuePrefix, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $queueFull) -ne 'queue') {
    throw '只能操作本插件在 ProgramData 下的队列目录'
}
if ($TaskName -notmatch '^MogooOilCodexTitleWorker-[0-9a-f]{16}$') { throw '任务名称无效' }
$sandbox = Get-LocalGroup -Name 'CodexSandboxUsers' -ErrorAction SilentlyContinue
if ($Remove) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    if ($sandbox -and (Test-Path -LiteralPath $queueFull)) {
        & icacls.exe $queueFull /remove:g ('*' + $sandbox.SID.Value) /T | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '无法撤销队列授权' }
    }
    @{status='removed'} | ConvertTo-Json -Compress
    exit 0
}
foreach ($folder in @('pending', 'working', 'done', 'locks')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $queueFull $folder) | Out-Null
}
if ($sandbox) {
    & icacls.exe $queueFull /grant ('*' + $sandbox.SID.Value + ':(OI)(CI)M') /T | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法授权本插件队列' }
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument ('-X utf8 "' + $RunnerPath + '"')
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings `
    -Description '以当前用户身份消费标题插件的队列，不执行队列提供的命令' -Force | Out-Null
@{status='installed'; sandbox_queue_authorized=[bool]$sandbox} | ConvertTo-Json -Compress
