"""The Live view's working data: one log file, over one time window, parsed
into a disposable SQLite file (data/live.sqlite3) next to the permanent
database.

Nothing here is permanent. Picking a log + window throws the previous
contents away and re-reads that slice of the log file with the current
parser -- so a parser fix applies to the next review immediately, with no
backfill. The log files themselves are the permanent record.

    select(path, window)   # what the picker calls
    connection()           # what Live pages query through

connection() opens the live file with the permanent database attached as
`kb`. SQLite resolves an unqualified table name against main first, then
attached databases in order -- so `events` means the live window's events
and `games`/`characters` come from the permanent database, and the existing
encounter queries run unchanged.

Every connection() also catches up on whatever's been appended to the log
since the last one (cheap: a stat call when nothing's new), so a Live page
reflects the game as of the moment it loads, independent of the tailer. If
the file got smaller or was replaced by a different file (e.g. old logs
moved to an archive and a fresh one started), the window is reloaded from
scratch instead of reading from a stale byte position.

Windows are counted back from the current time. "now" means "from the
moment it was picked". The app starts empty every time: live data is only
ever the log explicitly selected in this run (selecting it is also what
records it into the database -- see select()).
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from eq_log_suite import db, ingest, sqlite_compat
from eq_log_suite.importer import GAME_NAMES, guess_character_from_filename, guess_server_from_filename
from eq_log_suite.parsers.registry import detect_parser, get_parser

WINDOWS = {
    "now": "From now on",
    "12h": "Last 12 hours",
    "24h": "Last 24 hours",
    "3d": "Last 3 days",
    "1w": "Last week",
    "all": "All time",
}
DEFAULT_WINDOW = "24h"
_DELTAS = {"12h": timedelta(hours=12), "24h": timedelta(hours=24), "3d": timedelta(days=3), "1w": timedelta(weeks=1)}

INSERT_BATCH = 2000
RECENT_LIMIT = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    log_source_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT COLLATE NOCASE NOT NULL,
    source_name TEXT COLLATE NOCASE NULL,
    source_type TEXT COLLATE NOCASE NULL,
    target_name TEXT COLLATE NOCASE NULL,
    target_type TEXT COLLATE NOCASE NULL,
    verb TEXT COLLATE NOCASE NULL,
    amount INTEGER NULL,
    outcome TEXT COLLATE NOCASE NULL,
    extra TEXT NULL,
    raw_line TEXT COLLATE NOCASE NOT NULL,
    line_no INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_char_ts ON events (character_id, ts);
CREATE INDEX IF NOT EXISTS idx_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_event_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_source_name ON events (source_name);
CREATE INDEX IF NOT EXISTS idx_target_name ON events (target_name);
CREATE TABLE IF NOT EXISTS live_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# One loader at a time per process: a page load's catch-up and a picker
# change must not interleave their inserts.
_lock = threading.RLock()
_started = False


def live_path() -> Path:
    return db.sqlite_path().with_name("live.sqlite3")


def _raw():
    path = live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")  # disposable: rebuilt from the log if ever lost
    conn.executescript(_SCHEMA)
    return conn


def _meta(raw) -> dict:
    return {k: json.loads(v) for k, v in raw.execute("SELECT key, value FROM live_meta")}


def _set_meta(raw, **values):
    raw.executemany(
        "INSERT INTO live_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(k, json.dumps(v)) for k, v in values.items()],
    )


# --------------------------------------------------------------------------
# reading the log file


def _file_id(path) -> list:
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def _first_line_at(f, p: int, size: int, parse_ts):
    """Start offset and timestamp of the first timestamped line beginning at
    or after byte p, or (size, None) if there isn't one."""
    if p <= 0:
        f.seek(0)
    else:
        f.seek(p - 1)
        f.readline()  # to just past the next newline at or after p-1 (p itself, if p starts a line)
    while True:
        start = f.tell()
        if start >= size:
            return size, None
        line = f.readline()
        if not line.endswith(b"\n"):
            return size, None  # partial last line, still being written
        parsed = parse_ts(line.decode("utf-8", "replace").rstrip("\r\n"))
        if parsed:
            return start, parsed[0]


