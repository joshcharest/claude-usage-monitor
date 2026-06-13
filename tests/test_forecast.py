"""Unit tests for the pure budget-burndown forecaster."""

import pytest

from claude_usage_monitor.config import load_config
from claude_usage_monitor.forecast import (
    ForecastCfg,
    _recent_rate_pp_per_sec,
    forecast_for_window,
    forecast_from_period_start,
    forecast_window,
    model_is_layered,
)

WINDOW_7D = 604800.0
WINDOW_5H = 18000.0


def test_period_start_halfway_projects_double():
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2  # half remaining => half elapsed
    fc = forecast_from_period_start(30.0, WINDOW_7D, resets, now)
    assert abs(fc.projected_pct - 60.0) < 1e-6
    assert fc.on_pace is True


def test_period_start_overflow_not_on_pace():
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2  # halfway through
    fc = forecast_from_period_start(60.0, WINDOW_7D, resets, now)
    assert abs(fc.projected_pct - 120.0) < 1e-6
    assert fc.on_pace is False


def test_period_start_quarter_elapsed():
    now = 1_000_000.0
    resets = now + WINDOW_7D * 0.75  # 25% elapsed
    fc = forecast_from_period_start(10.0, WINDOW_7D, resets, now)
    assert abs(fc.projected_pct - 40.0) < 1e-6  # 10% / 0.25
    assert fc.on_pace is True


def test_period_start_just_opened_is_unknown():
    now = 1_000_000.0
    resets = now + WINDOW_7D  # full window remaining => elapsed 0
    fc = forecast_from_period_start(5.0, WINDOW_7D, resets, now)
    assert fc.on_pace is None
    assert fc.projected_pct == 5.0


def test_period_start_early_window_holds_no_runaway():
    # <20% elapsed: a rolling metric must NOT be extrapolated from a tiny
    # denominator. Hold at current and report pace as unknown.
    now = 1_000_000.0
    resets = now + WINDOW_7D * 0.999  # 0.1% elapsed
    fc = forecast_from_period_start(1.0, WINDOW_7D, resets, now, cap=999.0)
    assert fc.projected_pct == 1.0  # held, not 999
    assert fc.on_pace is None


def test_period_start_past_reset_holds_and_unknown():
    now = 1_000_000.0
    resets = now - 100  # already past reset (stale snapshot)
    fc = forecast_from_period_start(80.0, WINDOW_7D, resets, now)
    assert fc.projected_pct == 80.0  # held at current
    assert fc.on_pace is None  # not extrapolated


def test_period_start_nan_used_is_none():
    fc = forecast_from_period_start(float("nan"), WINDOW_7D, 1_000_000.0, 990_000.0)
    assert fc.current_pct is None
    assert fc.projected_pct is None
    assert fc.on_pace is None


def test_period_start_none_used_is_none():
    fc = forecast_from_period_start(None, WINDOW_7D, 1_000_000.0, 999_000.0)
    assert fc.current_pct is None
    assert fc.projected_pct is None
    assert fc.on_pace is None


# ===========================================================================
# Layered model — forecast_window
# ===========================================================================


def _climb(now, secs_to_reset, n, span, start, rate_pp_per_sec):
    """Build oldest-first (ts, used%) fresh samples climbing at a known rate.

    ``span`` seconds of history ending at ``now``, ``n`` evenly-spaced points,
    starting at ``start`` and rising at ``rate_pp_per_sec``.
    """
    pts = []
    for i in range(n):
        t = now - span + (span * i / (n - 1))
        used = start + rate_pp_per_sec * (t - (now - span))
        pts.append((t, used))
    return pts


def _cost_climb(fresh, rate_dollars_per_pp=1.0, session="s"):
    """A co-occurring per-session cost running-total series ``(ts, sid, cost)``.

    Cost climbs with used% so the pp-per-$ calibration finds co-moving deltas.
    """
    base = fresh[0][1]
    return [(ts, session, (u - base) / rate_dollars_per_pp + 1.0) for ts, u in fresh]


