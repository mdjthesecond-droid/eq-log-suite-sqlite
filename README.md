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

The simplest path is the shell scripts in `bin/` (`start-app.sh`,
`start-web.sh`, `start-overlay-native.sh`, `start-overlay.sh`,
`stop-all.sh`). All are safe to re-run -- they check what's already running
before starting anything new. Logs go to `logs/` (`app.log`, `web.log`,
`overlay_native_companion.log`, `overlay.log`).

- **App window** (`start-app.sh`) -- the app in its own window instead of
  a browser tab (pywebview: Qt WebEngine on Linux, Edge WebView2 on
  Windows). Pages that open a new tab open another app window. Closing the
  last window stops the app. If it's already running, this just opens a
  window onto it.
- **App** (`start-web.sh`) -- starts the app (if not already running) at
  http://localhost:8000 and opens it in your browser. This is the normal one to run when you
  play: select your log on `/encounters` and it records and follows it --
  the tailer runs inside the app, there's nothing separate to start.
- **In-game overlay** (`start-overlay-native.sh`) -- starts the app (if it
  isn't running) plus the companion window for positioning the Vulkan
  overlay's boxes -- see below.
- **Overlay window** (`start-overlay.sh`) -- starts the app plus a plain
  GTK overlay window. On Linux with a compositor like KWin, a focused
  fullscreen game typically gets its own stacking layer above ordinary
  "always on top" windows, so a plain GTK/Qt overlay window like this one
  only reliably renders when the game isn't fullscreen-focused (menus,
  loading screens, or true windowed mode). That's a compositor-level
  limitation, not something fixable from app settings like
  `WindowsBlockCompositing` -- the real fix (below) is drawing inside the
  game itself, as the Vulkan overlay does.
- **Stop** (`stop-all.sh`) -- stops the app (and with it the tailer) and
  the overlays, whichever of them are running.

If you'd like desktop-launcher entries (e.g. KDE's application menu) instead
of running scripts from a terminal, create `.desktop` files in
`~/.local/share/applications/` that point at the scripts in this project's
`bin/` directory by absolute path. They'll keep working as long as the
project stays at that path.

## The in-game overlay (Vulkan, Linux)

`overlay-native/` is a Vulkan layer that draws boxes inside the game's own
frames -- so it shows over a fullscreen game, which an ordinary window
can't. It connects to the app's alert socket (`/tmp/eq_log_suite_overlay.sock`)
by itself and reconnects whenever the app restarts, so there's nothing to
start for it beyond the app. It draws:

- a **DPS** box -- your live DPS, hit % and crit % while you're fighting;
- **alert boxes** -- whatever your alert rules on `/alerts` send to the
  overlay (none until you add rules; they take effect within 15 seconds).

Setup (one-time): build and install the layer with `overlay-native/install.sh`
(in the `eq-overlay-dev` distrobox -- and never while the game is running:
it replaces a library the game has loaded), then add `EQLOG_OVERLAY_ENABLE=1`
to the game's Steam launch options (`EQLOG_OVERLAY_ENABLE=1 %command%`); the
layer stays off for every other Vulkan program. To move the boxes, run
`bin/start-overlay-native.sh` for the companion window.

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

### Add files, Rebuild, Archive

The **Add / Rebuild / Archive** page (`/database`, in the nav) changes what
the database holds:

- **Add files** -- record log files without opening them in the Live view:
  rotated `.txt.<date>` copies, old or archived logs, a friend's raid log.
  Pick a folder, tick files (character/server editable for ones not
  recorded yet). Anything already recorded is skipped, whatever the file is
  called now.
- **Rebuild** -- re-read the ticked logs with the current parser and replace
  all knowledge and history with what they give: after a parser fix or a new
  fact, or to drop logs you don't want counted. It's built on a copy off to
  the side and swapped in only when finished, so a failure or a missing file
  changes nothing. Recording pauses and catches up after (nothing written
  meanwhile is lost); the Live view keeps working. Your alert rules, NPC/zone
  notes and overrides, rotation settings, log folders and picker are kept
  unless you tick "reset settings too". "Archive first" is on by default.
- **Archive** -- a full copy of the database, with a note and a summary of
  what's in it (logs, date range, characters, row counts) inside it, saved
  to `data/archive/` (or a folder you choose) and listed on the page.

The same rebuild from the command line (app stopped; backs up to
`data/backups/` first; every recorded log, and refuses if one has been moved
or deleted):

```bash
.venv/bin/python -m eq_log_suite.facts
```

## Windows

The app runs on Windows as a normal program -- same pages, same recording,
in its own window (Edge WebView2, built into Windows 10 and 11). What
Windows doesn't get: the in-game overlay (the Vulkan layer is Linux-only)
and desktop-notification alerts (sound alerts work, with `.wav` files).

### Using it (no Python needed)

1. Unzip `EQ-Log-Suite-windows-*.zip` anywhere (e.g. your Documents) and
   run `EQ Log Suite.exe` inside the `EQ Log Suite` folder. It isn't signed,
   so the first time Windows SmartScreen says "Windows protected your PC":
   click **More info**, then **Run anyway**.
2. In game, turn logging on: `/log on`.
3. The app opens on Live. The first time, it suggests your EQ `Logs`
   folder (it looks under `C:\Users\Public\Daybreak Game Company\Installed Games`
   and Program Files) -- open it, choose **Use this folder as my log folder**,
   then pick your character's log in the picker. Start the app before or
   after the game, either works; closing its window stops recording.

Your database and settings live in `%LOCALAPPDATA%\EQ Log Suite` (paste
that into Explorer's address bar), not in the app folder -- so to update,
delete the old app folder and unzip the new one; nothing is lost. To back
up, close the app and copy `data\eqlogs.sqlite3` from there. If something
goes wrong, `logs\app.log` in the same place has the details.

Log rotation (on the Database page) renames the game's log file, which
Windows won't allow while the game has it open -- the app just keeps
following the file and tries again every 10 minutes.

### Building the Windows app

On Windows, with [Python](https://www.python.org/downloads/windows/) 3.11
or newer (the installer's defaults are fine) and the project cloned or
unzipped:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```

It makes `.venv`, installs the requirements and PyInstaller, builds
`dist\EQ Log Suite\`, opens the app once to check it works (a window
flashes up and closes -- the smoke test below), and zips the folder as
`dist\EQ-Log-Suite-windows-<version>.zip` to share. `-SkipSmokeTest`
skips the check; `-Python 3.13` picks a Python version.

To run from source instead (no build), after the script has made `.venv`:
`.venv\Scripts\python -m eq_log_suite.desktop`. The `bin\*.sh` scripts
are Linux-only.

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
a Windows build has to be made on Windows (`packaging\build-windows.ps1`,
above).

A packaged app keeps its config and database in a per-user folder, not
next to itself (so updating is just replacing the folder):
`%LOCALAPPDATA%\EQ Log Suite` on Windows, `~/.local/share/eq-log-suite` on
Linux. To check a build actually works -- the page renders in the window
and talks to the app -- run it with `EQLS_SMOKE_TEST=1`; it prints
`SMOKE TEST: loaded=True ...` and exits (on Windows the exe has no console,
so that line goes to `logs\app.log` in the per-user folder).

The in-game overlay (the Vulkan layer and its companion) isn't part of the
packaged app; it's Linux-only and runs from source.

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