def find_offset(f, size: int, cutoff: datetime, parse_ts) -> int:
    """Byte offset of the first line timestamped at or after `cutoff`,
    found by binary search -- logs are written in time order, so a 24h
    window into a 300MB log reads a few dozen lines to find its start
    instead of parsing the whole file."""
    lo, hi = 0, size
    while lo < hi:
        mid = (lo + hi) // 2
        start, ts = _first_line_at(f, mid, size, parse_ts)
        if ts is None or ts >= cutoff:
            hi = mid
        else:
            lo = start + 1
    return _first_line_at(f, lo, size, parse_ts)[0]


def _count_lines_before(f, offset: int) -> int:
    f.seek(0)
    n, left = 0, offset
    while left > 0:
        chunk = f.read(min(left, 1 << 20))
        if not chunk:
            break
        n += chunk.count(b"\n")
        left -= len(chunk)
    return n


def _parser_for(path: str, game_code: str | None):
    if game_code:
        return get_parser(game_code)
    # An empty or brand-new file can't be sniffed -- fall back to whichever
    # configured log folder it's in.
    parser = detect_parser(path) if os.path.getsize(path) else None
    if parser:
        return parser
    resolved = Path(path).resolve()
    for code, root in db.log_roots().items():
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return get_parser(code)
        except OSError:
            continue
    raise ValueError(f"Can't tell which game {Path(path).name} is from -- it's empty and not in a configured log folder.")


def _identity(path: str) -> dict:
    """game/character/log_source ids for a log file, from (or added to) the
    permanent database -- the live window's rows join against them."""
    kb = db.get_connection()
    try:
        with kb.cursor() as cur:
            cur.execute(
                "SELECT ls.id AS log_source_id, ls.game_id, ls.character_id, g.code AS game_code "
                "FROM log_sources ls JOIN games g ON g.id=ls.game_id WHERE ls.file_path=%s",
                (path,),
            )
            row = cur.fetchone()
        if row:
            return row
        parser = _parser_for(path, None)
        game_id = db.get_or_create_game(kb, parser.game_code, GAME_NAMES.get(parser.game_code, parser.game_code))
        character_id = db.get_or_create_character(
            kb, game_id, guess_character_from_filename(path) or "Unknown", guess_server_from_filename(path)
        )
        # Registered so the window's rows have something to point at (the
        # selection records it right after).
        source = db.get_or_create_log_source(kb, game_id, character_id, path)
        return {"log_source_id": source["id"], "game_id": game_id, "character_id": character_id,
                "game_code": parser.game_code}
    finally:
        kb.close()


def _parse_into(raw, f, offset: int, size: int, line_no: int, parser, ident) -> tuple[int, int, int]:
    """Parses complete lines from offset up to size into the events table.
    Returns (new offset, new line_no, events added). Stops before a partial
    last line -- it gets read on the next catch-up, once it's complete."""
    f.seek(offset)
    batch, added = [], 0
    while offset < size:
        line_bytes = f.readline()
        if not line_bytes.endswith(b"\n"):
            break
        offset += len(line_bytes)
        line = line_bytes.decode("utf-8", "replace").rstrip("\r\n")
        event = parser.parse_line(line, line_no=line_no)
        line_no += 1
        if event is None:
            continue
        ingest.normalize_names(event)
        batch.append((
            ident["game_id"], ident["character_id"], ident["log_source_id"], sqlite_compat.format_dt(event.ts),
            event.event_type, event.source_name, event.source_type, event.target_name, event.target_type,
            event.verb, event.amount, event.outcome,
            json.dumps(event.extra) if event.extra else None, event.raw_line, event.line_no,
        ))
        if len(batch) >= INSERT_BATCH:
            _insert(raw, batch)
            added += len(batch)
            batch = []
    if batch:
        _insert(raw, batch)
        added += len(batch)
    return offset, line_no, added