def test_recent_rate_pp_per_sec_matches_known_slope():
    now = 2_000_000.0
    rate = 0.01  # pp/sec
    fresh = _climb(now, 0, 10, 1800.0, 20.0, rate)
    got = _recent_rate_pp_per_sec(fresh, now, 1800.0)
    assert got is not None
    assert abs(got - rate) < 1e-6


def test_recent_rate_none_on_thin():
    assert _recent_rate_pp_per_sec([], 1000.0, 1800.0) is None
    assert _recent_rate_pp_per_sec([(1000.0, 5.0)], 1000.0, 1800.0) is None


def test_forecast_window_level_hold_thin_history_falls_back_to_ratio():
    # 0-1 fresh samples -> ratio fallback, equals forecast_from_period_start.
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2  # halfway -> ratio doubles
    fc = forecast_window(30.0, resets, now, WINDOW_7D, fresh_samples=[], which="7d")
    assert fc.method == "ratio"
    assert abs(fc.projected_pct - 60.0) < 1e-6
    # ratio path: band collapses to a point (low/high None).
    assert fc.projected_low is None and fc.projected_high is None
    assert fc.on_pace is True


def test_forecast_window_fallback_equals_ratio_regression_anchor():
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2
    one = [(now - 100.0, 30.0)]  # below slope_min_points -> fallback
    fw = forecast_window(30.0, resets, now, WINDOW_7D, fresh_samples=one, which="7d")
    fr = forecast_from_period_start(30.0, WINDOW_7D, resets, now)
    assert fw.method == "ratio"
    assert fw.projected_pct == fr.projected_pct
    assert fw.on_pace == fr.on_pace
    assert fw.current_pct == fr.current_pct


def test_forecast_window_additive_base():
    # Steady climb at a known rate over the slope window + co-occurring cost.
    now = 2_000_000.0
    secs_to_reset = 6000.0
    resets = now + secs_to_reset
    rate = 0.002  # pp/sec
    cur = 40.0
    fresh = _climb(now, secs_to_reset, 12, 1800.0, cur - rate * 1800.0, rate)
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(
        burst_horizon_seconds=secs_to_reset,  # trust the rate flat the whole way
        idle_seconds=10_000.0,
    )
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg,
    )
    expected = cur + rate * secs_to_reset
    assert fc.method in ("additive", "decay_sim")
    assert abs(fc.projected_pct - expected) < 1.0
    assert fc.on_pace is True  # well under 100


def test_forecast_window_value_at_reset_does_not_divide_by_elapsed():
    # Early-window, high live rate: the ratio model would EXPLODE (used/tiny
    # elapsed). The additive value-at-reset target must stay sane.
    now = 2_000_000.0
    secs_to_reset = WINDOW_5H * 0.95  # only 5% elapsed
    resets = now + secs_to_reset
    rate = 0.0005  # pp/sec, modest
    cur = 8.0
    fresh = _climb(now, secs_to_reset, 12, 1800.0, cur - rate * 1800.0, rate)
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(burst_horizon_seconds=secs_to_reset, idle_seconds=10_000.0)
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg,
    )
    additive = cur + rate * secs_to_reset
    # Sane: ~current + rate*secs, NOT current / 0.05 = 160.
    assert fc.projected_pct < 50.0
    assert abs(fc.projected_pct - additive) < 2.0


def _flat_then_spike(now, n_flat, n_spike, flat_span, spike_span, start,
                     flat_rate, spike_rate):
    """A long mostly-flat history with a steep spike at the very end.

    The flat segment sets a LOW recent baseline; the trailing spike sets a HIGH
    live OLS slope — so the burst cap / mean-reversion has something to bite on.
    """
    pts = []
    t0 = now - flat_span - spike_span
    for i in range(n_flat):
        t = t0 + flat_span * i / (n_flat - 1)
        pts.append((t, start + flat_rate * (t - t0)))
    base_used = pts[-1][1]
    t1 = now - spike_span
    for i in range(1, n_spike + 1):
        t = t1 + spike_span * i / n_spike
        pts.append((t, base_used + spike_rate * (t - t1)))
    return pts


