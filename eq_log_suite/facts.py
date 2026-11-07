"""The knowledge base: facts worth keeping after an encounter is over,
derived from each line as it's recorded (tailer and importer -- never from
viewing a log).

Per mob x base zone x tier x solo (schema.sql: mob_facts, loot_facts), plus
each character's zone history (zone_visits) that attributes everything to
a zone in the first place:

* deaths        every death of the mob seen in the log (group/raid kills too)
* kills_by_you  the killing blow was yours
* kills_engaged you were in the fight: the killing blow was yours, or you
                and it traded hits within ENGAGED_WINDOW before it died, or
                you looted its corpse (only your encounter's kills can be
                looted -- covers being dead while the party finished it).
                Encounter locking makes that your kill -- you see its loot
                whoever landed the blow -- so it's /loot's drop-chance base.
                (Same-named mobs can't be told apart: another "a ghoul"
                killed nearby within the window counts too.)
* cons, level_min/max, rare
* loot          item, number of drops, total quantity
* HP            only for rare mobs: one is only ever up at a time, so the
                damage it took until it died is its HP. Ordinary names can't
                be pinned to one mob (three "a ghoul"s at once), so no HP for
                them. Reads low if others' damage is missing from the log.
* first/last seen

Zone/tier/solo come from the most recent zone_change before the line (or
the character's zone_start_overrides entry, before their first one) -- the
same attribution the Database pages have always used. Lines before any zone
is known go to base_zone ''.

"Test <level>" sparring dummies are left out entirely, as on every page.

Usage: one FactExtractor per recording pass over a log (it keeps the
current zone and in-progress fight damage); observe() each recorded event,
flush() before every commit on the same connection.
"""

import json
from datetime import timedelta

DAMAGE_TYPES = ("melee", "spell_damage", "ability_damage")
# A rare that took damage, then nothing for this long, didn't die in that
# fight (it got away, or we did) -- start counting its HP over.
HP_FIGHT_GAP = timedelta(minutes=10)
# You were "in the fight" if you and the mob traded hits this recently
# before it died (every hit refreshes it).
ENGAGED_WINDOW = timedelta(seconds=60)
# A loot from a corpse proves the kill was yours if it comes this soon after
# that mob's death.
LOOT_AFTER_DEATH = timedelta(minutes=5)


def _is_dummy(name):
    return name.startswith("Test ")


