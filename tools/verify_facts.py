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
    page = app._compute_npc_list_from_events(game)
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
        new, old = app._compute_npc_list(game, **kw), app._compute_npc_list_from_events(game, **kw)
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
    ok &= verify_zoneinfo(game)
    ok &= verify_npc_detail(game)
    return ok


def verify_npc_detail(game):
    """Every NPC's detail page, at "all" and at each loot tier: facts version
    vs the pre-facts version (the combat table and dialogue are shared
    code, so not compared)."""
    names = [r["npc"] for r in app._compute_npc_list(game)]
    tier_key = lambda t: (str(t["tier"] if t["tier"] is not None else 0), t["tier_label"])  # noqa: E731

    def norm(d):
        return {
            "zones": sorted(z.lower() for z in d["zones"]),
            "levels": (d["level_min"], d["level_max"]),
            "rare": bool(d["rare"]), "kills": int(d["total_kills"] or 0), "vendor": bool(d["vendor"]),
            "catalog": sorted((c["item"].lower(), c["qty"], c["price"] or "") for c in d["catalog"]),
            "loot": sorted((r["item"].lower(), float(r["avg_qty"] or 0), r["drops"]) for r in d["loot"]),
            "loot_tiers": sorted(set(tier_key(t) for t in d["loot_tiers"])),
        }
    pages = same = 0
    shown = 0
    for name in names:
        tiers = ["all"] + sorted({tier_key(t)[0] for t in app._compute_npc_detail(game, name)["loot_tiers"]})
        for tier in tiers:
            a, b = norm(app._compute_npc_detail(game, name, tier)), norm(app._compute_npc_detail_from_events(game, name, tier))
            pages += 1
            if a == b:
                same += 1
                continue
            if shown < 12:
                shown += 1
                print(f"  DIFFER {name!r} tier={tier}: " + "; ".join(f"{k}: facts {a[k]} events {b[k]}" for k in a if a[k] != b[k]))
    print(f"/npcs detail: {len(names)} NPCs, {pages} pages; {same} identical, {pages - same} DIFFER")
    return same == pages


def verify_zoneinfo(game):
    """/zoneinfo list and every zone x tier x solo detail page: facts version
    vs the pre-facts version, compared as the templates show them."""
    ok = True
    new, old = app._compute_zone_list(game), app._compute_zone_list_from_events(game)
    N, O = {r["zone"]: r for r in new}, {r["zone"]: r for r in old}
    differ = [(z, {k: (N.get(z, {}).get(k), O.get(z, {}).get(k)) for k in ("level_min", "level_max", "npc_count", "rare_count")
                   if N.get(z, {}).get(k) != O.get(z, {}).get(k)})
              for z in sorted(N.keys() | O.keys()) if N.get(z) != O.get(z)]
    ok &= not differ
    print(f"/zoneinfo: {len(N)} vs {len(O)} zones; {len(N.keys() | O.keys()) - len(differ)} identical, {len(differ)} DIFFER")
    for d in differ[:15]:
        print("   ", d)

    norm_npc = lambda r: (r["npc"].lower(), r["level"], int(bool(r["rare"])), int(r["kills"] or 0),  # noqa: E731
                          int(r["dialogue_lines"] or 0))
    norm_loot = lambda r: (r["npc"].lower(), r["item"].lower(), float(r["avg_qty"] or 0), r["drops"])  # noqa: E731
    pages = same = 0
    for zone in sorted(N):
        variants = app._compute_zone_detail(game, zone)["variants"]
        choices = [("all", "any")] + [(t, "any") for t in sorted({v["tier"] for v in variants})] \
            + [("all", "1"), ("all", "0")]
        for tier, solo in choices:
            a, b = app._compute_zone_detail(game, zone, tier, solo), app._compute_zone_detail_from_events(game, zone, tier, solo)
            parts = {
                "tiers": ([(v["tier"], v["tier_label"]) for v in a["variants"]],
                          sorted({(str(v["tier"] if v["tier"] is not None else 0), v["tier_label"]) for v in b["variants"]},
                                 key=lambda t: int(t[0]))),
                "npcs": ([norm_npc(r) for r in a["npcs"]], [norm_npc(r) for r in b["npcs"]]),
                "loot": ([norm_loot(r) for r in a["loot"]], [norm_loot(r) for r in b["loot"]]),
                "ground": ([(r["item"].lower(), r["times"]) for r in a["ground_spawns"]],
                           [(r["item"].lower(), r["times"]) for r in b["ground_spawns"]]),
                "rares": ([(r["npc"].lower(), r["level"], len(r["drops"])) for r in a["rares"]],
                          [(r["npc"].lower(), r["level"], len(r["drops"])) for r in b["rares"]]),
            }
            # tier-toggle entries: the facts version de-duplicates by tier
            parts["tiers"] = (sorted(set(parts["tiers"][0]), key=lambda t: int(t[0])), parts["tiers"][1])
            pages += 1
            bad = [k for k, (x, y) in parts.items() if x != y]
            if not bad:
                same += 1
                continue
            ok = False
            print(f"  DIFFER {zone!r} tier={tier} solo={solo}: {', '.join(bad)}")
            for k in bad:
                x, y = parts[k]
                print(f"     {k}: facts-only {sorted(set(x) - set(y))[:4]}  events-only {sorted(set(y) - set(x))[:4]}")
    print(f"/zoneinfo detail: {pages} zone x tier/solo pages; {same} identical, {pages - same} DIFFER")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(*(sys.argv[1:2] or ["eql"])) else 1)