def test_forecast_window_5h_burst_caps():
    # A steep RECENT spike on a low baseline must be capped by the burst horizon,
    # NOT integrated at the spike rate over the full secs_to_reset.
    now = 2_000_000.0
    secs_to_reset = 14_000.0
    resets = now + secs_to_reset
    spike_rate = 0.01  # pp/sec — steep, only in the last few minutes
    # Baseline (per-bucket median) is dominated by the long flat tail ~0.
    fresh = _flat_then_spike(
        now, n_flat=40, n_spike=6, flat_span=1700.0, spike_span=300.0,
        start=20.0, flat_rate=0.0, spike_rate=spike_rate,
    )
    cur = fresh[-1][1]
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(
        burst_horizon_seconds=600.0,
        revert_tau_seconds=300.0,  # quick revert toward the ~0 baseline
        idle_seconds=10_000.0,
    )
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg,
    )
    full = cur + spike_rate * secs_to_reset  # if the spike rate ran the whole way
    # Burst-capped: the spike only integrates for ~burst_horizon, then reverts to
    # the ~0 baseline, so the projection is FAR below the naive full extrapolation.
    assert fc.projected_pct < full
    assert fc.projected_pct < cur + spike_rate * 1500.0


def test_forecast_window_5h_idle_zero_rate():
    # No fresh sample within idle_seconds AND flat cost -> rate 0 -> hold current.
    now = 2_000_000.0
    secs_to_reset = 6000.0
    resets = now + secs_to_reset
    rate = 0.002
    # Recent fresh samples exist (so the rate is defined), but cost is FLAT, so
    # the idle gate (no positive cost delta within idle_seconds) forces rate 0.
    fresh = _climb(now, secs_to_reset, 12, 1800.0, 30.0 - rate * 1800.0, rate)
    flat_cost = [(ts, "s", 100.0) for ts, _ in fresh]  # cost flat (present, no spend)
    cfg = ForecastCfg(idle_seconds=600.0)
    cur = fresh[-1][1]
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=flat_cost, which="5h", cfg=cfg,
    )
    assert fc.method == "level_hold"
    assert abs(fc.projected_pct - cur) < 1e-6
    # band collapses to a point at current.
    assert abs(fc.projected_low - cur) < 1e-6
    assert abs(fc.projected_high - cur) < 1e-6


def test_forecast_window_band_ordering_and_widening():
    now = 2_000_000.0
    secs_to_reset = 6000.0
    resets = now + secs_to_reset
    rate = 0.002
    cur = 40.0
    fresh = _climb(now, secs_to_reset, 12, 1800.0, cur - rate * 1800.0, rate)
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(burst_horizon_seconds=secs_to_reset, idle_seconds=10_000.0)
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg,
    )
    assert fc.projected_low <= fc.projected_pct <= fc.projected_high
    assert fc.projected_low < fc.projected_high  # non-degenerate band when active


def test_forecast_window_7d_mean_reverts_live_blip():
    # 7d: a steep RECENT live rate on a low long-run baseline must NOT be
    # extrapolated flat across the week — it reverts toward the baseline, so the
    # projected additions are far below the naive (live-rate * secs_to_reset).
    now = 5_000_000.0
    secs_to_reset = WINDOW_7D * 0.5
    resets = now + secs_to_reset
    spike_rate = 0.0006  # pp/sec in the last few minutes
    fresh = _flat_then_spike(
        now, n_flat=40, n_spike=6, flat_span=20_000.0, spike_span=600.0,
        start=30.0, flat_rate=0.0, spike_rate=spike_rate,
    )
    cur = fresh[-1][1]
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(
        revert_tau_7d_seconds=3600.0,  # blip fades within an hour over the week
        idle_seconds=1e9,
    )
    fc = forecast_window(
        cur, resets, now, WINDOW_7D, fresh_samples=fresh,
        cost_rows=cost, which="7d", cfg=cfg,
    )
    naive_flat = cur + spike_rate * secs_to_reset
    assert fc.projected_pct < naive_flat  # reverted, not extrapolated flat
    assert fc.method in ("additive", "decay_sim")


