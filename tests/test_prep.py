"""Tests for the pure dashboard prep layer (no Streamlit needed).

Skipped entirely if the `dashboard` extra (pandas) isn't installed.
"""

import pytest

pd = pytest.importorskip("pandas")

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from claude_usage_monitor.config import load_config  # noqa: E402
from claude_usage_monitor.dashboard import prep  # noqa: E402


def test_month_start_epoch():
    tz = ZoneInfo("America/New_York")
    # 2026-06-13 12:34:56 in NY -> month start is 2026-06-01 00:00 NY.
    now = datetime(2026, 6, 13, 12, 34, 56, tzinfo=tz).timestamp()
    start = prep.month_start_epoch(now, tz=tz)
    assert datetime.fromtimestamp(start, tz) == datetime(2026, 6, 1, 0, 0, tzinfo=tz)


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


_KPI_LABELS = [
    "Time Left", "5 Hour Used", "5 Hour Projected",
    "Time Left", "7 Day Used", "7 Day Projected", "Monthly Overage",
]


def test_build_kpis_empty():
    kpis = prep.build_kpis(None, load_config(), now=0.0)
    assert [k.label for k in kpis] == _KPI_LABELS
    assert all(k.value == "—" for k in kpis)
    assert all(k.status is None for k in kpis)
    assert all(k.progress is None for k in kpis)  # no bars without data


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
    kpis = prep.build_kpis(latest, config, now, overage_usd=42.5)
    # Each window trio is [Time Left, Used, Projected]; then the monthly tile.
    assert [k.label for k in kpis] == _KPI_LABELS
    assert kpis[0].value == "2h30m"    # Time Left until 5h reset (9000 s)
    assert kpis[1].value == "30%"      # 5 Hour Used
    assert kpis[2].value == "60%"      # 5 Hour Projected (half-elapsed -> 60%)
    assert kpis[2].status == "ok"      # 60% < warn 90% -> on pace (green tile)
    assert kpis[4].value == "40%"      # 7 Day Used
    assert kpis[6].value == "$42.50"   # Monthly Overage
    # Groups drive the separators: 5h trio, 7d trio, then the monthly tile.
    assert [k.group for k in kpis] == ["5h", "5h", "5h", "7d", "7d", "7d", "other"]
    # Gauge bars: window-elapsed on the Time Left tiles, used % on the Used tiles.
    assert kpis[0].progress == pytest.approx(0.50)  # 5h Time Left (half elapsed)
    assert kpis[1].progress == pytest.approx(0.30)  # 5 Hour Used
    assert kpis[3].progress == pytest.approx(0.50)  # 7d Time Left (half elapsed)
    assert kpis[4].progress == pytest.approx(0.40)  # 7 Day Used
    # Projected and monthly tiles stay text-only (no bar).
    assert kpis[2].progress is None and kpis[6].progress is None


def test_build_kpis_layered_with_recent():
    # Passing a trailing fresh series under the layered model produces a layered
    # projection while keeping the exact same 7-tile shape/labels.
    now = 2_000_000.0
    reset = now + 9000.0  # 50% elapsed in the 5h window
    config = {
        "alerts": {"warn_projected_pct": 90.0, "over_limit_pct": 100.0},
        "forecast": {
            "model": "layered",
            "window_5h_seconds": 18000,
            "window_7d_seconds": 604800,
            "slope_window_seconds": 1800,
            "slope_bucket_seconds": 30,
            "slope_min_points": 4,
            "burst_horizon_seconds": 18000,
            "idle_seconds": 100000,
        },
    }
    recent = []
    for i in range(12):
        ts = now - 1800 + (1800 * i / 11)
        used = 40.0 + 0.002 * (ts - (now - 1800))
        recent.append({"ts": ts, "session_id": "s1", "used_pct_5h": used,
                       "resets_at_5h": reset, "cost_usd": 1.0 + i})
    latest = {"used_pct_5h": recent[-1]["used_pct_5h"], "resets_at_5h": reset,
              "used_pct_7d": 40.0, "resets_at_7d": now + 604800 / 2}
    kpis = prep.build_kpis(latest, config, now, overage_usd=0.0, recent=recent)
    # Same 7-tile shape and groups as the ratio path.
    assert [k.label for k in kpis] == _KPI_LABELS
    assert [k.group for k in kpis] == ["5h", "5h", "5h", "7d", "7d", "7d", "other"]
    # The 5h Projected tile shows a real layered projection at/above the used %.
    assert kpis[2].value != "—"
    # The layered band is surfaced in the Projected tile's tooltip (low–high range).
    assert kpis[2].help is not None and "likely range" in kpis[2].help


def test_build_kpis_without_recent_stays_ratio():
    # No `recent` -> ratio fallback -> exact legacy numbers (regression anchor).
    config = load_config()
    now = 1_000_000.0
    latest = {"used_pct_5h": 30.0, "resets_at_5h": now + 18000 / 2,
              "used_pct_7d": 40.0, "resets_at_7d": now + 604800 / 2}
    kpis = prep.build_kpis(latest, config, now)
    assert kpis[2].value == "60%"  # half-elapsed ratio doubles 30 -> 60


