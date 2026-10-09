# Persistent Siege Server: game launch, host-menu automation and health probes.
#
# The menus are driven with real input (SendInput) at positions taken from the
# game's own interface files (ui_layout.psd1). Clicks are expressed in the
# 800x600 UI space and scaled to the window's client area. Text is typed with
# hardware scan codes so it reaches the game whether it reads window messages
# or DirectInput.

Set-StrictMode -Version 2

if (-not ('Pss.Native' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Pss {
public static class Native {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Explicit)] struct INPUTU { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public INPUTU u; }

    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);

    // Windows only lets the process that received the last input take the
    // foreground; a synthetic ALT tap satisfies that rule.
    public static bool Activate(IntPtr h) {
        for (int i = 0; i < 5; i++) {
            if (GetForegroundWindow() == h) return true;
            keybd_event(0x12, 0, 0, IntPtr.Zero);
            keybd_event(0x12, 0, 2, IntPtr.Zero);
            ShowWindow(h, 9);
            BringWindowToTop(h);
            SetForegroundWindow(h);
            System.Threading.Thread.Sleep(200);
        }
        return GetForegroundWindow() == h;
    }
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] static extern uint MapVirtualKey(uint code, uint mapType);
    [DllImport("user32.dll")] static extern short VkKeyScan(char c);

    const uint MOUSE_LEFTDOWN = 0x2, MOUSE_LEFTUP = 0x4;
    const uint KEY_UP = 0x2, KEY_SCANCODE = 0x8;

    static void Send(INPUT i) { SendInput(1, new[] { i }, Marshal.SizeOf(typeof(INPUT))); }

    public static void Click() {
        INPUT i = new INPUT(); i.type = 0;
        i.u.mi.flags = MOUSE_LEFTDOWN; Send(i);
        System.Threading.Thread.Sleep(60);
        i.u.mi.flags = MOUSE_LEFTUP; Send(i);
    }

    public static void Key(ushort vk, bool shift) {
        if (shift) KeyEdge(0x10, false);
        KeyEdge(vk, false); System.Threading.Thread.Sleep(25); KeyEdge(vk, true);
        if (shift) KeyEdge(0x10, true);
        System.Threading.Thread.Sleep(25);
    }

    static void KeyEdge(ushort vk, bool up) {
        INPUT i = new INPUT(); i.type = 1;
        i.u.ki.scan = (ushort)MapVirtualKey(vk, 0);
        i.u.ki.flags = KEY_SCANCODE | (up ? KEY_UP : 0);
        Send(i);
    }

    public static void TypeChar(char c) {
        short v = VkKeyScan(c);
        if (v == -1) return;
        Key((ushort)(v & 0xff), (v & 0x100) != 0);
    }
}
}
'@
}

$script:Layout = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'ui_layout.psd1')

function Read-PssConfig {
    param([Parameter(Mandatory)][string]$Path)
    $cfg = @{}
    $section = ''
    foreach ($line in Get-Content -LiteralPath $Path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith(';') -or $t.StartsWith('#')) { continue }
        if ($t -match '^\[(.+)\]$') { $section = $Matches[1].Trim(); continue }
        if ($t -match '^([^=]+?)\s*=\s*(.*)$') { $cfg["$section.$($Matches[1].Trim())"] = $Matches[2].Trim() }
    }
    return $cfg
}

function Get-PssMapName([hashtable]$Cfg) {
    switch ($Cfg['game.world']) {
        'ehb'   { 'map_world' }
        'utrae' { 'multiplayer_world' }
        default { throw "server.cfg: game.world must be ehb or utrae, not '$($Cfg['game.world'])'" }
    }
}