def test_forecast_window_7d_fixed_window_no_inwindow_rolloff():
    # Anthropic's 7d limit is a FIXED window: used_pct accumulates from window-open
    # (reset - W) and drops only at the hard reset — it does NOT continuously roll
    # off. So even with steady cost history spanning a full window back, NONE of it
    # ages off before the reset (every roll-off lookup floors at window-open), and
    # the decay sim must project the SAME as the additive value-at-reset rather than
    # subtracting phantom roll-off from a prior, already-reset window.
    now = 5_000_000.0
    secs_to_reset = WINDOW_7D * 0.5
    resets = now + secs_to_reset
    rate = 0.00004  # gentle steady climb, pp/sec
    cur = 50.0
    # Fresh slope history over the recent ~30 min for the rate fit.
    fresh = _climb(now, secs_to_reset, 16, 1800.0, cur - rate * 1800.0, rate)
    cfg = ForecastCfg(revert_tau_7d_seconds=WINDOW_7D, idle_seconds=1e9)

    # No cost history -> additive value-at-reset (no decay term).
    fc_add = forecast_window(
        cur, resets, now, WINDOW_7D, fresh_samples=fresh,
        cost_rows=[], which="7d", cfg=cfg,
    )
    # Steady spend spanning the whole window back to now-W, co-moving with used%.
    # The calibration still fires (method == decay_sim), but the roll-off term is
    # inert inside a fixed window, so the projection is unchanged.
    cost = []
    c = 0.0
    t = now - WINDOW_7D
    while t <= now:
        c += rate * 900.0  # $ proportional to pp added per coarse bucket
        cost.append((t, "s", c))
        t += 900.0
    fc_decay = forecast_window(
        cur, resets, now, WINDOW_7D, fresh_samples=fresh,
        cost_rows=cost, which="7d", cfg=cfg,
    )
    assert fc_decay.method == "decay_sim"
    assert fc_decay.projected_pct == pytest.approx(fc_add.projected_pct)


def test_forecast_window_decay_lowers_front_loaded_5h():
    # A front-loaded spike (steep recent live rate on a low baseline) projects
    # LOWER under the layered model (burst-capped + mean-reverted) than the naive
    # full-horizon additive at the spike rate.
    now = 2_000_000.0
    secs_to_reset = 16_000.0
    resets = now + secs_to_reset
    spike_rate = 0.008
    fresh = _flat_then_spike(
        now, n_flat=40, n_spike=6, flat_span=1700.0, spike_span=300.0,
        start=50.0, flat_rate=0.0, spike_rate=spike_rate,
    )
    cur = fresh[-1][1]
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(
        burst_horizon_seconds=1800.0, revert_tau_seconds=600.0, idle_seconds=1e9
    )
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg,
    )
    naive_full = cur + spike_rate * secs_to_reset
    assert fc.projected_pct < naive_full


def test_forecast_window_cap_guard():
    now = 2_000_000.0
    secs_to_reset = 17_000.0
    resets = now + secs_to_reset
    rate = 0.5  # absurd
    cur = 90.0
    fresh = _climb(now, secs_to_reset, 12, 1800.0, cur - rate * 1800.0, rate)
    cost = _cost_climb(fresh)
    cfg = ForecastCfg(
        burst_horizon_seconds=secs_to_reset, idle_seconds=1e9, cap=999.0
    )
    fc = forecast_window(
        cur, resets, now, WINDOW_5H, fresh_samples=fresh,
        cost_rows=cost, which="5h", cfg=cfg, cap=999.0,
    )
    assert fc.projected_pct <= 999.0
    assert fc.projected_high <= 999.0
    assert fc.on_pace is False


def test_forecast_window_past_reset_holds():
    now = 2_000_000.0
    resets = now - 100.0  # stale
    fresh = _climb(now, 0, 12, 1800.0, 80.0, 0.001)
    fc = forecast_window(80.0, resets, now, WINDOW_5H, fresh_samples=fresh, which="5h")
    assert fc.method == "ratio"
    assert fc.projected_pct == 80.0
    assert fc.on_pace is None