def test_build_kpis_projected_status_thresholds():
    config = load_config()
    now = 1_000_000.0

    def proj_status(used_5h):
        # half-elapsed 5h window -> projected = used / 0.5 = 2 * used
        latest = {"used_pct_5h": used_5h, "resets_at_5h": now + 18000 / 2}
        return prep.build_kpis(latest, config, now)[2].status  # 5 Hour Projected

    assert proj_status(30.0) == "ok"     # proj 60%  < warn 90
    assert proj_status(47.0) == "warn"   # proj 94%  in [90, 100)
    assert proj_status(60.0) == "over"   # proj 120% >= 100


def test_usage_frame_long_format():
    rows = [{"ts": 1000.0, "used_pct_5h": 10.0, "used_pct_7d": 40.0},
            {"ts": 1100.0, "used_pct_5h": 12.0, "used_pct_7d": None}]
    df = prep.usage_frame(rows)
    assert set(df["window"]) == {"5h", "7d"}
    assert len(df) == 3  # 2 5h + 1 7d (the None is dropped)


def test_pace_share_stack_uses_peak_not_overcounted_or_diluted():
    # One bucket: stack top = the PEAK used % (not a cumulative overcount, and
    # not the mean which stale-low interleaved snapshots would deflate).
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 50.0},
        {"ts": 1001.0, "session_id": "s1", "used_pct": 20.0},  # stale-low snapshot
        {"ts": 1002.0, "session_id": "s1", "used_pct": 56.0},  # true current
    ]
    df = prep.pace_share_frame(series, {"s1": "br/one"}, bucket_seconds=60)
    assert df["share"].sum() == pytest.approx(56.0)  # peak, not 42 (mean) or 126
    assert set(df["conversation"]) == {"br/one"}


def test_pace_share_cost_weighted():
    # One bucket's used% is split by $ spent: s1 +$3, s2 +$1 -> 3:1 of 40%.
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 40.0, "cost_usd": 10.0},
        {"ts": 1.0, "session_id": "s2", "used_pct": 40.0, "cost_usd": 20.0},
        {"ts": 70.0, "session_id": "s1", "used_pct": 40.0, "cost_usd": 13.0},  # +3
        {"ts": 71.0, "session_id": "s2", "used_pct": 40.0, "cost_usd": 21.0},  # +1
    ]
    df = prep.pace_share_frame(series, {"s1": "a", "s2": "b"}, bucket_seconds=60)
    last = df[df["time"] == df["time"].max()]
    shares = dict(zip(last["conversation"], last["share"]))
    assert shares["a"] == pytest.approx(30.0)  # 40 * 3/4
    assert shares["b"] == pytest.approx(10.0)  # 40 * 1/4


def test_pace_share_equal_split_without_cost():
    # No cost movement -> fall back to an equal split (not poll-count).
    series = [
        {"ts": 1000.0, "session_id": "s1", "used_pct": 30.0},
        {"ts": 1005.0, "session_id": "s1", "used_pct": 30.0},  # s1 polled twice
        {"ts": 1010.0, "session_id": "s2", "used_pct": 30.0},
    ]
    df = prep.pace_share_frame(series, {"s1": "a", "s2": "b"}, bucket_seconds=60)
    shares = dict(zip(df["conversation"], df["share"]))
    assert shares["a"] == pytest.approx(15.0)  # equal, NOT 20 (2/3 by count)
    assert shares["b"] == pytest.approx(15.0)


def test_pace_share_drops_stale_high_zombie():
    # A frozen session reporting used%=53 with a reset_at in the PAST must be
    # dropped before the max — the stack reflects the fresh 5%, not 53%.
    series = [
        {"ts": 1000.0, "session_id": "z", "used_pct": 53.0, "reset_at": 100.0},
        {"ts": 1001.0, "session_id": "s1", "used_pct": 5.0, "reset_at": 5000.0},
    ]
    df = prep.pace_share_frame(series, {"z": "zombie", "s1": "live"}, bucket_seconds=60)
    assert df["share"].sum() == pytest.approx(5.0)  # not 53
    assert "zombie" not in set(df["conversation"])


def test_pace_share_frame_empty():
    df = prep.pace_share_frame([])
    assert list(df.columns) == ["time", "conversation", "share"]


def test_usage_accumulation_rolling_fill_no_latch():
    # Used % rises then falls; the chart tracks the actual rolling fill (no
    # high-water latch), so the last bucket shows 20, not a held 30.
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 10.0},
        {"ts": 70.0, "session_id": "s1", "used_pct": 30.0},
        {"ts": 140.0, "session_id": "s1", "used_pct": 20.0},  # drops back
    ]
    df = prep.usage_accumulation_frame(series, {"s1": "main"}, bucket_seconds=60)
    last_t = df["time"].max()
    assert df[df["time"] == last_t]["used_pct"].sum() == pytest.approx(20.0)


