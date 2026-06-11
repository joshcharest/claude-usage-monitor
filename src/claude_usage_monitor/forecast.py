"""Burn-rate forecasting for a rolling usage window.

Pure functions only — no I/O — so they are trivially unit-testable.

Given a series of ``(timestamp, used_percentage)`` observations and the window's
reset time, we estimate the linear burn rate and project where usage will land
when the window resets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WindowForecast:
    """Forecast result for a single rolling window."""

    current_pct: float | None
    burn_rate_pct_per_sec: float | None
    projected_pct: float | None
    on_pace: bool | None  # True = projected to stay under 100% before reset
    secs_to_reset: float | None


def forecast_window(
    points: list[tuple[float, float | None]],
    resets_at: float | None,
    now: float,
) -> WindowForecast:
    """Project end-of-window usage from recent observations.

    Parameters
    ----------
    points
        ``(ts, used_pct)`` pairs, oldest first. ``used_pct`` may be ``None``
        (those points are ignored).
    resets_at
        Epoch seconds when the window resets, or ``None`` if unknown.
    now
        Current epoch seconds.

    Notes
    -----
    - Fewer than 2 usable points => burn rate unknown, ``on_pace`` is ``None``.
    - Negative burn (usage dropped, e.g. a reset occurred) is clamped to 0 for
      projection so we never forecast a decrease.
    """
    pts = [(t, p) for (t, p) in points if p is not None]
    secs_to_reset = (resets_at - now) if resets_at is not None else None

    if not pts:
        return WindowForecast(None, None, None, None, secs_to_reset)

    current = pts[-1][1]

    if len(pts) < 2:
        return WindowForecast(current, None, current, None, secs_to_reset)

    t0, p0 = pts[0]
    t1, p1 = pts[-1]
    dt = t1 - t0
    if dt <= 0:
        return WindowForecast(current, None, current, None, secs_to_reset)

    burn = (p1 - p0) / dt
    effective_burn = max(burn, 0.0)

    if secs_to_reset is None or secs_to_reset < 0:
        projected = current
    else:
        projected = current + effective_burn * secs_to_reset

    on_pace = projected <= 100.0
    return WindowForecast(current, burn, projected, on_pace, secs_to_reset)
