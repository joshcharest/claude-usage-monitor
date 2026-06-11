"""Tests for the pure dashboard prep layer (no Streamlit needed).

Skipped entirely if the `dashboard` extra (pandas) isn't installed.
"""

import pytest

pd = pytest.importorskip("pandas")

from datetime import datetime  # noqa: E402

from claude_usage_monitor.config import load_config  # noqa: E402
from claude_usage_monitor.dashboard import prep  # noqa: E402


def test_to_local_matches_system_wall_clock():
    epoch = 1_700_000_000.0
    got = prep.to_local([epoch])[0]
    # datetime.fromtimestamp (no tz) is local wall-clock; must match to the second.
    expected = pd.Timestamp(datetime.fromtimestamp(epoch))
    assert got == expected
    assert got.tzinfo is None  # naive, so charts render it as local


def test_usage_frame_uses_local_time():
    # A sample at a known epoch should appear at the local wall-clock, not UTC.
    rows = [{"ts": 1_700_000_000.0, "used_pct_5h": 10.0}]
    df = prep.usage_frame(rows)
    expected = pd.Timestamp(datetime.fromtimestamp(1_700_000_000.0))
    assert df["time"].iloc[0] == expected


def test_fmt_duration():
    assert prep.fmt_duration(None) == "?"
    assert prep.fmt_duration(-5) == "?"
    assert prep.fmt_duration(90) == "1m"
    assert prep.fmt_duration(3700) == "1h1m"
    assert prep.fmt_duration(2 * 86400 + 3 * 3600) == "2d3h"


def test_window_position_halfway():
    now = 1_000_000.0
    win = 18000.0
    pos = prep.window_position(now + win / 2, win, now)  # half remaining => half elapsed
    assert pos["elapsed_frac"] == pytest.approx(0.5)
    assert pos["remaining_secs"] == pytest.approx(win / 2)


def test_window_position_unknown_reset():
    pos = prep.window_position(None, 18000.0, 0.0)
    assert pos["elapsed_frac"] is None


def test_controls_window_seconds():
    assert prep.Controls(range_label="5h").window_seconds == 18000.0
    assert prep.Controls(range_label="All").window_seconds is None


def test_build_kpis_empty():
    kpis = prep.build_kpis(None, load_config(), now=0.0)
    assert [k.label for k in kpis][:2] == ["5h used", "7d used"]
    assert all(k.value in ("—", "no samples yet") for k in kpis)


def test_build_kpis_with_sample():
    config = load_config()
    now = 1_000_000.0
    latest = {
        "used_pct_5h": 30.0,
        "resets_at_5h": now + 18000 / 2,  # half elapsed -> projects 60%
        "used_pct_7d": 40.0,
        "resets_at_7d": now + 604800 / 2,
        "cost_usd": 12.34,
        "model": "claude-opus-4-8",
        "effort": "high",
    }
    kpis = {k.label: k for k in prep.build_kpis(latest, config, now)}
    assert kpis["5h used"].value == "30%"
    assert kpis["Projected 5h"].value == "60%"
    assert kpis["Session $"].value == "$12.34"
    assert "claude-opus-4-8" in kpis["Active"].value


def test_usage_frame_long_format():
    rows = [{"ts": 1000.0, "used_pct_5h": 10.0, "used_pct_7d": 40.0},
            {"ts": 1100.0, "used_pct_5h": 12.0, "used_pct_7d": None}]
    df = prep.usage_frame(rows)
    assert set(df["window"]) == {"5h", "7d"}
    assert len(df) == 3  # 2 5h + 1 7d (the None is dropped)


def test_pace_active_frame_tracks_actual_not_overcounted():
    # A rolling, oscillating used_pct: the chart must follow the actual value,
    # never a cumulative sum of the up-moves.
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 50.0},
        {"ts": 1001.0, "session_id": "s1", "used_pct": 20.0},  # drops (ages out)
        {"ts": 1002.0, "session_id": "s1", "used_pct": 55.0},  # rises again
    ]
    df = prep.pace_active_frame(series, {"s1": "Chat one"})
    assert df["used_pct"].max() == 55.0  # not 50+35=85 overcount
    assert set(df["conversation"]) == {"Chat one"}


def test_pace_active_frame_bucket_dominant_and_mean():
    # In one 60s bucket, s1 has more samples than s2 -> bucket colored s1.
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 10.0},
        {"ts": 1005.0, "session_id": "s1", "used_pct": 12.0},
        {"ts": 1010.0, "session_id": "s2", "used_pct": 14.0},
    ]
    df = prep.pace_active_frame(series, {"s1": "one"}, bucket_seconds=60)
    assert len(df) == 1  # one 60s bucket
    assert df["conversation"].iloc[0] == "one"  # dominant (2 of 3 samples)
    assert df["used_pct"].iloc[0] == pytest.approx(12.0)  # mean of 10,12,14


def test_pace_active_frame_empty():
    df = prep.pace_active_frame([])
    assert list(df.columns) == ["time", "conversation", "used_pct"]


def test_pace_bucket_seconds():
    assert prep.pace_bucket_seconds(15000, [], target_points=150) == 100.0
    # floor at 30s for tiny ranges
    assert prep.pace_bucket_seconds(100, []) == 30.0
    # derive span from series when no window given
    series = [{"ts": 0.0}, {"ts": 30000.0}]
    assert prep.pace_bucket_seconds(None, series, target_points=150) == 200.0


def test_conversations_frame_columns():
    rows = [{"session_id": "s1", "title": "hi", "started_at": 1000.0,
             "message_count": 5, "cost_usd": 1.0}]
    df = prep.conversations_frame(rows)
    assert list(df.columns)[0] == "title"
    assert "session_id" in df.columns
    assert df["title"].iloc[0] == "hi"


def test_empty_frames_have_columns():
    assert list(prep.usage_frame([]).columns) == ["time", "window", "used_pct"]
    assert list(prep.model_frame([]).columns) == ["model", "messages", "sessions"]
    assert list(prep.conversations_frame([]).columns)[0] == "title"
