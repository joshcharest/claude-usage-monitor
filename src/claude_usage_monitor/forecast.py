"""Usage-window forecasting.

Pure functions only — no I/O — so they are trivially unit-testable.

Two models live here:

* :func:`forecast_from_period_start` — the legacy budget-burndown ratio model
  (``projected = used_pct / elapsed_fraction``). Kept verbatim as the documented
  FALLBACK; every existing reader is unaffected.
* :func:`forecast_window` — the new *layered* projector. It does NOT divide by
  the elapsed fraction. Instead it builds the projection additively from the live
  reading plus a smartly-predicted stream of future ADDITIONS:

      L1 (additive)   projected = current + recent_rate * secs_to_reset
      L3 (predictor)  the rate over the REST of the window is not flat — 5h
                      mean-reverts a bursty live slope over a burst horizon; 7d
                      holds it a few hours then decays to zero (a 30-min slope
                      can't predict a week), leaning on current ± roll-off.
      L2 (decay sim)  the rolling metric also DECAYS as past usage ages off the
                      back; what rolls off at future t is what was added at t-W,
                      already observable. We simulate U(t)=current+additions-rolloff
                      forward (additions calibrated from never-aging cost deltas)
                      and report the PEAK over [now, reset] — the throttle quantity.

All layers are fallback-safe: no calibrated history drops the decay term (additive
value-at-reset); thin history degrades to the ratio model; so the public contract
never breaks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class WindowForecast:
    """Forecast result for a single rolling window.

    The first five fields are the stable public contract (every reader and the
    existing tests touch only these). The trailing three are OPTIONAL additions
    for the layered model; they default so every existing positional/keyword
    constructor call keeps working unchanged:

    * ``projected_low`` / ``projected_high`` — an additive uncertainty band that
      brackets ``projected_pct`` (the expected point estimate). ``None`` for the
      ratio fallback (which collapses to a point).
    * ``method`` — which layer produced the result: ``'ratio'`` (legacy
      fallback), ``'level_hold'`` (held at current), ``'additive'`` (L1), or
      ``'decay_sim'`` (full L2/L3).
    """

    current_pct: float | None
    burn_rate_pct_per_sec: float | None
    projected_pct: float | None
    on_pace: bool | None  # True = projected to stay under 100% before reset
    secs_to_reset: float | None
    projected_low: float | None = None
    projected_high: float | None = None
    method: str = "ratio"


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


# ===========================================================================
# Layered model (forecast_window) — primary projector.
#
# The whole section below is PURE: callers fetch samples and pass plain lists
# of ``(ts, value)`` tuples plus a ForecastCfg. No DB, no clock, no tz.
# ===========================================================================


@dataclass
class ForecastCfg:
    """Tunables for :func:`forecast_window`, read from ``[forecast]`` config.

    Every field has the documented default so the engine works with no config
    (and tests can construct it bare). :meth:`from_config` lifts the values out
    of a merged config dict, falling back to these defaults per key.
    """

    # L1 trailing-rate fit.
    slope_window_seconds: float = 1800.0
    slope_bucket_seconds: float = 30.0
    slope_min_points: int = 4
    # 5h mean-reversion of a bursty live rate.
    burst_horizon_seconds: float = 2700.0
    revert_tau_seconds: float = 3600.0
    idle_seconds: float = 600.0
    # 7d: hold the live rate only this long (a few hours) before decaying it to
    # zero — a 30-min slope can't predict a week, so a recent burst contributes a
    # few hours of additions, not days.
    burst_horizon_7d_seconds: float = 10800.0
    revert_tau_7d_seconds: float = 3600.0
    # L2 decay simulation: coarse-bucket the window-spanning cost history to this
    # resolution for the roll-off profile, and walk the forward sim in this many
    # steps from now->reset (peak detection granularity).
    decay_bucket_seconds: float = 900.0
    decay_sim_steps: int = 24
    # Band half-width and the global clamp.
    band_frac: float = 0.25
    cap: float = 999.0

    @classmethod
    def from_config(cls, config: dict | None) -> "ForecastCfg":
        fc = config.get("forecast") if isinstance(config, dict) else None
        fc = fc if isinstance(fc, dict) else {}

        def g(key: str, default: float) -> float:
            v = fc.get(key)
            return default if v is None else float(v)

        return cls(
            slope_window_seconds=g("slope_window_seconds", 1800.0),
            slope_bucket_seconds=g("slope_bucket_seconds", 30.0),
            slope_min_points=int(g("slope_min_points", 4)),
            burst_horizon_seconds=g("burst_horizon_seconds", 2700.0),
            revert_tau_seconds=g("revert_tau_seconds", 3600.0),
            idle_seconds=g("idle_seconds", 600.0),
            burst_horizon_7d_seconds=g("burst_horizon_7d_seconds", 10800.0),
            revert_tau_7d_seconds=g("revert_tau_7d_seconds", 3600.0),
            decay_bucket_seconds=g("decay_bucket_seconds", 900.0),
            decay_sim_steps=int(g("decay_sim_steps", 24)),
            band_frac=g("band_frac", 0.25),
            cap=g("cap", 999.0),
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ----------------------------------------------------------------- L1: the rate


def _recent_rate_pp_per_sec(
    fresh_samples: list[tuple[float, float]], now: float, window_seconds: float
) -> float | None:
    """Trailing OLS slope of fresh used% over the last ``window_seconds``, pp/SEC.

    Thin wrapper that REUSES the audited slope code in :mod:`alerts`
    (``_slope_pp_per_min`` returns pp/MIN, so divide by 60). The import is local
    to avoid a forecast<-alerts module cycle (alerts already imports forecast).
    Returns ``None`` when fewer than 2 trailing points or zero time span.
    """
    if not fresh_samples:
        return None
    from .alerts import _slope_pp_per_min  # lazy: break the import cycle

    trailing = [(ts, u) for ts, u in fresh_samples if ts >= now - window_seconds]
    if len(trailing) < 2:
        return None
    slope_per_min = _slope_pp_per_min(trailing)
    if slope_per_min is None:
        return None
    return slope_per_min / 60.0


# --------------------------------------------------- additions / cost calibration


def _positive_deltas(series: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """``(ts, increment)`` for each positive step in an oldest-first series.

    Used for the cost-delta additions signal: cost_usd is a monotone per-session
    running total whose positive steps are clean never-aging additions.
    """
    out: list[tuple[float, float]] = []
    prev: float | None = None
    for ts, v in series:
        if prev is not None:
            d = v - prev
            if d > 0:
                out.append((ts, d))
        prev = v
    return out


def _account_add_buckets(
    cost_rows: list[tuple[float, str | None, float]], bucket_seconds: float
) -> list[tuple[float, float]]:
    """Account-wide $ ADDED per time bucket, summed across sessions.

    ``cost_usd`` is a PER-SESSION running total, so a flat (ts, cost) list across
    interleaved sessions is not monotone — its raw deltas are noise. We group by
    session, take each session's positive running-total steps, and sum those into
    ``bucket_seconds`` time buckets keyed by the bucket START. Returns sorted
    ``(bucket_ts, dollars_added)``. This is the clean never-aging additions signal.
    """
    if bucket_seconds <= 0:
        bucket_seconds = 1.0
    by_sess: dict[str | None, list[tuple[float, float]]] = {}
    for ts, sid, c in cost_rows:
        by_sess.setdefault(sid, []).append((ts, c))
    adds: dict[float, float] = {}
    for rows in by_sess.values():
        rows.sort(key=lambda p: p[0])
        prev: float | None = None
        for ts, c in rows:
            if prev is not None and c > prev:
                key = math.floor(ts / bucket_seconds) * bucket_seconds
                adds[key] = adds.get(key, 0.0) + (c - prev)
            prev = c if prev is None else max(prev, c)
    return sorted(adds.items())


def _pp_per_dollar(
    fresh_samples: list[tuple[float, float]],
    cost_rows: list[tuple[float, str | None, float]],
    now: float,
    window_seconds: float,
    bucket_seconds: float,
) -> float | None:
    """Median pp-per-$ calibration from co-occurring positive deltas (recent window).

    Bucket the fresh used% series and the account $-added series (per-session deltas
    summed, :func:`_account_add_buckets`) to the same grid over the trailing window;
    for each bucket where BOTH a used% rise and a $ addition occur, record
    ``used_delta / dollars``. Returns the median, or ``None`` when too few pairs
    exist (caller then skips the decay term). Used% deltas alone are corrupted by
    roll-off; $ deltas are not — this anchors pp to never-aging spend.
    """
    lo = now - window_seconds
    f = [(ts, u) for ts, u in fresh_samples if ts >= lo]
    if len(f) < 2:
        return None
    fb: dict[float, float] = {}
    for ts, u in f:
        key = math.floor(ts / bucket_seconds) * bucket_seconds if bucket_seconds > 0 else ts
        fb[key] = u  # last (== max) used% in the bucket
    fdelta = {ts: d for ts, d in _positive_deltas(sorted(fb.items()))}
    cdelta = dict(
        _account_add_buckets([r for r in cost_rows if r[0] >= lo], bucket_seconds)
    )
    ratios: list[float] = []
    for ts, ud in fdelta.items():
        cd = cdelta.get(ts)
        if cd is not None and cd > 0 and ud > 0:
            ratios.append(ud / cd)
    if not ratios:
        return None
    ratios.sort()
    n = len(ratios)
    mid = n // 2
    return ratios[mid] if n % 2 else 0.5 * (ratios[mid - 1] + ratios[mid])


def _cum_pp_lookup(add_buckets: list[tuple[float, float]], pp_per_dollar: float):
    """Build ``cum_at(t)`` = cumulative pp ADDED in buckets starting at or before t.

    Turns the per-bucket $ additions into a calibrated cumulative pp step-function
    so the decay simulation can ask "how much was added in [a, b]" as
    ``cum_at(b) - cum_at(a)`` in O(log n). Returns ``(cum_at, has_history)``.
    """
    import bisect

    times: list[float] = []
    cums: list[float] = []
    c = 0.0
    for ts, dollars in add_buckets:
        c += dollars * pp_per_dollar
        times.append(ts)
        cums.append(c)

    def cum_at(t: float) -> float:
        if not times:
            return 0.0
        i = bisect.bisect_right(times, t)
        return cums[i - 1] if i > 0 else 0.0

    return cum_at, bool(times)


def _is_idle(
    fresh_samples: list[tuple[float, float]],
    cost_rows: list[tuple[float, str | None, float]],
    add_buckets: list[tuple[float, float]],
    now: float,
    idle_seconds: float,
    bucket_seconds: float,
) -> bool:
    """Idle = no fresh reading recently (so the future rate should be 0).

    Primary signal: no fresh used% sample within ``idle_seconds``. Secondary: if we
    DO have recent cost rows but none of them represent a positive account addition
    (spend is flat), the account is doing nothing — also idle. Passing the raw
    ``cost_rows`` (not just ``add_buckets``) lets us tell "present but flat" (idle)
    apart from "no cost data at all" (can't disprove activity → not idle).
    """
    if not any(ts >= now - idle_seconds for ts, _ in fresh_samples):
        return True
    if any(ts >= now - idle_seconds for ts, _sid, _c in cost_rows):
        slack = now - idle_seconds - max(0.0, bucket_seconds)
        if not any(ts >= slack and d > 0 for ts, d in add_buckets):
            return True
    return False


# --------------------------------------------- L3: future additions (rate profile)


def _integrate_mean_reverting(
    base_rate: float,
    baseline_rate: float,
    horizon: float,
    secs: float,
    tau: float,
) -> float:
    """Integral over ``[0, secs]`` of a rate that holds ``base_rate`` flat for
    ``horizon`` seconds, then exponentially reverts toward ``baseline_rate`` with
    time-constant ``tau``.

    rate(t) = base_rate                                       for t <= horizon
    rate(t) = baseline + (base_rate-baseline)*exp(-(t-h)/tau)  for t > horizon

    Returns total additions (rate is pp/sec, result is pp). The closed-form integral
    of the decaying tail avoids any per-minute loop.
    """
    if secs <= 0:
        return 0.0
    flat = min(secs, horizon)
    total = base_rate * flat
    rem = secs - flat
    if rem > 0:
        total += baseline_rate * rem
        if tau > 0:
            # ∫0^rem (base-baseline) e^{-t/tau} dt = (base-baseline)*tau*(1-e^{-rem/tau})
            total += (base_rate - baseline_rate) * tau * (1.0 - math.exp(-rem / tau))
        # tau<=0 => instant revert, decaying term contributes nothing.
    return total


def _baseline_rate_pp_per_sec(
    fresh_samples: list[tuple[float, float]],
    now: float,
    window_seconds: float,
    bucket_seconds: float,
) -> float:
    """Recent baseline rate = median of per-adjacent-bucket pp/sec over the window.

    A steadier reference than the live OLS slope; the live rate mean-reverts toward
    this. Returns 0.0 when there isn't enough history (a conservative baseline).
    """
    from .alerts import _bucketed_max  # lazy: cycle guard

    lo = now - window_seconds
    trailing = [(ts, u) for ts, u in fresh_samples if ts >= lo]
    bucketed = _bucketed_max(trailing, bucket_seconds)
    if len(bucketed) < 2:
        return 0.0
    rates: list[float] = []
    for (t0, v0), (t1, v1) in zip(bucketed, bucketed[1:]):
        dt = t1 - t0
        if dt > 0:
            rates.append((v1 - v0) / dt)
    if not rates:
        return 0.0
    rates.sort()
    n = len(rates)
    mid = n // 2
    return rates[mid] if n % 2 else 0.5 * (rates[mid - 1] + rates[mid])


def _future_additions_fn(
    which: str, base_rate: float, baseline: float, idle: bool, cfg: ForecastCfg
):
    """Closure ``fn(dt)`` → predicted future additions (pp) over ``[now, now+dt]``.

    The rate over the rest of the window is NOT flat (proposal L3):

    * **5h** — hold the live ``base_rate`` for ``burst_horizon_seconds``, then revert
      toward the recent ``baseline`` with ``revert_tau_seconds`` (a burst fades).
    * **7d** — a 30-min slope must NOT be extrapolated across a week. Hold it only
      for ``burst_horizon_7d_seconds`` (a few hours), then decay toward ZERO with the
      short ``revert_tau_7d_seconds``. So a recent burst contributes a few hours of
      additions, not days — the rolling 7d figure then comes from current ± roll-off,
      which is the robust signal at that timescale.

    Idle (no recent fresh reading / no recent spend) → 0. The same closure is
    sampled by the decay simulation at intermediate horizons, so the predicted curve
    and the reported peak stay consistent.
    """
    if idle or (base_rate <= 0.0 and baseline <= 0.0):
        return lambda dt: 0.0
    if which == "7d":
        # Revert toward 0 (not the co-elevated recent baseline) over a few hours.
        horizon, tau, target = cfg.burst_horizon_7d_seconds, cfg.revert_tau_7d_seconds, 0.0
    else:
        horizon, tau, target = cfg.burst_horizon_seconds, cfg.revert_tau_seconds, baseline
    return lambda dt: max(
        0.0, _integrate_mean_reverting(base_rate, target, horizon, max(0.0, dt), tau)
    )


# ----------------------------------------- L2: forward roll-off peak simulation


def _simulate_peak(
    current_pct: float,
    future_fn,
    cum_at,
    now: float,
    secs_to_reset: float,
    window_length: float,
    steps: int,
) -> float:
    """Peak rolling used% over ``[now, reset]`` from additions MINUS roll-off.

    The rolling metric evolves as ``dU/dt = additions(t) - decay(t)`` where what ages
    off the back at future time ``t`` is exactly what was added at ``t - window`` —
    already in the PAST and observable via the calibrated ``cum_at``. So::

        U(t) = current + future_fn(t-now) - (cum_at(t-W) - cum_at(now-W))

    The throttle is peak-driven, so we walk ``steps`` points to ``reset`` and return
    the max U (>= current). With no calibrated history ``cum_at`` is 0 everywhere, so
    the decay term vanishes and this degrades to the additive value-at-reset.
    """
    if secs_to_reset <= 0:
        return current_pct
    steps = max(1, int(steps))
    base_decay = cum_at(now - window_length)
    peak = current_pct
    for i in range(1, steps + 1):
        dt = secs_to_reset * (i / steps)
        rolled_off = max(0.0, cum_at(now + dt - window_length) - base_decay)
        u = current_pct + future_fn(dt) - rolled_off
        if u > peak:
            peak = u
    return peak


# ----------------------------------------------------------- the entry point


def forecast_window(
    current_pct: float | None,
    resets_at: float | None,
    now: float,
    window_length: float,
    fresh_samples: list[tuple[float, float]] | None = None,
    cost_rows: list[tuple[float, str | None, float]] | None = None,
    which: str = "5h",
    *,
    cfg: ForecastCfg | None = None,
    cap: float | None = None,
    decay_cum=None,
) -> WindowForecast:
    """PRIMARY layered projector (pure).

    Parameters
    ----------
    current_pct
        The live FRESH rolling used% reading for this window.
    resets_at, now, window_length
        Reset epoch, current epoch, total window length (seconds).
    fresh_samples
        Oldest-first ``(ts, used_pct)`` for THIS window, already filtered to fresh
        and bucketed by the caller (via ``alerts._fresh_series`` /
        ``alerts._bucketed_max``) so the slope machinery is shared, not reinvented.
    cost_rows
        Oldest-first ``(ts, session_id, cost_usd)`` per-session running-total rows
        spanning ~the window. Drives the idle signal, the $-to-pp calibration, and
        the roll-off decay (per-session positive deltas = clean never-aging adds).
    which
        ``'5h'`` or ``'7d'`` — selects the L3 predictor.
    cfg, cap
        Tunables and the global clamp.

    Returns
    -------
    WindowForecast
        ``projected_pct`` is the EXPECTED point estimate (the L2 peak), so every
        existing reader is unaffected; ``projected_low``/``projected_high`` are an
        additive-only band. ``method`` is one of ``ratio`` / ``level_hold`` /
        ``additive`` / ``decay_sim``.

    Falls back to :func:`forecast_from_period_start` VERBATIM when ``fresh_samples``
    has fewer than ``slope_min_points`` points or the slope is undefined.
    """
    cfg = cfg or ForecastCfg()
    cap = cfg.cap if cap is None else cap
    fresh_samples = fresh_samples or []
    cost_rows = cost_rows or []
    secs_to_reset = (resets_at - now) if resets_at is not None else None

    # --- Fallback gate 1: degenerate inputs -> ratio model verbatim. ---------
    if (
        current_pct is None
        or (isinstance(current_pct, float) and not math.isfinite(current_pct))
        or resets_at is None
        or window_length <= 0
    ):
        return forecast_from_period_start(current_pct, window_length, resets_at, now, cap)

    # Past the reset: hold at current, pace unknown (mirror the ratio model).
    if secs_to_reset is not None and secs_to_reset < 0:
        return forecast_from_period_start(current_pct, window_length, resets_at, now, cap)

    # --- Fallback gate 2: thin history -> ratio model verbatim. --------------
    if len(fresh_samples) < cfg.slope_min_points:
        return forecast_from_period_start(current_pct, window_length, resets_at, now, cap)

    rate = _recent_rate_pp_per_sec(fresh_samples, now, cfg.slope_window_seconds)
    if rate is None:
        return forecast_from_period_start(current_pct, window_length, resets_at, now, cap)

    secs = max(0.0, secs_to_reset or 0.0)
    base_rate = max(0.0, rate)
    baseline = _baseline_rate_pp_per_sec(
        fresh_samples, now, cfg.slope_window_seconds, cfg.slope_bucket_seconds
    )

    # --- Account additions: coarse buckets spanning the window for the roll-off
    # profile (per-session deltas summed, never-aging), and an idle check. -----
    add_buckets = _account_add_buckets(cost_rows, cfg.decay_bucket_seconds)
    idle = _is_idle(
        fresh_samples, cost_rows, add_buckets, now,
        cfg.idle_seconds, cfg.decay_bucket_seconds,
    )

    # --- L3: predicted future additions as a rate profile (5h burst-revert /
    # 7d long-revert), sampled by the sim and integrated for the band. --------
    future_fn = _future_additions_fn(which, base_rate, baseline, idle, cfg)
    exp_add = future_fn(secs)
    thin = base_rate <= 0.0 and baseline <= 0.0
    band_half = cfg.band_frac * (2.0 if thin else 1.0) * exp_add

    # --- L2: forward roll-off sim → peak rolling used%. Calibrate pp-per-$ from
    # recent co-occurring (used%, $) deltas; with a calibration + history the
    # decay term is real, else it vanishes and this is the additive value-at-reset.
    if decay_cum is not None:
        # Caller supplied a precomputed global cumulative-additions lookup (the chart
        # replay does this once instead of rebuilding it per evaluated point).
        cum_at, has_decay = decay_cum, True
    else:
        pp_per_dollar = _pp_per_dollar(
            fresh_samples, cost_rows, now,
            cfg.slope_window_seconds, cfg.slope_bucket_seconds,
        )
        if pp_per_dollar is not None and add_buckets:
            cum_at, has_decay = _cum_pp_lookup(add_buckets, pp_per_dollar)
        else:
            cum_at, has_decay = (lambda t: 0.0), False

    projected = _simulate_peak(
        current_pct, future_fn, cum_at, now, secs, window_length, cfg.decay_sim_steps
    )
    projected = min(projected, cap)

    # --- Band brackets the projected peak by the additions uncertainty. -------
    low = _clamp(projected - band_half, 0.0, cap)
    high = _clamp(projected + band_half, 0.0, cap)

    # method tag: flat additions -> level-hold; real roll-off applied -> decay_sim;
    # else plain additive (positive additions, no calibrated history to decay).
    if exp_add <= 0.0:
        method = "level_hold"
    elif has_decay:
        method = "decay_sim"
    else:
        method = "additive"

    on_pace = projected <= 100.0
    return WindowForecast(
        current_pct=current_pct,
        burn_rate_pct_per_sec=rate,
        projected_pct=projected,
        on_pace=on_pace,
        secs_to_reset=secs_to_reset,
        projected_low=low,
        projected_high=high,
        method=method,
    )


# =========================================================================== #
# Call-site glue.
#
# The four readers (statusline, dashboard prep, queries.pace_timeseries,
# alerts.evaluate_window) all need the same two things: decide whether the
# layered model is switched on, and turn a raw interleaved sample stream into
# the per-window ``(fresh, cost)`` lists ``forecast_window`` expects. These thin
# helpers live here (still pure) so that logic isn't copy-pasted four times.
# ``forecast.model`` is a CALLER decision per the spec, so it is read here — NOT
# inside ``forecast_window`` / ``ForecastCfg.from_config``.
# =========================================================================== #


def model_is_layered(config: dict | None) -> bool:
    """True when ``[forecast].model`` selects the layered projector (the default).

    Any value other than the explicit string ``"ratio"`` is treated as layered,
    so a missing key ships the new model; ``"ratio"`` is the instant rollback.
    NOTE: test configs that omit the ``[forecast]`` section entirely (e.g. the
    alerts unit tests) get ``None`` here and therefore stay on the ratio path —
    that is intentional, it keeps those tests on the legacy code.
    """
    fc = config.get("forecast") if isinstance(config, dict) else None
    if not isinstance(fc, dict):
        return False
    return str(fc.get("model", "ratio")).lower() == "layered"


def window_inputs(
    samples: list[dict],
    which: str,
    now: float,
    cfg: ForecastCfg,
) -> tuple[list[tuple[float, float]], list[tuple[float, str | None, float]]]:
    """Build ``(fresh_samples, cost_rows)`` for one window from raw rows.

    ``fresh_samples`` are oldest-first ``(ts, used_pct)`` for ``which``, filtered
    to FRESH snapshots and bucketed to ``cfg.slope_bucket_seconds`` MAX (reusing
    ``alerts._fresh_series`` / ``alerts._bucketed_max`` so the slope machinery is
    shared, not reinvented). ``cost_rows`` are oldest-first ``(ts, session_id,
    cost_usd)`` per-session running totals — the decay needs per-session deltas, so
    the session id is carried, not flattened. The import is local to avoid a
    forecast<-alerts cycle.
    """
    from .alerts import _bucketed_max, _fresh_series  # lazy: cycle guard

    fresh = _bucketed_max(_fresh_series(samples, which), cfg.slope_bucket_seconds)
    cost: list[tuple[float, str | None, float]] = []
    for r in samples:
        ts = r.get("ts")
        c = r.get("cost_usd")
        if ts is None or c is None:
            continue
        try:
            cf = float(c)
        except (TypeError, ValueError):
            continue
        if math.isfinite(cf):
            cost.append((float(ts), r.get("session_id"), cf))
    cost.sort(key=lambda p: p[0])
    return fresh, cost


def build_decay_cum(samples: list[dict], which: str, now: float, cfg: ForecastCfg):
    """Precompute a global calibrated cumulative-additions lookup ``cum_at(t)``.

    For the dashboard's pace replay, building the roll-off decay profile per
    evaluated point is O(window) each; instead we calibrate pp-per-$ and the
    cumulative pp-added curve ONCE over a window-spanning sample set and inject the
    result into :func:`forecast_window` via ``decay_cum=`` (an O(log n) lookup per
    point). Returns ``cum_at`` (absolute-time cumulative pp added), or ``None`` when
    there's no calibratable spend (the forecast then drops the decay term).
    """
    ts_all = [s["ts"] for s in samples if s.get("ts") is not None] if samples else []
    if not ts_all:
        return None
    fresh, cost_rows = window_inputs(samples, which, now, cfg)
    span = max(1.0, now - min(ts_all))
    pp = _pp_per_dollar(fresh, cost_rows, now, span + 1.0, cfg.slope_bucket_seconds)
    add_buckets = _account_add_buckets(cost_rows, cfg.decay_bucket_seconds)
    if pp is None or not add_buckets:
        return None
    cum_at, _ = _cum_pp_lookup(add_buckets, pp)
    return cum_at


def forecast_for_window(
    which: str,
    used_pct: float | None,
    resets_at: float | None,
    window_length: float,
    now: float,
    samples: list[dict] | None,
    config: dict | None,
    *,
    cfg: ForecastCfg | None = None,
    cap: float | None = None,
) -> WindowForecast:
    """Single dispatch every reader calls: layered when on + history present, else ratio.

    When ``[forecast].model == "layered"`` and ``samples`` carry trailing history,
    builds the per-window ``(fresh, cost_rows)`` lists and runs :func:`forecast_window`
    (which itself falls back to the ratio model on thin history). Otherwise — model
    off, or no samples — calls :func:`forecast_from_period_start` verbatim, so the
    output is byte-identical to the legacy path. This is the seam that keeps every
    "no samples passed" test on the ratio numbers.
    """
    cfg = cfg or ForecastCfg.from_config(config)
    if not (model_is_layered(config) and samples):
        eff_cap = cfg.cap if cap is None else cap
        return forecast_from_period_start(used_pct, window_length, resets_at, now, eff_cap)
    fresh, cost = window_inputs(samples, which, now, cfg)
    # Calibrate the roll-off and the long-run rate from the same window-spanning
    # samples. 7d reverts toward the long-run rate (not a 30-min slope); 5h keeps
    # its recent baseline (its short burst horizon already tames a spike).
    decay_cum = build_decay_cum(samples, which, now, cfg)
    return forecast_window(
        used_pct, resets_at, now, window_length,
        fresh_samples=fresh, cost_rows=cost, which=which, cfg=cfg, cap=cap,
        decay_cum=decay_cum,
    )
