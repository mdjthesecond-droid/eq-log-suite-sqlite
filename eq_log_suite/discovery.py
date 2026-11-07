"""Auto-discovers new/rotated EQ-family log files (a new character-server
combo) and starts tracking them automatically -- imports the file and marks
it live, with no manual `importer.py --live` step needed. Called once at
tailer startup and on a recurring interval while it runs (see tailer.py).

Only one log_source should be "live" per character at a time. Which one
that is gets resolved by file mtime (the actual signal for "which file is
the game currently writing to"), as a pass over all of a character's known
files -- not by "whichever was imported most recently in this scan", which
breaks when several new files for the same character show up in one pass
(they'd cascade-demote each other in scan order instead of picking the one
that's actually active).
"""

import os
import re
from pathlib import Path

from eq_log_suite import db
from eq_log_suite.importer import guess_character_from_filename, guess_server_from_filename

EQLOG_NAME_RE = re.compile(r"^eqlog_.*\.txt$", re.IGNORECASE)


def _discover_eql(root: str) -> list[str]:
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    return [str(p) for p in sorted(root_path.glob("*.txt")) if EQLOG_NAME_RE.match(p.name)]


def _discover_eq(root: str) -> list[str]:
    # Same eqlog_<Character>_<Server>.txt naming as EQL -- both are
    # EQ-client-derived, see eq_log_suite/parsers/eq.py.
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    return [str(p) for p in sorted(root_path.glob("*.txt")) if EQLOG_NAME_RE.match(p.name)]


def _already_tracked(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM log_sources")
        return {row["file_path"] for row in cur.fetchall()}


def _import_new_file(path: str, game_code: str, character_name: str, server: str | None) -> dict:
    """Imports a newly-discovered file (not yet resolved for live status --
    see _resolve_live_status) and returns its log_sources row."""
    from eq_log_suite.importer import import_file  # local import: avoids a circular import with tailer

    import_file(path, game_code=game_code, character_name=character_name, server=server, mark_live=False)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_sources WHERE file_path=%s", (path,))
            return cur.fetchone()
    finally:
        conn.close()


def _resolve_live_status(character_ids: set[int]) -> None:
    """For each given character, marks live=1 on whichever of its known log
    files has the most recent mtime (the one actually being written to right
    now) and live=0 on all its others."""
    if not character_ids:
        return
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            for character_id in character_ids:
                cur.execute("SELECT * FROM log_sources WHERE character_id=%s", (character_id,))
                sources = cur.fetchall()
                if not sources:
                    continue
                by_mtime = sorted(
                    sources,
                    key=lambda s: os.path.getmtime(s["file_path"]) if os.path.exists(s["file_path"]) else -1,
                    reverse=True,
                )
                most_recent = by_mtime[0]
                for s in sources:
                    live = 1 if s["id"] == most_recent["id"] else 0
                    if s["live"] != live:
                        cur.execute("UPDATE log_sources SET live=%s WHERE id=%s", (live, s["id"]))
        conn.commit()
    finally:
        conn.close()


def scan_and_import(eql_root: str, eq_root: str = "") -> list[dict]:
    """Returns the (possibly now-live) log_sources rows for any
    newly-discovered files."""
    conn = db.get_connection()
    try:
        tracked = _already_tracked(conn)
    finally:
        conn.close()

    newly_added = []
    touched_character_ids: set[int] = set()

    for path in _discover_eql(eql_root):
        if path in tracked:
            continue
        character = guess_character_from_filename(path) or "Unknown"
        server = guess_server_from_filename(path)
        row = _import_new_file(path, "eql", character, server)
        newly_added.append(row)
        touched_character_ids.add(row["character_id"])

    for path in _discover_eq(eq_root):
        if path in tracked:
            continue
        character = guess_character_from_filename(path) or "Unknown"
        server = guess_server_from_filename(path)
        row = _import_new_file(path, "eq", character, server)
        newly_added.append(row)
        touched_character_ids.add(row["character_id"])

    _resolve_live_status(touched_character_ids)

    if not newly_added:
        return []
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            ids = [row["id"] for row in newly_added]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT * FROM log_sources WHERE id IN ({placeholders})", ids)
            return cur.fetchall()
    finally:
        conn.close()


def start_at_end(eql_root: str, eq_root: str = "") -> list[tuple[str, int]]:
    """Starts recording *from now on*: registers every log file found that
    isn't tracked yet at its current end, without importing anything already
    in it. Run once on a fresh database when old log contents shouldn't be
    imported (e.g. after a big game patch, when only new data is wanted).

    Only files that exist right now are affected. Any file discovered later
    (a new character or server) is imported from its start as usual, since
    everything in it is new. Returns [(path, bytes skipped)]."""
    from eq_log_suite.importer import GAME_NAMES

    conn = db.get_connection()
    skipped = []
    touched_character_ids: set[int] = set()
    try:
        tracked = _already_tracked(conn)
        for game_code, paths in (("eql", _discover_eql(eql_root)), ("eq", _discover_eq(eq_root))):
            for path in paths:
                if path in tracked:
                    continue
                character = guess_character_from_filename(path) or "Unknown"
                server = guess_server_from_filename(path)
                game_id = db.get_or_create_game(conn, game_code, GAME_NAMES.get(game_code, game_code))
                character_id = db.get_or_create_character(conn, game_id, character, server)
                source = db.get_or_create_log_source(conn, game_id, character_id, path)
                size = os.path.getsize(path)
                db.update_log_source_offset(conn, source["id"], size)
                skipped.append((path, size))
                touched_character_ids.add(character_id)
    finally:
        conn.close()
    _resolve_live_status(touched_character_ids)
    return skipped


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Log-file discovery (normally run automatically by the tailer).")
    ap.add_argument(
        "--start-at-end", action="store_true",
        help="register every current log file at its current end, so only lines written from now on get recorded",
    )
    args = ap.parse_args()
    roots = db.enabled_log_roots()
    if not args.start_at_end:
        new = scan_and_import(roots.get("eql", ""), roots.get("eq", ""))
        print(f"{len(new)} new log file(s) imported.")
        return
    skipped = start_at_end(roots.get("eql", ""), roots.get("eq", ""))
    if not skipped:
        print("No untracked log files found -- nothing to do (already-tracked files are left alone).")
    for path, size in skipped:
        print(f"recording from now on: {path} (skipped {size:,} existing bytes)")


if __name__ == "__main__":
    main()