class FactExtractor:
    def __init__(self, conn, game_id: int, character_id: int):
        self.conn = conn
        self.game_id = game_id
        self.character_id = character_id
        self.zone = None            # (base_zone, tier, solo), resolved on the first event
        self.mobs: dict = {}        # pending deltas, keyed (npc, base_zone, tier, solo)
        self.loot: dict = {}        # pending deltas, keyed (npc, base_zone, tier, solo, item)
        self.visits: list = []
        self.fight: dict = {}       # npc -> [damage taken so far, last hit ts], for rare HP
        self.engaged: dict = {}     # npc -> last time you and it traded hits
        self.uncounted: dict = {}   # npc -> (mob key, death ts) of its last death not yet counted as yours
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT npc FROM mob_facts WHERE game_id=%s AND rare=1", (game_id,))
            self.rares = {r["npc"].lower() for r in cur.fetchall()}

    # -- zone ---------------------------------------------------------------

    def _resolve_zone(self, ts):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT base_zone, tier, solo FROM zone_visits WHERE character_id=%s AND ts<=%s "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (self.character_id, ts),
            )
            row = cur.fetchone()
            if row:
                return (row["base_zone"], row["tier"], row["solo"])
            cur.execute("SELECT zone FROM zone_start_overrides WHERE character_id=%s", (self.character_id,))
            row = cur.fetchone()
        return (row["zone"], 0, 0) if row else ("", 0, 0)

    # -- facts --------------------------------------------------------------

    def _mob(self, npc, ts, key=None):
        key = key or ((npc,) + self.zone)
        m = self.mobs.get(key)
        if m is None:
            m = self.mobs[key] = {"deaths": 0, "kills_by_you": 0, "kills_engaged": 0, "cons": 0, "level_min": None, "level_max": None,
                                  "rare": 0, "hp": [], "first": ts, "last": ts}
        m["first"] = min(m["first"], ts)
        m["last"] = max(m["last"], ts)
        return m

    def observe(self, ev):
        """ev: a ParsedEvent (or anything with the same attributes; extra as
        a dict). Only call it for lines actually being recorded."""
        ts = ev.ts
        if ts is None:
            return
        if self.zone is None:
            self.zone = self._resolve_zone(ts)
        et = ev.event_type
        extra = ev.extra or {}

        if et == "zone_change" and ev.source_type == "you":
            base = extra.get("base_zone") or ev.target_name or ""
            tier = int(extra.get("tier") or 0)
            solo = 1 if extra.get("solo") else 0
            self.zone = (base, tier, solo)
            self.visits.append((ts, ev.target_name or base, base, tier, extra.get("tier_label"), solo))
            return

        if et == "con" and ev.source_type != "you" and ev.source_name and not _is_dummy(ev.source_name):
            m = self._mob(ev.source_name, ts)
            m["cons"] += 1
            if ev.amount is not None:
                m["level_min"] = ev.amount if m["level_min"] is None else min(m["level_min"], ev.amount)
                m["level_max"] = ev.amount if m["level_max"] is None else max(m["level_max"], ev.amount)
            if extra.get("rare"):
                m["rare"] = 1
                self.rares.add(ev.source_name.lower())
            return

        if et == "death" and ev.target_type != "you" and ev.target_name and not _is_dummy(ev.target_name):
            npc = ev.target_name
            m = self._mob(npc, ts)
            m["deaths"] += 1
            if ev.source_type == "you":
                m["kills_by_you"] += 1
            last = self.engaged.get(npc.lower())
            if ev.source_type == "you" or (last is not None and ts - last <= ENGAGED_WINDOW):
                m["kills_engaged"] += 1
                self.uncounted.pop(npc.lower(), None)
            else:
                self.uncounted[npc.lower()] = ((npc,) + self.zone, ts)
            f = self.fight.pop(npc.lower(), None)
            if f and npc.lower() in self.rares and f[0] > 0:
                m["hp"].append(f[0])
            return

        if et == "loot" and ev.source_name and ev.target_name and not _is_dummy(ev.source_name):
            pending = self.uncounted.pop(ev.source_name.lower(), None)
            if pending and ts - pending[1] <= LOOT_AFTER_DEATH:
                # you looted it, so it was your encounter's kill -- counted once,
                # however many items come off the corpse
                key, death_ts = pending
                # the zone it died in, even if a delta for it was already flushed
                self._mob(key[0], death_ts, key=key)["kills_engaged"] += 1
            self._mob(ev.source_name, ts)
            key = (ev.source_name,) + self.zone + (ev.target_name,)
            d = self.loot.setdefault(key, [0, 0])
            d[0] += 1
            d[1] += ev.amount if ev.amount is not None else 1
            return

        if et in DAMAGE_TYPES:
            for name, kind in ((ev.source_name, ev.source_type), (ev.target_name, ev.target_type)):
                if name and kind == "npc" and not _is_dummy(name):
                    self._mob(name, ts)
            if ev.target_name and ev.target_type != "you" and ev.amount:
                self._fight_add(ev.target_name, ev.amount, ts)
            # you and it traded hits (either way, landed or not)
            if ev.source_type == "you" and ev.target_name and ev.target_type != "you":
                self.engaged[ev.target_name.lower()] = ts
            elif ev.target_type == "you" and ev.source_name and ev.source_type != "you":
                self.engaged[ev.source_name.lower()] = ts
            return

        if et == "spell_heal" and ev.target_name and ev.target_type == "npc" and ev.amount:
            self._fight_add(ev.target_name, -ev.amount, ts)

    def _fight_add(self, npc, amount, ts):
        k = npc.lower()
        f = self.fight.get(k)
        if f is None or ts - f[1] > HP_FIGHT_GAP:
            f = self.fight[k] = [0, ts]
        f[0] += amount
        f[1] = ts

    # -- writing ------------------------------------------------------------

    def flush(self):
        """Writes pending facts. Doesn't commit -- the caller commits them
        together with the lines they came from."""
        if not (self.mobs or self.loot or self.visits):
            return
        with self.conn.cursor() as cur:
            for ts, name, base, tier, label, solo in self.visits:
                cur.execute(
                    "INSERT INTO zone_visits (character_id, ts, zone_name, base_zone, tier, tier_label, solo) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (self.character_id, ts, name, base, tier, label, solo),
                )
            for (npc, base, tier, solo), m in self.mobs.items():
                hp = m["hp"]
                cur.execute(
                    "INSERT INTO mob_facts (game_id, npc, base_zone, tier, solo, deaths, kills_by_you, kills_engaged, "
                    "cons, level_min, level_max, rare, hp_samples, hp_min, hp_max, first_seen, last_seen) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, npc, base_zone, tier, solo) DO UPDATE SET "
                    "deaths = deaths + excluded.deaths, kills_by_you = kills_by_you + excluded.kills_by_you, "
                    "kills_engaged = kills_engaged + excluded.kills_engaged, "
                    "cons = cons + excluded.cons, "
                    "level_min = COALESCE(MIN(level_min, excluded.level_min), level_min, excluded.level_min), "
                    "level_max = COALESCE(MAX(level_max, excluded.level_max), level_max, excluded.level_max), "
                    "rare = MAX(rare, excluded.rare), hp_samples = hp_samples + excluded.hp_samples, "
                    "hp_min = COALESCE(MIN(hp_min, excluded.hp_min), hp_min, excluded.hp_min), "
                    "hp_max = COALESCE(MAX(hp_max, excluded.hp_max), hp_max, excluded.hp_max), "
                    "first_seen = MIN(first_seen, excluded.first_seen), last_seen = MAX(last_seen, excluded.last_seen)",
                    (self.game_id, npc, base, tier, solo, m["deaths"], m["kills_by_you"], m["kills_engaged"], m["cons"],
                     m["level_min"], m["level_max"], m["rare"], len(hp), min(hp) if hp else None,
                     max(hp) if hp else None, m["first"], m["last"]),
                )
            for (npc, base, tier, solo, item), (drops, qty) in self.loot.items():
                cur.execute(
                    "INSERT INTO loot_facts (game_id, npc, base_zone, tier, solo, item, drops, qty_total) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, npc, base_zone, tier, solo, item) DO UPDATE SET "
                    "drops = drops + excluded.drops, qty_total = qty_total + excluded.qty_total",
                    (self.game_id, npc, base, tier, solo, item, drops, qty),
                )
        self.mobs.clear()
        self.loot.clear()
        self.visits.clear()


