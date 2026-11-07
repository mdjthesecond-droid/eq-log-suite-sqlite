# EQ Log Suite

[![GitHub repo](https://img.shields.io/badge/GitHub-eq--log--suite--sqlite-blue?logo=github)](https://github.com/mdjthesecond-droid/eq-log-suite-sqlite)
[![License: GPLv2](https://img.shields.io/badge/License-GPLv2-blue.svg)](LICENSE)

EverQuest-family log parser: full combat breakdown into a local SQLite database,
browsable/filterable through a web UI (raw SQL or point-and-click), a live
in-game overlay, and user-editable alert rules. Multi-game by design -- one
small parser plugin per game feeds a common schema, so the UI never changes.

Currently supports:
- **eql** -- EverQuest Legends (classic EQ log format)
- **eq** -- live EverQuest

Licensed under GPLv2 -- see [`LICENSE`](LICENSE).

**History:** this is the SQLite-only continuation (started 2026-11-07) of
[eq-log-suite](https://github.com/mdjthesecond-droid/eq-log-suite), which ran
on MariaDB and is now archived -- see it for the full commit history behind
everything here. No database server is needed anymore, which also makes this
version much easier to share.

## Local use only

The web UI (`eq_log_suite/web/app.py`) has no authentication. It's designed
to run on `localhost` for a single player, not to be exposed on a network or
the internet. If you want to share it with others, put it behind your own
auth/reverse proxy rather than binding it publicly as-is.

## One-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

```

That's it -- no database server and no config file to edit. Start the app
(`bin/start-web.sh`); the first time, it points you at your EQ log folder
(it looks in the usual Windows and Steam/Proton install locations) -- open it
and choose "Use this folder as my log folder". `config/local.example.yaml`
shows the optional settings, if you ever want any (copy it to
`config/local.yaml`). The database is one file
(`data/eqlogs.sqlite3` by default, gitignored), created with all its tables
the first time anything opens it. To back it up, stop the app and copy
that file.

**Recording is manual: you choose the log.** Start the app (before or after
the game -- either works) and select your log in the picker on
`/encounters`. Selecting a log is the one thing that records anything:

- it's read into the database -- the whole file the first time, only
  what's new after that, and never a line that's already recorded, even
  from a renamed or archived copy (`eq_log_suite/coverage.py`);
- it's loaded into the Live view over the time window you picked
  (now / 12h / 24h / 3 days / 1 week / all time);
- the app follows it, recording new lines as the game writes them.

The app starts empty every launch and waits for a selection (the last log
is pre-selected in the dropdown). It never scans folders on its own. The
log itself doesn't say whose it is, so the picker shows the character and
server guessed from the filename -- correct them for a renamed copy.

The code's SQL is written in MariaDB's dialect (this project's original
database) and translated as it runs -- see `eq_log_suite/sqlite_compat.py`.
That includes what you type into `/query`, so either dialect works there.
`.venv/bin/python tests/test_sqlite_compat.py` checks the translation.

## Everyday use -- launching manually or via desktop launchers

The simplest path is the shell scripts in `bin/` (`start-web.sh`,
`start-overlay.sh`, `start-mangohud-alerts.sh`, `stop-all.sh`). All are safe
to re-run -- they check what's already running before starting anything
new. Logs from each go to `logs/web.log`, `logs/overlay.log`,
`logs/mangohud_alerts.log`.

- **App window** (`start-app.sh`) -- the app in its own window instead of
  a browser tab (pywebview: Qt WebEngine on Linux, Edge WebView2 on
  Windows). Pages that open a new tab open another app window. Closing the
  last window stops the app. If it's already running, this just opens a
  window onto it.
- **App** (`start-web.sh`) -- starts the app (if not already running) at
  http://localhost:8000 and opens it in your browser. This is the normal one to run when you
  play: select your log on `/encounters` and it records and follows it --
  the tailer runs inside the app, there's nothing separate to start.
- **Overlay** (`start-overlay.sh`) -- starts the app plus the in-game
  overlay window. On Linux with a compositor like KWin, a focused
  fullscreen game typically gets its own stacking layer above ordinary
  "always on top" windows, so a plain GTK/Qt overlay window like this one
  only reliably renders when the game isn't fullscreen-focused (menus,
  loading screens, or true windowed mode). That's a compositor-level
  limitation, not something fixable from app settings like
  `WindowsBlockCompositing` -- the real fix (below) is graphics-API-level
  overlay injection, same approach as MangoHud/Steam's own overlay.
- **Stop** (`stop-all.sh`) -- stops the app (and with it the tailer), the
  overlay, and the MangoHud alert writer, whichever of them are running.

If you'd like desktop-launcher entries (e.g. KDE's application menu) instead
of running scripts from a terminal, create `.desktop` files in
`~/.local/share/applications/` that point at the scripts in this project's
`bin/` directory by absolute path. They'll keep working as long as the
project stays at that path.

## Alerts over fullscreen (MangoHud)

The GTK overlay above doesn't reliably render over a focused fullscreen
game on Linux (see its note above); MangoHud does, because it draws inside
the game's own Vulkan/GL swapchain rather than being a window the
compositor has to stack. Confirmed working through Proton via a Steam
non-Steam-game shortcut. (Windows users: the GTK overlay
and this MangoHud path are Linux-specific; the parser, database, and web UI
underneath are all cross-platform.)

Setup (one-time):

1. Install [MangoHud](https://github.com/flightlessmango/MangoHud) if it's
   not already on your system (Bazzite and some other distros ship it
   preinstalled).
2. Right-click the game's shortcut in Steam -> Properties -> Launch Options:
   ```
   MANGOHUD_CONFIGFILE=$HOME/.config/MangoHud/eq_log_suite.conf mangohud %command%
   ```
   Using a dedicated config file (not `~/.config/MangoHud/MangoHud.conf`)
   keeps this from touching whatever MangoHud settings you use for other
   games.
3. Create that config file with two `exec`/`custom_text` pairs -- one for a
   live DPS readout, one for alerts. Replace `<path-to-eq-log-suite>` with
   this project's actual location on disk:
   ```
   position=top-left
   exec=cat <path-to-eq-log-suite>/logs/mangohud_dps.txt
   custom_text=DPS
   exec=cat <path-to-eq-log-suite>/logs/mangohud_alert.txt
   custom_text=Alert
   ```
   `exec`'s output is the value shown; `custom_text` is just its label --
   the two must be paired like this, `custom_text` alone doesn't render
   anything (learned the hard way -- a bare `custom_text` with no `exec`
   next to it never appears on screen, regardless of position or preset).
   `custom_text_center` was tried for the alert slot (hoping for a
   screen-centered alert, separate from the DPS line) but it doesn't pair
   with a preceding `exec` the way plain `custom_text` does -- it only ever
   renders its own literal configured string, centered and disconnected
   from any exec output next to it. So both DPS and Alert use plain
   `custom_text` and sit in the same row for now.

   MangoHud polls each `exec` command on its own and re-renders on change,
   no reload keypress needed -- confirmed by editing the file live and
   watching it update in-game. Also confirmed safe to add MangoHud's own
   layout/color/font options (`horizontal`, `table_columns`, `*_color`,
   `font_size`, etc., e.g. via the MangoJuice GUI) into this same file
   alongside the two `exec`/`custom_text` pairs above -- just watch out
   that GUI config tools tend to rewrite the *entire* file when you save
   from them, silently dropping the `exec=` lines since they're not exposed
   in the GUI. Re-add the two pairs above after any GUI-driven edit.

Everyday use: `bin/start-mangohud-alerts.sh` instead of the regular Overlay
launcher. It starts the app (if not already running -- select your log in it) plus
`eq_log_suite/overlay/mangohud_writer.py`, which listens on the same
alert-broadcast socket the GTK overlay uses and writes:
- the live DPS/hit%/crit% snapshot to `logs/mangohud_dps.txt`, refreshed
  every 0.5s while active and frozen (not cleared) once combat pauses, same
  behavior as the GTK overlay's own DPS meter;
- whichever alert most recently fired to `logs/mangohud_alert.txt`,
  clearing it again once that alert's configured duration has passed. This
  includes a built-in in-combat/out-of-combat alert (`tailer.py`'s
  `CombatTracker`) showing how many distinct mob *names* you're currently
  trading hits with -- note "names," not instances: the log format has no
  per-mob ID, so two simultaneously-engaged mobs sharing an identical
  generic name (e.g. two "a lowland viper") collapse into one count.

Known limitation: MangoHud only gives you a couple of fixed exec/custom_text
slots, not a real queue -- so unlike the GTK overlay's stacked,
per-message-colored annotations, this shows one line per slot: whatever
fired most recently. Fine for "something just happened," not a substitute
for the richer overlay if you're ever in windowed mode.

## New log files (new character, new server, a fresh log)

Nothing automatic: when you play a different character or server, or start
a fresh log, select that log in the picker. The dropdown lists the `eqlog_*`
files in the folders under `log_roots` (for the games in `enabled_games`),
rotated copies included; Browse opens any `.txt` from anywhere else.

EQ reuses one filename per character + server, so when an old log is moved
away and a new one starts under the same name, the app tells them apart by
their first line and reads the new one from the start -- nothing skipped,
nothing recorded twice.

## What's kept: knowledge and history -- not combat

The log files are the permanent record. The database keeps only what's
been chosen to keep from them (`eq_log_suite/facts.py`), built from every
line that gets recorded (never just viewed). Combat lines and raw lines
aren't stored at all: the Live view (`/live`, `/encounters`, Browse Events)
re-reads them from the selected log.

**Knowledge, per mob x zone x tier:**

- **Kills** -- every death seen, and **your kills**: the killing blow was
  yours, *or* you traded hits with it in the minute before it died, *or*
  you looted its corpse, *or* a group member killed it while you were
  grouped. Encounter locking makes all of those your kill -- you see the
  loot -- so they're what `/loot`'s drop chance is measured against.
- **Loot** -- drops and quantity per item; **ground spawns**; what
  **vendors** sell.
- **Level range and rare flag**, from cons; **HP only for rare mobs** (one
  is up at a time, so the damage it took until it died is its HP; reads low
  if others' damage is missing from your log). A mob counts as rare once a
  con has said so.
- **Attacks and spells** -- each melee attack with its max hit, each
  skill/spell it cast or landed with its max hit / max heal.
- **Where it's been seen**, and NPCs you only talked to or traded with.

**History, per character:**

- **Zone visits** (`/zones`) -- each visit summarized: when you left, your
  damage, DPS, hit and crit rate, and kills there.
- **Dialogue, quests, tasks** -- every hail, say, NPC line, reward,
  turn-in, quest and task line, plus kills, loot, zone changes and group
  changes (what those pages and "your kills" need).

After the parser learns new lines or a new fact is added, rebuild
everything from the recorded log files (stop the app first; it backs the
database up to `data/backups/` before starting, and refuses if a recorded
log has been moved or deleted):

```bash
.venv/bin/python -m eq_log_suite.facts
```

## Packaging the app (no Python needed to run it)

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --noconfirm packaging/eq-log-suite.spec
```

That makes `dist/EQ Log Suite/` -- one folder to zip up and share; run
`EQ Log Suite` (`EQ Log Suite.exe` on Windows) inside it. It's the app
window (`start-app.sh`) with Python and everything else bundled: ~590 MB on
Linux, mostly Qt WebEngine (Windows uses the built-in Edge WebView2
instead, so it's much smaller). Build on the system you're building for --
a Windows build has to be made on Windows.

A packaged app keeps its config and database in a per-user folder, not
next to itself (so updating is just replacing the folder):
`%LOCALAPPDATA%\EQ Log Suite` on Windows, `~/.local/share/eq-log-suite` on
Linux. To check a build actually works -- the page renders in the window
and talks to the app -- run it with `EQLS_SMOKE_TEST=1`; it prints
`SMOKE TEST: loaded=True ...` and exits.

The in-game overlays (MangoHud alerts, the Vulkan layer and its companion)
aren't part of the packaged app; they're Linux-only and run from source.

## Extending to a new game

1. Get a real log sample (enable in-game logging, play a bit).
2. Add `eq_log_suite/parsers/<game>.py`: a `GameParser` subclass with
   `game_code`, optionally a custom `TIMESTAMP_RE`/`TIMESTAMP_FMT`, and
   `PATTERNS` -- an ordered list of `(regex, handler)` pairs. Look at
   `eq_legends.py` and `eq.py` for the pattern: scan the real log for
   distinct line shapes, write a regex + handler per shape, and don't worry
   about 100% coverage -- an unmatched line is just skipped (it's still in the
   log file, so a rebuild picks it up once a pattern exists).
3. Register it in `eq_log_suite/parsers/registry.py`.
4. Everything else (schema, importer, tailer, web UI, overlay, alerts) works
   unchanged.
