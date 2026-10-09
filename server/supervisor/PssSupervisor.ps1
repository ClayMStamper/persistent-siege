<#
.SYNOPSIS
    Persistent Siege Server supervisor: owns the Dungeon Siege host process.

.DESCRIPTION
    Launches the stock game, drives the host-game menus (PssHost.psm1), then
    watches the session: process alive, window responsive, DirectPlay port
    bound. Repeated failures restart the host; a daily restart in a quiet
    window returns bosses, gates and world clutter to their authored state.

    Runs in the interactive session of the server account (logon task
    registered by install-supervisor.ps1); a Windows service cannot, because
    services have no desktop for the game to render or be driven on.

.PARAMETER Config
    Path to server.cfg. Defaults to server.cfg next to this script.

.PARAMETER Once
    Host once, report whether the session came up, and exit (for testing).
#>
[CmdletBinding()]
param(
    [string]$Config = (Join-Path $PSScriptRoot 'server.cfg'),
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PssHost.psm1') -Force

$cfg = Read-PssConfig $Config
$logDir = if ($cfg['supervisor.log_dir']) { $cfg['supervisor.log_dir'] } else { Join-Path $PSScriptRoot 'logs' }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'supervisor.log'
$stateFile = Join-Path $PSScriptRoot 'state.json'

function Write-Log([string]$Message, [string]$Level = 'INFO') {
    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $Level, $Message
    Write-Host $line
    $max = [int]$cfg['supervisor.log_max_mb'] * 1MB
    if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt $max) {
        $keep = [int]$cfg['supervisor.log_keep']
        for ($i = $keep - 1; $i -ge 1; $i--) {
            if (Test-Path "$logFile.$i") { Move-Item -Force "$logFile.$i" "$logFile.$($i + 1)" }
        }
        Move-Item -Force $logFile "$logFile.1"
        if (Test-Path "$logFile.$($keep + 1)") { Remove-Item -Force "$logFile.$($keep + 1)" }
    }
    Add-Content -LiteralPath $logFile -Value $line
}

function Get-State {
    if (Test-Path $stateFile) { return Get-Content $stateFile -Raw | ConvertFrom-Json }
    return [pscustomobject]@{ host_character_created = $false }
}

function Save-State($State) { $State | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8 }

function Stop-Host([System.Diagnostics.Process]$Process) {
    if ($Process -and -not $Process.HasExited) {
        Write-Log "stopping host pid $($Process.Id)"
        $Process.CloseMainWindow() | Out-Null
        if (-not $Process.WaitForExit(10000)) { $Process.Kill(); $Process.WaitForExit(5000) | Out-Null }
    }
    # stray instances from a crashed supervisor would hold the DirectPlay port
    Get-Process DungeonSiege -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Log "killing stray DungeonSiege.exe pid $($_.Id)" 'WARN'
        $_.Kill()
    }
}

function Start-Host {
    # Returns the host process once the session is up, or $null.
    $state = Get-State
    $cfg['pss.create_character'] = if ($state.host_character_created) { 'false' } else { 'true' }
    Stop-Host $null
    Write-Log "launching host: world=$($cfg['game.world']) map=$(Get-PssMapName $cfg)"
    $p = Start-PssGame $cfg
    try {
        Invoke-PssHostSetup -Process $p -Cfg $cfg -Log { param($m) Write-Log $m }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $timeout = [int]$cfg['supervisor.setup_timeout']
        while ($sw.Elapsed.TotalSeconds -lt $timeout) {
            if ($p.HasExited) { throw "host exited during setup (code $($p.ExitCode))" }
            if (Test-PssSessionBound $p.Id) {
                Write-Log "session up: pid $($p.Id), UDP 6073 bound after $([int]$sw.Elapsed.TotalSeconds)s"
                if (-not $state.host_character_created) {
                    $state.host_character_created = $true
                    Save-State $state
                }
                return $p
            }
            Start-Sleep -Seconds 2
        }
        throw "session port not bound within $timeout s"
    }
    catch {
        Write-Log "host setup failed: $($_.Exception.Message)" 'ERROR'
        if (-not $p.HasExited -and $p.MainWindowHandle -ne [IntPtr]::Zero) {
            $shot = Join-Path $logDir ('setup-failure-{0:yyyyMMdd-HHmmss}.png' -f (Get-Date))
            try { Save-PssScreenshot $p.MainWindowHandle $shot; Write-Log "screenshot: $shot" } catch { }
        }
        Stop-Host $p
        return $null
    }
}

function Get-NextDailyRestart {
    $spec = $cfg['supervisor.daily_restart']
    if (-not $spec) { return [datetime]::MaxValue }
    $t = [datetime]::ParseExact($spec, 'HH:mm', $null)
    $next = (Get-Date).Date.Add($t.TimeOfDay)
    if ($next -le (Get-Date)) { $next = $next.AddDays(1) }
    return $next
}

# One supervisor per machine.
$mutex = New-Object System.Threading.Mutex($false, 'Global\PersistentSiegeSupervisor')
if (-not $mutex.WaitOne(0)) { Write-Log 'another supervisor is already running; exiting' 'WARN'; exit 1 }

Write-Log "supervisor starting (config $Config)"
$backoff = 15
$host_ = $null
try {
    while ($true) {
        $host_ = Start-Host
        if ($Once) { if ($host_) { Write-Log 'test host is up; leaving it running' }; exit ([int](-not $host_)) }
        if (-not $host_) {
            Write-Log "retrying in $backoff s" 'WARN'
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min($backoff * 2, 600)
            continue
        }
        $backoff = 15
        $restartAt = Get-NextDailyRestart
        Write-Log "next scheduled restart: $restartAt"
        $failures = 0
        $interval = [int]$cfg['supervisor.health_interval']
        $limit = [int]$cfg['supervisor.health_failures']

        while ($true) {
            Start-Sleep -Seconds $interval
            if ((Get-Date) -ge $restartAt) {
                Write-Log 'scheduled daily restart'
                break
            }
            $problems = @()
            if ($host_.HasExited) { $problems += "process exited (code $($host_.ExitCode))" }
            elseif (-not (Test-PssResponsive $host_)) { $problems += 'window not responding' }
            elseif (-not (Test-PssSessionBound $host_.Id)) { $problems += 'session port not bound' }
            if ($problems.Count -eq 0) {
                $failures = 0
                # Dungeon Siege stops simulating while its window is inactive
                if ([Pss.Native]::GetForegroundWindow() -ne $host_.MainWindowHandle) {
                    Write-Log 'host window lost focus; reactivating' 'WARN'
                    [void][Pss.Native]::Activate($host_.MainWindowHandle)
                }
                continue
            }
            $failures++
            Write-Log ("health check failed ({0}/{1}): {2}" -f $failures, $limit, ($problems -join '; ')) 'WARN'
            if ($host_.HasExited -or $failures -ge $limit) { break }
        }
        Stop-Host $host_
    }
}
finally {
    if (-not $Once) { Stop-Host $host_ }
    Write-Log 'supervisor exiting'
    $mutex.ReleaseMutex()
}