def _insert(raw, rows):
    raw.executemany(
        "INSERT INTO events (game_id, character_id, log_source_id, ts, event_type, source_name, source_type, "
        "target_name, target_type, verb, amount, outcome, extra, raw_line, line_no) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


# --------------------------------------------------------------------------
# loading and catching up


def cutoff_for(window: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    if window == "all":
        return None
    if window == "now":
        return now
    return now - _DELTAS[window]


def _load(path: str, window: str, cutoff: datetime | None) -> dict:
    t0 = time.monotonic()
    ident = _identity(path)
    parser = _parser_for(path, ident["game_code"])
    raw = _raw()
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            offset = 0 if cutoff is None else find_offset(f, size, cutoff, parser.parse_timestamp)
            line_no = _count_lines_before(f, offset)
            raw.execute("DELETE FROM events")
            raw.execute("DELETE FROM live_meta")
            new_offset, new_line_no, added = _parse_into(raw, f, offset, size, line_no, parser, ident)
        _set_meta(
            raw, path=path, window=window, cutoff=cutoff.isoformat() if cutoff else None,
            file_id=_file_id(path), start_offset=offset, loaded_offset=new_offset, line_no=new_line_no,
            loaded_at=datetime.now().isoformat(timespec="seconds"), load_seconds=round(time.monotonic() - t0, 2),
            game_code=ident["game_code"], character_id=ident["character_id"],
            # Changes on every (re)load: event ids restart with each load, so
            # a page polling "events after id N" must start over when it does.
            generation=uuid.uuid4().hex,
        )
        raw.commit()
        return _meta(raw)
    finally:
        raw.close()


def refresh() -> dict:
    """Catches the window up with whatever's been appended to its log since
    the last call. Reloads from scratch if the file shrank or is now a
    different file. Returns the current state ({} if nothing's loaded)."""
    with _lock:
        raw = _raw()
        try:
            meta = _meta(raw)
        finally:
            raw.close()
        if not meta.get("path"):
            return {}
        path = meta["path"]
        try:
            size = os.path.getsize(path)
            same_file = _file_id(path) == meta["file_id"]
        except OSError:
            return {**meta, "missing": True}
        cutoff = datetime.fromisoformat(meta["cutoff"]) if meta.get("cutoff") else None
        if not same_file or size < meta["loaded_offset"]:
            return _load(path, meta["window"], cutoff)
        if size == meta["loaded_offset"]:
            return meta
        ident = {"game_id": None, "character_id": meta["character_id"], "log_source_id": None}
        ident.update(_identity(path))
        parser = _parser_for(path, meta["game_code"])
        raw = _raw()
        try:
            with open(path, "rb") as f:
                offset, line_no, _ = _parse_into(raw, f, meta["loaded_offset"], size, meta["line_no"], parser, ident)
            _set_meta(raw, loaded_offset=offset, line_no=line_no)
            raw.commit()
            return _meta(raw)
        finally:
            raw.close()


def _record(path: str, game_code: str, character: str | None, server: str | None):
    """Reads the selected log into the permanent database: the whole file
    the first time (or if it's a different file than last time -- see
    coverage.py), only what's new since the saved offset otherwise, never a
    line that's already recorded under any file name. Also makes it the one
    log being followed (live=1)."""
    from eq_log_suite.importer import import_file
    import_file(path, game_code, character, server)
    kb = db.get_connection()
    try:
        with kb.cursor() as cur:
            cur.execute("UPDATE log_sources SET live = CASE WHEN file_path=%s THEN 1 ELSE 0 END", (path,))
        kb.commit()
    finally:
        kb.close()


def select(path: str, window: str, character: str | None = None, server: str | None = None) -> dict:
    """The picker's action, and the only way a log becomes active: record
    `path` into the database (de-duplicated), load it into the Live window
    over `window`, and make it the log being followed. Raises ValueError for
    an unknown window or a file that can't be read or identified."""
    global _started
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}")
    path = str(Path(path).expanduser())
    if not os.path.isfile(path):
        raise ValueError(f"{path} isn't a file")
    character = (character or "").strip() or guess_character_from_filename(path) or None
    server = (server or "").strip() or guess_server_from_filename(path) or None
    if not character:
        raise ValueError(f"Can't tell whose log {Path(path).name} is -- enter the character name.")
    known = recorded_identity(path)
    if known and (known["name"].lower(), (known["server"] or "").lower()) != (character.lower(), (server or "").lower()):
        raise ValueError(
            f"{Path(path).name} is already recorded as {known['name']} ({known['server'] or 'no server'}) -- "
            "a file can't be re-assigned to a different character."
        )
    with _lock:
        _started = True  # an explicit pick is this process's starting point
        _record(path, _parser_for(path, None).game_code, character, server)
        state = _load(path, window, cutoff_for(window))
    db.set_setting("live.log_path", path)
    recent = [p for p in (db.get_setting("live.recent") or []) if p != path]
    db.set_setting("live.recent", [path] + recent[: RECENT_LIMIT - 1])
    return state


def last_selected() -> str | None:
    """The log selected last time, if it still exists -- only used to
    pre-select it in the picker. Nothing is loaded until it's picked."""
    path = db.get_setting("live.log_path")
    return path if path and os.path.isfile(path) else None


def ensure_started() -> dict:
    """Live data is only ever the log explicitly selected in this run of
    the app. So the first time anything asks, in a fresh process, whatever
    the live file holds from a previous run is cleared: the app starts
    empty and waits for a selection. After that, just catches up."""
    global _started
    with _lock:
        if _started:
            return refresh()
        _started = True
        raw = _raw()
        try:
            raw.execute("DELETE FROM events")
            raw.execute("DELETE FROM live_meta")
            raw.commit()
        finally:
            raw.close()
        return {}


def generation() -> str | None:
    """Id of the current load (see _load), None if nothing's loaded."""
    raw = _raw()
    try:
        return _meta(raw).get("generation")
    finally:
        raw.close()


def connection():
    """A connection for Live pages: the live window as main, the permanent
    database attached as kb. Catches the window up first."""
    ensure_started()
    conn = sqlite_compat.Connection(str(live_path()))
    conn.raw.execute("ATTACH DATABASE ? AS kb", (str(db.sqlite_path()),))
    return conn


def state() -> dict:
    """Current window for display: what's loaded, its bounds and size."""
    meta = ensure_started()
    if not meta:
        return {}
    raw = _raw()
    try:
        count, first, last = raw.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM events").fetchone()
    finally:
        raw.close()
    kb = db.get_connection()
    try:
        with kb.cursor() as cur:
            cur.execute("SELECT name, server FROM characters WHERE id=%s", (meta["character_id"],))
            ch = cur.fetchone() or {}
    finally:
        kb.close()
    return {
        **meta,
        "file_name": Path(meta["path"]).name,
        "window_label": WINDOWS.get(meta["window"], meta["window"]),
        "cutoff_dt": datetime.fromisoformat(meta["cutoff"]) if meta.get("cutoff") else None,
        "event_count": count,
        "first_ts": first,
        "last_ts": last,
        "character_name": ch.get("name"),
        "server": ch.get("server"),
    }


# --------------------------------------------------------------------------
# finding logs


def _is_log_name(name: str) -> bool:
    low = name.lower()
    return low.startswith("eqlog_") and (low.endswith(".txt") or ".txt." in low)


def recorded_identity(path: str) -> dict | None:
    """(character, server) this exact file was already recorded as, if any."""
    kb = db.get_connection()
    try:
        with kb.cursor() as cur:
            cur.execute("SELECT c.name, c.server FROM log_sources ls JOIN characters c ON c.id=ls.character_id "
                        "WHERE ls.file_path=%s", (path,))
            return cur.fetchone()
    finally:
        kb.close()


def _describe(path: str, in_default: bool) -> dict:
    st = os.stat(path)
    known = recorded_identity(path)
    return {
        "path": path,
        "name": Path(path).name,
        # Already recorded: that's who it is. Otherwise a guess from the
        # filename, shown editable in the picker -- the log itself never
        # says whose it is, and a renamed copy guesses wrong.
        "character": known["name"] if known else guess_character_from_filename(path),
        "server": known["server"] if known else guess_server_from_filename(path),
        "identity_known": bool(known),
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime),
        "in_default_folder": in_default,
    }


