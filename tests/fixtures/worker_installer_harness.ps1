param(
    [Parameter(Mandatory=$true)][string]$InstallerPath,
    [Parameter(Mandatory=$true)][string]$ProgramDataRoot,
    [Parameter(Mandatory=$true)][string]$QueueDir,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$RunnerPath
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PROGRAMDATA = $ProgramDataRoot
$global:record = @{ registered = 0; acl_calls = 0 }

function Get-LocalGroup {
    [CmdletBinding()]
    param([string]$Name)
    if ($Name -ne 'CodexSandboxUsers') { throw 'Unexpected group' }
    [pscustomobject]@{ SID = [pscustomobject]@{ Value = 'S-1-5-21-123456789-123456789-123456789-1001' } }
}
function icacls.exe {
    $global:record.acl_calls += 1
    $global:record.acl_arguments = @($args)
    $global:LASTEXITCODE = 0
}
function New-ScheduledTaskAction {
    param($Execute, $Argument)
    [pscustomobject]@{ Execute = $Execute; Arguments = $Argument }
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{ LogonType = $LogonType; RunLevel = $RunLevel }
}
function New-ScheduledTaskTrigger {
    param([switch]$Once, $At, $RepetitionInterval)
    [pscustomobject]@{ IntervalSeconds = $RepetitionInterval.TotalSeconds }
}
function New-ScheduledTaskSettingsSet {
    param($MultipleInstances, $ExecutionTimeLimit, [switch]$StartWhenAvailable)
    [pscustomobject]@{ MultipleInstances = $MultipleInstances; LimitSeconds = $ExecutionTimeLimit.TotalSeconds }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Principal, $Trigger, $Settings, $Description, [switch]$Force)
    $global:record.registered += 1
    $global:record.task = @{
        name = $TaskName; action = $Action; principal = $Principal
        trigger = $Trigger; settings = $Settings; description = $Description
    }
}
function Get-ScheduledTask { throw 'Unexpected real-task query' }
function Unregister-ScheduledTask { throw 'Unexpected task removal' }

& $InstallerPath -QueueDir $QueueDir -TaskName 'MogooOilCodexTitleWorker-0123456789abcdef' `
    -PythonPath $PythonPath -RunnerPath $RunnerPath
$global:record | ConvertTo-Json -Compress -Depth 6
