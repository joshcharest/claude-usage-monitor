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


def test_pace_share_stack_sums_to_actual_used_not_overcounted():
    # Rolling, oscillating used_pct in one bucket: the stack top (sum of shares)
    # must equal the bucket's actual mean used %, never a cumulative overcount.
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 50.0},
        {"ts": 1001.0, "session_id": "s1", "used_pct": 20.0},  # drops (ages out)
        {"ts": 1002.0, "session_id": "s1", "used_pct": 56.0},  # rises again
    ]
    df = prep.pace_share_frame(series, {"s1": "br/one"}, bucket_seconds=60)
    assert df["share"].sum() == pytest.approx(42.0)  # mean(50,20,56), not 50+36
    assert set(df["conversation"]) == {"br/one"}


def test_pace_share_proportional_allocation():
    # One 60s bucket, s1 has 2 samples and s2 has 1 -> shares split 2:1 of mean.
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 30.0},
        {"ts": 1005.0, "session_id": "s1", "used_pct": 30.0},
        {"ts": 1010.0, "session_id": "s2", "used_pct": 30.0},
    ]
    df = prep.pace_share_frame(series, {"s1": "one", "s2": "two"}, bucket_seconds=60)
    shares = dict(zip(df["conversation"], df["share"]))
    assert shares["one"] == pytest.approx(20.0)  # 30 * 2/3
    assert shares["two"] == pytest.approx(10.0)  # 30 * 1/3
    assert sum(shares.values()) == pytest.approx(30.0)  # stack top = used %


def test_pace_share_frame_empty():
    df = prep.pace_share_frame([])
    assert list(df.columns) == ["time", "conversation", "share"]


def test_pace_share_smoothing_reduces_spikes():
    # One conversation with a single-bucket spike (0,0,90,0,0). A centered
    # rolling mean spreads the spike, lowering its peak.
    series = []
    for i, used in enumerate([0.0, 0.0, 90.0, 0.0, 0.0]):
        series.append({"ts": 1000.0 + i * 60, "session_id": "s1", "used_pct": used})
    raw = prep.pace_share_frame(series, {"s1": "br"}, bucket_seconds=60, smooth_buckets=1)
    smooth = prep.pace_share_frame(series, {"s1": "br"}, bucket_seconds=60, smooth_buckets=3)
    assert raw["share"].max() == pytest.approx(90.0)
    assert smooth["share"].max() < 90.0  # spike spread out
    # total area is conserved within rounding (mean smoothing)
    assert smooth["share"].sum() == pytest.approx(raw["share"].sum(), abs=1e-6)


def test_pace_reset_times_detects_real_reset_ignores_jitter():
    H = 3600.0
    series = [
        {"ts": 0.0, "reset_at": 14 * H},        # first window, max=14h
        {"ts": 100.0, "reset_at": 14 * H},       # unchanged
        {"ts": 200.0, "reset_at": 19 * H},       # +5h advance -> RESET at 14h
        {"ts": 300.0, "reset_at": 19 * H + 600}, # +10min jitter -> not a reset
        {"ts": 400.0, "reset_at": 14 * H},       # stale snapshot (< max) -> ignored
        {"ts": 500.0, "reset_at": 19 * H},       # back to max -> ignored
    ]
    resets = prep.pace_reset_times(series, min_advance_seconds=3600)
    assert len(resets) == 1
    assert resets[0] == pd.Timestamp(datetime.fromtimestamp(14 * H))


def test_pace_reset_times_none():
    assert prep.pace_reset_times([]) == []
    # Only jitter, never a real block change.
    series = [{"ts": 0.0, "reset_at": 100.0}, {"ts": 1.0, "reset_at": 200.0}]
    assert prep.pace_reset_times(series, min_advance_seconds=3600) == []


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
