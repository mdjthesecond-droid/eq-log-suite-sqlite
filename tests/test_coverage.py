"""Duplicate-prevention tests (eq_log_suite/coverage.py), through the real
importer, against a throwaway config and database. Plain Python:
    .venv/bin/python tests/test_coverage.py"""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="eqls-coverage-"))
(TMP / "config.yaml").write_text(f"database:\n  path: {TMP / 'kb.sqlite3'}\nlog_roots: {{}}\nenabled_games: [eql]\n")
os.environ["EQ_LOG_SUITE_CONFIG"] = str(TMP / "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import db  # noqa: E402
from eq_log_suite.importer import import_file  # noqa: E402

FMT = "%a %b %d %H:%M:%S %Y"
BASE = datetime(2026, 11, 7, 20, 0, 0)


def L(sec, text):
    return f"[{(BASE + timedelta(seconds=sec)).strftime(FMT)}] {text}\r\n"


def write(path, lines, mode="w"):
    with open(path, mode, newline="") as f:
        f.write("".join(lines))


def recorded(character=None):
    """Recorded raw lines for a character, as a sorted list of texts."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            sql = ("SELECT r.raw_text FROM raw_lines r JOIN log_sources ls ON ls.id=r.log_source_id "
                   "JOIN characters c ON c.id=ls.character_id")
            if character:
                cur.execute(sql + " WHERE c.name=%s", (character,))
            else:
                cur.execute(sql)
            return sorted(r["raw_text"] for r in cur.fetchall())
    finally:
        conn.close()


def texts(lines):
    """Lines as they're stored: without the log's CRLF."""
    return sorted(l.rstrip("\r\n") for l in lines)


def imp(path):
    import_file(str(path), "eql", None, None)


def fresh_dir(name):
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    return d


# a realistic stretch: a fight with repeated identical lines in one second
FIGHT = [L(0, "You slash a ghoul for 10 points of damage."),
         L(1, "You try to slash a ghoul, but miss!"),
         L(1, "You try to slash a ghoul, but miss!"),
         L(2, "A ghoul hits YOU for 4 points of damage."),
         L(3, "You slash a ghoul for 12 points of damage."),
         L(4, "You have slain a ghoul!")]


def test_same_log_under_another_name_adds_nothing():
    d = fresh_dir("rename")
    a = d / "eqlog_Alpha_s1.txt"
    write(a, FIGHT)
    imp(a)
    before = recorded("Alpha")
    assert len(before) == len(FIGHT)
    archived = d / "eqlog_Alpha_s1.txt.20261107-archived"
    shutil.copy(a, archived)
    imp(archived)
    assert recorded("Alpha") == before


def test_archived_copy_overlapping_new_play_adds_only_new():
    d = fresh_dir("overlap")
    a = d / "eqlog_Bravo_s1.txt"
    write(a, FIGHT)
    imp(a)
    # a later copy: the second half of the old content plus new play
    new_play = [L(10, "You slash a skeleton for 8 points of damage."), L(11, "You have slain a skeleton!")]
    b = d / "eqlog_Bravo_s1.txt.20261108-archived"
    write(b, FIGHT[3:] + new_play)
    imp(b)
    got = recorded("Bravo")
    assert got == texts(FIGHT + new_play), got


def test_boundary_second_repeats_counted_exactly():
    d = fresh_dir("boundary")
    a = d / "eqlog_Charlie_s1.txt"
    # first file ends in second 5 with two identical misses...
    write(a, [L(4, "You slash a rat for 3 points of damage."),
              L(5, "You try to slash a rat, but miss!"), L(5, "You try to slash a rat, but miss!")])
    imp(a)
    # ...a later file starts in second 5 with three of them (the third is new)
    b = d / "eqlog_Charlie_s1.txt.later"
    write(b, [L(5, "You try to slash a rat, but miss!")] * 3 + [L(6, "You have slain a rat!")])
    imp(b)
    got = recorded("Charlie")
    assert got.count(L(5, "You try to slash a rat, but miss!").rstrip("\r\n")) == 3, got
    assert len(got) == 5, got


def test_two_characters_same_hours_both_recorded():
    d = fresh_dir("twochars")
    write(d / "eqlog_Delta_s1.txt", FIGHT)
    write(d / "eqlog_Echo_s1.txt", FIGHT)
    imp(d / "eqlog_Delta_s1.txt")
    imp(d / "eqlog_Echo_s1.txt")
    assert len(recorded("Delta")) == len(FIGHT) and len(recorded("Echo")) == len(FIGHT)


def test_replaced_file_grown_past_old_offset_is_read_from_start():
    d = fresh_dir("replaced")
    a = d / "eqlog_Foxtrot_s1.txt"
    write(a, FIGHT[:2])
    imp(a)
    # old log moved away; the new one at the same name is already longer
    # than the old offset by the next read (tailer was off) -- size alone
    # can't tell, the first line can
    a.unlink()
    new = [L(100 + i, f"You slash a bat for {i} points of damage.") for i in range(10)]
    write(a, new)
    imp(a)
    got = recorded("Foxtrot")
    assert got == texts(FIGHT[:2] + new), got


def test_half_written_last_line_is_recorded_once_complete():
    d = fresh_dir("partial")
    a = d / "eqlog_Golf_s1.txt"
    write(a, FIGHT[:2] + ["[Sat Nov 07 20:00:09 2026] You slash a gh"])
    imp(a)
    assert len(recorded("Golf")) == 2
    write(a, ["oul for 99 points of damage.\r\n"], mode="a")
    imp(a)
    got = recorded("Golf")
    assert "[Sat Nov 07 20:00:09 2026] You slash a ghoul for 99 points of damage." in got and len(got) == 3, got


def test_resume_mid_second_with_repeats():
    d = fresh_dir("resume")
    a = d / "eqlog_Hotel_s1.txt"
    miss = L(7, "You try to slash a rat, but miss!")
    write(a, [L(6, "You slash a rat for 3 points of damage."), miss, miss])
    imp(a)
    # same file keeps growing, still inside second 7, with the same line
    write(a, [miss, L(8, "You have slain a rat!")], mode="a")
    imp(a)
    got = recorded("Hotel")
    assert got.count(miss.rstrip("\r\n")) == 3 and len(got) == 5, got
    # and an archived copy of the whole thing afterwards adds nothing
    shutil.copy(a, d / "eqlog_Hotel_s1.txt.copy")
    imp(d / "eqlog_Hotel_s1.txt.copy")
    assert recorded("Hotel") == got


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
