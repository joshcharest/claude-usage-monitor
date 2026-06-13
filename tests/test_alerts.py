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
        # 0 = disable the sustained-over guard for the basic reset tests; a
        # dedicated test exercises the guard with a realistic value.
        "reset_min_over_seconds": 0.0,
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


def _layered_config(**alerts_over):
    # CONFIG with the layered forecast model switched ON for the over-leg.
    cfg = json.loads(json.dumps(CONFIG))
    cfg["forecast"]["model"] = "layered"
    cfg["alerts"].update(alerts_over)
    return cfg


def test_alerts_over_leg_layered_fires_on_climbing_history():
    # Layered model on + a steep fresh climb whose additive projection lands far
    # over the threshold -> wa.over fires (and on_pace meaningful).
    cfg = _layered_config(alert_over_pct=125.0)
    reset = NOW + 9000.0  # 50% elapsed -> well past the early-window guard
    # ~0.01 pp/sec over 9000s adds ~90pp on top of 60 -> ~150% projected.
    samples = []
    for i in range(12):
        ts = NOW - 1800 + (1800 * i / 11)
        used = 60.0 + 0.01 * (ts - (NOW - 1800))
        samples.append({
            "ts": ts, "session_id": "s", "used_pct_5h": used,
            "resets_at_5h": reset, "used_pct_7d": None, "resets_at_7d": None,
            "cost_usd": 1.0 + i,
        })
    current = {"used_pct_5h": samples[-1]["used_pct_5h"], "resets_at_5h": reset}
    wa = alerts.evaluate_window("5h", samples, current, cfg, NOW)
    assert wa.projected_pct is not None
    assert wa.over is True


def test_alerts_over_leg_layered_fires_in_early_window():
    # CONTRACT CHANGE (intentional, early-window-blindness fix): the layered model
    # has NO early-window guard — it always reports on_pace — so the "over" leg can
    # trip in the first 20% of a window when a genuinely steep climb projects past
    # the threshold. The legacy ratio model stayed silent there (on_pace None). Both
    # are asserted below on the SAME early-window, fast-burn scenario.
    reset = NOW + 18000 * 0.95  # only 5% elapsed
    samples = []
    for i in range(12):
        ts = NOW - 1800 + (1800 * i / 11)
        samples.append({
            "ts": ts, "session_id": "s", "used_pct_5h": 30.0 + i * 2.0,  # steep
            "resets_at_5h": reset, "used_pct_7d": None, "resets_at_7d": None,
            "cost_usd": 1.0 + i,
        })
    cur_used = samples[-1]["used_pct_5h"]
    current = {"used_pct_5h": cur_used, "resets_at_5h": reset}

    # Layered: fires despite being early in the window.
    wa = alerts.evaluate_window("5h", samples, current,
                                _layered_config(alert_over_pct=125.0), NOW)
    assert wa.over is True

    # Ratio (default config): early-window guard suppresses the over leg.
    wa_ratio = alerts.evaluate_window("5h", [], current, CONFIG, NOW)
    assert wa_ratio.over is False


def test_alerts_over_leg_default_config_stays_ratio():
    # The bare CONFIG (no [forecast].model) must keep hitting the ratio model even
    # when samples are present, so all the legacy over-leg expectations hold.
    current = {"used_pct_5h": 70.0, "resets_at_5h": NOW + 9000}
    samples = []  # the legacy tests pass [] to the over leg
    wa = alerts.evaluate_window("5h", samples, current, CONFIG, NOW)
    assert wa.projected_pct == pytest.approx(140.0)  # ratio: 70 / 0.5
    assert wa.over is True


def test_alerts_over_leg_gates_on_band_high_not_expected():
    # Conservative gate: the over decision is made on projected_high (the band's
    # upper end), so a case whose EXPECTED projection sits just below the threshold
    # but whose band high crosses it still trips the alert. We assert the gate keys
    # off projected_high by setting the threshold strictly between the two.
    cfg = _layered_config()
    reset = NOW + 9000.0  # 50% elapsed -> past the early-window guard
    samples = []
    for i in range(12):
        ts = NOW - 1800 + (1800 * i / 11)
        used = 60.0 + 0.01 * (ts - (NOW - 1800))
        samples.append({
            "ts": ts, "session_id": "s", "used_pct_5h": used,
            "resets_at_5h": reset, "used_pct_7d": None, "resets_at_7d": None,
            "cost_usd": 1.0 + i,
        })
    current = {"used_pct_5h": samples[-1]["used_pct_5h"], "resets_at_5h": reset}
    base = alerts.evaluate_window("5h", samples, current, cfg, NOW)
    assert base.projected_high is not None and base.projected_pct is not None
    assert base.projected_high > base.projected_pct  # a real band exists
    # Threshold strictly between expected and high: expected is UNDER, high is OVER.
    mid = 0.5 * (base.projected_pct + base.projected_high)
    cfg_mid = _layered_config(alert_over_pct=mid)
    wa = alerts.evaluate_window("5h", samples, current, cfg_mid, NOW)
    assert wa.over is True  # tripped by the conservative band high


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


# --------------------------------------------------- reset-after-exceeded leg


def _prior(state):
    return {"fired_at": state.fired_at, "active": state.active, "period": state.period}


