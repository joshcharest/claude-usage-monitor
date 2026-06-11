"""Unit tests for the pure burn-rate forecaster."""

from claude_usage_monitor.forecast import forecast_window


def test_rising_series_projects_higher_and_on_pace():
    # 10% -> 20% over 1000s = 0.01 %/s. 1000s left -> +10% -> 30%.
    now = 10_000.0
    points = [(now - 1000, 10.0), (now, 20.0)]
    fc = forecast_window(points, resets_at=now + 1000, now=now)
    assert fc.current_pct == 20.0
    assert abs(fc.burn_rate_pct_per_sec - 0.01) < 1e-9
    assert abs(fc.projected_pct - 30.0) < 1e-6
    assert fc.on_pace is True


def test_rising_series_projects_overflow_not_on_pace():
    # 50% -> 70% over 1000s = 0.02 %/s. 2000s left -> +40% -> 110%.
    now = 10_000.0
    points = [(now - 1000, 50.0), (now, 70.0)]
    fc = forecast_window(points, resets_at=now + 2000, now=now)
    assert abs(fc.projected_pct - 110.0) < 1e-6
    assert fc.on_pace is False


def test_single_point_is_unknown_pace():
    now = 10_000.0
    fc = forecast_window([(now, 42.0)], resets_at=now + 1000, now=now)
    assert fc.current_pct == 42.0
    assert fc.burn_rate_pct_per_sec is None
    assert fc.on_pace is None
    assert fc.projected_pct == 42.0


def test_empty_series_is_all_none():
    fc = forecast_window([], resets_at=20_000.0, now=10_000.0)
    assert fc.current_pct is None
    assert fc.on_pace is None
    assert fc.secs_to_reset == 10_000.0


def test_negative_burn_clamped_no_decrease():
    # Usage dropped (e.g. window reset). Should not forecast a decrease.
    now = 10_000.0
    points = [(now - 1000, 80.0), (now, 30.0)]
    fc = forecast_window(points, resets_at=now + 1000, now=now)
    assert fc.burn_rate_pct_per_sec < 0  # observed rate is negative
    assert fc.projected_pct == 30.0  # but projection clamps to current
    assert fc.on_pace is True


def test_none_used_pct_points_ignored():
    now = 10_000.0
    points = [(now - 1000, None), (now - 500, 10.0), (now, 20.0)]
    fc = forecast_window(points, resets_at=now + 500, now=now)
    # Burn from 10->20 over 500s = 0.02 %/s; 500s left -> 30%.
    assert abs(fc.projected_pct - 30.0) < 1e-6


def test_unknown_reset_holds_at_current():
    now = 10_000.0
    points = [(now - 1000, 10.0), (now, 20.0)]
    fc = forecast_window(points, resets_at=None, now=now)
    assert fc.projected_pct == 20.0
    assert fc.secs_to_reset is None
