"""SQLite access layer that speaks the dialect the rest of the code was
written in -- pymysql + MariaDB, which this project ran on until 2026-11
(history: the eq-log-suite repo) -- so the queries throughout the app run
unchanged. New queries can be written in either dialect; native SQLite
syntax passes straight through.

Two halves:

* translate() -- rewrites the handful of MariaDB-only constructs this
  codebase actually uses into SQLite equivalents. Cached per SQL string, so
  it's a one-time cost per distinct query.
* Connection/Cursor -- a pymysql DictCursor look-alike (`with conn.cursor()
  as cur`, dict rows, lastrowid, commit/close) over sqlite3, plus the value
  conversions needed for results to come back the same shape as pymysql's.

Behavior that matters and is deliberately matched (all verified against
real MariaDB data and side-by-side page comparisons, 2026-11-07):

* JSON_EXTRACT(a, p) -> (a -> p). SQLite's `->` returns JSON *text* exactly
  like MariaDB's JSON_EXTRACT ('true', '4', '"Foo"'), unlike SQLite's own
  json_extract() (1, 4, 'Foo'). That keeps every `MAX(...)='true'`
  comparison and every raw tier/solo value handed to Python identical.
  JSON_UNQUOTE(JSON_EXTRACT(a, p)) -> (a ->> p).
* Case-insensitive text comparison: MariaDB's utf8mb4_uca1400_ai_ci merges
  e.g. "A ghoul"/"a ghoul" in every =, GROUP BY and DISTINCT. schema.sql
  declares text columns COLLATE NOCASE to match (the real data has no
  accent-only variants, so ASCII-only NOCASE is enough).
* Datetimes are stored as 'YYYY-MM-DD HH:MM:SS.fff' text, one format for
  every column, so plain text comparison orders them correctly. Datetime
  parameters (including the browser's 'YYYY-MM-DDTHH:MM' strings, which
  MariaDB parses but SQLite would compare as raw text) are normalized into
  that format on the way in, and come back out as datetime objects like
  pymysql returns.
* pymysql only %-formats a query when params are passed, so '%%' is only
  unescaped then -- mirrored exactly (see _pyformat_to_qmark).
"""

import functools
import json
import re
import sqlite3
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------
# value conversion

_DT_OUT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\Z")
_DT_IN = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?\Z")


def format_dt(v: datetime) -> str:
    # Truncated to milliseconds, same as a DATETIME(3) column.
    return v.strftime("%Y-%m-%d %H:%M:%S.") + f"{v.microsecond // 1000:03d}"


def _adapt_in(v):
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return format_dt(v)
    if isinstance(v, date):
        return v.isoformat() + " 00:00:00.000"
    if isinstance(v, str) and len(v) <= 26:
        m = _DT_IN.match(v)
        if m:
            day, hm, sec, frac = m.groups()
            return f"{day} {hm or '00:00'}:{sec or '00'}.{(frac or '').ljust(3, '0')[:3]}"
    return v


def _convert_out(v):
    if isinstance(v, str) and len(v) <= 26 and _DT_OUT.match(v):
        return datetime.fromisoformat(v)
    return v


# --------------------------------------------------------------------------
# SQL translation


class TranslationError(ValueError):
    pass


