"""Usage-alert detection engine + a small delivery helper.

Two conditions are detected from the live ``budget.db`` sample stream:

1. **Projected WAY over** — the budget-burndown forecast projects the current
   FRESH reading to land at/above a hard threshold (default 125%) before the
   window resets, AND the projection is *meaningful* (``on_pace is not None``,
   i.e. past the early-window guard in :func:`forecast.forecast_from_period_start`).

2. **Spiking rapidly** — FRESH ``used_pct`` is climbing faster than a threshold
   (default 5 pp/min) over a short trailing window (default last 10 min).

Both conditions are computed PER WINDOW ("5h" and "7d"). The detection layer is
pure (no I/O, no clock-of-its-own): inputs are recent samples + config + ``now``
+ prior state; output is a structured :class:`AlertState`. State (last-fired
times, for cooldown/debounce) is persisted by the thin JSON helpers at the bottom,
which the statusline calls.

DATA REALITY this engine is hardened against (a prior audit fixed this once):
budget.db interleaves ~6 concurrent sessions; each writes its own snapshot of the
ACCOUNT-WIDE rate-limit counter, and lagging/frozen sessions report STALE values.
``used_pct_*`` is ROLLING (rises AND falls as usage ages out). So every read here
filters to FRESH samples only — the predicate is ``resets_at > ts`` (a real reset
time is in the future) — and dedupes the interleave with a per-small-bucket MAX.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .forecast import forecast_from_period_start

# --------------------------------------------------------------------- defaults
# Mirrored into config/budget.toml [alerts]; config wins when present. Kept here
# so the engine works even with an empty config (and tests can call it bare).
DEFAULTS: dict[str, Any] = {
    # Master switch for the whole engine (detection + delivery).
    "enabled": True,
    # Fire desktop notify-send notifications (marker is independent of this).
    "notify": True,
    # "Projected WAY over": alert when the forecast projects >= this %.
    "alert_over_pct": 125.0,
    # "Spiking": alert when FRESH used% climbs faster than this (pp per minute)...
    "spike_pp_per_min": 5.0,
    # ...measured over this trailing window (seconds)...
    "spike_window_seconds": 600.0,
    # ...deduped into buckets this size (seconds) — collapses the interleave...
    "spike_bucket_seconds": 30.0,
    # ...requiring at least this many distinct fresh buckets (anti-noise).
    "spike_min_points": 4,
    # Debounce: re-notify the SAME condition at most once per this many seconds.
    "cooldown_seconds": 1800.0,
    # Hysteresis: once firing, keep the marker shown until the metric falls below
    # threshold * this factor (so it doesn't flap right at the boundary).
    "clear_factor": 0.9,
}

WINDOWS = ("5h", "7d")

# Per-window column names in budget.db / the synthesized reading.
_COLS = {
    "5h": ("used_pct_5h", "resets_at_5h"),
    "7d": ("used_pct_7d", "resets_at_7d"),
}


def _cfg(config: dict | None, key: str) -> Any:
    """Read an [alerts] setting, falling back to the module default."""
    val = None
    if isinstance(config, dict):
        section = config.get("alerts")
        if isinstance(section, dict):
            val = section.get(key)
    return DEFAULTS[key] if val is None else val


def _window_length(config: dict | None, which: str) -> float:
    fc = config.get("forecast") if isinstance(config, dict) else None
    fc = fc if isinstance(fc, dict) else {}
    if which == "7d":
        return float(fc.get("window_7d_seconds", 604800))
    return float(fc.get("window_5h_seconds", 18000))


# ------------------------------------------------------------------ data model


@dataclass
class WindowAlert:
    """Detection outcome for one window ("5h" or "7d")."""

    window: str
    # "projected over budget" leg
    projected_pct: float | None = None
    over: bool = False  # projected_pct >= alert_over_pct AND on_pace is not None
    # "spiking" leg
    slope_pp_per_min: float | None = None
    spiking: bool = False
    n_fresh_points: int = 0
    # convenience: either leg active
    @property
    def alerting(self) -> bool:
        return self.over or self.spiking


@dataclass
class AlertState:
    """Full evaluation result + the (possibly updated) debounce/cooldown state.

    ``windows`` maps "5h"/"7d" -> :class:`WindowAlert`.
    ``notifications`` is the list of NEW notifications that should fire THIS tick
    (already filtered by cooldown + transition logic). Each item is a dict with
    ``key``, ``title``, ``body``.
    ``fired_at`` is the persisted cooldown clock: ``{condition_key: epoch}``.
    ``active`` is the persisted hysteresis set of currently-latched keys.
    """

    now: float
    windows: dict[str, WindowAlert] = field(default_factory=dict)
    notifications: list[dict[str, str]] = field(default_factory=list)
    fired_at: dict[str, float] = field(default_factory=dict)
    active: dict[str, bool] = field(default_factory=dict)

    @property
    def any_alerting(self) -> bool:
        return any(w.alerting for w in self.windows.values())

    def markers(self) -> list[str]:
        """Short statusline markers for every currently-alerting leg."""
        out: list[str] = []
        for win in WINDOWS:
            wa = self.windows.get(win)
            if not wa:
                continue
            if wa.over and wa.projected_pct is not None:
                out.append(f"🚨 {win} proj {wa.projected_pct:.0f}%")
            if wa.spiking and wa.slope_pp_per_min is not None:
                out.append(f"🚨 {win} spiking {wa.slope_pp_per_min:.0f}pp/m")
        return out


# ------------------------------------------------------------------- detection


def _fresh_series(samples: list[dict], which: str) -> list[tuple[float, float]]:
    """Extract FRESH ``(ts, used_pct)`` pairs for a window, oldest first.

    Fresh = the row's reset time for this window is in the FUTURE relative to the
    row's own ``ts`` (``resets_at > ts``). Stale/zombie snapshots from lagging
    concurrent sessions are dropped. NaN / None used% values are dropped.
    """
    used_col, reset_col = _COLS[which]
    out: list[tuple[float, float]] = []
    for r in samples:
        ts = r.get("ts")
        used = r.get(used_col)
        reset = r.get(reset_col)
        if ts is None or used is None or reset is None:
            continue
        try:
            used = float(used)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(used):
            continue
        if reset <= ts:  # stale: reset already in the past for this snapshot
            continue
        out.append((float(ts), used))
    out.sort(key=lambda p: p[0])
    return out


def _bucketed_max(
    series: list[tuple[float, float]], bucket_seconds: float
) -> list[tuple[float, float]]:
    """Collapse the interleave: per fixed-width time bucket, keep the MAX used%.

    Several concurrent sessions each emit a snapshot within the same instant;
    bucketing to the bucket's MAX yields one true reading per bucket and removes
    the per-session jitter that would otherwise corrupt a slope. Returns one
    ``(bucket_start_ts, max_used_pct)`` per occupied bucket, oldest first.
    """
    if bucket_seconds <= 0:
        return list(series)
    buckets: dict[float, float] = {}
    for ts, used in series:
        b = math.floor(ts / bucket_seconds) * bucket_seconds
        if b not in buckets or used > buckets[b]:
            buckets[b] = used
    return sorted(buckets.items())


def _slope_pp_per_min(points: list[tuple[float, float]]) -> float | None:
    """Robust trailing slope in percentage-points per MINUTE.

    Ordinary least-squares fit over the deduped bucket points. OLS (vs. a naive
    first/last difference) tolerates a single outlier bucket. Returns ``None`` if
    fewer than 2 points or zero time span. A *negative* slope (usage aging out of
    the rolling window) is returned as-is; the caller only alerts on a positive
    slope above threshold.
    """
    n = len(points)
    if n < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope_per_sec = sxy / sxx  # pp per second
    return slope_per_sec * 60.0


def evaluate_window(
    which: str,
    samples: list[dict],
    current: dict | None,
    config: dict | None,
    now: float,
) -> WindowAlert:
    """Pure detection for a single window.

    Parameters
    ----------
    which
        "5h" or "7d".
    samples
        Recent raw samples (interleaved) covering at least the spike window;
        filtered to fresh internally.
    current
        The synthesized 'current reading' (max-of-fresh) used for the projection
        leg; if ``None`` we fall back to the latest fresh point in ``samples``.
    config, now
        Merged config and current epoch seconds.
    """
    used_col, reset_col = _COLS[which]
    wa = WindowAlert(window=which)

    # ---- "Projected WAY over" leg -----------------------------------------
    over_pct = float(_cfg(config, "alert_over_pct"))
    cur_used = cur_reset = None
    if current is not None:
        cur_used = current.get(used_col)
        cur_reset = current.get(reset_col)
    fresh = _fresh_series(samples, which)
    if cur_used is None and fresh:
        cur_used = fresh[-1][1]
        # find that point's reset from the raw rows is not tracked; use samples
        for r in reversed(samples):
            if r.get(used_col) is not None and r.get(reset_col) is not None and (
                r[reset_col] > r["ts"]
            ):
                cur_reset = r[reset_col]
                break

    fc = forecast_from_period_start(
        cur_used, _window_length(config, which), cur_reset, now
    )
    wa.projected_pct = fc.projected_pct
    # Gate: only meaningful once past the early-window guard (on_pace is not None).
    if fc.on_pace is not None and fc.projected_pct is not None:
        wa.over = fc.projected_pct >= over_pct

    # ---- "Spiking rapidly" leg --------------------------------------------
    win_s = float(_cfg(config, "spike_window_seconds"))
    bucket_s = float(_cfg(config, "spike_bucket_seconds"))
    min_pts = int(_cfg(config, "spike_min_points"))
    thresh = float(_cfg(config, "spike_pp_per_min"))

    trailing = [(ts, u) for ts, u in fresh if ts >= now - win_s]
    bucketed = _bucketed_max(trailing, bucket_s)
    wa.n_fresh_points = len(bucketed)
    slope = _slope_pp_per_min(bucketed)
    wa.slope_pp_per_min = slope
    if slope is not None and wa.n_fresh_points >= min_pts:
        wa.spiking = slope >= thresh

    return wa


def evaluate(
    samples: list[dict],
    current: dict | None,
    config: dict | None,
    now: float,
    prior: dict[str, Any] | None = None,
) -> AlertState:
    """Evaluate BOTH windows and apply cooldown + hysteresis to derive notifications.

    Parameters
    ----------
    samples
        Recent raw (interleaved) samples — pass at least ``spike_window_seconds``
        of history. Filtered to fresh per-window internally.
    current
        The synthesized 'current reading' (``queries.current_reading``) for the
        projection leg. May be ``None``.
    config, now
        Merged config and epoch seconds.
    prior
        Persisted state from a previous tick: ``{"fired_at": {...}, "active": {...}}``.
        ``None`` on first run.

    Returns
    -------
    AlertState
        ``windows`` per-window outcome, ``notifications`` to fire NOW (cooldown +
        edge filtered), and the updated ``fired_at`` / ``active`` maps to persist.
    """
    prior = prior or {}
    fired_at: dict[str, float] = dict(prior.get("fired_at") or {})
    active_prev: dict[str, bool] = dict(prior.get("active") or {})

    cooldown = float(_cfg(config, "cooldown_seconds"))
    clear_factor = float(_cfg(config, "clear_factor"))
    over_pct = float(_cfg(config, "alert_over_pct"))
    spike_thresh = float(_cfg(config, "spike_pp_per_min"))

    state = AlertState(now=now, fired_at=fired_at, active=dict(active_prev))
    notifications: list[dict[str, str]] = []

    for which in WINDOWS:
        wa = evaluate_window(which, samples, current, config, now)
        state.windows[which] = wa

        # Two independent condition keys per window: <win>_over, <win>_spike.
        legs = (
            (
                f"{which}_over",
                wa.over,
                wa.projected_pct,
                over_pct * clear_factor,
                f"Projected {which} usage {wa.projected_pct:.0f}%"
                if wa.projected_pct is not None
                else f"Projected {which} usage over budget",
                "Usage projected WAY over the limit before reset.",
            ),
            (
                f"{which}_spike",
                wa.spiking,
                wa.slope_pp_per_min,
                spike_thresh * clear_factor,
                f"{which} usage spiking {wa.slope_pp_per_min:.0f} pp/min"
                if wa.slope_pp_per_min is not None
                else f"{which} usage spiking",
                "Usage is climbing rapidly.",
            ),
        )

        for key, firing, metric, clear_level, title, body in legs:
            was_active = active_prev.get(key, False)
            if firing:
                state.active[key] = True
                last = fired_at.get(key)
                # Notify on transition into alert, OR re-notify after cooldown.
                if (not was_active) or (last is None) or (now - last >= cooldown):
                    notifications.append({"key": key, "title": title, "body": body})
                    fired_at[key] = now
            else:
                # Hysteresis: clear the latch only once the metric drops below the
                # clear level (below threshold * clear_factor), else keep it latched
                # so the marker doesn't flap at the boundary.
                if was_active and metric is not None and metric >= clear_level:
                    state.active[key] = True  # still elevated; stay latched
                else:
                    state.active.pop(key, None)

    state.notifications = notifications
    return state


# ------------------------------------------------------ state persistence (I/O)

_DEFAULT_STATE = Path.home() / ".claude" / "usage-alert-state.json"


def state_path() -> Path:
    """Resolve the alert-state JSON path, honoring ``CLAUDE_ALERT_STATE``."""
    override = os.environ.get("CLAUDE_ALERT_STATE")
    return Path(override) if override else _DEFAULT_STATE


def load_state() -> dict[str, Any]:
    """Load persisted ``{"fired_at": {...}, "active": {...}}``; {} on any error."""
    try:
        with state_path().open() as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state: AlertState) -> None:
    """Persist the cooldown/hysteresis state. Never raises."""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fired_at": state.fired_at,
            "active": {k: True for k, v in state.active.items() if v},
            "updated_at": state.now,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w") as fh:
            json.dump(payload, fh)
        tmp.replace(path)
    except Exception:
        pass


# ----------------------------------------------------------- desktop delivery


def notify(title: str, body: str) -> bool:
    """Fire a Linux ``notify-send`` desktop notification.

    Degrades gracefully: returns ``False`` (and never raises) if ``notify-send``
    is unavailable or the call fails. Disabled when ``CLAUDE_ALERT_NO_NOTIFY`` is
    set (tests / headless).
    """
    if os.environ.get("CLAUDE_ALERT_NO_NOTIFY"):
        return False
    exe = shutil.which("notify-send")
    if not exe:
        return False
    try:
        subprocess.run(
            [exe, "--app-name=claude-usage-monitor", "--urgency=critical", title, body],
            check=False,
            timeout=5,
        )
        return True
    except Exception:
        return False


def deliver(state: AlertState) -> int:
    """Fire desktop notifications for every queued notification. Returns count sent."""
    sent = 0
    for n in state.notifications:
        if notify(n.get("title", "Usage alert"), n.get("body", "")):
            sent += 1
    return sent


def run_tick(
    samples: list[dict],
    current: dict | None,
    config: dict | None,
    now: float | None = None,
) -> AlertState:
    """End-to-end one-tick helper: load state, evaluate, deliver, persist.

    The statusline calls this each update. It loads the prior state from disk,
    evaluates both windows, fires any due desktop notifications, persists the new
    state, and returns the :class:`AlertState` (so the caller can render markers).
    Wrapped so a failure degrades to a no-op empty state — it must NEVER crash the
    statusline.
    """
    now = time.time() if now is None else now
    try:
        if not bool(_cfg(config, "enabled")):
            return AlertState(now=now)
        prior = load_state()
        state = evaluate(samples, current, config, now, prior)
        if bool(_cfg(config, "notify")):
            deliver(state)
        save_state(state)
        return state
    except Exception:
        return AlertState(now=now)
