"""Knowledge-base fact tests (eq_log_suite/facts.py), through the real
importer, against a throwaway config and database. Log lines use the real
formats (checked against real logs). Plain Python:
    .venv/bin/python tests/test_facts.py"""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="eqls-facts-"))
(TMP / "config.yaml").write_text(f"database:\n  path: {TMP / 'kb.sqlite3'}\nlog_roots: {{}}\nenabled_games: [eql]\n")
os.environ["EQ_LOG_SUITE_CONFIG"] = str(TMP / "config.yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import db, facts  # noqa: E402
from eq_log_suite.importer import import_file  # noqa: E402
from eq_log_suite.parsers.eq_legends import EQLegendsParser, parse_zone_tier  # noqa: E402

FMT = "%a %b %d %H:%M:%S %Y"
BASE = datetime(2026, 11, 7, 20, 0, 0)
ZONE = "The Permafrost Caverns - Solo 4 (Refined)"


def L(sec, text):
    return f"[{(BASE + timedelta(seconds=sec)).strftime(FMT)}] {text}\r\n"


LOG = [
    L(0, "A ghoul scowls at you, ready to attack -- looks kind of dangerous. (Lvl: 12)"),     # before any zone
    L(5, f"You have entered {ZONE}."),
    L(6, "A ghoul scowls at you, ready to attack -- looks kind of dangerous. (Lvl: 14)"),
    L(7, "You slash a ghoul for 20 points of damage."),
    L(8, "You have slain a ghoul!"),
    L(9, "You looted a Ruined Cat Pelt from a ghoul's corpse."),
    L(10, "A ghoul has been slain by Jenobn!"),                                               # a group kill, not yours
    L(11, "You looted 2 Bone Chips from a ghoul's corpse."),
    L(20, "Bloodgurgler - a rare creature - scowls at you, ready to attack -- what would you like your tombstone to say? (Lvl: 19)"),
    L(21, "You slash Bloodgurgler for 100 points of damage."),
    L(22, "You slash Bloodgurgler for 100 points of damage."),
    L(23, "You slash Bloodgurgler for 150 points of damage."),
    L(24, "You have slain Bloodgurgler!"),
    L(30, "You slash Test 10 for 999 points of damage."),                                    # sparring dummy
    L(31, "You have slain Test 10!"),
    L(40, "You slash Bloodgurgler for 400 points of damage."),                               # it respawned
    L(41, "You have slain Bloodgurgler!"),
]


def facts_rows(table, cols="*"):
    order = "1, 2" if cols.count(",") == 1 else "1, 2, 3"
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {table} ORDER BY {order}")
            return cur.fetchall()
    finally:
        conn.close()


def mob(npc, base):
    return next((r for r in facts_rows("mob_facts") if r["npc"].lower() == npc.lower() and r["base_zone"] == base), None)


def test_parser_reads_these_lines_as_expected():
    kinds = [getattr(EQLegendsParser.parse_line(l), "event_type", None) for l in LOG]
    assert kinds.count("con") == 3 and kinds.count("death") == 5 and kinds.count("loot") == 2, kinds
    assert kinds.count("zone_change") == 1


def test_facts_from_one_import():
    d = TMP / "one"
    d.mkdir()
    log = d / "eqlog_Alpha_s1.txt"
    log.write_text("".join(LOG), newline="")
    import_file(str(log), "eql", None, None)

    base, tier, solo, label = parse_zone_tier(ZONE)
    visits = facts_rows("zone_visits", "zone_name, base_zone, tier, tier_label, solo")
    assert [(v["zone_name"], v["base_zone"], v["tier"], v["tier_label"], v["solo"]) for v in visits] == \
        [(ZONE, base, tier, label, 1 if solo else 0)], visits

    before_zone = mob("a ghoul", "")
    assert before_zone["cons"] == 1 and before_zone["level_min"] == 12 and before_zone["deaths"] == 0

    g = mob("a ghoul", base)
    assert (g["tier"], g["solo"]) == (tier, 1 if solo else 0)
    assert g["cons"] == 1 and g["level_min"] == g["level_max"] == 14
    assert g["deaths"] == 2 and g["kills_by_you"] == 1          # one yours, one Jenobn's
    assert g["kills_engaged"] == 2                               # ...both while you were fighting ghouls
    assert g["rare"] == 0 and g["hp_samples"] == 0               # never HP for an ordinary name

    b = mob("Bloodgurgler", base)
    assert b["rare"] == 1 and b["level_min"] == 19
    assert b["deaths"] == 2 and b["hp_samples"] == 2 and (b["hp_min"], b["hp_max"]) == (350, 400), dict(b)

    loot = {(r["npc"].lower(), r["item"]): (r["drops"], r["qty_total"]) for r in facts_rows("loot_facts")}
    assert loot == {("a ghoul", "Ruined Cat Pelt"): (1, 1), ("a ghoul", "Bone Chips"): (1, 2)}, loot

    assert not any(r["npc"].startswith("Test ") for r in facts_rows("mob_facts"))


def test_kills_you_were_in_on():
    d = TMP / "engaged"
    d.mkdir()
    log = d / "eqlog_Bravo_s1.txt"
    log.write_text("".join([
        L(0, "You have entered Kedge Keep."),
        L(1, "A skeleton has been slain by Jenobn!"),                  # not your fight
        L(100, "A skeleton hits YOU for 5 points of damage."),
        L(110, "A skeleton has been slain by Jenobn!"),                # your fight, his blow
        L(200, "You try to slash a skeleton, but miss!"),
        L(300, "A skeleton has been slain by Jenobn!"),                # last exchange 100s ago
    ]), newline="")
    import_file(str(log), "eql", None, None)
    s = mob("a skeleton", "Kedge Keep")
    assert (s["deaths"], s["kills_by_you"], s["kills_engaged"]) == (3, 0, 1), dict(s)


def test_looted_corpse_counts_as_your_kill():
    # dead (or healing, or pulling) while the party finished it -- you never
    # traded hits, but you looted it, so it was your encounter's kill
    d = TMP / "looted"
    d.mkdir()
    log = d / "eqlog_Charlie_s1.txt"
    log.write_text("".join([
        L(0, "You have entered The Plane of Fear - Group."),
        L(10, "A phantasm has been slain by Risutachi!"),
        L(11, "You looted a Crystallized Sulfur from a phantasm's corpse."),
        L(12, "You looted a Bone Chip from a phantasm's corpse."),      # same corpse: still one kill
        L(500, "A phantasm has been slain by Risutachi!"),              # not looted, not your fight
    ]), newline="")
    import_file(str(log), "eql", None, None)
    m = mob("a phantasm", parse_zone_tier("The Plane of Fear - Group")[0])
    assert (m["deaths"], m["kills_engaged"]) == (2, 1), dict(m)


def test_copy_of_same_log_adds_no_facts():
    before = [dict(r) for r in facts_rows("mob_facts")], [dict(r) for r in facts_rows("loot_facts")]
    visits_before = len(facts_rows("zone_visits"))
    copy = TMP / "one" / "eqlog_Alpha_s1.txt.archived"
    shutil.copy(TMP / "one" / "eqlog_Alpha_s1.txt", copy)
    import_file(str(copy), "eql", None, None)
    after = [dict(r) for r in facts_rows("mob_facts")], [dict(r) for r in facts_rows("loot_facts")]
    assert before == after
    assert len(facts_rows("zone_visits")) == visits_before


def test_rebuild_from_events_matches_incremental():
    incremental = ([dict(r) for r in facts_rows("mob_facts")], [dict(r) for r in facts_rows("loot_facts")],
                   [(r["zone_name"], r["ts"]) for r in facts_rows("zone_visits", "zone_name, ts")])
    conn = db.get_connection()
    try:
        facts.rebuild(conn, verbose=False)
    finally:
        conn.close()
    rebuilt = ([dict(r) for r in facts_rows("mob_facts")], [dict(r) for r in facts_rows("loot_facts")],
               [(r["zone_name"], r["ts"]) for r in facts_rows("zone_visits", "zone_name, ts")])
    assert incremental == rebuilt


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
