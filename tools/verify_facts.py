"""Checks the knowledge-base facts (facts.py) against what the Database
pages compute from recorded events, on whatever database the config points
at. Read-only. Run it before switching a page over to facts:

    EQ_LOG_SUITE_CONFIG=/path/to/config.yaml .venv/bin/python tools/verify_facts.py [game]

First run (2026-11-07, three real rotated logs, 125k events): /loot 429/429
rows identical; /npcs every number identical -- its 50 extra NPCs are ones
only seen talking/trading (dialogue/vendor facts not extracted yet).
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eq_log_suite import db  # noqa: E402
from eq_log_suite.web import app  # noqa: E402


def main(game="eql"):
    app.LOOT_RESULT_LIMIT = 10**9
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mf.* FROM mob_facts mf JOIN games g ON g.id=mf.game_id WHERE g.code=%s", (game,))
            mobs = cur.fetchall()
            cur.execute("SELECT lf.* FROM loot_facts lf JOIN games g ON g.id=lf.game_id WHERE g.code=%s", (game,))
            loot = cur.fetchall()
    finally:
        conn.close()
    ok = True

    # /npcs: kills, level range, rare -- per NPC across zones/tiers
    agg = defaultdict(lambda: {"deaths": 0, "lmin": None, "lmax": None, "rare": 0})
    for m in mobs:
        a = agg[m["npc"].lower()]
        a["deaths"] += m["deaths"]
        for k, f, v in (("lmin", min, m["level_min"]), ("lmax", max, m["level_max"])):
            if v is not None:
                a[k] = v if a[k] is None else f(a[k], v)
        a["rare"] |= m["rare"]
    page = app._compute_npc_list(game)
    differ, talk_only = [], 0
    for r in page:
        want = (int(r["kills"] or 0), r["level_min"], r["level_max"], int(bool(r["rare"])))
        a = agg.get(r["npc"].lower())
        if a is None:
            if want == (0, None, None, 0):
                talk_only += 1   # only ever seen talking/trading -- not a fact yet
                continue
            differ.append((r["npc"], want, None))
        elif (a["deaths"], a["lmin"], a["lmax"], a["rare"]) != want:
            differ.append((r["npc"], want, (a["deaths"], a["lmin"], a["lmax"], a["rare"])))
    print(f"/npcs: {len(page)} NPCs; {len(page) - len(differ) - talk_only} identical, "
          f"{talk_only} only seen talking/trading, {len(differ)} DIFFER")
    for d in differ[:10]:
        print("   ", d)
    ok &= not differ

    # /npcs as the page renders it: facts version vs the pre-facts version
    for label, kw in (("no filter", {}), ("search", {"search": "an"}), ("zone", {"zone": "Kedge"})):
        new, old = app._compute_npc_list_from_facts(game, **kw), app._compute_npc_list(game, **kw)
        norm = lambda r: {**r, "rare": int(bool(r["rare"])), "vendor": int(bool(r["vendor"])),  # noqa: E731
                          "kills": int(r["kills"] or 0), "dialogue_lines": int(r["dialogue_lines"] or 0)}
        same = [norm(r) for r in new] == [norm(r) for r in old]
        ok &= same
        print(f"/npcs page ({label}): {len(new)} vs {len(old)} rows, {'identical' if same else 'DIFFER'}")
        if not same:
            for x, y in zip(new, old):
                if norm(x) != norm(y):
                    print("     facts:", norm(x), "\n     events:", norm(y)); break

    # /loot: drops and your kills per npc x zone x item
    kills, drops = defaultdict(int), defaultdict(int)
    for m in mobs:
        kills[(m["npc"].lower(), m["base_zone"])] += m["kills_by_you"]
    for lf in loot:
        drops[(lf["npc"].lower(), lf["base_zone"], lf["item"].lower())] += lf["drops"]
    rows, _ = app._compute_loot_from_events(game)
    differ = []
    for r in rows:
        z = r["zone"] or ""
        want = (r["drops"], r["kill_count"] or 0)
        got = (drops.get((r["npc"].lower(), z, r["item"].lower()), 0), kills.get((r["npc"].lower(), z), 0))
        if got != want:
            differ.append((r["npc"], z, r["item"], want, got))
    print(f"/loot: {len(rows)} npc x zone x item rows; {len(rows) - len(differ)} identical, {len(differ)} DIFFER")
    for d in differ[:10]:
        print("   ", d)
    ok &= not differ

    # /loot as the page renders it: facts version vs the old event version.
    # Everything must match except the drop chance, which now counts every
    # kill you were in on (kills_engaged), not just killing blows -- so check
    # that it never counts fewer kills than before, and how >100% rows change.
    first = rows[0] if rows else {"npc": "", "item": "", "zone": ""}
    same_fields = ("zone", "npc", "item", "avg_qty", "drops", "level")
    for label, kw in (("no filter", {}), ("npc", {"npc": first["npc"][:4]}),
                      ("item", {"item": first["item"][:4]}), ("zone", {"zone": (first["zone"] or "")[:5]})):
        new, old = app._compute_loot(game, **kw)[0], app._compute_loot_from_events(game, **kw)[0]
        key = lambda r: (r["npc"].lower(), r["zone"], r["item"].lower())  # noqa: E731
        N, O = {key(r): r for r in new}, {key(r): r for r in old}
        fields_ok = N.keys() == O.keys() and all(all(N[k][f] == O[k][f] for f in same_fields) for k in N)
        fewer = [k for k in N if (N[k]["kill_count"] or 0) < (O[k]["kill_count"] or 0)]
        ok &= fields_ok and not fewer
        print(f"/loot page ({label}): {len(N)} rows; drops/qty/level/zone {'identical' if fields_ok else 'DIFFER'}; "
              f"kills counted lower than before: {len(fewer)}")
        if label == "no filter":
            over = lambda rs: sum(1 for r in rs if r["chance_pct"] and r["chance_pct"] > 100)  # noqa: E731
            nokill = lambda rs: sum(1 for r in rs if not r["kill_count"])  # noqa: E731
            print(f"    over 100%: {over(old)} -> {over(new)};  drops with no kill counted: {nokill(old)} -> {nokill(new)}")
    app.LOOT_RESULT_LIMIT = 10**9
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(*(sys.argv[1:2] or ["eql"])) else 1)
