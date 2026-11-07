-- EverQuest Log Suite schema (SQLite). Applied automatically by db.py the
-- first time the database file is opened; every statement is IF NOT EXISTS,
-- so re-applying it to an existing database is harmless.
--
-- Choices that matter (carried over from this project's MariaDB days, where
-- they were verified against real data -- see sqlite_compat.py):
--  * Text columns are COLLATE NOCASE: "A ghoul" and "a ghoul" are the same
--    NPC in every =, GROUP BY and DISTINCT (the log capitalizes a mob's name
--    differently depending on where it falls in the sentence).
--  * Datetimes are TEXT in one fixed 'YYYY-MM-DD HH:MM:SS.fff' format (see
--    sqlite_compat.py), so plain text comparison is chronological.
--  * BOOLEAN is INTEGER 0/1; JSON is TEXT.
--
-- Everything after the "-- indexes" marker line can be skipped and created
-- later (db.apply_schema(include_indexes=False)), which is much faster for
-- a bulk load.

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT COLLATE NOCASE NOT NULL UNIQUE,   -- 'eql', 'eq', ...
    name TEXT COLLATE NOCASE NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    name TEXT COLLATE NOCASE NOT NULL,
    server TEXT COLLATE NOCASE NULL,
    UNIQUE (game_id, name, server)
);

CREATE TABLE IF NOT EXISTS log_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    character_id INTEGER NOT NULL REFERENCES characters(id),
    file_path TEXT COLLATE NOCASE NOT NULL UNIQUE,
    last_byte_offset INTEGER NOT NULL DEFAULT 0,
    last_parsed_at TEXT NULL,
    last_rotated_at TEXT NULL,  -- UTC; see tailer.py's weekly rotation
    live INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NULL       -- hash of the file's first line, see coverage.py
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    character_id INTEGER NOT NULL REFERENCES characters(id),
    log_source_id INTEGER NOT NULL REFERENCES log_sources(id),
    ts TEXT NOT NULL,
    event_type TEXT COLLATE NOCASE NOT NULL,
    source_name TEXT COLLATE NOCASE NULL,
    source_type TEXT COLLATE NOCASE NULL,
    target_name TEXT COLLATE NOCASE NULL,
    target_type TEXT COLLATE NOCASE NULL,
    verb TEXT COLLATE NOCASE NULL,
    amount INTEGER NULL,
    outcome TEXT COLLATE NOCASE NULL,
    extra TEXT NULL,             -- JSON
    raw_line TEXT COLLATE NOCASE NOT NULL,
    line_no INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NULL REFERENCES games(id),  -- NULL = applies to all games
    name TEXT COLLATE NOCASE NOT NULL,
    description TEXT COLLATE NOCASE NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('regex', 'field_condition')),
    pattern TEXT NOT NULL,
    reaction_types TEXT NOT NULL DEFAULT 'log', -- comma list of notify,sound,overlay,log
    reaction_config TEXT NULL,                  -- JSON, e.g. {"sound_file": "/path/to.wav"}
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES alert_rules(id),
    event_id INTEGER NULL REFERENCES events(id),
    ts TEXT NOT NULL,
    matched_text TEXT COLLATE NOCASE NOT NULL
);

-- User-curated NPC type (log text can strongly suggest "vendor" via
-- vendor_buy/vendor_sell events, or "mob" via classify_actor's
-- article-prefix heuristic, but can't say "class trainer" or otherwise
-- distinguish a unique-named NPC from a unique-named mob) -- /npcs shows
-- an auto-suggested type alongside this override.
CREATE TABLE IF NOT EXISTS npc_info (
    npc TEXT COLLATE NOCASE NOT NULL PRIMARY KEY,
    npc_type TEXT COLLATE NOCASE NULL,
    note TEXT COLLATE NOCASE NULL,
    confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'))
);

