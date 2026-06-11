"""Unit tests for the pure budget-burndown forecaster."""

from claude_usage_monitor.forecast import forecast_from_period_start

WINDOW_7D = 604800.0


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


def test_period_start_caps_runaway_early_ratio():
    now = 1_000_000.0
    resets = now + WINDOW_7D * 0.999  # 0.1% elapsed -> huge ratio
    fc = forecast_from_period_start(1.0, WINDOW_7D, resets, now, cap=999.0)
    assert fc.projected_pct == 999.0
    assert fc.on_pace is False


def test_period_start_past_reset_holds_at_current():
    now = 1_000_000.0
    resets = now - 100  # already past reset (stale)
    fc = forecast_from_period_start(80.0, WINDOW_7D, resets, now)
    assert fc.projected_pct == 80.0  # elapsed_fraction clamped to 1.0


def test_period_start_none_used_is_none():
    fc = forecast_from_period_start(None, WINDOW_7D, 1_000_000.0, 999_000.0)
    assert fc.current_pct is None
    assert fc.projected_pct is None
    assert fc.on_pace is None