def list_logs() -> list[dict]:
    """The picker's dropdown: logs in the enabled games' folders (rotated
    copies included), plus recently used ones from anywhere, newest first."""
    seen, logs = set(), []
    for root in db.enabled_log_roots().values():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if _is_log_name(name) and os.path.isfile(path) and path not in seen:
                seen.add(path)
                logs.append(_describe(path, True))
    for path in db.get_setting("live.recent") or []:
        if path not in seen and os.path.isfile(path):
            seen.add(path)
            logs.append(_describe(path, False))
    logs.sort(key=lambda l: l["mtime"], reverse=True)
    return logs


def _game_for_folder(path: str) -> str:
    """eql/eq from an install folder's name ("EverQuest Legends" vs "EverQuest")."""
    return "eql" if "legend" in path.lower() else "eq"


def suggested_log_folders() -> list[dict]:
    """Existing Logs folders in the usual install locations -- Windows'
    Daybreak default, and Steam/Proton prefixes on Linux -- for a first run
    that hasn't chosen one yet. Newest activity first."""
    import glob
    home = Path.home()
    patterns = [
        r"C:\Users\Public\Daybreak Game Company\Installed Games\*\Logs",
        r"C:\Program Files*\Daybreak Game Company\Installed Games\*\Logs",
    ]
    for steam in (home / ".local/share/Steam", home / ".steam/steam",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
        patterns.append(str(steam / "steamapps/compatdata/*/pfx/drive_c/users/Public/"
                            "Daybreak Game Company/Installed Games/*/Logs"))
    found = {}
    for pat in patterns:
        for d in glob.glob(pat):
            if os.path.isdir(d):
                found[os.path.realpath(d)] = d
    out = [{"path": d, "game": _game_for_folder(d), "mtime": os.path.getmtime(d)} for d in found.values()]
    return sorted(out, key=lambda f: f["mtime"], reverse=True)


def browse(directory: str | None) -> dict:
    """One folder's worth of the Browse view: subfolders and log files.
    Starts in the default log folder."""
    if not directory:
        roots = list(db.enabled_log_roots().values())
        suggested = suggested_log_folders() if not roots else []
        directory = roots[0] if roots else (suggested[0]["path"] if suggested else str(Path.home()))
    d = Path(directory).expanduser()
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
    except OSError as e:
        return {"dir": str(d), "parent": str(d.parent), "dirs": [], "logs": [], "error": str(e)}
    dirs = [e.path for e in entries if e.is_dir() and not e.name.startswith(".")]
    # Any .txt here, not just eqlog_* -- an archived copy may have been
    # renamed to anything; the character/server are confirmed on open.
    logs = [_describe(e.path, False) for e in entries
            if e.is_file() and (e.name.lower().endswith(".txt") or ".txt." in e.name.lower())]
    logs.sort(key=lambda l: l["mtime"], reverse=True)
    return {"dir": str(d), "parent": str(d.parent) if d.parent != d else None, "dirs": dirs, "logs": logs, "error": None,
            "game_guess": _game_for_folder(str(d)), "is_log_folder": str(d) in db.log_roots().values()}
