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


def test_current_reading_uses_fresh_max_not_zombie(env):
    now = 1_000_000.0
    # Frozen "zombie": high used %, but its window already reset (stale).
    db.ingest(Sample(ts=now, session_id="z", used_pct_5h=53.0,
                     resets_at_5h=int(now - 1000), used_pct_7d=53.0,
                     resets_at_7d=int(now - 1000), cost_usd=1.0))
    # Fresh low reading.
    db.ingest(Sample(ts=now + 1, session_id="s1", used_pct_5h=5.0,
                     resets_at_5h=int(now + 9000), used_pct_7d=8.0,
                     resets_at_7d=int(now + 200000), cost_usd=2.0, model="opus"))
    r = queries.current_reading(recent_seconds=60)
    assert r["used_pct_5h"] == 5.0  # fresh max, not the stale 53
    assert r["used_pct_7d"] == 8.0
    assert r["resets_at_5h"] == int(now + 9000)


def test_current_reading_falls_back_to_latest_when_all_stale(env):
    now = 1_000_000.0
    db.ingest(Sample(ts=now, session_id="z", used_pct_5h=40.0,
                     resets_at_5h=int(now - 1000)))
    r = queries.current_reading(recent_seconds=60)
    assert r is not None and r["used_pct_5h"] == 40.0  # graceful fallback


def test_current_reading_drops_frozen_zombie_after_reset(env):
    now = 1_000_000.0
    f = int(now + 200000)  # reset time did NOT advance (the observed real case)
    # Hung session: re-emits used=100 for >90s with fresh ts (passes freshness).
    for i in range(0, 130, 10):
        db.ingest(Sample(ts=now - 120 + i, session_id="zombie",
                         used_pct_7d=100.0, resets_at_7d=f, cost_usd=50.0))
    # Live session: was at 100, just reset, now climbing from a low base.
    db.ingest(Sample(ts=now - 25, session_id="live", used_pct_7d=100.0, resets_at_7d=f))
    db.ingest(Sample(ts=now - 5, session_id="live", used_pct_7d=2.0, resets_at_7d=f))
    db.ingest(Sample(ts=now - 1, session_id="live", used_pct_7d=3.0, resets_at_7d=f))
    r = queries.current_reading(recent_seconds=30, freeze_seconds=90)
    assert r["used_pct_7d"] == 3.0  # live latest, NOT the stuck zombie's 100
    assert r["resets_at_7d"] == f


def test_current_reading_prefers_leading_edge_on_climb(env):
    now = 1_000_000.0
    f = int(now + 200000)
    # No frozen session: max-of-fresh leading edge is preserved (old behavior).
    db.ingest(Sample(ts=now - 5, session_id="a", used_pct_5h=70.0, resets_at_5h=f))
    db.ingest(Sample(ts=now - 3, session_id="b", used_pct_5h=72.0, resets_at_5h=f))
    db.ingest(Sample(ts=now - 1, session_id="a", used_pct_5h=71.0, resets_at_5h=f))
    r = queries.current_reading(recent_seconds=30, freeze_seconds=90)
    assert r["used_pct_5h"] == 72.0  # leading edge, not a's lagging latest


def test_current_reading_consensus_when_all_frozen(env):
    now = 1_000_000.0
    f = int(now + 200000)
    # Idle after a reset: three live sessions flat-low, one stuck zombie at 100.
    # All "frozen" (constant) -> consensus median must ignore the lone zombie.
    for sid, val in [("l1", 2.0), ("l2", 2.0), ("l3", 2.0), ("z", 100.0)]:
        for i in range(0, 130, 10):
            db.ingest(Sample(ts=now - 120 + i, session_id=sid,
                             used_pct_7d=val, resets_at_7d=f))
    r = queries.current_reading(recent_seconds=30, freeze_seconds=90)
    assert r["used_pct_7d"] == 2.0


def test_current_reading_keeps_genuine_max_when_maxed(env):
    now = 1_000_000.0
    f = int(now + 200000)
    # Genuinely pinned at 100 (not a reset): every session agrees -> still 100.
    for sid in ("a", "b"):
        for i in range(0, 130, 10):
            db.ingest(Sample(ts=now - 120 + i, session_id=sid,
                             used_pct_7d=100.0, resets_at_7d=f))
    r = queries.current_reading(recent_seconds=30, freeze_seconds=90)
    assert r["used_pct_7d"] == 100.0


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


