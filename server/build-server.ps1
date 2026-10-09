<#
.SYNOPSIS
    Builds a ready-to-run Persistent Siege Server from a licensed Dungeon Siege install.

.DESCRIPTION
    1. Verifies the game install and its version (1.11.1).
    2. Copies the game to OutDir and installs the content tank into Resources.
    3. Writes DungeonSiege.ini (windowed 800x600) and server.cfg for the world.
    4. Enables DirectPlay and adds firewall rules for the session ports.
    5. With -Lobby: builds open-zone from -LobbySource with its deploy-build
       script and points the host at it.
    6. Registers and starts the supervisor (logon task of the current user).

    Run from an elevated PowerShell on the server machine:

        powershell -ExecutionPolicy Bypass -File build-server.ps1 `
            -GameDir "C:\Program Files (x86)\Steam\steamapps\common\Dungeon Siege 1" `
            -OutDir C:\PersistentSiege -World utrae

.PARAMETER LobbySource
    Path to an open-zone checkout. Its repository publishes no license, so it
    is not redistributed with this package; obtain it (with the author's
    permission) and point this at it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$GameDir,
    [Parameter(Mandatory)][string]$OutDir,
    [Parameter(Mandatory)][ValidateSet('ehb', 'utrae')][string]$World,
    [switch]$Lobby,
    [string]$LobbySource,
    [string]$LobbyAddress = '127.0.0.1',
    [switch]$NoSupervisor,
    [switch]$SkipDirectPlay
)
$ErrorActionPreference = 'Stop'
$pkg = Split-Path $PSScriptRoot -Parent

function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw 'run build-server.ps1 from an elevated PowerShell (DirectPlay and firewall need it)' }

# 1. Verify the install ---------------------------------------------------------
Step 'verifying game install'
$exe = Join-Path $GameDir 'DungeonSiege.exe'
foreach ($f in @($exe, 'Resources\Logic.dsres', 'Resources\Objects.dsres', 'Resources\Terrain.dsres',
                 'Maps\World.dsmap', 'Maps\MpWorld.dsmap')) {
    $p = if ([IO.Path]::IsPathRooted($f)) { $f } else { Join-Path $GameDir $f }
    if (-not (Test-Path $p)) { throw "not a complete Dungeon Siege install: missing $p" }
}
$ver = (Get-Item $exe).VersionInfo.FileVersion
if ($ver -notlike '1.11.1*') {
    throw "DungeonSiege.exe is version $ver; the server is built and tested against 1.11.1 (UI automation depends on it)"
}
$tank = Get-ChildItem (Join-Path $pkg 'Resources') -Filter 'pss_content_v*.dsres' |
    Sort-Object { [int]($_.BaseName -replace '\D', '') } | Select-Object -Last 1
if (-not $tank) { throw "content tank not found in $pkg\Resources" }
Write-Host "game $ver, content $($tank.Name)"

# 2. Copy the game and install the tank ----------------------------------------
Step "copying game to $OutDir"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
robocopy $GameDir $OutDir /E /NFL /NDL /NJH /NJS /NP /XD (Join-Path $GameDir 'PersistentSiege') | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
# only one content version may be present: server and clients must match
Get-ChildItem (Join-Path $OutDir 'Resources') -Filter 'pss_content_v*.dsres' | Remove-Item -Force
Copy-Item $tank.FullName (Join-Path $OutDir 'Resources') -Force
$sup = Join-Path $OutDir 'PersistentSiege\supervisor'
New-Item -ItemType Directory -Force -Path $sup | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'supervisor\*') $sup -Recurse -Force -Exclude 'server.cfg', 'state.json', 'logs'

# 3. Configuration --------------------------------------------------------------
Step 'writing DungeonSiege.ini and server.cfg'
$ini = @(
    'width = 800', 'height = 600', 'bpp = 32', 'fullscreen = false',
    'driver_description = Primary Display Driver - Hardware TnL', '',
    '[multiplayer]'
)
if ($Lobby) { $ini += @("gun_server = $LobbyAddress", 'gun_server_port = 2300') }
else { $ini += @('gun_server = dungeonsiege.zone.com', 'gun_server_port = 2300') }
Set-Content -LiteralPath (Join-Path $OutDir 'DungeonSiege.ini') -Value $ini -Encoding ASCII

