"""Tests for eq_log_suite/live_window.py, against a throwaway config and
database (never the real ones). Runs with plain Python:
    .venv/bin/python tests/test_live_window.py"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="eqls-livewin-"))
LOGS = TMP / "Logs"
LOGS.mkdir()
(TMP / "config.yaml").write_text(
    f"database:\n  path: {TMP / 'kb.sqlite3'}\nlog_roots:\n  eql: {LOGS}\nenabled_games: [eql]\n"
)
os.environ["EQ_LOG_SUITE_CONFIG"] = str(TMP / "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import live_window as lw  # noqa: E402
from eq_log_suite.parsers.eq_legends import EQLegendsParser  # noqa: E402

FMT = "%a %b %d %H:%M:%S %Y"
parse_ts = EQLegendsParser.parse_timestamp


def line(ts: datetime, text: str) -> bytes:
    # EQ writes CRLF, even under Proton
    return f"[{ts.strftime(FMT)}] {text}\r\n".encode()


def melee(ts, dmg=10):
    return line(ts, f"You slash a ghoul for {dmg} points of damage.")


def write_log(path, base, seconds):
    with open(path, "wb") as f:
        for s in seconds:
            f.write(melee(base + timedelta(seconds=s), dmg=s + 1))


def test_find_offset_edges():
    base = datetime(2026, 11, 7, 10, 0, 0)
    p = TMP / "edges.txt"
    # duplicate seconds (5,5,5) and an untimestamped line in the middle
    with open(p, "wb") as f:
        for s in (0, 1, 2, 5, 5, 5, 9):
            f.write(melee(base + timedelta(seconds=s)))
            if s == 2:
                f.write(b"garbage without a timestamp\r\n")
    data = p.read_bytes()
    starts = [0] + [i + 1 for i, b in enumerate(data) if b == ord("\n")][:-1]
    timed_starts = [s for s in starts if data[s:s + 1] == b"["]
    size = len(data)
    with open(p, "rb") as f:
        def at(sec):
            return lw.find_offset(f, size, base + timedelta(seconds=sec), parse_ts)
        assert at(-100) == 0                      # window starts before the file
        assert at(0) == 0                         # exact match on the first line
        assert at(1) == timed_starts[1]
        assert at(3) == timed_starts[3]           # between lines -> next line (first of the dupes)
        assert at(5) == timed_starts[3]           # first of several same-second lines, not a later one
        assert at(9) == timed_starts[6]
        assert at(10) == size                     # window starts after the file -> nothing
    # half-written last line is never treated as a line
    with open(p, "ab") as f:
        f.write(b"[Sat Nov 07 10:00:20 2026] You slash a gh")
    size = p.stat().st_size
    with open(p, "rb") as f:
        assert lw.find_offset(f, size, base + timedelta(seconds=15), parse_ts) == size


def _count():
    raw = lw._raw()
    try:
        return raw.execute("SELECT COUNT(*), MIN(amount), MAX(amount) FROM events").fetchone()
    finally:
        raw.close()


def test_load_window_append_shrink_replace():
    now = datetime.now().replace(microsecond=0)
    log = LOGS / "eqlog_Tester_testserver.txt"
    # 48 hours of one line per hour, ending an hour ago: hours -48 .. -1,
    # each a minute inside its hour so none sits exactly on the 24h boundary
    # (the cutoff is measured a few ms after `now`)
    with open(log, "wb") as f:
        for h in range(48, 0, -1):
            f.write(melee(now - timedelta(hours=h) + timedelta(minutes=1), dmg=100 - h))

    st = lw.select(str(log), "24h")
    n, lo, hi = _count()
    assert n == 24 and lo == 100 - 24 and hi == 99, (n, lo, hi)   # hours -24 .. -1 only
    assert st["window"] == "24h"

    st = lw.select(str(log), "all")
    assert _count()[0] == 48

    # "now": loads nothing that's already there...
    lw.select(str(log), "now")
    assert _count()[0] == 0
    # ...but picks up what's written next, on the next connection()
    with open(log, "ab") as f:
        f.write(melee(datetime.now(), dmg=500))
        f.write(b"[Sat Nov 07 10:00:20 2026] half a li")   # partial: must wait
    lw.connection().close()
    assert _count()[0] == 1 and _count()[2] == 500
    with open(log, "ab") as f:
        f.write(b"ne\r\n")
    lw.connection().close()
    assert _count()[0] == 1  # the completed line is an old timestamp but was written after "now": still read
    # (it isn't a melee line, so no event -- but it must not break anything)

    # log moved away and a fresh empty one started at the same path: reload,
    # not a seek past the end of the new file
    lw.select(str(log), "24h")
    log.rename(LOGS / "eqlog_Tester_testserver.txt.archived")
    write_log(log, datetime.now() - timedelta(seconds=30), [0, 1, 2])
    lw.connection().close()
    n, lo, hi = _count()
    assert n == 3 and (lo, hi) == (1, 3), (n, lo, hi)

    # the file got smaller in place (truncated): reload too
    with open(log, "wb") as f:
        f.write(melee(datetime.now(), dmg=7))
    lw.connection().close()
    n, lo, hi = _count()
    assert n == 1 and lo == 7, (n, lo, hi)


def test_pick_before_first_page_load_sticks():
    # Real bug, found loading a real log: picking "all" as the very first
    # thing in a fresh process, then loading a page, reset it to 24h.
    log = LOGS / "eqlog_Tester_testserver.txt"
    lw._started = False
    lw.select(str(log), "all")
    st = lw.state()
    assert st["window"] == "all", st["window"]


def test_restart_behavior():
    log = LOGS / "eqlog_Tester_testserver.txt"
    lw.select(str(log), "now")
    since = lw.db.get_setting("live.now_since")
    # simulate an app restart: "now" keeps its original start
    lw._started = False
    st = lw.ensure_started()
    assert st["window"] == "now" and st["cutoff"] == since
    # any other window comes back as the 24h default
    lw.select(str(log), "1w")
    lw._started = False
    assert lw.ensure_started()["window"] == "24h"


def test_live_connection_sees_both_databases():
    conn = lw.connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT c.name AS who, COUNT(*) n FROM events ev JOIN characters c ON c.id=ev.character_id GROUP BY c.name")
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows and rows[0]["who"] == "Tester", rows


def test_lists_rotated_and_recent():
    rotated = LOGS / "eqlog_Tester_testserver.txt.20261101-090000"
    rotated.write_bytes(melee(datetime(2026, 11, 1, 9)))
    elsewhere = TMP / "eqlog_Friend_otherserver.txt"
    elsewhere.write_bytes(melee(datetime(2026, 11, 2, 9)))
    lw.select(str(elsewhere), "all")
    names = {l["name"]: l for l in lw.list_logs()}
    assert rotated.name in names and names[rotated.name]["server"] == "testserver"
    assert elsewhere.name in names and not names[elsewhere.name]["in_default_folder"]


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
