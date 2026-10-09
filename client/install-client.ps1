<#
.SYNOPSIS
    Installs the Persistent Siege content tank into a Dungeon Siege install.

.DESCRIPTION
    Copies Resources\pss_content_v<N>.dsres into the game's Resources folder,
    removing other versions (client and server must run the same one).
    With -LobbyAddress, points the in-game server browser at an open-zone
    lobby. -Uninstall removes the tank again.

        powershell -ExecutionPolicy Bypass -File install-client.ps1
#>
[CmdletBinding()]
param(
    [string]$GameDir = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$LobbyAddress,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$pkg = Split-Path $PSScriptRoot -Parent
$res = Join-Path $GameDir 'Resources'
if (-not (Test-Path (Join-Path $GameDir 'DungeonSiege.exe'))) { throw "no DungeonSiege.exe in $GameDir (pass -GameDir)" }

Get-ChildItem $res -Filter 'pss_content_v*.dsres' | ForEach-Object {
    Remove-Item $_.FullName -Force
    Write-Host "removed $($_.Name)"
}
if ($Uninstall) { return }

$tank = Get-ChildItem (Join-Path $pkg 'Resources') -Filter 'pss_content_v*.dsres' |
    Sort-Object { [int]($_.BaseName -replace '\D', '') } | Select-Object -Last 1
if (-not $tank) { throw "content tank not found in $pkg\Resources" }
Copy-Item $tank.FullName $res -Force
Write-Host "installed $($tank.Name)"

if ($LobbyAddress) {
    $ini = Join-Path $GameDir 'DungeonSiege.ini'
    $lines = if (Test-Path $ini) { Get-Content $ini } else { @() }
    $out = New-Object System.Collections.Generic.List[string]
    $section = ''; $done = $false
    foreach ($l in $lines) {
        if ($l -match '^\s*\[(.+)\]') {
            if ($section -eq 'multiplayer' -and -not $done) { $out.Add("gun_server = $LobbyAddress"); $out.Add('gun_server_port = 2300'); $done = $true }
            $section = $Matches[1].Trim().ToLower()
        }
        if ($section -eq 'multiplayer' -and $l -match '^\s*gun_server(_port)?\s*=') { continue }
        $out.Add($l)
    }
    if (-not $done) {
        if ($section -ne 'multiplayer') { $out.Add('[multiplayer]') }
        $out.Add("gun_server = $LobbyAddress"); $out.Add('gun_server_port = 2300')
    }
    Set-Content -LiteralPath $ini -Value $out -Encoding ASCII
    Write-Host "server browser now uses lobby $LobbyAddress"
}
