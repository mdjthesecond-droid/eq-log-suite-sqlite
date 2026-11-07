"""Shared DB-write path used by both the bulk importer and the live tailer,
so historical import and live catch-up never diverge in behavior."""

import json

from eq_log_suite.parsers.base import normalize_actor_name


def normalize_names(event):
    """Same mob logs as "A jeering gargoyle" (sentence-initial, e.g.
    dealing damage to you) or "a jeering gargoyle" (mid-sentence, e.g. you
    dealing damage to it) -- normalize so both group as one NPC everywhere
    downstream. Gated on type in ('npc', 'unknown') -- both are actor-name
    slots (never items/zones), 'unknown' included because a few handlers
    (e.g. eq_legends.py's h_slain_by, for "X has been slain by Y") hardcode
    it without running classify_actor at all; normalize_actor_name's own
    regex (only a literal "A "/"An " prefix) is the real safety net here,
    not this type check. Used by every path that stores events, so live
    window loads (live_window.py) and ingestion group names identically."""
    if event.source_type in ("npc", "unknown"):
        event.source_name = normalize_actor_name(event.source_name)
    if event.target_type in ("npc", "unknown"):
        event.target_name = normalize_actor_name(event.target_name)


def insert_batch(conn, game_id, character_id, log_source_id, events_batch, raw_batch, facts=None):
    """events_batch: list of (idx, ParsedEvent).
    raw_batch: list of (idx, line_no, ts, raw_text) -- one entry per line
    being recorded, whether or not it parsed into an event. Only checked
    for emptiness: raw lines aren't stored (the log file is the record).

    Every event goes to the knowledge base (facts.observe) first -- which
    can add to it (a zone change's cast_s_before) -- then only history
    (facts.HISTORY_TYPES) is stored. Sets `.db_id` on each stored event.
    """
    from eq_log_suite.facts import HISTORY_TYPES
    if not raw_batch:
        return
    with conn.cursor() as cur:
        for idx, event in events_batch:
            normalize_names(event)
            if facts is not None:
                facts.observe(event)
            if event.event_type not in HISTORY_TYPES:
                continue
            cur.execute(
                "INSERT INTO events (game_id, character_id, log_source_id, ts, event_type, "
                "source_name, source_type, target_name, target_type, verb, amount, outcome, "
                "extra, raw_line, line_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    game_id, character_id, log_source_id, event.ts, event.event_type,
                    event.source_name, event.source_type, event.target_name, event.target_type,
                    event.verb, event.amount, event.outcome,
                    json.dumps(event.extra) if event.extra else None,
                    event.raw_line, event.line_no,
                ),
            )
            event.db_id = cur.lastrowid
    if facts is not None:
        facts.flush()  # same transaction as the lines they came from
    conn.commit()
