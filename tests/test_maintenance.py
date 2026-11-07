"""Database actions (eq_log_suite/maintenance.py): Add files, Rebuild,
Archive -- against a throwaway config and database. Plain Python:
    .venv/bin/python tests/test_maintenance.py"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="eqls-maint-"))
(TMP / "config.yaml").write_text(f"database:\n  path: {TMP / 'data' / 'kb.sqlite3'}\nlog_roots: {{}}\nenabled_games: [eql]\n")
os.environ["EQ_LOG_SUITE_CONFIG"] = str(TMP / "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import db, maintenance  # noqa: E402

FMT = "%a %b %d %H:%M:%S %Y"
BASE = datetime(2026, 11, 7, 20, 0, 0)


def L(sec, text):
    return f"[{(BASE + timedelta(seconds=sec)).strftime(FMT)}] {text}\r\n"


def write(name, lines):
    p = TMP / name
    p.write_text("".join(lines), newline="")
    return str(p)


A = write("eqlog_Alpha_s1.txt", [L(0, "You have entered Kerra Isle."), L(1, "You have slain a kerran puma!"),
                                 L(2, "You have slain a kerran puma!")])
B = write("eqlog_Bravo_s1.txt", [L(0, "You have entered Kedge Keep."), L(5, "You have slain a seahorse!")])


def run(kind, **kw):
    maintenance.start(kind, **kw)
    for _ in range(300):
        if not maintenance.busy():
            break
        time.sleep(0.05)
    s = maintenance.status()
    return s


def q(sql, *args):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def deaths():
    return {r["npc"]: r["deaths"] for r in q("SELECT npc, SUM(deaths) deaths FROM mob_facts GROUP BY npc")}


def test_add_files_records_and_skips_what_is_recorded():
    s = run("add", items=[{"path": A, "game": "eql"}, {"path": B, "game": "eql"}, {"path": str(TMP / "nope.txt")}])
    assert s["error"] is None, s
    assert deaths() == {"a kerran puma": 2, "a seahorse": 1}, deaths()
    assert any("not found" in line for line in s["log"])
    s = run("add", items=[{"path": A, "game": "eql"}])           # again: nothing new
    assert deaths() == {"a kerran puma": 2, "a seahorse": 1}


def test_archive_lists_with_its_info():
    s = run("archive", name="first", note="two logs")
    assert s["error"] is None, s
    (a,) = [x for x in maintenance.list_archives() if x["name"] == "first"]
    assert a["exists"] and a["note"] == "two logs"
    assert sorted(a["info"]["source_logs"]) == sorted([A, B]) and a["info"]["characters"] == ["Alpha (s1)", "Bravo (s1)"]
    run("archive", name="first")                                  # same name: kept apart, not overwritten
    assert len([x for x in maintenance.list_archives() if x["name"].startswith("first")]) == 2


def test_rebuild_from_some_logs_keeps_settings():
    q("INSERT INTO npc_info (npc, npc_type, note) VALUES ('a kerran puma', 'mob', 'cat')")
    db.set_setting("live.recent", [A])
    s = run("rebuild", logs=[A], archive_first={"name": "before", "note": "both"})
    assert s["error"] is None, s
    assert deaths() == {"a kerran puma": 2}, deaths()              # B left out
    assert [r["note"] for r in q("SELECT note FROM npc_info")] == ["cat"]
    assert db.get_setting("live.recent") == [A]
    before = [x for x in maintenance.list_archives() if x["name"] == "before"][0]
    c = sqlite3.connect(f"file:{before['file']}?mode=ro", uri=True)
    assert dict(c.execute("SELECT npc, SUM(deaths) FROM mob_facts GROUP BY npc").fetchall()) == \
        {"a kerran puma": 2, "a seahorse": 1}                      # the archive is from before
    assert not [p for p in os.listdir(TMP / "data") if "rebuild" in p]


def test_rebuild_with_a_missing_log_changes_nothing():
    before = deaths()
    s = run("rebuild", logs=[A, str(TMP / "gone.txt")])
    assert s["error"] and "no longer exist" in s["error"], s
    assert deaths() == before


def test_rebuild_reset_settings():
    s = run("rebuild", logs=[A], reset_settings=True)
    assert s["error"] is None, s
    assert q("SELECT * FROM npc_info") == [] and db.get_setting("live.recent") is None
    assert deaths() == {"a kerran puma": 2}


def test_one_action_at_a_time():
    big = write("eqlog_Charlie_s1.txt", [L(i, "You have slain a ghoul!") for i in range(20000)])
    maintenance.start("add", items=[{"path": big, "game": "eql"}])
    try:
        maintenance.start("archive")
        raise AssertionError("a second action started while one was running")
    except RuntimeError:
        pass
    while maintenance.busy():
        time.sleep(0.05)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as e:
                failures += 1
                import traceback
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