function Start-PssGame {
    param([Parameter(Mandatory)][hashtable]$Cfg)
    $dir = $Cfg['host.game_dir']
    $argList = @(
        'nointro=true', 'fullscreen=false',
        "width=$($Cfg['host.width'])", "height=$($Cfg['host.height'])",
        'zonematch=true',
        "mpmap=$(Get-PssMapName $Cfg)",
        # read by pss_player.skrit on the host only
        'pss_host_avatar=true', "pss_max_players=$($Cfg['game.player_limit'])"
    )
    if ($Cfg['host.extra_args']) { $argList += ($Cfg['host.extra_args'] -split '\s+') }
    $p = Start-Process -FilePath (Join-Path $dir 'DungeonSiege.exe') -ArgumentList $argList `
        -WorkingDirectory $dir -PassThru
    return $p
}

function Wait-PssWindow {
    param([Parameter(Mandatory)][System.Diagnostics.Process]$Process, [int]$TimeoutSec = 60)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if ($Process.HasExited) { throw "game exited during startup (code $($Process.ExitCode))" }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne [IntPtr]::Zero) { return $Process.MainWindowHandle }
        Start-Sleep -Milliseconds 500
    }
    throw "no game window after $TimeoutSec s"
}

function Invoke-PssClick {
    param([IntPtr]$Hwnd, [int[]]$Point, [int]$DelayMs = 900)
    $r = New-Object Pss.Native+RECT
    [void][Pss.Native]::GetClientRect($Hwnd, [ref]$r)
    $p = New-Object Pss.Native+POINT
    $p.X = [int]($Point[0] * ($r.R - $r.L) / 800)
    $p.Y = [int]($Point[1] * ($r.B - $r.T) / 600)
    [void][Pss.Native]::ClientToScreen($Hwnd, [ref]$p)
    # Never click unless the game is in front: input would land elsewhere.
    if (-not [Pss.Native]::Activate($Hwnd)) { throw 'game window is not in the foreground; refusing to send input' }
    [void][Pss.Native]::SetCursorPos($p.X, $p.Y)
    Start-Sleep -Milliseconds 120
    [Pss.Native]::Click()
    Start-Sleep -Milliseconds $DelayMs
}

function Set-PssText {
    # Focus an edit box, clear it, type a new value.
    param([IntPtr]$Hwnd, [int[]]$Point, [string]$Text)
    if ($Text -notmatch '^[A-Za-z0-9 ]*$') { throw "only letters, digits and spaces can be typed: '$Text'" }
    Invoke-PssClick $Hwnd $Point 300
    if (-not [Pss.Native]::Activate($Hwnd)) { throw 'game window lost the foreground; refusing to type' }
    [Pss.Native]::Key(0x23, $false)                 # End
    for ($i = 0; $i -lt 40; $i++) { [Pss.Native]::Key(0x08, $false) }   # Backspace
    foreach ($c in $Text.ToCharArray()) { [Pss.Native]::TypeChar($c) }
    Start-Sleep -Milliseconds 300
}

function Select-PssComboItem {
    param([IntPtr]$Hwnd, [int[]]$Button, [int]$ListTop, [int]$ListX, [int]$Index)
    Invoke-PssClick $Hwnd $Button 500
    $eh = $script:Layout.ElementHeight
    Invoke-PssClick $Hwnd @($ListX, ($ListTop + $eh * $Index + [int]($eh / 2))) 500
}

function Test-PssSessionBound {
    # DirectPlay hosts listen on UDP 6073 once the session exists.
    param([int]$ProcessId)
    $ep = Get-NetUDPEndpoint -LocalPort 6073 -ErrorAction SilentlyContinue |
        Where-Object OwningProcess -eq $ProcessId
    return [bool]$ep
}

function Test-PssResponsive {
    param([System.Diagnostics.Process]$Process)
    if ($Process.HasExited) { return $false }
    $Process.Refresh()
    if ($Process.MainWindowHandle -eq [IntPtr]::Zero) { return $false }
    return (-not [Pss.Native]::IsHungAppWindow($Process.MainWindowHandle)) -and $Process.Responding
}

function Save-PssScreenshot {
    # Captures the game window itself (works while it is covered).
    param([Parameter(Mandatory)][IntPtr]$Hwnd, [Parameter(Mandatory)][string]$Path)
    Add-Type -AssemblyName System.Drawing
    $r = New-Object Pss.Native+RECT
    [void][Pss.Native]::GetWindowRect($Hwnd, [ref]$r)
    $bmp = New-Object System.Drawing.Bitmap ([Math]::Max(1, $r.R - $r.L)), ([Math]::Max(1, $r.B - $r.T))
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $dc = $g.GetHdc()
    [void][Pss.Native]::PrintWindow($Hwnd, $dc, 2)   # PW_RENDERFULLCONTENT
    $g.ReleaseHdc($dc)
    $bmp.Save($Path)
    $g.Dispose(); $bmp.Dispose()
}

function Invoke-PssHostSetup {
    <#
      Main menu -> Multiplayer -> Internet -> Host Game -> staging area,
      then character, world level, game settings, Ready, Start.
      The map comes from the mpmap= command line option. Session options
      the UI can leave in an unknown state (join in progress, pausing, new
      characters only, player limit) are enforced in-game by pss_player.skrit.
    #>
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)][hashtable]$Cfg,
        [scriptblock]$Log = { param($m) Write-Host $m }
    )
    $L = $script:Layout
    $h = Wait-PssWindow $Process
    [void][Pss.Native]::ShowWindow($h, 9)   # restore if minimised
    Start-Sleep -Seconds 8                   # front end finishes loading

    & $Log 'menu: multiplayer'
    Invoke-PssClick $h $L.MainMenu.Multiplayer 2500
    & $Log 'menu: internet (TCP/IP)'
    Invoke-PssClick $h $L.Provider.Internet 2500
    Set-PssText $h $L.InternetMenu.PlayerName $Cfg['game.host_player_name']
    & $Log 'menu: host game'
    Invoke-PssClick $h $L.InternetMenu.Host 2000
    Set-PssText $h $L.HostDialog.GameName $Cfg['game.game_name']
    if ($Cfg['game.password']) {
        Invoke-PssClick $h $L.HostDialog.PasswordCheck 400
        Set-PssText $h $L.HostDialog.Password $Cfg['game.password']
    }
    Invoke-PssClick $h $L.HostDialog.Ok 5000

    & $Log 'staging: character'
    Invoke-PssClick $h $L.Staging.SelectCharacter 2000
    if ($Cfg['pss.create_character'] -eq 'true') {
        Invoke-PssClick $h $L.SelectCharacter.Create 2000
        Set-PssText $h $L.CreateCharacter.Name $Cfg['game.host_character']
        Invoke-PssClick $h $L.CreateCharacter.Accept 3000
    }
    Invoke-PssClick $h $L.SelectCharacter.FirstRow 600
    Invoke-PssClick $h $L.SelectCharacter.Accept 2500

    if ($Cfg['game.world'] -eq 'utrae') {
        & $Log "staging: world level $($Cfg['game.world_level'])"
        $lvl = @('regular', 'veteran', 'elite').IndexOf($Cfg['game.world_level'])
        if ($lvl -lt 0) { throw "server.cfg: unknown world_level '$($Cfg['game.world_level'])'" }
        Invoke-PssClick $h $L.Staging.MapSettings 2000
        Select-PssComboItem $h $L.MapSettings.WorldCombo $L.MapSettings.WorldListTop $L.MapSettings.WorldListX $lvl
        Invoke-PssClick $h $L.MapSettings.Accept 2000
    }

    & $Log 'staging: game settings'
    $G = $L.GameSettings
    $limit = [int]$Cfg['game.player_limit']
    $diff = @('easy', 'normal', 'hard').IndexOf($Cfg['game.difficulty'])
    $drop = @('nothing', 'equipped', 'inventory', 'all').IndexOf($Cfg['game.drop_on_death'])
    if ($diff -lt 0) { throw "server.cfg: unknown difficulty '$($Cfg['game.difficulty'])'" }
    if ($drop -lt 0) { throw "server.cfg: unknown drop_on_death '$($Cfg['game.drop_on_death'])'" }
    if ($limit -lt 2 -or $limit -gt 8) { throw "server.cfg: player_limit must be 2..8" }
    Invoke-PssClick $h $L.Staging.GameSettings 2000
    Select-PssComboItem $h $G.PlayerLimitCombo $G.PlayerLimitTop $G.ListX ($limit - 2)
    Select-PssComboItem $h $G.DifficultyCombo $G.DifficultyTop $G.ListX $diff
    Select-PssComboItem $h $G.DropCombo $G.DropTop $G.ListX $drop
    Invoke-PssClick $h $G.Accept 2000

    & $Log 'staging: ready + start'
    Invoke-PssClick $h $L.Staging.Ready 1000
    Invoke-PssClick $h $L.Staging.Start 1000
}

Export-ModuleMember -Function Read-PssConfig, Get-PssMapName, Start-PssGame, Wait-PssWindow,
    Invoke-PssClick, Set-PssText, Select-PssComboItem, Test-PssSessionBound, Test-PssResponsive,
    Save-PssScreenshot, Invoke-PssHostSetup