def test_reset_notifies_only_after_exceeded():
    # Build up to the cap, then a sharp collapse on the next tick = reset.
    over = {"used_pct_5h": 100.0, "resets_at_5h": RESET_5H}
    s1 = alerts.evaluate([], over, CONFIG, NOW, prior={})
    assert s1.period["5h"]["peak"] == 100.0
    assert "5h_reset" not in {n["key"] for n in s1.notifications}

    low = {"used_pct_5h": 3.0, "resets_at_5h": RESET_5H}
    s2 = alerts.evaluate([], low, CONFIG, NOW + 5, prior=_prior(s1))
    assert "5h_reset" in {n["key"] for n in s2.notifications}
    assert s2.period["5h"]["peak"] == 3.0  # peak re-baselines to the post-reset level


def test_reset_silent_when_never_exceeded():
    # Peaks at 60 (< cap), then collapses: a reset, but NOT after an over-period.
    mid = {"used_pct_5h": 60.0, "resets_at_5h": RESET_5H}
    s1 = alerts.evaluate([], mid, CONFIG, NOW, prior={})
    low = {"used_pct_5h": 2.0, "resets_at_5h": RESET_5H}
    s2 = alerts.evaluate([], low, CONFIG, NOW + 5, prior=_prior(s1))
    assert "5h_reset" not in {n["key"] for n in s2.notifications}


def test_reset_ignores_gradual_rolling_decline():
    # The 7d counter ages out gradually; small per-tick drops must never read as a
    # reset, even after the period was over the cap.
    prior: dict = {}
    used, now, fired = 100.0, NOW, False
    for _ in range(11):  # 100 -> 50 in 5-pp steps (each < reset_drop_pp=40)
        st = alerts.evaluate([], {"used_pct_5h": used, "resets_at_5h": RESET_5H},
                             CONFIG, now, prior=prior)
        fired = fired or "5h_reset" in {n["key"] for n in st.notifications}
        prior, used, now = _prior(st), used - 5.0, now + 60
    assert fired is False


def test_reset_detected_on_reset_time_advance():
    # Zombie pins used% at 100 (no collapse), but the reset time jumps forward >1h.
    c1 = {"used_pct_5h": 100.0, "resets_at_5h": NOW + 1000}
    s1 = alerts.evaluate([], c1, CONFIG, NOW, prior={})
    c2 = {"used_pct_5h": 100.0, "resets_at_5h": NOW + 1000 + 7200}
    s2 = alerts.evaluate([], c2, CONFIG, NOW + 5, prior=_prior(s1))
    assert "5h_reset" in {n["key"] for n in s2.notifications}


def test_reset_respects_cooldown():
    over = {"used_pct_5h": 100.0, "resets_at_5h": RESET_5H}
    s1 = alerts.evaluate([], over, CONFIG, NOW, prior={})
    s2 = alerts.evaluate([], {"used_pct_5h": 3.0, "resets_at_5h": RESET_5H},
                         CONFIG, NOW + 5, prior=_prior(s1))
    assert "5h_reset" in {n["key"] for n in s2.notifications}
    # Exceed and collapse again within the cooldown -> no second notification.
    s3 = alerts.evaluate([], over, CONFIG, NOW + 10, prior=_prior(s2))
    s4 = alerts.evaluate([], {"used_pct_5h": 3.0, "resets_at_5h": RESET_5H},
                         CONFIG, NOW + 15, prior=_prior(s3))
    assert "5h_reset" not in {n["key"] for n in s4.notifications}


def test_reset_suppresses_transient_over_blip():
    # With a realistic sustained-over guard, a one-tick blip back to the cap (a
    # hung session momentarily winning the reading) must NOT trigger a reset.
    cfg = {**CONFIG, "alerts": {**CONFIG["alerts"], "reset_min_over_seconds": 300.0}}
    cooldown = CONFIG["alerts"]["cooldown_seconds"]

    # Sustained over-cap (>300s), then a drop -> genuine reset fires.
    prior: dict = {}
    t = NOW
    for dt in (0, 400):  # over the cap, spanning > min_over
        st = alerts.evaluate([], {"used_pct_5h": 100.0, "resets_at_5h": RESET_5H},
                             cfg, t + dt, prior=prior)
        prior = _prior(st)
    t += 405
    st = alerts.evaluate([], {"used_pct_5h": 3.0, "resets_at_5h": RESET_5H},
                         cfg, t, prior=prior)
    assert "5h_reset" in {n["key"] for n in st.notifications}
    prior = _prior(st)

    # Move past cooldown so it can't be what suppresses the next one.
    t += cooldown + 100
    # One-tick blip back to 100, then drop again -> NOT sustained -> suppressed.
    st = alerts.evaluate([], {"used_pct_5h": 100.0, "resets_at_5h": RESET_5H},
                         cfg, t, prior=prior)
    prior = _prior(st)
    st = alerts.evaluate([], {"used_pct_5h": 1.0, "resets_at_5h": RESET_5H},
                         cfg, t + 5, prior=prior)
    assert "5h_reset" not in {n["key"] for n in st.notifications}


def test_reset_state_survives_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ALERT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("CLAUDE_ALERT_NO_NOTIFY", "1")
    over = {"used_pct_5h": 100.0, "resets_at_5h": RESET_5H}
    alerts.run_tick([], over, CONFIG, NOW)
    loaded = alerts.load_state()
    assert loaded["period"]["5h"]["peak"] == 100.0


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
