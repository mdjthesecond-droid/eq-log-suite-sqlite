"""Database actions -- the /database page: Add files, Rebuild, Archive.

* Add files: record log files into the database without opening them in the
  Live view (rotated .txt.<date> copies, a friend's raid log, old logs).
  Anything already recorded is skipped (coverage.py), whatever the file is
  called now.
* Rebuild: re-read chosen log files with the current parser and replace all
  facts and history with what they give. Built off to the side -- a copy of
  the database, rebuilt in place of the original only once it's finished --
  so a failure (or a missing file) leaves everything as it was. Recording
  pauses meanwhile and catches up after; the Live view keeps working (it
  reads the log itself). Keeps what you entered (alert rules, NPC/zone notes
  and overrides, rotation settings, log folders and picker) unless "reset
  settings too".
* Archive: a full copy of the database, with an archive_info table inside
  saying what it is (when, note, source logs, date range, characters, row
  counts), listed in data/archives.json -- outside any one database. Made
  on its own or as the first step of a rebuild.

One action at a time, in a background thread; the page polls status().
While one runs, the picker can't select a log (it would record into the
database being replaced -- live_window.select checks rebuilding()).
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from eq_log_suite import db

# What you entered yourself -- kept by a rebuild unless settings are reset.
USER_TABLES = ("alert_rules", "alert_log", "npc_info", "zone_info", "zone_start_overrides",
               "rotation_settings", "app_settings")

_lock = threading.Lock()
_job: dict = {"running": False}


def busy() -> bool:
    return bool(_job.get("running"))


def rebuilding() -> bool:
    return busy() and _job.get("kind") == "rebuild"


def status() -> dict:
    return dict(_job, log=list(_job.get("log", [])))


def _step(text: str):
    _job["step"] = text
    _job.setdefault("log", []).append(f"{datetime.now():%H:%M:%S}  {text}")


def start(kind: str, **kwargs):
    """Runs one action (add / rebuild / archive) in the background. Raises
    RuntimeError if one is already running."""
    with _lock:
        if _job.get("running"):
            raise RuntimeError(f"Busy: {_job.get('kind')} is still running.")
        _job.clear()
        _job.update(running=True, kind=kind, started=datetime.now().isoformat(timespec="seconds"),
                    log=[], error=None, result=None)
    fn = {"add": add_files, "rebuild": rebuild, "archive": archive}[kind]

    def run():
        try:
            _job["result"] = fn(**kwargs)
            _step("Done.")
        except Exception as e:   # shown on the page
            _job["error"] = f"{type(e).__name__}: {e}"
            _step(f"Failed: {e}")
        finally:
            _job["running"] = False
            _job["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=run, name=f"eqls-{kind}", daemon=True).start()


# -- add files ---------------------------------------------------------------

def add_files(items: list[dict]):
    """items: {"path", "game"?, "character"?, "server"?} -- identity guessed
    from the file name when not given. The log the tailer is following is
    skipped: it's already being recorded."""
    from eq_log_suite import tailer_service
    from eq_log_suite.importer import import_file
    svc = tailer_service.current()
    following = svc.following if svc else None
    results = []
    for n, item in enumerate(items, 1):
        path = item["path"]
        name = os.path.basename(path)
        if following and os.path.realpath(path) == os.path.realpath(following):
            _step(f"Skipped {name} -- it's the log being recorded live.")
            continue
        if not os.path.isfile(path):
            _step(f"Skipped {name} -- not found.")
            continue
        _step(f"Reading {name} ({n} of {len(items)})")
        r = import_file(path, item.get("game") or None, item.get("character") or None, item.get("server") or None)
        _step(f"{name}: {r['lines'] - r['skipped']:,} lines recorded"
              + (f", {r['skipped']:,} already recorded" if r["skipped"] else ""))
        results.append(r)
    return results


# -- archive -----------------------------------------------------------------

def archive_dir() -> Path:
    return db.main_path().parent / "archive"


def _index_path() -> Path:
    return db.main_path().parent / "archives.json"


def list_archives() -> list[dict]:
    """The app-level list of archives (newest first), each with its own
    archive_info read back from the file (missing: it was moved/deleted)."""
    try:
        entries = json.loads(_index_path().read_text())
    except (OSError, ValueError):
        entries = []
    out = []
    for e in entries:
        info = {}
        if os.path.isfile(e["file"]):
            try:
                c = sqlite3.connect(f"file:{e['file']}?mode=ro", uri=True)
                info = {k: json.loads(v) for k, v in c.execute("SELECT key, value FROM archive_info")}
                c.close()
            except sqlite3.Error:
                pass
        out.append({**e, "exists": os.path.isfile(e["file"]),
                    "size_mb": round(os.path.getsize(e["file"]) / 1e6, 1) if os.path.isfile(e["file"]) else None,
                    "info": info})
    return sorted(out, key=lambda e: e.get("made_at", ""), reverse=True)


