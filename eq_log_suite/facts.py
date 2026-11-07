"""The knowledge base: facts worth keeping after an encounter is over,
derived from each line as it's recorded (tailer and importer -- never from
viewing a log).

Per mob x base zone x tier x solo (schema.sql: mob_facts, loot_facts,
ground_spawn_facts), plus each character's zone history (zone_visits) that
attributes everything to a zone in the first place:

* deaths        every death of the mob seen in the log (group/raid kills too)
* kills_by_you  the killing blow was yours
* kills_engaged you were in the fight: the killing blow was yours, or you
                and it traded hits within ENGAGED_WINDOW before it died, or
                you looted its corpse (only your encounter's kills can be
                looted -- covers being dead while the party finished it), or
                a member of your group landed the blow while you were in it
                (healing, pulling, running up -- encounter locking makes it
                your kill). Group membership comes from the log's "... has
                joined/left the group" lines.
                Encounter locking makes that your kill -- you see its loot
                whoever landed the blow -- so it's /loot's drop-chance base.
                (Same-named mobs can't be told apart: another "a ghoul"
                killed nearby within the window counts too.)
* fought        it was in a fight here: hit or was hit (any damage line,
                landed or missed), resisted a spell, had a spell land on it,
                died or killed. Either side counts, whatever the parser
                typed it as (many lines can't tell a mob from a player), so
                player names get rows too -- never `listed`, so no page shows
                them. /zoneinfo's level range counts only mobs fought
                somewhere, so a zone's level-70 quest-givers don't stretch it.
* cons, level_min/max, rare
* loot          item, number of drops, total quantity
* ground spawns item picked up off the ground, per zone (ground_spawn_facts)
* vendor catalog item x quantity x price you bought from a vendor
                (vendor_catalog_facts, per vendor -- not per zone)
* abilities     what a mob attacks with (npc_ability_facts): each melee
                attack (tries, hits, max hit) and each skill/spell -- cast
                ("begins casting X"), landed ("... by X", damage over time)
                or healed with (max hit, max heal). Damage shields aren't
                the mob attacking, so they're left out.

And per character, history -- what the Database view's history pages show:

* zone visits   each zone you entered (zone_visits), summarized: when you
                left, your damage/swings/hits/crits and first/last fight
                there, your last line there (for a log that just ended).
                Each zone change also notes how soon after your last spell
                cast and your last Gate/Origin it came (cast_s_before /
                gate_s_before in its extra) -- what /zoneinfo's connections
                and PoK books need, so your casts themselves aren't kept.
* history lines the events table keeps only HISTORY_TYPES: zone changes,
                group, kills and loot, and every dialogue/quest/task line.
                Combat lines (and raw lines) aren't stored at all: they live
                in the log file, and the Live view re-reads them from there.
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

from datetime import timedelta

# The only event types kept in the permanent events table -- history the
# Database pages show (dialogue, quests, tasks, zone visits' kills/loot,
# group membership for "your kills"). Everything else contributes to facts
# and is dropped.
HISTORY_TYPES = frozenset({
    "zone_change", "group", "death", "loot",
    "hail", "say", "npc_dialogue", "reward", "item_offer", "trade_complete",
    "quest", "task_assigned", "task_updated", "task_reward", "exp", "faction",
})
GATE_SPELLS = ("Gate", "Origin")
# A zone change this soon after a cast wasn't walked (real gate/origin casts
# land the zone change ~10-20s later) -- see /zoneinfo's connections.
GATE_EXCLUSION_WINDOW_S = 60

DAMAGE_TYPES = ("melee", "spell_damage", "ability_damage")
# Lines that put both sides "in a fight" (the `fought` fact).
FIGHT_TYPES = DAMAGE_TYPES + ("spell_effect", "spell_resist", "death")
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
        self.ground: dict = {}      # pending deltas, keyed (base_zone, tier, solo, item)
        self.catalog: dict = {}     # pending deltas, keyed (npc, item, qty, price_text)
        self.visit = None           # the zone visit lines are landing in (see _new_visit)
        self.visits: list = []      # visits with unwritten changes
        self.abilities: dict = {}   # pending, keyed (npc, base_zone, tier, solo, kind, name)
        self.last_cast = None       # your last spell cast's ts, and your last Gate/Origin's
        self.last_gate = None
        self.fight: dict = {}       # npc -> [damage taken so far, last hit ts], for rare HP
        self.engaged: dict = {}     # npc -> last time you and it traded hits
        self.uncounted: dict = {}   # npc -> (mob key, death ts) of its last death not yet counted as yours
        self.group = None           # set of member names (lowercase) while you're in a group, else None
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT npc FROM mob_facts WHERE game_id=%s AND rare=1", (game_id,))
            self.rares = {r["npc"].lower() for r in cur.fetchall()}

    # -- zone ---------------------------------------------------------------

    def _resolve_zone(self, ts):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, base_zone, tier, solo FROM zone_visits WHERE character_id=%s AND ts<=%s "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (self.character_id, ts),
            )
            row = cur.fetchone()
            if row:
                self.visit = self._new_visit(row["id"])
                return (row["base_zone"], row["tier"], row["solo"])
            cur.execute("SELECT zone FROM zone_start_overrides WHERE character_id=%s", (self.character_id,))
            row = cur.fetchone()
        return (row["zone"], 0, 0) if row else ("", 0, 0)

    def _resolve_group(self, ts):
        """Group membership as of ts, replayed from this character's recorded
        group lines -- so a pass starting mid-session (tailer restart) still
        knows who's in the group."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT source_name, source_type, verb FROM events WHERE character_id=%s "
                "AND event_type='group' AND ts<%s ORDER BY ts, id",
                (self.character_id, ts),
            )
            rows = cur.fetchall()
        group = None
        for r in rows:
            group = self._apply_group(group, r["source_name"], r["source_type"], r["verb"])
        return group

    @staticmethod
    def _apply_group(group, who, who_type, verb):
        if who_type == "you":
            return set() if verb == "joined" else None   # removed (or left) -> not in a group
        if group is not None and who:
            if verb == "joined":
                group.add(who.lower())
            elif verb == "left":
                group.discard(who.lower())
        return group

    @staticmethod
    def _new_visit(visit_id=None, row=None):
        """A zone visit: its row (None until written) plus unwritten changes."""
        return {"id": visit_id, "row": row, "left_at": None, "last_line": None, "first_fight": None,
                "last_fight": None, "damage": 0, "swings": 0, "landed": 0, "crits": 0}

    def _touch_visit(self):
        v = self.visit
        if v is not None and not any(v is x for x in self.visits):
            self.visits.append(v)
        return v

    def _visit_line(self, ev, ts):
        """Folds a line into the current visit's summary."""
        v = self._touch_visit()
        if v is None:
            return
        v["last_line"] = ts if v["last_line"] is None else max(v["last_line"], ts)
        if ev.event_type in DAMAGE_TYPES and ev.source_type == "you":
            v["first_fight"] = ts if v["first_fight"] is None else min(v["first_fight"], ts)
            v["last_fight"] = ts if v["last_fight"] is None else max(v["last_fight"], ts)
            v["damage"] += ev.amount or 0
            if ev.event_type == "melee":
                v["swings"] += 1
                if ev.outcome in ("hit", "crit"):
                    v["landed"] += 1
                if ev.outcome == "crit":
                    v["crits"] += 1

    def _ability(self, npc, kind, name, used=0, hit=None, heal=None):
        key = (npc,) + self.zone + (kind, name)
        a = self.abilities.setdefault(key, [0, 0, None, None])
        a[0] += used
        if hit is not None:
            a[1] += 1
            a[2] = hit if a[2] is None else max(a[2], hit)
        if heal is not None:
            a[3] = heal if a[3] is None else max(a[3], heal)

    def _abilities(self, ev, et, extra):
        who = ev.source_name
        if not who or ev.source_type == "you" or _is_dummy(who) or not ev.verb:
            return
        if et == "melee":
            if extra.get("proc"):          # "X hit you for N points of magic damage by Wrath."
                self._ability(who, "spell", ev.verb, hit=ev.amount)
            else:                           # "X bashes YOU for N" / "X tries to bash YOU, but misses!"
                self._ability(who, "attack", ev.verb, used=1, hit=ev.amount)
        elif et == "spell_damage" and not extra.get("damage_shield"):
            self._ability(who, "spell", ev.verb, hit=ev.amount)
        elif et == "spell_cast":
            self._ability(who, "spell", ev.verb, used=1)
        elif et == "spell_heal" and ev.amount:
            self._ability(who, "spell", ev.verb, heal=ev.amount)

    # -- facts --------------------------------------------------------------

    def _mob(self, npc, ts, key=None):
        key = key or ((npc,) + self.zone)
        m = self.mobs.get(key)
        if m is None:
            m = self.mobs[key] = {"deaths": 0, "kills_by_you": 0, "kills_engaged": 0, "cons": 0,
                                  "dialogue_lines": 0, "vendor_events": 0, "listed": 0, "fought": 0,
                                  "level_min": None, "level_max": None,
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
            self.group = self._resolve_group(ts)
        et = ev.event_type
        if ev.extra is None:
            ev.extra = {}
        extra = ev.extra

        if et == "zone_change" and ev.source_type == "you":
            # How soon after your last cast / Gate this came -- kept on the
            # zone change itself, since the casts aren't stored.
            for key, last in (("cast_s_before", self.last_cast), ("gate_s_before", self.last_gate)):
                if last is not None and 0 <= (ts - last).total_seconds() <= GATE_EXCLUSION_WINDOW_S:
                    extra[key] = int((ts - last).total_seconds())
            if self._touch_visit() is not None:
                self.visit["left_at"] = ts
            base = extra.get("base_zone") or ev.target_name or ""
            tier = int(extra.get("tier") or 0)
            solo = 1 if extra.get("solo") else 0
            self.zone = (base, tier, solo)
            self.visit = self._new_visit(row=(ts, ev.target_name or base, base, tier, extra.get("tier_label"), solo))
            self._visit_line(ev, ts)
            return

        self._visit_line(ev, ts)
        self._abilities(ev, et, extra)
        if et == "spell_cast" and ev.source_type == "you":
            self.last_cast = ts
            if ev.verb in GATE_SPELLS:
                self.last_gate = ts

        if et == "group":
            self.group = self._apply_group(self.group, ev.source_name, ev.source_type, ev.verb)
            return

        if et in FIGHT_TYPES:
            for name, kind in ((ev.source_name, ev.source_type), (ev.target_name, ev.target_type)):
                if name and kind != "you" and not _is_dummy(name):
                    self._mob(name, ts)["fought"] = 1

        if et == "ground_spawn" and ev.target_name:
            key = self.zone + (ev.target_name,)
            self.ground[key] = self.ground.get(key, 0) + 1
            return

        if et == "con" and ev.source_type != "you" and ev.source_name and not _is_dummy(ev.source_name):
            m = self._mob(ev.source_name, ts)
            m["cons"] += 1
            m["listed"] = 1
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
            m["listed"] = 1
            if ev.source_type == "you":
                m["kills_by_you"] += 1
            last = self.engaged.get(npc.lower())
            by_group = self.group is not None and (ev.source_name or "").lower() in self.group
            if ev.source_type == "you" or by_group or (last is not None and ts - last <= ENGAGED_WINDOW):
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
            self._mob(ev.source_name, ts)["listed"] = 1
            key = (ev.source_name,) + self.zone + (ev.target_name,)
            d = self.loot.setdefault(key, [0, 0])
            d[0] += 1
            d[1] += ev.amount if ev.amount is not None else 1
            return

        if et in ("npc_dialogue", "vendor_buy", "vendor_sell") and ev.source_name and not _is_dummy(ev.source_name):
            if et == "vendor_buy" and ev.target_name:
                key = (ev.source_name, ev.target_name, ev.amount if ev.amount is not None else 1,
                       extra.get("price_text") or "")
                self.catalog[key] = self.catalog.get(key, 0) + 1
            m = self._mob(ev.source_name, ts)
            m["listed"] = 1
            m["dialogue_lines" if et == "npc_dialogue" else "vendor_events"] += 1
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
        if not (self.mobs or self.loot or self.ground or self.catalog or self.abilities or self.visits):
            return
        with self.conn.cursor() as cur:
            for v in self.visits:
                stats = (v["damage"], v["swings"], v["landed"], v["crits"])
                if v["id"] is None:
                    ts, name, base, tier, label, solo = v["row"]
                    cur.execute(
                        "INSERT INTO zone_visits (character_id, ts, zone_name, base_zone, tier, tier_label, solo, "
                        "left_at, last_line_at, first_fight_at, last_fight_at, total_damage, swings, landed, crits) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (self.character_id, ts, name, base, tier, label, solo, v["left_at"], v["last_line"],
                         v["first_fight"], v["last_fight"]) + stats,
                    )
                    v["id"] = cur.lastrowid
                else:
                    cur.execute(
                        "UPDATE zone_visits SET left_at = COALESCE(%s, left_at), "
                        "last_line_at = COALESCE(MAX(last_line_at, %s), last_line_at, %s), "
                        "first_fight_at = COALESCE(MIN(first_fight_at, %s), first_fight_at, %s), "
                        "last_fight_at = COALESCE(MAX(last_fight_at, %s), last_fight_at, %s), "
                        "total_damage = total_damage + %s, swings = swings + %s, landed = landed + %s, "
                        "crits = crits + %s WHERE id=%s",
                        (v["left_at"], v["last_line"], v["last_line"], v["first_fight"], v["first_fight"],
                         v["last_fight"], v["last_fight"]) + stats + (v["id"],),
                    )
                v.update(self._new_visit(v["id"], v["row"]))
            for (npc, base, tier, solo, kind, name), (uses, hits, max_hit, max_heal) in self.abilities.items():
                cur.execute(
                    "INSERT INTO npc_ability_facts (game_id, npc, base_zone, tier, solo, kind, name, uses, hits, "
                    "max_hit, max_heal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, npc, base_zone, tier, solo, kind, name) DO UPDATE SET "
                    "uses = uses + excluded.uses, hits = hits + excluded.hits, "
                    "max_hit = COALESCE(MAX(max_hit, excluded.max_hit), max_hit, excluded.max_hit), "
                    "max_heal = COALESCE(MAX(max_heal, excluded.max_heal), max_heal, excluded.max_heal)",
                    (self.game_id, npc, base, tier, solo, kind, name, uses, hits, max_hit, max_heal),
                )
            for (npc, base, tier, solo), m in self.mobs.items():
                hp = m["hp"]
                cur.execute(
                    "INSERT INTO mob_facts (game_id, npc, base_zone, tier, solo, deaths, kills_by_you, kills_engaged, "
                    "cons, dialogue_lines, vendor_events, listed, fought, "
                    "level_min, level_max, rare, hp_samples, hp_min, hp_max, first_seen, last_seen) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, npc, base_zone, tier, solo) DO UPDATE SET "
                    "deaths = deaths + excluded.deaths, kills_by_you = kills_by_you + excluded.kills_by_you, "
                    "kills_engaged = kills_engaged + excluded.kills_engaged, "
                    "cons = cons + excluded.cons, dialogue_lines = dialogue_lines + excluded.dialogue_lines, "
                    "vendor_events = vendor_events + excluded.vendor_events, listed = MAX(listed, excluded.listed), "
                    "fought = MAX(fought, excluded.fought), "
                    "level_min = COALESCE(MIN(level_min, excluded.level_min), level_min, excluded.level_min), "
                    "level_max = COALESCE(MAX(level_max, excluded.level_max), level_max, excluded.level_max), "
                    "rare = MAX(rare, excluded.rare), hp_samples = hp_samples + excluded.hp_samples, "
                    "hp_min = COALESCE(MIN(hp_min, excluded.hp_min), hp_min, excluded.hp_min), "
                    "hp_max = COALESCE(MAX(hp_max, excluded.hp_max), hp_max, excluded.hp_max), "
                    "first_seen = MIN(first_seen, excluded.first_seen), last_seen = MAX(last_seen, excluded.last_seen)",
                    (self.game_id, npc, base, tier, solo, m["deaths"], m["kills_by_you"], m["kills_engaged"], m["cons"],
                     m["dialogue_lines"], m["vendor_events"], m["listed"], m["fought"],
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
            for (base, tier, solo, item), times in self.ground.items():
                cur.execute(
                    "INSERT INTO ground_spawn_facts (game_id, base_zone, tier, solo, item, times) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, base_zone, tier, solo, item) DO UPDATE SET times = times + excluded.times",
                    (self.game_id, base, tier, solo, item, times),
                )
            for (npc, item, qty, price), times in self.catalog.items():
                cur.execute(
                    "INSERT INTO vendor_catalog_facts (game_id, npc, item, qty, price_text, times) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (game_id, npc, item, qty, price_text) DO UPDATE SET times = times + excluded.times",
                    (self.game_id, npc, item, qty, price, times),
                )
        self.mobs.clear()
        self.loot.clear()
        self.ground.clear()
        self.catalog.clear()
        self.abilities.clear()
        self.visits.clear()


FACT_TABLES = ("loot_facts", "ground_spawn_facts", "vendor_catalog_facts", "npc_ability_facts", "mob_facts")


def _first_ts(path):
    """The first timestamp in a log, for reading logs oldest first."""
    from eq_log_suite.parsers.registry import detect_parser
    parser = detect_parser(path)
    with open(path, "rb") as f:
        for _ in range(200):
            line = f.readline()
            if not line:
                break
            ts = parser.parse_timestamp(line.decode("utf-8", "replace").rstrip("\r\n")) if parser else None
            if ts:
                return ts[0]
    return None


def backup(conn, verbose=True):
    """Copies the database to data/backups/<name>-<time>.sqlite3 first."""
    from datetime import datetime
    from eq_log_suite import db
    src = db.sqlite_path()
    dest = src.parent / "backups" / f"{src.stem}-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    out = sqlite3.connect(str(dest))
    try:
        conn.raw.backup(out)
    finally:
        out.close()
    if verbose:
        print(f"  backed up to {dest}")
    return dest


def rebuild(conn, verbose=True):
    """Re-reads every recorded log file from the start with the current
    parser, replacing all facts and history -- for after a change to what's
    extracted. The log files are the permanent record (combat lines aren't
    stored), so every one has to still exist; nothing is touched if one
    doesn't. Backs the database up first. Keeps everything you entered
    (alert rules, NPC/zone notes and overrides, settings). Run it with the
    app stopped."""
    import os
    from eq_log_suite.importer import import_file
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ls.id, ls.file_path, g.code AS game, c.name, c.server FROM log_sources ls "
            "JOIN games g ON g.id=ls.game_id JOIN characters c ON c.id=ls.character_id"
        )
        sources = cur.fetchall()
        cur.execute("SELECT DISTINCT log_source_id FROM recorded_ranges")
        recorded = {r["log_source_id"] for r in cur.fetchall()}
    sources = [s for s in sources if s["id"] in recorded]   # selected once but never read: nothing to redo
    missing = [s["file_path"] for s in sources if not os.path.isfile(s["file_path"])]
    if missing:
        raise SystemExit("Not rebuilding -- these recorded logs no longer exist (move them back first):\n  "
                         + "\n  ".join(missing))
    backup(conn, verbose)
    with conn.cursor() as cur:
        cur.execute("UPDATE alert_log SET event_id=NULL")
        cur.execute("DROP TABLE IF EXISTS raw_lines")   # no longer kept at all
        for t in FACT_TABLES + ("zone_visits", "events", "range_edge_lines", "recorded_ranges"):
            cur.execute(f"DELETE FROM {t}")
        cur.execute("UPDATE log_sources SET last_byte_offset=0, fingerprint=NULL")
    conn.commit()
    for src in sorted(sources, key=lambda s: (_first_ts(s["file_path"]) is None, _first_ts(s["file_path"]) or 0)):
        if verbose:
            print(f"  reading {src['file_path']}")
        import_file(src["file_path"], src["game"], src["name"], src["server"])
    conn.raw.execute("VACUUM")


if __name__ == "__main__":
    from eq_log_suite import db
    c = db.get_connection()
    try:
        print("Rebuilding facts and history from the recorded log files...")
        rebuild(c)
    finally:
        c.close()