class _Row:
    """An events-table row shaped like a ParsedEvent, for rebuild()."""
    __slots__ = ("ts", "event_type", "source_name", "source_type", "target_name", "target_type", "amount", "extra")

    def __init__(self, r):
        for k in self.__slots__[:-1]:
            setattr(self, k, r[k])
        self.extra = json.loads(r["extra"]) if r["extra"] else None


def rebuild(conn, verbose=True):
    """Recomputes every fact from the events already recorded -- for a
    database recorded before facts existed, or after a change to what's
    extracted. Replaces all facts; one transaction."""
    with conn.cursor() as cur:
        for t in ("loot_facts", "mob_facts", "zone_visits"):
            cur.execute(f"DELETE FROM {t}")
        cur.execute("SELECT id, game_id FROM characters")
        chars = cur.fetchall()
    for ch in chars:
        fx = FactExtractor(conn, ch["game_id"], ch["id"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, event_type, source_name, source_type, target_name, target_type, amount, extra "
                "FROM events WHERE character_id=%s ORDER BY ts, id",
                (ch["id"],),
            )
            n = 0
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for r in rows:
                    fx.observe(_Row(r))
                n += len(rows)
                fx.flush()
        if verbose:
            print(f"  character {ch['id']}: {n:,} events")
    conn.commit()


if __name__ == "__main__":
    from eq_log_suite import db
    c = db.get_connection()
    try:
        print("Rebuilding facts from recorded events...")
        rebuild(c)
    finally:
        c.close()