def test_forecast_window_band_collapses_in_ratio_fallback():
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2
    fc = forecast_window(30.0, resets, now, WINDOW_7D, fresh_samples=[], which="7d")
    assert fc.projected_low is None
    assert fc.projected_high is None
    assert fc.method == "ratio"


# ===========================================================================
# Config surface + call-site glue (model_is_layered / forecast_for_window)
# ===========================================================================


def test_forecast_config_keys_have_documented_defaults():
    # load_config() must surface the [forecast] keys the layered model reads.
    config = load_config()
    fc = config["forecast"]
    assert fc["model"] == "layered"
    assert fc["slope_window_seconds"] == 1800
    assert fc["slope_bucket_seconds"] == 30
    assert fc["slope_min_points"] == 4
    assert fc["burst_horizon_seconds"] == 2700
    assert fc["revert_tau_seconds"] == 3600
    assert fc["idle_seconds"] == 600
    assert fc["burst_horizon_7d_seconds"] == 10800
    assert fc["revert_tau_7d_seconds"] == 3600
    assert fc["decay_bucket_seconds"] == 900
    assert fc["decay_sim_steps"] == 24
    assert fc["band_frac"] == 0.25
    assert fc["cap"] == 999.0
    # ForecastCfg.from_config lifts them faithfully.
    cfg = ForecastCfg.from_config(config)
    assert cfg.slope_window_seconds == 1800.0
    assert cfg.slope_min_points == 4
    assert cfg.burst_horizon_seconds == 2700.0
    assert cfg.revert_tau_7d_seconds == 3600.0
    assert cfg.burst_horizon_7d_seconds == 10800.0
    assert cfg.decay_sim_steps == 24


def test_model_is_layered_switch():
    assert model_is_layered({"forecast": {"model": "layered"}}) is True
    assert model_is_layered({"forecast": {"model": "ratio"}}) is False
    # No [forecast] section at all (e.g. bare alerts test config) -> ratio path,
    # so those legacy tests keep hitting forecast_from_period_start.
    assert model_is_layered({}) is False
    assert model_is_layered(None) is False


def test_forecast_for_window_falls_back_without_samples():
    # Model on but no samples -> byte-identical to forecast_from_period_start.
    now = 1_000_000.0
    resets = now + WINDOW_7D / 2
    config = {"forecast": {"model": "layered"}}
    fc = forecast_for_window(
        "7d", 30.0, resets, WINDOW_7D, now, None, config)
    fr = forecast_from_period_start(30.0, WINDOW_7D, resets, now)
    assert fc.method == "ratio"
    assert fc.projected_pct == fr.projected_pct
    assert fc.on_pace == fr.on_pace


def test_forecast_for_window_ratio_model_ignores_samples():
    # Model explicitly 'ratio' -> ratio path even with rich history.
    now = 2_000_000.0
    resets = now + 6000.0
    samples = [
        {"ts": now - 1800 + i * 150, "used_pct_5h": 40.0 + i * 0.5,
         "resets_at_5h": resets, "cost_usd": 1.0 + i}
        for i in range(12)
    ]
    config = {"forecast": {"model": "ratio"}}
    fc = forecast_for_window(
        "5h", samples[-1]["used_pct_5h"], resets, WINDOW_5H, now, samples, config)
    assert fc.method == "ratio"


def test_forecast_for_window_layered_uses_history():
    # Model on + a steady fresh climb -> the layered (additive/decay) projection.
    now = 2_000_000.0
    secs_to_reset = 6000.0
    resets = now + secs_to_reset
    samples = []
    for i in range(12):
        ts = now - 1800 + (1800 * i / 11)
        used = 40.0 + 0.002 * (ts - (now - 1800))
        samples.append({"ts": ts, "used_pct_5h": used, "resets_at_5h": resets,
                        "cost_usd": 1.0 + i})
    config = {"forecast": {"model": "layered", "burst_horizon_seconds": secs_to_reset,
                           "idle_seconds": 10_000}}
    fc = forecast_for_window(
        "5h", samples[-1]["used_pct_5h"], resets, WINDOW_5H, now, samples, config)
    assert fc.method in ("additive", "decay_sim")
    assert fc.projected_pct > samples[-1]["used_pct_5h"]  # additive climb
