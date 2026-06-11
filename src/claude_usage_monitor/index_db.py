"""SQLite store for the transcript-derived conversation index.

This is a SEPARATE database from ``budget.db`` so that re-indexing (large batch
writes) never contends for the write lock with the latency-sensitive statusline
writer. It is a rebuildable derived cache — deleting ``~/.claude/usage-index.db``
forces a clean rebuild without risking the irreplaceable ``samples`` history.

Override the path with the ``CLAUDE_USAGE_INDEX_DB`` environment variable
(used by tests). Mirrors the conventions in :mod:`claude_usage_monitor.db`.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DEFAULT_DB = Path.home() / ".claude" / "usage-index.db"

_SCHEMA = """
-- One row per transcript file we've parsed; drives incremental re-indexing.
CREATE TABLE IF NOT EXISTS index_files (
    path          TEXT PRIMARY KEY,
    session_id    TEXT,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    offset_bytes  INTEGER NOT NULL,
    last_indexed  REAL    NOT NULL,
    parse_error   TEXT
);

-- One row per conversation/session.
CREATE TABLE IF NOT EXISTS conversations (
    session_id      TEXT PRIMARY KEY,
    title           TEXT,
    cwd             TEXT,
    repo            TEXT,
    git_branch      TEXT,
    slug            TEXT,
    started_at      REAL,
    ended_at        REAL,
    duration_secs   REAL,
    active_secs     REAL,
    message_count   INTEGER,
    sidechain_count INTEGER,
    turn_count      INTEGER,
    models_csv      TEXT,
    version         TEXT,
    cost_usd        REAL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_started ON conversations(started_at);
CREATE INDEX IF NOT EXISTS idx_conv_repo ON conversations(repo);

-- Per-session per-model rollup (reliable counts, not token-based).
CREATE TABLE IF NOT EXISTS model_usage (
    session_id    TEXT NOT NULL,
    model         TEXT NOT NULL,
    is_sidechain  INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL,
    first_ts      REAL,
    last_ts       REAL,
    PRIMARY KEY (session_id, model, is_sidechain)
);
CREATE INDEX IF NOT EXISTS idx_model_usage_model ON model_usage(model);

-- Schema version + a generation counter bumped on every write, so UI caches
-- can invalidate exactly when the index changes.
CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the initial release, ensured on existing DBs without a
# manual migration. (table -> {column -> SQLite type})
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {}


def index_db_path() -> Path:
    """Resolve the index database path, honoring ``CLAUDE_USAGE_INDEX_DB``."""
    override = os.environ.get("CLAUDE_USAGE_INDEX_DB")
    return Path(override) if override else _DEFAULT_DB


def connect() -> sqlite3.Connection:
    """Open the index DB, creating parent dir and schema on first use."""
    path = index_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while the indexer writes; safe for a cache.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add any additive columns missing from pre-existing tables."""
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO index_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bump_generation(conn: sqlite3.Connection) -> int:
    """Increment and return the generation counter (UI cache-busting key)."""
    current = int(get_meta(conn, "generation", "0") or "0")
    nxt = current + 1
    set_meta(conn, "generation", str(nxt))
    return nxt
