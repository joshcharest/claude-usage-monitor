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
