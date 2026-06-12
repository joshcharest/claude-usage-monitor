"""Usage-window forecasting.

Pure functions only — no I/O — so they are trivially unit-testable.

We project where usage will land when a window resets using a budget-burndown
model: cumulative usage divided by how far through the window we are.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class WindowForecast:
    """Forecast result for a single rolling window."""

    current_pct: float | None
    burn_rate_pct_per_sec: float | None
    projected_pct: float | None
    on_pace: bool | None  # True = projected to stay under 100% before reset
    secs_to_reset: float | None


def forecast_from_period_start(
    used_pct: float | None,
    window_length: float,
    resets_at: float | None,
    now: float,
    cap: float = 999.0,
) -> WindowForecast:
    """Project end-of-window usage from the average pace since the window opened.

    A budget-burndown projection: assume usage continues at the average rate
    observed since the period began, and extrapolate to the reset. It needs no
    history — only the current cumulative ``used_pct``, the window length, and
    when it resets.

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

    # Guard degenerate inputs: missing data, or a non-finite used_pct (NaN).
    if (
        used_pct is None
        or (isinstance(used_pct, float) and not math.isfinite(used_pct))
        or resets_at is None
        or window_length <= 0
    ):
        return WindowForecast(None, None, None, None, secs_to_reset)

    # Past the reset (stale row): don't extrapolate; hold at current, pace unknown.
    if secs_to_reset is not None and secs_to_reset < 0:
        return WindowForecast(used_pct, None, used_pct, None, secs_to_reset)

    period_start = resets_at - window_length
    elapsed = now - period_start
    if elapsed <= 0:
        return WindowForecast(used_pct, None, used_pct, None, secs_to_reset)

    elapsed_fraction = min(elapsed / window_length, 1.0)
    # Early-window guard: used_pct is a ROLLING metric, so dividing by a tiny
    # elapsed fraction wildly over-projects. Until 20% of the window has elapsed,
    # hold the projection at the current value and report pace as unknown.
    if elapsed_fraction < 0.2:
        return WindowForecast(used_pct, None, used_pct, None, secs_to_reset)

    avg_rate = used_pct / elapsed  # %/sec averaged over the elapsed period
    projected = min(used_pct / elapsed_fraction, cap)
    on_pace = projected <= 100.0
    return WindowForecast(used_pct, avg_rate, projected, on_pace, secs_to_reset)
