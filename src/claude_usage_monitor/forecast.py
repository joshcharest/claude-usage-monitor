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


def forecast_from_period_start(
    used_pct: float | None,
    window_length: float,
    resets_at: float | None,
    now: float,
    cap: float = 999.0,
) -> WindowForecast:
    """Project end-of-window usage from the average pace since the window opened.

    A budget-burndown projection: assume usage continues at the average rate
    observed since the period began, and extrapolate to the reset. Unlike
    :func:`forecast_window`, this needs no history — only the current cumulative
    ``used_pct``, the window length, and when it resets.

    The window opened at ``resets_at - window_length``::

        elapsed_fraction = (now - period_start) / window_length
        projected        = used_pct / elapsed_fraction

    Parameters
    ----------
    used_pct
        Cumulative usage percentage since the window opened.
    window_length
        Total length of the window in seconds (e.g. 7 days = 604800).
    resets_at
        Epoch seconds when the window resets, or ``None`` if unknown.
    now
        Current epoch seconds.
    cap
        Upper bound on the reported projection, to keep early-period ratios
        (tiny elapsed fraction) from producing absurd numbers.

    Notes
    -----
    - If the window has only just opened (elapsed <= 0), the pace is undefined,
      so ``on_pace`` is ``None`` and the projection holds at ``used_pct``.
    - ``elapsed_fraction`` is clamped to 1.0, so past the reset the projection
      equals the current usage.
    """
    secs_to_reset = (resets_at - now) if resets_at is not None else None

    if used_pct is None or resets_at is None or window_length <= 0:
        return WindowForecast(used_pct, None, used_pct, None, secs_to_reset)

    period_start = resets_at - window_length
    elapsed = now - period_start
    if elapsed <= 0:
        return WindowForecast(used_pct, None, used_pct, None, secs_to_reset)

    elapsed_fraction = min(elapsed / window_length, 1.0)
    avg_rate = used_pct / elapsed  # %/sec averaged over the elapsed period
    projected = min(used_pct / elapsed_fraction, cap)
    on_pace = projected <= 100.0
    return WindowForecast(used_pct, avg_rate, projected, on_pace, secs_to_reset)
