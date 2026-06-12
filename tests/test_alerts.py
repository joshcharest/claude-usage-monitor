"""Unit tests for the usage-alert detection engine (pure) + delivery helpers."""

from __future__ import annotations

import json

import pytest

from claude_usage_monitor import alerts

# A window length config so projections are deterministic.
CONFIG = {
    "forecast": {"window_5h_seconds": 18000, "window_7d_seconds": 604800},
    "alerts": {
        "alert_over_pct": 125.0,
        "spike_pp_per_min": 5.0,
        "spike_window_seconds": 600.0,
        "spike_bucket_seconds": 30.0,
        "spike_min_points": 4,
        "cooldown_seconds": 1800.0,
        "clear_factor": 0.9,
    },
}

NOW = 1_000_000.0
RESET_5H = NOW + 18000  # future reset => fresh, and well past early-window guard


def _sample(ts, used_5h, reset_5h=RESET_5H, used_7d=None, reset_7d=None):
    return {
        "ts": ts,
        "session_id": "s",
        "used_pct_5h": used_5h,
        "resets_at_5h": reset_5h,
        "used_pct_7d": used_7d,
        "resets_at_7d": reset_7d,
    }


# --------------------------------------------------------------- freshness


def test_fresh_series_drops_stale_rows():
    samples = [
        _sample(NOW - 100, 50.0, reset_5h=NOW - 100),  # stale: reset <= ts
        _sample(NOW - 90, 60.0, reset_5h=NOW + 1000),  # fresh
        _sample(NOW - 80, float("nan"), reset_5h=NOW + 1000),  # NaN dropped
        _sample(NOW - 70, None, reset_5h=NOW + 1000),  # None dropped
    ]
    fresh = alerts._fresh_series(samples, "5h")
    assert fresh == [(NOW - 90, 60.0)]


def test_bucketed_max_collapses_interleave():
    # Three concurrent snapshots in the same 30s bucket -> keep the max.
    series = [(NOW, 40.0), (NOW + 1, 70.0), (NOW + 2, 55.0), (NOW + 40, 80.0)]
    out = alerts._bucketed_max(series, 30.0)
    # bucket0 max=70, bucket1 max=80
    assert [u for _, u in out] == [70.0, 80.0]


def test_slope_handles_few_points():
    assert alerts._slope_pp_per_min([]) is None
    assert alerts._slope_pp_per_min([(NOW, 5.0)]) is None


# --------------------------------------------------------- spiking detection


def _spike_samples(start_used, pp_per_min, n=6, step=60):
    """n fresh samples climbing at pp_per_min, ending at NOW."""
    out = []
    for i in range(n):
        ts = NOW - (n - 1 - i) * step
        used = start_used + pp_per_min * (i * step) / 60.0
        out.append(_sample(ts, used))
    return out


def test_spike_fires_on_steep_climb():
    samples = _spike_samples(20.0, pp_per_min=8.0)  # > 5 threshold
    wa = alerts.evaluate_window("5h", samples, None, CONFIG, NOW)
    assert wa.spiking is True
    assert wa.slope_pp_per_min > 5.0


def test_spike_silent_on_gentle_climb():
    samples = _spike_samples(20.0, pp_per_min=2.0)  # < 5 threshold
    wa = alerts.evaluate_window("5h", samples, None, CONFIG, NOW)
    assert wa.spiking is False


def test_spike_requires_min_points():
    samples = _spike_samples(20.0, pp_per_min=20.0, n=2)  # steep but too few
    wa = alerts.evaluate_window("5h", samples, None, CONFIG, NOW)
    assert wa.n_fresh_points < CONFIG["alerts"]["spike_min_points"]
    assert wa.spiking is False


def test_spike_silent_when_all_stale():
    # All rows stale (reset in the past) -> no fresh points, no spike.
    samples = [_sample(NOW - i * 60, 20.0 + i, reset_5h=NOW - 99999) for i in range(6)]
    wa = alerts.evaluate_window("5h", samples, None, CONFIG, NOW)
    assert wa.n_fresh_points == 0
    assert wa.spiking is False


def test_spike_ignores_rolling_decline():
    # Rolling metric falling (usage aging out) -> negative slope -> no alert.
    samples = _spike_samples(80.0, pp_per_min=-8.0)
    wa = alerts.evaluate_window("5h", samples, None, CONFIG, NOW)
    assert wa.spiking is False


# --------------------------------------------------- projected-over detection


def test_projected_over_fires_when_way_over():
    # 10% elapsed-fraction? No: choose past the 20% guard. Window=18000, reset in
    # 9000 => period_start = reset-18000, elapsed=9000 => 50% elapsed. used=70 =>
    # projected = 70/0.5 = 140% >= 125 -> over.
    current = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}
    wa = alerts.evaluate_window("5h", [], current, CONFIG, NOW)
    assert wa.projected_pct == pytest.approx(140.0)
    assert wa.over is True


def test_projected_over_silent_below_threshold():
    current = {"used_pct_5h": 50.0, "resets_at_5h": NOW + 9000}  # proj 100 < 125
    wa = alerts.evaluate_window("5h", [], current, CONFIG, NOW)
    assert wa.over is False


