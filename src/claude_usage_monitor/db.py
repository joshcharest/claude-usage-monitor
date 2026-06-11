"""SQLite-backed store for usage samples.

The database lives OUTSIDE the (public) repo at ``~/.claude/budget.db`` so that
personal usage data is never committed. Override the path with the
``CLAUDE_BUDGET_DB`` environment variable (used by tests).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

_DEFAULT_DB = Path.home() / ".claude" / "budget.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    session_id    TEXT,
    model         TEXT,
    effort        TEXT,
    fast_mode     INTEGER,
    cost_usd      REAL,
    ctx_used_pct  REAL,
    used_pct_5h   REAL,
    resets_at_5h  INTEGER,
    used_pct_7d   REAL,
    resets_at_7d  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
"""

# Columns added after the initial release. Ensured on existing DBs so the schema
# can grow without a manual migration. (column_name -> SQLite type)
_ADDITIVE_COLUMNS = {
    "effort": "TEXT",
    "fast_mode": "INTEGER",
}


@dataclass
class Sample:
    """One usage observation captured from a statusline payload."""

    ts: float
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    fast_mode: bool | None = None
    cost_usd: float | None = None
    ctx_used_pct: float | None = None
    used_pct_5h: float | None = None
    resets_at_5h: int | None = None
    used_pct_7d: float | None = None
    resets_at_7d: int | None = None


def db_path() -> Path:
    """Resolve the database path, honoring ``CLAUDE_BUDGET_DB``."""
    override = os.environ.get("CLAUDE_BUDGET_DB")
    return Path(override) if override else _DEFAULT_DB


def connect() -> sqlite3.Connection:
    """Open the database, creating parent dir and schema on first use."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    # Guard on column presence so this never fails on an ancient DB that predates
    # the session_id column; current/new DBs always have it.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
    if "session_id" in cols:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id)")
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add any additive columns missing from a pre-existing ``samples`` table."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
    for column, sql_type in _ADDITIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {column} {sql_type}")
    conn.commit()


def ingest(sample: Sample, conn: sqlite3.Connection | None = None) -> None:
    """Insert one sample. Opens and closes its own connection if none given."""
    own = conn is None
    conn = conn or connect()
    try:
        fields = asdict(sample)
        cols = ", ".join(fields)
        placeholders = ", ".join(f":{k}" for k in fields)
        conn.execute(f"INSERT INTO samples ({cols}) VALUES ({placeholders})", fields)
        conn.commit()
    finally:
        if own:
            conn.close()


def recent(
    window_seconds: float, now: float, conn: sqlite3.Connection | None = None
) -> list[sqlite3.Row]:
    """Return samples with ``ts >= now - window_seconds``, oldest first."""
    own = conn is None
    conn = conn or connect()
    try:
        cutoff = now - window_seconds
        rows = conn.execute(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
        ).fetchall()
        return list(rows)
    finally:
        if own:
            conn.close()