def test_overage_spend_sums_over_cap_deltas(env):
    now = 1_000_000.0
    f = int(now + 200000)  # future reset => fresh
    # cost is a per-session running total; only deltas while used>=100 count.
    db.ingest(Sample(ts=now + 1, session_id="s1", used_pct_7d=50.0, resets_at_7d=f, cost_usd=10.0))
    db.ingest(Sample(ts=now + 2, session_id="s1", used_pct_7d=100.0, resets_at_7d=f, cost_usd=12.0))  # +2 over
    db.ingest(Sample(ts=now + 3, session_id="s1", used_pct_7d=100.0, resets_at_7d=f, cost_usd=15.0))  # +3 over
    db.ingest(Sample(ts=now + 4, session_id="s1", used_pct_7d=30.0, resets_at_7d=f, cost_usd=17.0))  # under, ignored
    assert queries.overage_spend_usd("7d", now=now + 10) == pytest.approx(5.0)


def test_overage_ignores_stale_over_samples(env):
    now = 1_000_000.0
    # Over the cap but STALE (reset already past) -> a frozen zombie -> not counted.
    db.ingest(Sample(ts=now + 1, session_id="z", used_pct_7d=100.0, resets_at_7d=int(now - 1), cost_usd=10.0))
    db.ingest(Sample(ts=now + 2, session_id="z", used_pct_7d=100.0, resets_at_7d=int(now - 1), cost_usd=20.0))
    assert queries.overage_spend_usd("7d", now=now + 10) == pytest.approx(0.0)


def test_overage_attributes_deltas_per_session(env):
    now = 1_000_000.0
    f = int(now + 200000)
    # Interleaved sessions, both over: deltas must be per-session (LAG partitions),
    # never (b.cost - a.cost) across the interleave.
    db.ingest(Sample(ts=now + 1, session_id="a", used_pct_7d=100.0, resets_at_7d=f, cost_usd=100.0))
    db.ingest(Sample(ts=now + 2, session_id="b", used_pct_7d=100.0, resets_at_7d=f, cost_usd=5.0))
    db.ingest(Sample(ts=now + 3, session_id="a", used_pct_7d=100.0, resets_at_7d=f, cost_usd=103.0))  # a +3
    db.ingest(Sample(ts=now + 4, session_id="b", used_pct_7d=100.0, resets_at_7d=f, cost_usd=9.0))  # b +4
    assert queries.overage_spend_usd("7d", now=now + 10) == pytest.approx(7.0)


def test_overage_since_bounds_the_scan(env):
    now = 1_000_000.0
    f = int(now + 200000)
    db.ingest(Sample(ts=now - 100, session_id="s", used_pct_7d=100.0, resets_at_7d=f, cost_usd=10.0))
    db.ingest(Sample(ts=now - 99, session_id="s", used_pct_7d=100.0, resets_at_7d=f, cost_usd=50.0))  # +40 BEFORE since
    db.ingest(Sample(ts=now + 1, session_id="s", used_pct_7d=100.0, resets_at_7d=f, cost_usd=60.0))  # first in window: no prev
    db.ingest(Sample(ts=now + 2, session_id="s", used_pct_7d=100.0, resets_at_7d=f, cost_usd=63.0))  # +3
    assert queries.overage_spend_usd("7d", since=now, now=now + 10) == pytest.approx(3.0)


def test_overage_any_window_dedupes(env):
    now = 1_000_000.0
    f = int(now + 200000)
    db.ingest(Sample(ts=now + 1, session_id="s", used_pct_5h=100.0, resets_at_5h=f,
                     used_pct_7d=100.0, resets_at_7d=f, cost_usd=0.0))   # seed
    db.ingest(Sample(ts=now + 2, session_id="s", used_pct_5h=100.0, resets_at_5h=f,
                     used_pct_7d=100.0, resets_at_7d=f, cost_usd=10.0))  # +10, BOTH over
    db.ingest(Sample(ts=now + 3, session_id="s", used_pct_5h=10.0, resets_at_5h=f,
                     used_pct_7d=100.0, resets_at_7d=f, cost_usd=14.0))  # +4, 7d only
    db.ingest(Sample(ts=now + 4, session_id="s", used_pct_5h=10.0, resets_at_5h=f,
                     used_pct_7d=10.0, resets_at_7d=f, cost_usd=20.0))   # +6, neither
    # per-window:
    assert queries.overage_spend_usd("5h", now=now + 10) == pytest.approx(10.0)
    assert queries.overage_spend_usd("7d", now=now + 10) == pytest.approx(14.0)
    # "any" counts the both-over interval ONCE (10 + 4 = 14), not 10+14 = 24.
    assert queries.overage_spend_usd("any", now=now + 10) == pytest.approx(14.0)


def test_overage_degrades_when_budget_db_missing(env):
    assert not db.db_path().exists()
    assert queries.overage_spend_usd("7d", now=0.0) == 0.0
    assert not db.db_path().exists()  # read-only: must not create the db


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