-- User-curated zone level-range override, keyed by base_zone (see
-- parse_zone_tier() in eq_legends.py) -- fills in/corrects the level range
-- /zoneinfo otherwise derives from `con` events, for zones with no con data
-- yet or where the user wants to override the observed range.
CREATE TABLE IF NOT EXISTS zone_info (
    zone TEXT COLLATE NOCASE NOT NULL PRIMARY KEY,
    level_min_override INTEGER NULL,
    level_max_override INTEGER NULL,
    note TEXT COLLATE NOCASE NULL,
    confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'))
);

-- Per-game weekly log rotation policy (one row per game) -- see tailer.py's
-- tail_log_source for how this is consumed. mode='manual' means no
-- automatic schedule; day_of_week/day_of_month/hour are all local to
-- ROTATION_TZ (tailer.py); size_bytes only applies to mode='size'.
-- manual_trigger_at is independent of mode -- set by the "Rotate now" button
-- on the home page, honored regardless of which automatic mode is active.
CREATE TABLE IF NOT EXISTS rotation_settings (
    game_id INTEGER NOT NULL PRIMARY KEY REFERENCES games(id),
    mode TEXT NOT NULL DEFAULT 'manual' CHECK (mode IN ('manual', 'day_of_week', 'day_of_month', 'size')),
    day_of_week INTEGER NULL,        -- 0=Monday .. 6=Sunday
    day_of_month INTEGER NULL,       -- 1-31 (capped to the last real day of short months)
    hour INTEGER NOT NULL DEFAULT 9, -- local hour in ROTATION_TZ, for day_of_week/day_of_month
    size_bytes INTEGER NULL,
    manual_trigger_at TEXT NULL      -- UTC
);

-- Zone correlation (used by /loot, /quests) works by finding the
-- most recent zone_change logged before an event's own ts -- but a
-- character's very first bit of activity, before their first zone_change
-- was ever logged (log file started mid-session, or /log was toggled on
-- after already zoning in), has no such row to find, and shows up as
-- "(unknown zone)". The log itself has no way to recover that -- this is a
-- one-time, per-character, user-confirmed answer to "where was I before the
-- log starts". Only ever fills in that one leading gap; once a character
-- has any real zone_change logged, everything after it resolves normally
-- without needing an entry here.
CREATE TABLE IF NOT EXISTS zone_start_overrides (
    character_id INTEGER NOT NULL PRIMARY KEY REFERENCES characters(id),
    zone TEXT COLLATE NOCASE NOT NULL,
    note TEXT COLLATE NOCASE NULL,
    confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'localtime'))
);

-- Small app state that should survive restarts and travel with the
-- database: the log picker's remembered log, its "Now" session start, the
-- recently browsed logs. value is JSON.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NULL
);

-- What's already been recorded, per character -- see coverage.py. One row
-- per read pass over a log file (a tailer session, an import); a pass that
-- continues a file at its saved offset extends that file's last row.
CREATE TABLE IF NOT EXISTS recorded_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id),
    log_source_id INTEGER NOT NULL REFERENCES log_sources(id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL
);

-- The exact lines in each range's first and last second (hash + repeat
-- count within that second), so a line in a boundary second is only
-- skipped if that same line was really recorded already.
CREATE TABLE IF NOT EXISTS range_edge_lines (
    range_id INTEGER NOT NULL REFERENCES recorded_ranges(id),
    ts TEXT NOT NULL,
    line_hash TEXT NOT NULL,
    occurrence INTEGER NOT NULL,
    PRIMARY KEY (range_id, ts, line_hash, occurrence)
);

-- ---------------------------------------------------------------------------
-- Knowledge base: what's worth keeping after each encounter is over, derived
-- as lines are recorded (facts.py) -- never from viewing. Per mob x zone x
-- tier, where zone is the base zone ("The Permafrost Caverns") and tier/solo
-- the instance variant, the same grain every Database page works at; the
-- "All" tier is summed up when a page asks, never stored.

