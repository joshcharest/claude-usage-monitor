"""Tests for the read-only dashboard query API and its two-tier merge."""

import pytest

from claude_usage_monitor import db, index_db, queries
from claude_usage_monitor.db import Sample


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "budget.db"))
    return tmp_path


def _add_conversation(session_id, title="t", repo=None, started=1000.0, models="claude-opus-4-8"):
    conn = index_db.connect()
    conn.execute(
        """INSERT INTO conversations
               (session_id, title, repo, started_at, ended_at, duration_secs,
                message_count, models_csv, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (session_id, title, repo, started, started + 100, 100, 10, models, 0.0),
    )
    conn.commit()
    conn.close()


def test_list_conversations_merges_samples(env):
    _add_conversation("s1", title="With samples")
    _add_conversation("s2", title="No samples")
    db.ingest(Sample(ts=1000.0, session_id="s1", used_pct_5h=20.0, used_pct_7d=40.0))
    db.ingest(Sample(ts=1100.0, session_id="s1", used_pct_5h=25.0, used_pct_7d=42.0))

    rows = {r["session_id"]: r for r in queries.list_conversations()}
    assert rows["s1"]["peak_5h"] == 25.0
    assert rows["s1"]["peak_7d"] == 42.0
    assert rows["s1"]["sample_count"] == 2
    # s2 still appears (conversations are the spine), just without sample aggs.
    assert "s2" in rows
    assert "peak_5h" not in rows["s2"]


def test_list_conversations_filters(env):
    _add_conversation("s1", title="alpha", repo="katomed/kato", started=2000.0)
    _add_conversation("s2", title="beta", repo="me/other", started=1000.0)
    assert [r["session_id"] for r in queries.list_conversations(repo="katomed/kato")] == ["s1"]
    assert [r["session_id"] for r in queries.list_conversations(search="bet")] == ["s2"]
    # default order is started_desc
    assert [r["session_id"] for r in queries.list_conversations()] == ["s1", "s2"]


def test_pace_timeseries_matches_forecast(env):
    _add_conversation("s1")
    # halfway through the 7d window, 30% used -> projected 60%.
    t = 1_000_000.0
    db.ingest(Sample(ts=t, session_id="s1", used_pct_7d=30.0,
                     resets_at_7d=int(t + 604800 / 2)))
    series = queries.pace_timeseries("7d", None, now=t)
    assert len(series) == 1
    assert series[0]["projected_pct"] == pytest.approx(60.0)
    assert series[0]["on_pace"] is True


def test_model_breakdown(env):
    conn = index_db.connect()
    conn.executemany(
        "INSERT INTO model_usage (session_id, model, is_sidechain, message_count) VALUES (?,?,?,?)",
        [("s1", "claude-opus-4-8", 0, 10),
         ("s2", "claude-opus-4-8", 0, 5),
         ("s1", "claude-haiku-4-5", 1, 3)],  # sidechain, excluded by default
    )
    conn.commit()
    conn.close()
    rows = queries.model_breakdown()
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-opus-4-8"
    assert rows[0]["messages"] == 15
    assert rows[0]["sessions"] == 2
    # include sidechain
    assert len(queries.model_breakdown(include_sidechain=True)) == 2


def test_effort_breakdown(env):
    db.ingest(Sample(ts=1.0, session_id="s1", effort="high", fast_mode=False))
    db.ingest(Sample(ts=2.0, session_id="s1", effort="high", fast_mode=True))
    db.ingest(Sample(ts=3.0, session_id="s1", effort="low", fast_mode=False))
    rows = {r["effort"]: r for r in queries.effort_breakdown()}
    assert rows["high"]["samples"] == 2
    assert rows["high"]["fast_samples"] == 1
    assert rows["low"]["samples"] == 1


def test_latest_sample(env):
    db.ingest(Sample(ts=1.0, session_id="s1", used_pct_5h=10.0))
    db.ingest(Sample(ts=5.0, session_id="s1", used_pct_5h=30.0))
    latest = queries.latest_sample()
    assert latest["ts"] == 5.0
    assert latest["used_pct_5h"] == 30.0


def test_degrades_when_budget_db_missing(env):
    # Seed only the index; never create budget.db.
    _add_conversation("s1", title="only index")
    assert not db.db_path().exists()
    rows = queries.list_conversations()
    assert [r["session_id"] for r in rows] == ["s1"]
    assert "peak_5h" not in rows[0]
    assert queries.latest_sample() is None
    assert queries.usage_timeseries(None, now=0.0) == []
    assert queries.effort_breakdown() == []
    # budget.db must not have been created by read-only queries.
    assert not db.db_path().exists()


def test_index_status(env):
    _add_conversation("s1")
    status = queries.index_status()
    assert status["conversations"] == 1
