from pathlib import Path
from typing import Optional

from eq_log_suite.parsers.base import GameParser
from eq_log_suite.parsers.eq import EQParser
from eq_log_suite.parsers.eq_legends import EQLegendsParser

PARSERS: dict[str, type[GameParser]] = {
    EQLegendsParser.game_code: EQLegendsParser,
    EQParser.game_code: EQParser,
}


def get_parser(game_code: str) -> type[GameParser]:
    try:
        return PARSERS[game_code]
    except KeyError:
        raise ValueError(
            f"unknown game code {game_code!r}; known games: {sorted(PARSERS)}"
        )


def detect_parser(path: str, sample_lines: int = 200) -> Optional[type[GameParser]]:
    """Sniff which game a log file belongs to by scoring how many of its
    first `sample_lines` each registered parser can decompose into an
    event. Ties favor whichever parser is listed first in PARSERS."""
    lines = []
    with open(path, errors="replace") as f:
        for _ in range(sample_lines):
            line = f.readline()
            if not line:
                break
            lines.append(line)

    best_parser, best_score = None, -1
    for parser in PARSERS.values():
        score = sum(1 for line in lines if parser.parse_line(line) is not None)
        if score > best_score:
            best_parser, best_score = parser, score
    return best_parser if best_score > 0 else None


def game_for_log(path: str) -> Optional[type[GameParser]]:
    """Which game's parser reads this log, most certain first:
    1. the game it was already recorded as (a known file's game never
       changes -- sniffing can flip as the log grows: EQ Legends logs now
       contain task lines only the classic-EQ parser knows, which once
       out-scored it on a real log and recorded lines under the wrong game),
    2. the game whose configured log folder it's in,
    3. sniffing its first lines (detect_parser), for anything else."""
    import os
    from eq_log_suite import db
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT g.code FROM log_sources ls JOIN games g ON g.id=ls.game_id WHERE ls.file_path=%s",
                        (path,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row and row["code"] in PARSERS:
        return PARSERS[row["code"]]
    resolved = Path(path).resolve()
    for code, root in db.log_roots().items():
        try:
            if code in PARSERS and resolved.is_relative_to(Path(root).resolve()):
                return PARSERS[code]
        except OSError:
            continue
    return detect_parser(path) if os.path.isfile(path) and os.path.getsize(path) else None