def _describe(conn) -> dict:
    """What a database holds -- written into an archive as archive_info."""
    def one(sql):
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    counts = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                             "AND name != 'archive_info'"):
        counts[t] = one(f'SELECT COUNT(*) FROM "{t}"')
    return {
        "source_logs": [r[0] for r in conn.execute(
            "SELECT file_path FROM log_sources WHERE id IN (SELECT log_source_id FROM recorded_ranges) ORDER BY file_path")],
        "first_line": one("SELECT MIN(start_ts) FROM recorded_ranges"),
        "last_line": one("SELECT MAX(end_ts) FROM recorded_ranges"),
        "characters": [f"{n} ({s})" if s else n for n, s in conn.execute(
            "SELECT c.name, c.server FROM characters c WHERE c.id IN (SELECT character_id FROM recorded_ranges) "
            "ORDER BY c.name")],
        "row_counts": counts,
    }


def archive(name: str = "", note: str = "", folder: str = ""):
    """Copies the main database to <folder or data/archive>/<name>.sqlite3
    with an archive_info table, and lists it in data/archives.json."""
    made = datetime.now()
    safe = re.sub(r"[^\w .-]+", "_", name).strip(" .") or f"eqlogs-{made:%Y%m%d-%H%M%S}"
    folder = Path(folder).expanduser() if folder else archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{safe}.sqlite3"
    n = 2
    while dest.exists():
        dest = folder / f"{safe} ({n}).sqlite3"
        n += 1
    _step(f"Archiving the database to {dest}")
    src = sqlite3.connect(str(db.main_path()))
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
        info = {"name": safe, "made_at": made.isoformat(timespec="seconds"), "note": note,
                "from": str(db.main_path()), **_describe(out)}
        out.execute("CREATE TABLE archive_info (key TEXT PRIMARY KEY, value TEXT)")
        out.executemany("INSERT INTO archive_info VALUES (?, ?)", [(k, json.dumps(v)) for k, v in info.items()])
        out.commit()
    finally:
        out.close()
        src.close()
    try:
        entries = json.loads(_index_path().read_text())
    except (OSError, ValueError):
        entries = []
    entry = {"file": str(dest), "name": safe, "note": note, "made_at": info["made_at"]}
    entries.append(entry)
    _index_path().write_text(json.dumps(entries, indent=1))
    _step(f"Archived: {dest.name} ({round(dest.stat().st_size / 1e6, 1)} MB)")
    return entry


# -- rebuild -----------------------------------------------------------------

def rebuild(logs: list, archive_first: dict | None = None, reset_settings: bool = False):
    """logs: paths or {"path", "game"?, "name"?, "server"?} to rebuild from.
    The log being recorded live is always included (leaving it out would
    just have the tailer record all of it again on resume)."""
    from eq_log_suite import facts, tailer_service
    main = db.main_path()
    svc = tailer_service.current()
    following = svc.following if svc else None
    paths = {(l["path"] if isinstance(l, dict) else l) for l in logs}
    if following and following not in paths:
        logs = list(logs) + [following]
        _step(f"Including {os.path.basename(following)} -- it's the log being recorded live.")

    missing = [p for p in ((l["path"] if isinstance(l, dict) else l) for l in logs) if not os.path.isfile(p)]
    if missing:
        raise ValueError("these logs no longer exist (move them back first): " + ", ".join(missing))

    temp = main.with_name("rebuild-in-progress.sqlite3")
    for p in (temp, temp.with_name(temp.name + "-wal"), temp.with_name(temp.name + "-shm")):
        p.unlink(missing_ok=True)

    paused = svc.pause() if svc else None
    if paused:
        _step("Recording paused (the Live view keeps working).")
    t0 = time.monotonic()
    try:
        _step("Copying the database to rebuild it on the side")
        src = sqlite3.connect(str(main))
        out = sqlite3.connect(str(temp))
        src.backup(out)
        out.close()
        src.close()

        with db.using_database(temp):
            conn = db.get_connection()
            try:
                if reset_settings:
                    _step("Resetting settings (alert rules, notes, overrides, folders)")
                    with conn.cursor() as cur:
                        for t in USER_TABLES:
                            cur.execute(f"DELETE FROM {t}")
                    conn.commit()
                results = facts.rebuild(conn, verbose=False, logs=logs, backup_first=False, progress=_step)
            finally:
                conn.close()

        if archive_first is not None:
            archive(**archive_first)

        _step("Replacing the database with the rebuilt one")
        src = sqlite3.connect(str(temp))
        out = sqlite3.connect(str(main), timeout=30)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        for p in (temp, temp.with_name(temp.name + "-wal"), temp.with_name(temp.name + "-shm")):
            p.unlink(missing_ok=True)
        _step(f"Rebuilt from {len(results)} log(s) in {time.monotonic() - t0:.1f}s")
        return {"logs": [r["path"] for r in results], "lines": sum(r["lines"] for r in results)}
    finally:
        if paused and os.path.isfile(paused):
            try:
                svc.follow(paused)
                _step("Recording resumed -- catching up on anything written meanwhile.")
            except Exception as e:
                _step(f"Couldn't resume recording ({e}) -- reselect the log in the picker.")
