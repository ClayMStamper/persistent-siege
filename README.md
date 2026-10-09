# Persistent Siege Server

An always-on, respawning Dungeon Siege (2002) world host, built on the stock
game executable. Implements *Persistent Siege Server – Architecture Brief, Rev 1*.

```
PersistentSiege/
  Resources/pss_content_v1.dsres     content tank (server and every client)
  Resources/build_report.json        what the content build converted / kept
  client/install-client.ps1          install the tank (optional: lobby address)
  client/multiplayer.ini.snippet     lobby lines for DungeonSiege.ini
  server/build-server.ps1            licensed install -> ready-to-run server
  server/supervisor/                 PssSupervisor.ps1, PssHost.psm1, ui_layout.psd1,
                                     install-supervisor.ps1, server.cfg.template
  content/                           Skrit components, templates, pss_content.json
  tools/                             tank.py (.dsres reader/writer), gas.py, build_content.py
```

## Players

```
powershell -ExecutionPolicy Bypass -File client\install-client.ps1
```

Copies the tank into the game's `Resources` folder (and removes other
versions; the server and clients must run the same one). Join by IP
(Multiplayer → Internet → enter the server address), or pass
`-LobbyAddress <host>` to use an open-zone lobby. `-Uninstall` removes it.

## Server

On a Windows VM with a desktop session, from an elevated PowerShell:

```
powershell -ExecutionPolicy Bypass -File server\build-server.ps1 -GameDir "<Dungeon Siege 1 install>" -OutDir C:\PersistentSiege -World utrae
```

This verifies the install (DungeonSiege.exe 1.11.1), copies it, installs the
tank, writes `DungeonSiege.ini` (windowed 800×600) and
`PersistentSiege\supervisor\server.cfg`, enables DirectPlay, opens UDP 6073
and 2302–2400, and registers the supervisor. Reboot if it says DirectPlay was
just enabled. Configure the server account to **log on automatically**
(e.g. Sysinternals Autologon): the supervisor runs in that desktop session.

Edit `server.cfg` for world level, difficulty, drop-on-death, game name,
password and the daily restart time. `PssSupervisor.ps1 -Once` hosts once
and exits, which is handy when checking a new setup.

With `-Lobby -LobbySource <open-zone checkout>` the script also builds the
lobby. open-zone publishes no license, so it is **not** included here;
redistributing it needs its author's permission.

## How it works

**Content tank** (`tools/build_content.py`, run against the retail tanks):

| | |
|---|---|
| Monster respawn | Each static monster in both worlds becomes a `pss_monster_generator` with the same scid and placement. It spawns the monster, respawns it `monster_respawn_delay` s after it dies (300 s), keeps one alive, and walks it back past `leash_range` (30 m). Pending respawns survive the region streaming out (scid bits). |
| Quest-item carriers | Instance overrides (`[inventory]`, `[aspect]`, `[mind]`…) are baked into a derived template, so respawns carry their items again. |
| Bosses | `*boss*` templates, the configured list, or ≥3000 life: one-hour timer, and only while no player is within 30 m. |
| Scripted encounters | Monsters referenced by scid anywhere in the map (triggers, commands, quests, conversations) or with instance triggers stay static: one-shot until the next scheduled restart. |
| Player respawn | `[pss_player]` on the four playable heroes: 10 s after death the hero revives at the last start location it reached (a `pss_checkpoint` placed at every start group), with half life. Party resurrection and the stock "Respawn as Ghost" take precedence. |
| Host avatar | With `pss_host_avatar=true` on the command line (supervisor only), the host's hero is made invisible, invincible, unselectable and non-colliding, and the session is forced to join-in-progress on, pausing off, saved characters allowed, player limit from `server.cfg`. |
| Scaling | `formulas.gas` `[mp_scaling]` shifted one step so the parked host does not count as a player. |

Current build: Ehb 6,252 monsters + 38 bosses converted, 442 kept static;
Utrae 27,552 + 222, 144 kept static; 18 checkpoints. Tuning lives in
`content/pss_content.json`; rebuild with
`python tools/build_content.py --game "<install>"` and bump `version` when
shipping a change (the tank name carries it).

**Supervisor**: launches `DungeonSiege.exe nointro=true fullscreen=false
width=800 height=600 zonematch=true mpmap=<world> pss_host_avatar=true`,
clicks through Multiplayer → Internet → Host Game → character → world level
→ game settings → Ready → Start, using the control rectangles from the game's
own UI files (`ui_layout.psd1`). Health checks every 30 s: process alive,
window responsive, UDP 6073 bound, and window in front. Three failures, or the
daily restart (05:00), restart the host. Logs rotate in `supervisor\logs`;
failed setups leave a screenshot there.

## Changes from the brief

* **800×600, not 640×480.** The UI files are authored at 800×600, so clicks map 1:1.
* **Logon task, not a Windows service.** Services run in session 0 with no
  desktop, where the game cannot render or be driven.
* **PowerShell input driver, not AutoHotkey.** It's one language with the supervisor,
  and there's nothing extra to install. Clicks are refused unless the game window is in front.
* **Host stays where it spawned** instead of a sealed holding area. A sealed area
  needs new terrain, which takes the Siege Editor. Invisible, invincible,
  unselectable and non-colliding in a town gives the same effect.
* **Respawn point is the last start location reached**, which on Ehb's linear
  route is the nearest one.
* Map selection uses the engine's `mpmap=` option, not the map list.

## Verified so far

* `tank.py` reads every retail tank with 0 CRC mismatches (3,417 files) and
  writes tanks the engine accepts (chunk layout and CRCs match retail).
* The game loads to the main menu with the tank installed, and the Kingdom of
  Ehb loads with the converted regions, checkpoints and patched heroes in place.
* All PowerShell parses; the module loads and reads `server.cfg`.

## Not yet verified (needs a session on the server VM)

* The menu click sequence end to end. Watch for combo-list item order (player limit,
  difficulty, drop on death), and the host character creation on first run.
* That `mpmap=` preselects the map in the staging area.
* Skrit behaviour in play: monster respawn and leash timing, boss gating,
  checkpoint reach, player revive and teleport, and the host-avatar parking and
  option enforcement. Retail Dungeon Siege reports Skrit errors only to its
  internal log.
* DirectPlay hosting and joining across machines, and the lobby.
* Dungeon Siege pauses whenever its window is inactive. The supervisor
  reactivates it, so keep the server desktop otherwise unused.
