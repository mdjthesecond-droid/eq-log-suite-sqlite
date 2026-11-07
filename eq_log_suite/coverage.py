"""Keeping log lines from being recorded twice.

Two separate questions, two checks:

1. "Is the file at this path still the file we were reading?" -- fingerprint().
   EQ reuses one name per character + server (eqlog_<Name>_<server>.txt), so
   when an old log is moved or deleted, the next one has the same name. A
   saved byte offset alone can't tell them apart: if the new file has grown
   past the old offset before we look again (tailer was off), resuming there
   silently skips the start of the new file. Each log_sources row keeps a
   hash of its file's first line; a different first line means a different
   file, read from byte 0.

2. "Has this line already been recorded, under any file name?" -- Recorder.
   Selecting an archived or copied log to add to the database must not
   re-add lines the tailer already recorded under the original name. Per
   character, the time ranges already recorded are kept (recorded_ranges),
   plus the exact lines in each range's first and last second
   (range_edge_lines). Reading a file from the start:
     * a line strictly inside an existing range is skipped;
     * a line in a boundary second is skipped only if that same line, at
       the same repeat count within that second, was already recorded --
       three identical "You miss!" lines in one second, two recorded,
       means the third still gets in;
     * anything else is recorded, extending this pass's own range.
   Resuming the same file at its saved offset (fingerprint matches) skips
   the checks -- the offset is already exact -- and just extends that
   file's own range, carrying its last second's line counts over so a
   resume in the middle of a second stays consistent.

Ranges are per character because the log is per character: two characters'
logs can cover the same hours without being duplicates of each other.
"""

import bisect
import hashlib
from collections import Counter

FINGERPRINT_BYTES = 4096


def fingerprint(f) -> str | None:
    """Hash of the file's first complete line (from a binary handle), or
    None if it doesn't have one yet. Leaves the handle at position 0."""
    f.seek(0)
    first = f.readline(FINGERPRINT_BYTES)
    f.seek(0)
    if not first.endswith(b"\n"):
        return None  # empty, or the first line is still being written
    return hashlib.blake2b(first.rstrip(b"\r\n"), digest_size=12).hexdigest()


def line_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()


class Recorder:
    """Decides, line by line, whether a line from one log file should be
    recorded, and keeps the coverage tables up to date for the lines that
    are. One per read pass over one file. Call flush() before every commit
    of the lines it approved (same connection), so coverage and data are
    committed together."""

    def __init__(self, conn, character_id: int, log_source_id: int, continuing: bool):
        self.conn = conn
        self.character_id = character_id
        self.log_source_id = log_source_id
        self.continuing = continuing
        self.range_id = None
        self.range_start = None
        self.range_end = None
        self.start_lines: list[tuple[str, int]] = []   # (hash, occurrence) in range_start's second
        self.end_counts: Counter = Counter()            # line counts in the current second (range_end)
        self.cur_sec = None
        self.sec_counts: Counter = Counter()             # line counts in the current second, this file
        self.prev_skipped = False
        self.skipped = 0
        self._edge_cache: dict = {}
        self._dirty = False

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, start_ts, end_ts, log_source_id FROM recorded_ranges WHERE character_id=%s",
                (character_id,),
            )
            ranges = cur.fetchall()
            if continuing:
                own = [r for r in ranges if r["log_source_id"] == log_source_id]
                if own:
                    last = max(own, key=lambda r: r["end_ts"])
                    self.range_id, self.range_start, self.range_end = last["id"], last["start_ts"], last["end_ts"]
                    cur.execute(
                        "SELECT ts, line_hash, occurrence FROM range_edge_lines WHERE range_id=%s",
                        (self.range_id,),
                    )
                    for e in cur.fetchall():
                        if e["ts"] == self.range_start:
                            self.start_lines.append((e["line_hash"], e["occurrence"]))
                        if e["ts"] == self.range_end:
                            self.end_counts[e["line_hash"]] = max(self.end_counts[e["line_hash"]], e["occurrence"] + 1)
                    self.cur_sec = self.range_end
                    self.sec_counts = Counter(self.end_counts)
        # Sorted by start for a binary search per line -- ranges don't
        # overlap (covered lines are never re-recorded), so only the last
        # couple starting at or before a line's ts can contain it. Kept
        # cheap because an import checks every line of a file.
        self.others = sorted((r for r in ranges if r["id"] != self.range_id), key=lambda r: r["start_ts"])
        self._starts = [r["start_ts"] for r in self.others]

    def _edges(self, range_id):
        if range_id not in self._edge_cache:
            with self.conn.cursor() as cur:
                cur.execute("SELECT ts, line_hash, occurrence FROM range_edge_lines WHERE range_id=%s", (range_id,))
                self._edge_cache[range_id] = {(e["ts"], e["line_hash"], e["occurrence"]) for e in cur.fetchall()}
        return self._edge_cache[range_id]

    def should_record(self, ts, text: str) -> bool:
        """ts: the line's timestamp (None for a line without one -- it
        follows whatever the line before it got)."""
        if ts is None:
            return not self.prev_skipped
        h = line_hash(text)
        if ts != self.cur_sec:
            self.cur_sec, self.sec_counts = ts, Counter()
        k = self.sec_counts[h]
        self.sec_counts[h] += 1

        if not self.continuing:
            i = bisect.bisect_right(self._starts, ts)
            for r in self.others[max(0, i - 3):i]:
                if r["start_ts"] < ts < r["end_ts"]:
                    self.prev_skipped = True
                    self.skipped += 1
                    return False
                if ts == r["start_ts"] or ts == r["end_ts"]:
                    if (ts, h, k) in self._edges(r["id"]):
                        self.prev_skipped = True
                        self.skipped += 1
                        return False

        self.prev_skipped = False
        self._extend(ts, h, k)
        return True

    def _extend(self, ts, h, k):
        self._dirty = True
        if self.range_start is None:
            self.range_start = self.range_end = ts
        if ts == self.range_start:
            self.start_lines.append((h, k))
        if ts != self.range_end:
            self.range_end = ts
            self.end_counts = Counter()
        self.end_counts[h] = max(self.end_counts[h], k + 1)

    def flush(self):
        """Writes this pass's range and its edge lines. Doesn't commit."""
        if not self._dirty:
            return
        with self.conn.cursor() as cur:
            if self.range_id is None:
                cur.execute(
                    "INSERT INTO recorded_ranges (character_id, log_source_id, start_ts, end_ts) VALUES (%s,%s,%s,%s)",
                    (self.character_id, self.log_source_id, self.range_start, self.range_end),
                )
                self.range_id = cur.lastrowid
            else:
                cur.execute("UPDATE recorded_ranges SET end_ts=%s WHERE id=%s", (self.range_end, self.range_id))
            # Only the first and last second's lines are ever needed.
            cur.execute("DELETE FROM range_edge_lines WHERE range_id=%s", (self.range_id,))
            rows = {(self.range_start, h, k) for h, k in self.start_lines}
            rows |= {(self.range_end, h, k) for h, n in self.end_counts.items() for k in range(n)}
            for ts, h, k in rows:
                cur.execute(
                    "INSERT INTO range_edge_lines (range_id, ts, line_hash, occurrence) VALUES (%s,%s,%s,%s)",
                    (self.range_id, ts, h, k),
                )
        self._dirty = False