def test_usage_accumulation_drops_stale_high():
    series = [
        {"ts": 1000.0, "session_id": "z", "used_pct": 67.0, "reset_at": 100.0},  # stale
        {"ts": 1001.0, "session_id": "s1", "used_pct": 4.0, "reset_at": 5000.0},  # fresh
    ]
    df = prep.usage_accumulation_frame(series, {}, bucket_seconds=60)
    assert df["used_pct"].sum() == pytest.approx(4.0)  # not 67


def test_usage_accumulation_resets_at_window_reset():
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 50.0},   # bucket 0
        {"ts": 70.0, "session_id": "s1", "used_pct": 10.0},  # bucket 1 (after reset)
    ]
    reset = prep.to_local([35.0])[0]  # reset between the two buckets
    df = prep.usage_accumulation_frame(series, {}, bucket_seconds=60, reset_times=[reset])
    last_t = df["time"].max()
    # New window after the reset -> high-water restarts at 10, not held at 50.
    assert df[df["time"] == last_t]["used_pct"].sum() == pytest.approx(10.0)


def test_usage_accumulation_current_window_only():
    # Two windows in range; current_window_only keeps just the latest segment.
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 50.0},   # bucket 0, pre-reset
        {"ts": 70.0, "session_id": "s1", "used_pct": 10.0},  # bucket 1, current window
    ]
    reset = prep.to_local([35.0])[0]
    df = prep.usage_accumulation_frame(
        series, {}, bucket_seconds=60, reset_times=[reset], current_window_only=True
    )
    # Only the current (post-reset) window is shown — the pre-reset bucket is dropped.
    assert df["time"].nunique() == 1
    assert df["used_pct"].sum() == pytest.approx(10.0)


def test_usage_accumulation_ignores_future_reset_boundary():
    # A flaky/advanced resets_at can yield a reset boundary AFTER all the data.
    # It must not be used as the current-window start (that would clip everything
    # away and blank the chart). Fall back to the most recent PAST reset.
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 50.0},   # pre-reset
        {"ts": 70.0, "session_id": "s1", "used_pct": 10.0},  # current window
    ]
    past = prep.to_local([35.0])[0]
    future = prep.to_local([10_000.0])[0]  # well beyond the data
    df = prep.usage_accumulation_frame(
        series, {}, bucket_seconds=60, reset_times=[past, future],
        current_window_only=True, window_seconds=18000.0,
    )
    assert not df.empty
    assert df["time"].nunique() == 1
    assert df["used_pct"].sum() == pytest.approx(10.0)


def test_usage_accumulation_cumulative_monotonic():
    # used% dips (rolling/aging), but cumulative=True must only climb.
    series = [
        {"ts": 0.0, "session_id": "s1", "used_pct": 20.0, "cost_usd": 1.0},
        {"ts": 70.0, "session_id": "s1", "used_pct": 50.0, "cost_usd": 2.0},
        {"ts": 140.0, "session_id": "s1", "used_pct": 35.0, "cost_usd": 3.0},  # dip
        {"ts": 210.0, "session_id": "s1", "used_pct": 60.0, "cost_usd": 4.0},
    ]
    df = prep.usage_accumulation_frame(series, {}, bucket_seconds=60, cumulative=True)
    total = df.groupby("time")["used_pct"].sum().sort_index()
    # running max -> non-decreasing, and the dip (35) is lifted to the prior peak.
    assert list(total) == pytest.approx([20.0, 50.0, 50.0, 60.0])
    assert (total.diff().dropna() >= 0).all()


def test_usage_accumulation_empty():
    df = prep.usage_accumulation_frame([])
    assert list(df.columns) == ["time", "conversation", "used_pct"]


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


def test_pace_reset_times_skips_stale_past_snapshots():
    H = 3600.0
    series = [
        {"ts": 100 * H, "reset_at": 50 * H},      # stale: reset_at in the past
        {"ts": 100 * H + 10, "reset_at": 105 * H},  # valid future -> max=105h
        {"ts": 100 * H + 20, "reset_at": 110 * H},  # +5h -> RESET at 105h
    ]
    resets = prep.pace_reset_times(series, min_advance_seconds=3600)
    assert len(resets) == 1  # the stale 50h is NOT marked
    assert resets[0] == pd.Timestamp(datetime.fromtimestamp(105 * H))


def test_pace_smooth_buckets_targets_fixed_time():
    # ~10 min target: window in buckets ~ 600s / bucket size.
    assert prep.pace_smooth_buckets(100.0) == 6     # 600/100
    assert prep.pace_smooth_buckets(60.0) == 10     # 600/60
    assert prep.pace_smooth_buckets(1000.0) == 1    # coarse buckets -> minimal
    assert prep.pace_smooth_buckets(0) == 1         # guard


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
