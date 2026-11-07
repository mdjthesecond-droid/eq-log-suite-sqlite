"""Database access for every part of the suite.

The whole database is one SQLite file (`database.path` in
config/local.yaml, relative to the project root unless absolute; default
data/eqlogs.sqlite3). No server, no password. It's created, with its
tables, the first time anything opens it -- see schema.sql.

The SQL throughout the code is written in MariaDB's dialect (this project
started on MariaDB -- see the eq-log-suite repo for that history) and is
translated as it runs; see sqlite_compat.py for what that covers and why.

The config file itself can be pointed elsewhere with EQ_LOG_SUITE_CONFIG
(handy for a throwaway test database).
"""

import functools
import os
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("EQ_LOG_SUITE_CONFIG") or PROJECT_DIR / "config" / "local.yaml")
# Only an explicitly requested config file has to exist; the default one is
# optional -- see config().
_CONFIG_REQUIRED = bool(os.environ.get("EQ_LOG_SUITE_CONFIG"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


@functools.lru_cache
def config() -> dict:
    """config/local.yaml, or {} if there isn't one -- a fresh install runs on
    defaults (database in data/) and the log folder is chosen inside the app
    (see log_roots()), so nobody has to edit a file to get started."""
    if not CONFIG_PATH.exists():
        if _CONFIG_REQUIRED:
            raise FileNotFoundError(f"{CONFIG_PATH} (from EQ_LOG_SUITE_CONFIG) not found")
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def log_roots() -> dict[str, str]:
    """Log folder per game code: the ones chosen in the app (Browse -> "use
    this folder", stored in app_settings) over any `log_roots` in the config
    file."""
    roots = dict(config().get("log_roots") or {})
    roots.update(get_setting("log_folders") or {})
    return roots


def set_log_folder(game_code: str, path: str):
    folders = dict(get_setting("log_folders") or {})
    folders[game_code] = path
    set_setting("log_folders", folders)


def enabled_games() -> set[str]:
    """Game codes whose log folders the picker lists, from config's
    `enabled_games` list -- lets a game's log_roots entry (and any log_sources
    rows already recorded for it) stay in place while uninstalled or just not
    being played right now, without either being auto-discovered or crashing
    the tailer when its folder doesn't exist. Defaults to every game with a
    log_roots entry."""
    cfg = config()
    games = set(cfg.get("enabled_games", (cfg.get("log_roots") or {}).keys()))
    # a folder chosen in the app is for a game that's being played
    return games | set((get_setting("log_folders") or {}).keys())


def enabled_log_roots() -> dict[str, str]:
    """log_roots filtered down to enabled_games -- the folders the picker's
    dropdown lists logs from."""
    roots = log_roots()
    enabled = enabled_games()
    return {code: path for code, path in roots.items() if code in enabled}


def sqlite_path() -> Path:
    p = Path((config().get("database") or {}).get("path") or "data/eqlogs.sqlite3").expanduser()
    return p if p.is_absolute() else PROJECT_DIR / p


def apply_schema(raw_conn, include_indexes: bool = True):
    """Runs schema.sql against a raw sqlite3 connection. Everything in it is
    IF NOT EXISTS, so this is safe on an existing database."""
    text = SCHEMA_PATH.read_text()
    # Split on the marker as a whole line -- the header comment mentions it too.
    tables, sep, indexes = text.partition("\n-- indexes\n")
    if not sep:
        raise ValueError(f"{SCHEMA_PATH} is missing its '-- indexes' marker line")
    raw_conn.executescript(tables)
    # CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a column
    # added to schema.sql later has to be added to existing databases here.
    for table, column, decl in _ADDED_COLUMNS:
        have = {r[1] for r in raw_conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            raw_conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    if include_indexes:
        raw_conn.executescript(indexes)


# (table, column, declaration) -- see apply_schema.
_ADDED_COLUMNS = [
    ("log_sources", "fingerprint", "TEXT NULL"),
    ("mob_facts", "kills_engaged", "INTEGER NOT NULL DEFAULT 0"),
    ("mob_facts", "dialogue_lines", "INTEGER NOT NULL DEFAULT 0"),
    ("mob_facts", "vendor_events", "INTEGER NOT NULL DEFAULT 0"),
    ("mob_facts", "listed", "INTEGER NOT NULL DEFAULT 0"),
]


@functools.lru_cache
def _ensure_schema():
    # Once per process: a brand-new install (or a shared copy someone just
    # unzipped) gets its tables created on first use, no setup step needed.
    from eq_log_suite import sqlite_compat
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite_compat.Connection(str(path))
    try:
        apply_schema(conn.raw)
    finally:
        conn.close()


def get_connection():
    """Returns a new connection -- callers should call conn.close() when
    done. Opening one is cheap (a local file: no network, no auth), so each
    call just opens the database file."""
    from eq_log_suite import sqlite_compat
    _ensure_schema()
    return sqlite_compat.Connection(str(sqlite_path()))


def get_setting(key: str, default=None):
    import json
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key=%s", (key,))
            row = cur.fetchone()
    finally:
        conn.close()
    return json.loads(row["value"]) if row and row["value"] is not None else default


def set_setting(key: str, value):
    import json
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


def get_or_create_game(conn, code: str, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM games WHERE code=%s", (code,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("INSERT INTO games (code, name) VALUES (%s, %s)", (code, name))
        conn.commit()
        return cur.lastrowid


def get_or_create_character(conn, game_id: int, name: str, server: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM characters WHERE game_id=%s AND name=%s AND (server=%s OR (server IS NULL AND %s IS NULL))",
            (game_id, name, server, server),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(
            "INSERT INTO characters (game_id, name, server) VALUES (%s, %s, %s)",
            (game_id, name, server),
        )
        conn.commit()
        return cur.lastrowid


def set_log_source_fingerprint(conn, log_source_id: int, fp: str | None):
    with conn.cursor() as cur:
        cur.execute("UPDATE log_sources SET fingerprint=%s WHERE id=%s", (fp, log_source_id))
    conn.commit()


def get_or_create_log_source(conn, game_id: int, character_id: int, file_path: str, live: bool = False) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM log_sources WHERE file_path=%s", (file_path,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            # last_rotated_at starts at creation time, not NULL/epoch -- so a
            # brand-new log file doesn't immediately get treated as
            # "overdue" and rotated away seconds after it starts being
            # tailed (see _most_recent_rotation_boundary in tailer.py).
            "INSERT INTO log_sources (game_id, character_id, file_path, last_byte_offset, live, last_rotated_at) "
            "VALUES (%s, %s, %s, 0, %s, UTC_TIMESTAMP())",
            (game_id, character_id, file_path, live),
        )
        conn.commit()
        cur.execute("SELECT * FROM log_sources WHERE id=%s", (cur.lastrowid,))
        return cur.fetchone()


def update_log_source_offset(conn, log_source_id: int, offset: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE log_sources SET last_byte_offset=%s, last_parsed_at=NOW() WHERE id=%s",
            (offset, log_source_id),
        )
    conn.commit()


def get_rotation_settings(conn, game_id: int) -> dict:
    """Returns this game's rotation policy, or a manual-only default if it's
    never been configured -- a game the user hasn't touched shouldn't
    silently start auto-rotating."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM rotation_settings WHERE game_id=%s", (game_id,))
        row = cur.fetchone()
    if row is not None:
        return row
    return {
        "game_id": game_id, "mode": "manual", "day_of_week": None,
        "day_of_month": None, "hour": 9, "size_bytes": None, "manual_trigger_at": None,
    }


def upsert_rotation_settings(
    conn, game_id: int, mode: str, day_of_week: int | None, day_of_month: int | None,
    hour: int, size_bytes: int | None,
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rotation_settings (game_id, mode, day_of_week, day_of_month, hour, size_bytes) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE mode=VALUES(mode), day_of_week=VALUES(day_of_week), "
            "day_of_month=VALUES(day_of_month), hour=VALUES(hour), size_bytes=VALUES(size_bytes)",
            (game_id, mode, day_of_week, day_of_month, hour, size_bytes),
        )
    conn.commit()


def trigger_rotation_now(conn, game_id: int):
    """Sets/refreshes this game's manual rotation trigger -- honored by
    tail_log_source regardless of its configured mode. Leaves mode/other
    columns alone if a row already exists (ON DUPLICATE KEY only touches
    manual_trigger_at); the mode='manual' in the INSERT list only applies
    the first time a game gets a row at all."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rotation_settings (game_id, mode, manual_trigger_at) "
            "VALUES (%s, 'manual', UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE manual_trigger_at=UTC_TIMESTAMP()",
            (game_id,),
        )
    conn.commit()


def mark_log_source_rotated(conn, log_source_id: int, offset: int):
    """Called right after a log rotation: resets the byte offset for the
    now-empty file and records the rotation in UTC so it survives a tailer
    restart -- see tailer.py's _rotation_due, which compares this against
    the game's configured rotation policy (or a manual trigger) to decide
    whether a rotation is overdue, catching up even after the tailer was off
    for days."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE log_sources SET last_byte_offset=%s, last_parsed_at=NOW(), last_rotated_at=UTC_TIMESTAMP() "
            "WHERE id=%s",
            (offset, log_source_id),
        )
    conn.commit()