def test_projected_over_gated_by_early_window():
    # Only 5% elapsed -> on_pace is None -> projection NOT meaningful -> no alert,
    # even though a naive ratio would explode.
    reset = NOW + 18000 * 0.95  # 5% elapsed
    current = {"used_pct_5h": 30.0, "resets_at_5h": reset}
    wa = alerts.evaluate_window("5h", [], current, CONFIG, NOW)
    assert wa.over is False


def test_projected_over_silent_after_reset():
    # Reset already in the past -> forecast holds, on_pace None -> no alert.
    current = {"used_pct_5h": 99.0, "resets_at_5h": NOW - 10}
    wa = alerts.evaluate_window("5h", [], current, CONFIG, NOW)
    assert wa.over is False


# ------------------------------------------------- cooldown / hysteresis / state


def test_evaluate_notifies_on_transition_then_respects_cooldown():
    current = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}  # proj 140 -> over

    # First tick: transition into alert -> notification.
    s1 = alerts.evaluate([], current, CONFIG, NOW, prior={})
    keys = {n["key"] for n in s1.notifications}
    assert "5h_over" in keys
    assert s1.fired_at["5h_over"] == NOW

    # Second tick shortly after, still over -> within cooldown -> NO new notify.
    prior = {"fired_at": s1.fired_at, "active": s1.active}
    s2 = alerts.evaluate([], current, CONFIG, NOW + 60, prior=prior)
    assert "5h_over" not in {n["key"] for n in s2.notifications}
    # marker still shown
    assert any("proj" in m for m in s2.markers())


def test_cooldown_expiry_renotifies():
    current = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}
    s1 = alerts.evaluate([], current, CONFIG, NOW, prior={})
    prior = {"fired_at": s1.fired_at, "active": s1.active}
    later = NOW + CONFIG["alerts"]["cooldown_seconds"] + 1
    # reset moves with time so still 50% elapsed-ish; keep over by giving new reset
    current2 = {"used_pct_5h": 70.0, "resets_at_5h": later + 9000}
    s2 = alerts.evaluate([], current2, CONFIG, later, prior=prior)
    assert "5h_over" in {n["key"] for n in s2.notifications}


def test_hysteresis_latches_until_below_clear_level():
    # Fire, then drop just below threshold but above clear level (125*0.9=112.5).
    over = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}  # proj 140
    s1 = alerts.evaluate([], over, CONFIG, NOW, prior={})
    assert s1.active.get("5h_over")

    prior = {"fired_at": s1.fired_at, "active": s1.active}
    # projected 120 (< 125, but >= 112.5 clear level): stays latched, no new notify.
    mid = {"used_pct_5h": 60.0, "resets_at_5h": NOW + 9000}  # 60/0.5 = 120
    s2 = alerts.evaluate([], mid, CONFIG, NOW, prior=prior)
    assert s2.windows["5h"].over is False
    assert s2.active.get("5h_over")  # still latched (120 >= 112.5)
    assert not s2.notifications

    # Drop below clear level -> latch cleared.
    low = {"used_pct_5h": 50.0, "resets_at_5h": NOW + 9000}  # 100 < 112.5
    prior2 = {"fired_at": s2.fired_at, "active": s2.active}
    s3 = alerts.evaluate([], low, CONFIG, NOW, prior=prior2)
    assert not s3.active.get("5h_over")


def test_empty_inputs_produce_no_alerts():
    s = alerts.evaluate([], None, CONFIG, NOW, prior={})
    assert not s.any_alerting
    assert not s.notifications
    assert s.markers() == []


# --------------------------------------------------------- state persistence I/O


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ALERT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("CLAUDE_ALERT_NO_NOTIFY", "1")
    current = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}

    state = alerts.run_tick([], current, CONFIG, NOW)
    assert state.fired_at.get("5h_over") == NOW

    loaded = alerts.load_state()
    assert loaded["fired_at"]["5h_over"] == NOW
    assert loaded["active"]["5h_over"] is True


def test_load_state_handles_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("{ not json")
    monkeypatch.setenv("CLAUDE_ALERT_STATE", str(p))
    assert alerts.load_state() == {}


def test_run_tick_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_ALERT_STATE", str(tmp_path / "s.json"))
    monkeypatch.setenv("CLAUDE_ALERT_NO_NOTIFY", "1")
    # Garbage samples must not crash.
    bad = [{"ts": "x"}, {}, {"ts": NOW, "used_pct_5h": "nope"}]
    state = alerts.run_tick(bad, None, CONFIG, NOW)
    assert isinstance(state, alerts.AlertState)


def test_notify_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("CLAUDE_ALERT_NO_NOTIFY", "1")
    assert alerts.notify("t", "b") is False


def test_defaults_used_when_config_empty():
    # No [alerts] section at all -> module DEFAULTS apply.
    current = {"used_pct_5h": 80.0, "resets_at_5h": NOW + 9000}  # proj 160 -> over
    wa = alerts.evaluate_window("5h", [], current, {}, NOW)
    assert wa.over is True