$cfgPath = Join-Path $sup 'server.cfg'
if (Test-Path $cfgPath) {
    Write-Host "keeping existing $cfgPath"
} else {
    (Get-Content (Join-Path $sup 'server.cfg.template') -Raw).
        Replace('{{WORLD}}', $World).
        Replace('{{GAME_DIR}}', $OutDir).
        Replace('{{LOG_DIR}}', (Join-Path $sup 'logs')) | Set-Content -LiteralPath $cfgPath -Encoding ASCII
}

# 4. DirectPlay and firewall ----------------------------------------------------
if (-not $SkipDirectPlay) {
    Step 'enabling DirectPlay'
    $reboot = $false
    foreach ($feat in 'LegacyComponents', 'DirectPlay') {
        $state = (Get-WindowsOptionalFeature -Online -FeatureName $feat).State
        if ($state -ne 'Enabled') {
            $r = Enable-WindowsOptionalFeature -Online -FeatureName $feat -All -NoRestart
            if ($r.RestartNeeded) { $reboot = $true }
        }
    }
    if ($reboot) { Write-Warning 'DirectPlay was enabled; reboot before the first session.' }
}

Step 'adding firewall rules'
$rules = @(
    @{ Name = 'PSS DirectPlay session'; Protocol = 'UDP'; LocalPort = '6073' },
    @{ Name = 'PSS DirectPlay game';    Protocol = 'UDP'; LocalPort = '2302-2400' }
)
if ($Lobby) {
    $rules += @{ Name = 'PSS lobby';      Protocol = 'UDP'; LocalPort = '2300' }
    $rules += @{ Name = 'PSS lobby news'; Protocol = 'TCP'; LocalPort = '2301' }
}
foreach ($r in $rules) {
    Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $r.Name -Direction Inbound -Action Allow `
        -Protocol $r.Protocol -LocalPort $r.LocalPort | Out-Null
}
Get-NetFirewallRule -DisplayName 'PSS host program' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'PSS host program' -Direction Inbound -Action Allow `
    -Program (Join-Path $OutDir 'DungeonSiege.exe') | Out-Null

# 5. Lobby ----------------------------------------------------------------------
if ($Lobby) {
    Step 'building open-zone lobby'
    if (-not $LobbySource -or -not (Test-Path $LobbySource)) {
        throw '-Lobby needs -LobbySource <open-zone checkout> (not redistributed: the repository has no license)'
    }
    foreach ($tool in 'go', 'msbuild') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "-Lobby needs $tool on PATH (Go and Visual Studio C++ build tools)"
        }
    }
    $deploy = Get-ChildItem $LobbySource -Recurse -Include 'deploy-build.ps1', 'deploy-build.bat', 'deploy-build.cmd' |
        Select-Object -First 1
    if (-not $deploy) { throw "no deploy-build script found in $LobbySource" }
    $lobbyOut = Join-Path $OutDir 'PersistentSiege\lobby'
    New-Item -ItemType Directory -Force -Path $lobbyOut | Out-Null
    Push-Location $deploy.DirectoryName
    try {
        if ($deploy.Extension -eq '.ps1') { & $deploy.FullName } else { & cmd /c $deploy.FullName }
        if ($LASTEXITCODE) { throw "open-zone deploy-build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
    Write-Host "open-zone built; follow its README to run the lobby service (clients: client\multiplayer.ini.snippet)"
}

# 6. Supervisor -----------------------------------------------------------------
if (-not $NoSupervisor) {
    Step 'registering the supervisor'
    & (Join-Path $sup 'install-supervisor.ps1') -StartNow
}

Write-Host ''
Write-Host "Server built in $OutDir" -ForegroundColor Green
Write-Host "  config:  $cfgPath"
Write-Host "  logs:    $(Join-Path $sup 'logs')"
Write-Host "  clients: install $($tank.Name) (client\install-client.ps1); join by IP or via the lobby."