-- Every zone you entered, per character: what attributes everything else to
-- a zone and tier.
CREATE TABLE IF NOT EXISTS zone_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id),
    ts TEXT NOT NULL,
    zone_name TEXT COLLATE NOCASE NOT NULL,      -- as logged: "The Permafrost Caverns - Solo 4 (Refined)"
    base_zone TEXT COLLATE NOCASE NOT NULL,      -- "The Permafrost Caverns"
    tier INTEGER NOT NULL DEFAULT 0,
    tier_label TEXT COLLATE NOCASE NULL,         -- "Refined"
    solo INTEGER NOT NULL DEFAULT 0,
    -- the visit's summary (facts.py) -- /zones
    left_at TEXT NULL,                           -- your next zone change; NULL while it's the latest
    last_line_at TEXT NULL,                      -- your last line here (a log that just ended)
    first_fight_at TEXT NULL,                    -- your first/last damage here
    last_fight_at TEXT NULL,
    total_damage INTEGER NOT NULL DEFAULT 0,     -- your damage
    swings INTEGER NOT NULL DEFAULT 0,           -- your melee attempts / hits / crits
    landed INTEGER NOT NULL DEFAULT 0,
    crits INTEGER NOT NULL DEFAULT 0
);

-- One row per mob x zone x tier x solo it's been seen in. base_zone '' =
-- before any zone was known for that character.
CREATE TABLE IF NOT EXISTS mob_facts (
    game_id INTEGER NOT NULL REFERENCES games(id),
    npc TEXT COLLATE NOCASE NOT NULL,
    base_zone TEXT COLLATE NOCASE NOT NULL,
    tier INTEGER NOT NULL,
    solo INTEGER NOT NULL,
    deaths INTEGER NOT NULL DEFAULT 0,       -- every death seen (group/raid kills too) -- /npcs, /zoneinfo
    kills_by_you INTEGER NOT NULL DEFAULT 0, -- killing blow yours
    kills_engaged INTEGER NOT NULL DEFAULT 0, -- you were in the fight (encounter locking: your kill, your
                                              -- loot, whoever landed the blow) -- /loot's drop chance
    cons INTEGER NOT NULL DEFAULT 0,
    dialogue_lines INTEGER NOT NULL DEFAULT 0, -- it spoke (npc_dialogue)
    vendor_events INTEGER NOT NULL DEFAULT 0,  -- you bought from / sold to it
    listed INTEGER NOT NULL DEFAULT 0,         -- conned/spoke/traded/looted/died here: shown on /npcs
    fought INTEGER NOT NULL DEFAULT 0,         -- in a fight here (hit/hit by/died/resisted) -- /zoneinfo's
                                               -- level range counts only mobs fought somewhere
    level_min INTEGER NULL,
    level_max INTEGER NULL,
    rare INTEGER NOT NULL DEFAULT 0,          -- a con said so, here
    hp_samples INTEGER NOT NULL DEFAULT 0,    -- rare mobs only: one rare is up at a time,
    hp_min INTEGER NULL,                      -- so damage taken until it died is its HP
    hp_max INTEGER NULL,                      -- (low if others' damage is missing from the log)
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (game_id, npc, base_zone, tier, solo)
);

CREATE TABLE IF NOT EXISTS loot_facts (
    game_id INTEGER NOT NULL REFERENCES games(id),
    npc TEXT COLLATE NOCASE NOT NULL,
    base_zone TEXT COLLATE NOCASE NOT NULL,
    tier INTEGER NOT NULL,
    solo INTEGER NOT NULL,
    item TEXT COLLATE NOCASE NOT NULL,
    drops INTEGER NOT NULL DEFAULT 0,
    qty_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, npc, base_zone, tier, solo, item)
);

