"""Bulk-import an EQ-family log file into the database.

Usage:
    python -m eq_log_suite.importer /home/myself/eql/Logs/eqlog_Cheerful_rivervale.txt

Resumable: each log_sources row tracks last_byte_offset, so re-running only
picks up bytes appended since the last run (the same mechanism the live
tailer uses to catch up after a restart).
"""

import argparse
import os
import sys
from pathlib import Path

from eq_log_suite import coverage, db, ingest
from eq_log_suite.parsers.registry import detect_parser, get_parser

BATCH_SIZE = 500

GAME_NAMES = {"eql": "EverQuest Legends", "eq": "EverQuest"}


def _log_base_name(path: str) -> str:
    """eqlog_<Character>_<Server>, with the .txt and anything after it
    dropped -- so a rotated copy like eqlog_Cheerful_rivervale.txt.20260922-161113
    names the same character/server as the live file (Path.stem alone
    leaves 'rivervale.txt' as the server)."""
    name = Path(path).name
    i = name.lower().find(".txt")
    return name[:i] if i >= 0 else Path(path).stem


def guess_character_from_filename(path: str) -> str | None:
    # classic EQ naming: eqlog_<Character>_<Server>.txt
    parts = _log_base_name(path).split("_")
    if len(parts) >= 2 and parts[0].lower() == "eqlog":
        return parts[1]
    return None


def guess_server_from_filename(path: str) -> str | None:
    parts = _log_base_name(path).split("_")
    if len(parts) >= 3 and parts[0].lower() == "eqlog":
        return "_".join(parts[2:])
    return None


def import_file(path: str, game_code: str | None, character_name: str | None, server: str | None, mark_live: bool = False):
    if game_code is None or game_code == "auto":
        parser_cls = detect_parser(path)
        if parser_cls is None:
            print(f"Could not auto-detect game for {path}; pass --game explicitly.", file=sys.stderr)
            sys.exit(1)
    else:
        parser_cls = get_parser(game_code)

    character_name = character_name or guess_character_from_filename(path) or "Unknown"
    server = server or guess_server_from_filename(path)

    conn = db.get_connection()
    game_id = db.get_or_create_game(
        conn, parser_cls.game_code, GAME_NAMES.get(parser_cls.game_code, parser_cls.game_code)
    )
    character_id = db.get_or_create_character(conn, game_id, character_name, server)
    log_source = db.get_or_create_log_source(conn, game_id, character_id, path, live=mark_live)
    log_source_id = log_source["id"]
    start_offset = log_source["last_byte_offset"]

    total_count = 0
    parsed_count = 0
    skipped_count = 0

    with open(path, "rb") as f:
        # Same name, different file? (EQ reuses one name per character +
        # server.) A different first line than last time means the saved
        # offset belongs to a file that's gone -- start over. See coverage.py.
        fp_now = coverage.fingerprint(f)
        fp_saved = log_source.get("fingerprint")
        if start_offset and fp_saved and fp_now and fp_now != fp_saved:
            print(f"{path} is a different file than last time (first line changed) -- reading it from the start.")
            start_offset = 0
        elif start_offset > os.fstat(f.fileno()).st_size:
            print(f"{path} is smaller than its saved position -- reading it from the start.")
            start_offset = 0
        if fp_now and fp_now != fp_saved:
            db.set_log_source_fingerprint(conn, log_source_id, fp_now)
        # From the start: check every line against what this character
        # already has recorded (so an archived/renamed copy of a log adds
        # nothing twice). Resuming at the saved offset: already exact.
        recorder = coverage.Recorder(conn, character_id, log_source_id, continuing=start_offset > 0)

        # Recompute the starting line number by counting newlines already
        # consumed on a prior run -- cheap relative to the parse itself.
        prefix = f.read(start_offset)
        line_no = prefix.count(b"\n")
        f.seek(start_offset)

        offset = start_offset
        events_batch: list = []
        raw_batch: list = []

        def flush():
            nonlocal events_batch, raw_batch
            recorder.flush()  # committed together with the lines by insert_batch
            ingest.insert_batch(conn, game_id, character_id, log_source_id, events_batch, raw_batch)
            events_batch = []
            raw_batch = []

        for raw_bytes in f:
            if not raw_bytes.endswith(b"\n"):
                # A half-written last line (the game is mid-write): leave it,
                # and the offset, for the next read -- consuming it here would
                # split it into two broken lines.
                break
            offset += len(raw_bytes)
            line = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            total_count += 1
            idx = total_count

            ts_only = parser_cls.parse_timestamp(line)
            if not recorder.should_record(ts_only[0] if ts_only else None, line):
                skipped_count += 1
                line_no += 1
                continue
            event = parser_cls.parse_line(line, line_no=line_no)
            if event is not None:
                parsed_count += 1
                events_batch.append((idx, event))
                raw_batch.append((idx, line_no, event.ts, line))
            else:
                raw_batch.append((idx, line_no, ts_only[0] if ts_only else None, line))
            line_no += 1

            if len(raw_batch) >= BATCH_SIZE:
                flush()
                db.update_log_source_offset(conn, log_source_id, offset)

        flush()
        db.update_log_source_offset(conn, log_source_id, offset)

    if skipped_count:
        print(f"  {skipped_count} line(s) skipped as already recorded (e.g. this is a copy of a log that was already read).")
    print(f"Imported {path}: {total_count} new lines read, {parsed_count} parsed into events (resumed from byte offset {start_offset}).")


def main():
    ap = argparse.ArgumentParser(description="Bulk-import an EQ-family log file into the database.")
    ap.add_argument("path")
    ap.add_argument("--game", default="auto", help="eql | eq | auto (default: auto-detect)")
    ap.add_argument("--character", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument("--live", action="store_true", help="mark this log_source for live tailing")
    args = ap.parse_args()
    import_file(args.path, args.game, args.character, args.server, mark_live=args.live)


if __name__ == "__main__":
    main()