def _skip_string(sql: str, i: int) -> int:
    """sql[i] is a quote character; returns the index just past the matching
    close quote. Handles both '' doubling and MariaDB's backslash escapes."""
    q = sql[i]
    j = i + 1
    n = len(sql)
    while j < n:
        c = sql[j]
        if c == "\\" and q != "`":
            j += 2
            continue
        if c == q:
            if j + 1 < n and sql[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    raise TranslationError("unterminated string literal")


def _pyformat_to_qmark(sql: str) -> str:
    """'%s' -> '?', '%%' -> '%', exactly as pymysql's `query % args` would
    treat them (it formats inside string literals too, so this does as well)."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "%":
            nxt = sql[i + 1] if i + 1 < n else ""
            if nxt == "s":
                out.append("?")
            elif nxt == "%":
                out.append("%")
            else:
                raise TranslationError(f"unsupported format sequence %{nxt} in query")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_JSON_FUNC = re.compile(r"(JSON_UNQUOTE|JSON_EXTRACT)\s*\(", re.IGNORECASE)


def _split_args(sql: str, start: int) -> tuple[list[str], int]:
    """sql[start:] is just inside a '('. Returns (top-level args, index just
    past the matching ')')."""
    args, depth, i, last = [], 0, start, start
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            i = _skip_string(sql, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                args.append(sql[last:i])
                return args, i + 1
            depth -= 1
        elif c == "," and depth == 0:
            args.append(sql[last:i])
            last = i + 1
        i += 1
    raise TranslationError("unbalanced parentheses")


def _rewrite_json(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = _skip_string(sql, i)
            out.append(sql[i:j])
            i = j
            continue
        m = _JSON_FUNC.match(sql, i)
        if m and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            name = m.group(1).upper()
            args, end = _split_args(sql, m.end())
            if name == "JSON_EXTRACT":
                if len(args) != 2:
                    raise TranslationError("JSON_EXTRACT with a path count other than one isn't supported")
                doc, path = (_rewrite_json(a).strip() for a in args)
                out.append(f"({doc} -> {path})")
            else:
                if len(args) != 1:
                    raise TranslationError("JSON_UNQUOTE takes one argument")
                inner = args[0].strip()
                im = _JSON_FUNC.match(inner)
                if im and im.group(1).upper() == "JSON_EXTRACT":
                    iargs, iend = _split_args(inner, im.end())
                    if iend == len(inner) and len(iargs) == 2:
                        doc, path = (_rewrite_json(a).strip() for a in iargs)
                        out.append(f"({doc} ->> {path})")
                        i = end
                        continue
                # Anything else falls through to the registered
                # JSON_UNQUOTE() function (see _json_unquote below).
                out.append(f"JSON_UNQUOTE({_rewrite_json(inner)})")
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


_UPSERT = re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.IGNORECASE)
_VALUES_FN = re.compile(r"\bVALUES\s*\(\s*(\w+)\s*\)", re.IGNORECASE)
_STRAIGHT_JOIN = re.compile(r"\bSELECT\s+STRAIGHT_JOIN\b", re.IGNORECASE)
_FORCE_INDEX = re.compile(r"\bFORCE\s+INDEX\s*\(\s*(\w+)\s*\)", re.IGNORECASE)


def _rewrite_upsert(sql: str) -> str:
    m = _UPSERT.search(sql)
    if not m:
        return sql
    # SQLite >= 3.35 allows the conflict target to be omitted on the last
    # ON CONFLICT clause -- same "any unique key" semantics as MariaDB.
    tail = _VALUES_FN.sub(r"excluded.\1", sql[m.end():])
    return sql[: m.start()] + "ON CONFLICT DO UPDATE SET" + tail


@functools.lru_cache(maxsize=4096)
def translate(sql: str, has_params: bool) -> str:
    if has_params:
        sql = _pyformat_to_qmark(sql)
    sql = _rewrite_json(sql)
    sql = _rewrite_upsert(sql)
    # FORCE INDEX's job (pin /events' unfiltered "recent activity" query to
    # walk idx_ts) maps onto SQLite's INDEXED BY; STRAIGHT_JOIN has no
    # equivalent and is just dropped.
    sql = _STRAIGHT_JOIN.sub("SELECT", sql)
    sql = _FORCE_INDEX.sub(r"INDEXED BY \1", sql)
    return sql


# --------------------------------------------------------------------------
# registered functions


def _now():
    return format_dt(datetime.now())


def _utc_timestamp():
    return format_dt(datetime.now(timezone.utc).replace(tzinfo=None))


def _json_unquote(v):
    if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# --------------------------------------------------------------------------
# connection / cursor


class Cursor:
    def __init__(self, conn: "Connection"):
        self._cur = conn.raw.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._cur.close()

    def execute(self, sql: str, params=None):
        if params is None:
            self._cur.execute(translate(sql, False))
        else:
            if isinstance(params, dict):
                raise TranslationError("named (%(name)s) parameters aren't supported")
            self._cur.execute(translate(sql, True), tuple(_adapt_in(p) for p in params))
        return self._cur.rowcount

    def executemany(self, sql: str, seq):
        self._cur.executemany(translate(sql, True), [tuple(_adapt_in(p) for p in params) for params in seq])
        return self._cur.rowcount

    def _row(self, row):
        if row is None:
            return None
        names = [d[0] for d in self._cur.description]
        return {k: _convert_out(v) for k, v in zip(names, row)}

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        return [self._row(r) for r in rows]

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description


class Connection:
    def __init__(self, path: str, cache_mb: int = 64, mmap_mb: int = 2048):
        # timeout = how long a writer waits for another writer's lock before
        # "database is locked" -- the tailer, importer and web app can all
        # write at once, and SQLite allows one writer at a time.
        self.raw = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.raw.create_function("NOW", 0, _now)
        self.raw.create_function("UTC_TIMESTAMP", 0, _utc_timestamp)
        self.raw.create_function("JSON_UNQUOTE", 1, _json_unquote, deterministic=True)
        for pragma in (
            "journal_mode=WAL",       # readers never block the writer or vice versa
            "synchronous=NORMAL",     # safe with WAL; only the last commit can be lost on power loss
            "foreign_keys=ON",        # InnoDB enforces them, so match it
            f"cache_size=-{cache_mb * 1024}",
            f"mmap_size={mmap_mb * 1024 * 1024}",
            "temp_store=MEMORY",
        ):
            self.raw.execute(f"PRAGMA {pragma}")

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()
