<#
.SYNOPSIS
    Registers the Persistent Siege supervisor to start at logon of the server account.

.DESCRIPTION
    The host game needs a desktop, so the supervisor runs as a scheduled task
    in the interactive session of -User (configure that account to log on
    automatically, e.g. with Sysinternals Autologon). The task restarts the
    supervisor if it ever exits and has no run-time limit.
#>
[CmdletBinding()]
param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$TaskName = 'PersistentSiegeSupervisor',
    [switch]$StartNow
)
$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'PssSupervisor.ps1'
if (-not (Test-Path (Join-Path $PSScriptRoot 'server.cfg'))) { throw "server.cfg not found next to $script; run build-server.ps1 first" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Persistent Siege Server: hosts and watches the Dungeon Siege world.' -Force | Out-Null
Write-Host "registered scheduled task '$TaskName' (runs at logon of $User)"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'started'
}
