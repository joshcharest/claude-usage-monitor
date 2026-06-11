"""Tests for the pure dashboard prep layer (no Streamlit needed).

Skipped entirely if the `dashboard` extra (pandas) isn't installed.
"""

import pytest

pytest.importorskip("pandas")

from claude_usage_monitor.config import load_config  # noqa: E402
from claude_usage_monitor.dashboard import prep  # noqa: E402


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


def test_pace_stack_sums_to_total_used():
    # Increments attributed to the active conversation; baseline -> (earlier).
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 10.0},  # baseline 10
        {"ts": 1001.0, "session_id": "s1", "used_pct": 20.0},  # +10 -> s1
        {"ts": 1002.0, "session_id": "s2", "used_pct": 35.0},  # +15 -> s2
        {"ts": 1003.0, "session_id": "s1", "used_pct": 40.0},  # +5  -> s1
    ]
    df = prep.pace_stack_frame(series, {"s1": "Chat one"})
    last_t = df["time"].max()
    stack_top = df[df["time"] == last_t]["contribution"].sum()
    assert stack_top == pytest.approx(40.0)  # == final used_pct
    convs = set(df["conversation"])
    assert "Chat one" in convs and "s2" in convs
    assert "(before monitoring)" in convs  # baseline band


def test_pace_stack_ignores_resets():
    # A drop (window reset / noise) must not subtract from the stack.
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 50.0},
        {"ts": 1001.0, "session_id": "s1", "used_pct": 30.0},  # drop -> ignored
        {"ts": 1002.0, "session_id": "s1", "used_pct": 40.0},  # +10 from 30
    ]
    df = prep.pace_stack_frame(series, {})
    last_t = df["time"].max()
    # baseline 50 + s1's positive increment 10 = 60 (drop ignored).
    assert df[df["time"] == last_t]["contribution"].sum() == pytest.approx(60.0)


def test_pace_stack_smooths_into_buckets():
    series = [
        {"ts": 1000.0 + i * 10, "session_id": "s1", "used_pct": 10.0 + i}
        for i in range(6)
    ]
    raw = prep.pace_stack_frame(series, {})
    bucketed = prep.pace_stack_frame(series, {}, bucket_seconds=60)
    assert bucketed["time"].nunique() < raw["time"].nunique()  # fewer time points


def test_pace_stack_empty():
    df = prep.pace_stack_frame([])
    assert list(df.columns) == ["time", "conversation", "contribution"]


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
