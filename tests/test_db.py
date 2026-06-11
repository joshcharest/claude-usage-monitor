"""Round-trip tests for the SQLite store against a temp database."""

from claude_usage_monitor import db
from claude_usage_monitor.db import Sample


def test_ingest_and_recent_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "test.db"))

    now = 10_000.0
    db.ingest(Sample(ts=now - 100, used_pct_5h=10.0, model="opus"))
    db.ingest(Sample(ts=now, used_pct_5h=20.0, model="opus"))

    rows = db.recent(window_seconds=200, now=now)
    assert len(rows) == 2
    assert rows[0]["used_pct_5h"] == 10.0  # oldest first
    assert rows[1]["used_pct_5h"] == 20.0
    assert rows[1]["model"] == "opus"


def test_recent_respects_window(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "test.db"))

    now = 10_000.0
    db.ingest(Sample(ts=now - 5000, used_pct_5h=5.0))  # outside 1000s window
    db.ingest(Sample(ts=now - 100, used_pct_5h=15.0))  # inside

    rows = db.recent(window_seconds=1000, now=now)
    assert len(rows) == 1
    assert rows[0]["used_pct_5h"] == 15.0


def test_schema_created_on_first_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "fresh.db"))
    rows = db.recent(window_seconds=1000, now=0.0)  # must not raise
    assert rows == []


def test_effort_and_fast_mode_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "test.db"))
    db.ingest(Sample(ts=1.0, effort="high", fast_mode=False))
    db.ingest(Sample(ts=2.0, effort="low", fast_mode=True))
    rows = db.recent(window_seconds=100, now=2.0)
    assert rows[0]["effort"] == "high"
    assert rows[0]["fast_mode"] == 0  # bool stored as int
    assert rows[1]["effort"] == "low"
    assert rows[1]["fast_mode"] == 1


def test_additive_columns_migrate_existing_db(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    # Simulate a pre-release DB without the additive columns.
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, ts REAL NOT NULL)")
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(path))
    conn = db.connect()  # must add missing columns without error
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(samples)")}
    conn.close()
    assert "effort" in cols
    assert "fast_mode" in cols
