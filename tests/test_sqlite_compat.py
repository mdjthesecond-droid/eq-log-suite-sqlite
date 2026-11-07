"""Tests for eq_log_suite/sqlite_compat.py. Runs with plain Python (no pytest
needed):  .venv/bin/python tests/test_sqlite_compat.py"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import db, sqlite_compat as sc  # noqa: E402

T = sc.translate


def test_placeholders_only_when_params():
    assert T("SELECT * FROM e WHERE a=%s AND b LIKE 'Test %%'", True) == "SELECT * FROM e WHERE a=? AND b LIKE 'Test %'"
    # pymysql leaves the query alone when no params are passed -- so must we.
    assert T("SELECT 'Test %%'", False) == "SELECT 'Test %%'"


def test_json_extract_and_unquote():
    assert T("SELECT JSON_EXTRACT(ev.extra,'$.tier') AS tier", False) == "SELECT (ev.extra -> '$.tier') AS tier"
    assert T("SELECT JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.base_zone')) z", False) == "SELECT (ev.extra ->> '$.base_zone') z"
    # nested subquery as the document argument (app.py's npc detail query)
    sql = ("JSON_UNQUOTE(JSON_EXTRACT((SELECT z.extra FROM events z WHERE z.event_type='zone_change' "
           "ORDER BY z.ts DESC LIMIT 1),'$.base_zone'))")
    assert T(sql, False) == ("((SELECT z.extra FROM events z WHERE z.event_type='zone_change' "
                             "ORDER BY z.ts DESC LIMIT 1) ->> '$.base_zone')")
    # inside an aggregate inside LAG()
    assert T("LAG(JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.base_zone'))) OVER (PARTITION BY x)", False) == \
        "LAG((e.extra ->> '$.base_zone')) OVER (PARTITION BY x)"
    # text that merely mentions the function inside a literal is left alone
    assert T("SELECT 'JSON_EXTRACT(a,b)'", False) == "SELECT 'JSON_EXTRACT(a,b)'"
    # and so is an identifier that only ends in the name
    assert T("SELECT MY_JSON_EXTRACT(a)", False) == "SELECT MY_JSON_EXTRACT(a)"


def test_upsert():
    sql = ("INSERT INTO npc_info (npc, npc_type, note) VALUES (%s,%s,%s) "
           "ON DUPLICATE KEY UPDATE npc_type=VALUES(npc_type), note=VALUES(note)")
    assert T(sql, True) == ("INSERT INTO npc_info (npc, npc_type, note) VALUES (?,?,?) "
                            "ON CONFLICT DO UPDATE SET npc_type=excluded.npc_type, note=excluded.note")
    assert T("INSERT INTO r (g, m) VALUES (%s, 'manual') ON DUPLICATE KEY UPDATE t=UTC_TIMESTAMP()", True) == \
        "INSERT INTO r (g, m) VALUES (?, 'manual') ON CONFLICT DO UPDATE SET t=UTC_TIMESTAMP()"


def test_index_hints():
    sql = "SELECT STRAIGHT_JOIN e.id FROM events e FORCE INDEX (idx_ts) JOIN games g ON 1"
    assert T(sql, False) == "SELECT e.id FROM events e INDEXED BY idx_ts JOIN games g ON 1"


def test_datetime_params_normalized():
    a = sc._adapt_in
    assert a(datetime(2026, 11, 7, 9, 5, 3, 123456)) == "2026-11-07 09:05:03.123"
    assert a("2026-11-07T09:05") == "2026-11-07 09:05:00.000"   # <input type=datetime-local>
    assert a("2026-11-07 09:05:03") == "2026-11-07 09:05:03.000"
    assert a("2026-11-07") == "2026-11-07 00:00:00.000"
    assert a("a ghoul") == "a ghoul"
    assert a(5) == 5


def test_schema_splits_tables_from_indexes():
    # A bulk load creates tables, copies rows, *then* builds indexes --
    # an early bug split at the marker text inside the header comment, which
    # silently created no tables at all.
    c = sqlite3.connect(":memory:")
    db.apply_schema(c, include_indexes=False)
    count = lambda kind: c.execute(  # noqa: E731
        "SELECT COUNT(*) FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", (kind,)).fetchone()[0]
    assert count("table") == 12 and count("index") == 0
    db.apply_schema(c)
    assert count("index") >= 16


def test_end_to_end_against_schema(tmp: Path):
    """Real schema, real wrapper: case-insensitive grouping, JSON values
    shaped like MariaDB's, datetimes round-tripping, upserts, lastrowid."""
    path = tmp / "t.sqlite3"
    raw = sqlite3.connect(path)
    db.apply_schema(raw)
    raw.close()
    conn = sc.Connection(str(path))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO games (code, name) VALUES (%s, %s)", ("eql", "EverQuest Legends"))
        gid = cur.lastrowid
        cur.execute("INSERT INTO characters (game_id, name, server) VALUES (%s,%s,%s)", (gid, "Vox", None))
        cid = cur.lastrowid
        cur.execute("INSERT INTO log_sources (game_id, character_id, file_path, last_byte_offset, live, last_rotated_at) "
                    "VALUES (%s,%s,%s,0,%s,UTC_TIMESTAMP())", (gid, cid, "/x.log", True))
        lsid = cur.lastrowid
        rows = [
            ("A ghoul", '{"rare":true,"tier":4}', datetime(2026, 11, 7, 10, 0, 0)),
            ("a ghoul", '{"rare":false,"tier":4}', datetime(2026, 11, 7, 10, 0, 1)),
            ("An imp", None, datetime(2026, 11, 7, 10, 0, 2)),
        ]
        for name, extra, ts in rows:
            cur.execute("INSERT INTO events (game_id, character_id, log_source_id, ts, event_type, source_name, extra, raw_line, line_no) "
                        "VALUES (%s,%s,%s,%s,'con',%s,%s,'x',1)", (gid, cid, lsid, ts, name, extra))
        conn.commit()

        cur.execute("SELECT source_name, COUNT(*) n, MAX(JSON_EXTRACT(extra,'$.rare'))='true' AS rare, "
                    "MAX(JSON_EXTRACT(extra,'$.tier')) AS tier FROM events GROUP BY source_name ORDER BY source_name")
        got = [(r["source_name"].lower(), r["n"], r["rare"], r["tier"]) for r in cur.fetchall()]
        assert got == [("a ghoul", 2, 1, "4"), ("an imp", 1, None, None)], got

        cur.execute("SELECT COUNT(*) n FROM events WHERE source_name=%s", ("A GHOUL",))
        assert cur.fetchone()["n"] == 2

        cur.execute("SELECT ts, MAX(ts) OVER () AS last_ts FROM events WHERE ts >= %s ORDER BY ts LIMIT 1",
                    ("2026-11-07T10:00",))
        r = cur.fetchone()
        assert r["ts"] == datetime(2026, 11, 7, 10, 0, 0) and r["last_ts"] == datetime(2026, 11, 7, 10, 0, 2), r

        cur.execute("SELECT last_rotated_at FROM log_sources")
        assert isinstance(cur.fetchone()["last_rotated_at"], datetime)

        for note in ("first", "second"):
            cur.execute("INSERT INTO npc_info (npc, npc_type, note) VALUES (%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE npc_type=VALUES(npc_type), note=VALUES(note)", ("a ghoul", "mob", note))
        conn.commit()
        cur.execute("SELECT npc, note, confirmed_at FROM npc_info")
        r = cur.fetchall()
        assert len(r) == 1 and r[0]["note"] == "second" and isinstance(r[0]["confirmed_at"], datetime), r

        cur.execute("SELECT JSON_UNQUOTE(%s) a", ('"quoted"',))
        assert cur.fetchone()["a"] == "quoted"
    conn.close()


if __name__ == "__main__":
    import tempfile
    failures = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            if "tmp" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"ok    {name}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