-- Items picked up off the ground (no source NPC), per zone x tier x solo.
CREATE TABLE IF NOT EXISTS ground_spawn_facts (
    game_id INTEGER NOT NULL REFERENCES games(id),
    base_zone TEXT COLLATE NOCASE NOT NULL,
    tier INTEGER NOT NULL,
    solo INTEGER NOT NULL,
    item TEXT COLLATE NOCASE NOT NULL,
    times INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, base_zone, tier, solo, item)
);

-- What a mob attacks with, per zone x tier x solo. kind 'attack': a melee
-- attack (uses = tries, hits = landed). kind 'spell': a skill/spell it cast
-- (uses = casts) or landed (hits). Max heal: what it healed with it.
CREATE TABLE IF NOT EXISTS npc_ability_facts (
    game_id INTEGER NOT NULL REFERENCES games(id),
    npc TEXT COLLATE NOCASE NOT NULL,
    base_zone TEXT COLLATE NOCASE NOT NULL,
    tier INTEGER NOT NULL,
    solo INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT COLLATE NOCASE NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    max_hit INTEGER NULL,
    max_heal INTEGER NULL,
    PRIMARY KEY (game_id, npc, base_zone, tier, solo, kind, name)
);

-- What a vendor sells: each distinct item x quantity x price you've bought
-- from them (price_text '' when the line had none).
CREATE TABLE IF NOT EXISTS vendor_catalog_facts (
    game_id INTEGER NOT NULL REFERENCES games(id),
    npc TEXT COLLATE NOCASE NOT NULL,
    item TEXT COLLATE NOCASE NOT NULL,
    qty INTEGER NOT NULL,
    price_text TEXT COLLATE NOCASE NOT NULL,
    times INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, npc, item, qty, price_text)
);

-- indexes
CREATE INDEX IF NOT EXISTS idx_zone_visits_char_ts ON zone_visits (character_id, ts);
CREATE INDEX IF NOT EXISTS idx_recorded_ranges_character ON recorded_ranges (character_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_game_ts ON events (game_id, ts);
CREATE INDEX IF NOT EXISTS idx_char_ts ON events (character_id, ts);
CREATE INDEX IF NOT EXISTS idx_event_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_source_name ON events (source_name);
CREATE INDEX IF NOT EXISTS idx_target_name ON events (target_name);
-- Covers "distinct source_name/target_name for this game + a handful of
-- event_types" queries (e.g. _compute_zone_list's fought_names) with an
-- index-only scan instead of a full table scan (measured on MariaDB,
-- 2026-08-24: 7.66s -> 2.23s on ~2.7M eql events).
CREATE INDEX IF NOT EXISTS idx_game_type_source ON events (game_id, event_type, source_name);
CREATE INDEX IF NOT EXISTS idx_game_type_target ON events (game_id, event_type, target_name);
-- /events with no filters ("browse recent activity") does ORDER BY ts DESC
-- LIMIT n across every game/character with no WHERE clause at all; this
-- lets it walk the index backwards instead of sorting the whole table
-- (/events pins it with INDEXED BY -- see events_browse in web/app.py).
CREATE INDEX IF NOT EXISTS idx_ts ON events (ts);
-- _known_logs() (the /encounters log picker) computes MIN/MAX/COUNT of ts
-- grouped by log_source_id just to populate a dropdown -- this makes that an
-- index-only aggregate per log source instead of a table scan.
CREATE INDEX IF NOT EXISTS idx_log_source_ts ON events (log_source_id, ts);
-- backfill.py's `WHERE event_id IS NULL` depends on this one.
CREATE INDEX IF NOT EXISTS idx_rule_ts ON alert_log (rule_id, ts);
CREATE INDEX IF NOT EXISTS idx_alert_log_event ON alert_log (event_id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_game ON alert_rules (game_id);
CREATE INDEX IF NOT EXISTS idx_log_sources_game ON log_sources (game_id);
CREATE INDEX IF NOT EXISTS idx_log_sources_character ON log_sources (character_id);
