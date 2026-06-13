"""Statusline entry point.

Claude Code pipes a JSON payload to this script's stdin on every status update.
We (1) record a usage sample to SQLite and (2) print one compact line showing
context usage plus a burn-rate forecast for the 5h and 7d plan windows.

The script must NEVER crash on a malformed or partial payload — a status line
that throws would disrupt the session. Every field access is defensive, and any
unexpected error falls back to a minimal line.

NOTE: the exact statusline JSON schema is verified empirically (see
scripts/capture-statusline.py). The field paths below match the documented
shape; `rate_limits` is only present on Pro/Max accounts and degrades to
"unknown" segments when absent.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Any

from . import alerts, db, queries
from .config import load_config
from .forecast import WindowForecast, forecast_from_period_start


def _get(d: Any, *path: str, default: Any = None) -> Any:
    """Safely walk a nested dict by keys, returning ``default`` if any miss."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
    return cur


def _sample_from_payload(payload: dict[str, Any], now: float) -> db.Sample:
    return db.Sample(
        ts=now,
        session_id=_get(payload, "session_id"),
        model=_get(payload, "model", "id"),
        effort=_get(payload, "effort", "level"),
        fast_mode=_get(payload, "fast_mode"),
        cost_usd=_get(payload, "cost", "total_cost_usd"),
        ctx_used_pct=_get(payload, "context_window", "used_percentage"),
        used_pct_5h=_get(payload, "rate_limits", "five_hour", "used_percentage"),
        resets_at_5h=_get(payload, "rate_limits", "five_hour", "resets_at"),
        used_pct_7d=_get(payload, "rate_limits", "seven_day", "used_percentage"),
        resets_at_7d=_get(payload, "rate_limits", "seven_day", "resets_at"),
    )


def _fmt_duration(secs: float | None) -> str:
    if secs is None or secs < 0:
        return "?"
    secs = int(secs)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _fmt_clock(epoch: float | None, now: float) -> str:
    """Local wall-clock time of a reset, e.g. '5:42p' (or 'Thu 7:00a' if not today)."""
    if epoch is None:
        return "?"
    dt = datetime.fromtimestamp(epoch)
    hour = dt.hour % 12 or 12
    ampm = "a" if dt.hour < 12 else "p"
    clock = f"{hour}:{dt.minute:02d}{ampm}"
    if dt.date() != datetime.fromtimestamp(now).date():
        return dt.strftime("%a ") + clock
    return clock


def _glyph(fc: WindowForecast, warn_pct: float) -> str:
    if fc.on_pace is None:
        return "·"
    if not fc.on_pace:
        return "⛔"
    if fc.projected_pct is not None and fc.projected_pct >= warn_pct:
        return "⚠"
    return "✅"


def _window_segment(label: str, fc: WindowForecast, warn_pct: float, now: float) -> str:
    if fc.current_pct is None:
        return f"{label} —"
    cur = f"{fc.current_pct:.0f}%"
    if fc.projected_pct is None or fc.on_pace is None:
        return f"{label} {cur}"
    proj = f"proj {fc.projected_pct:.0f}%"
    reset = _fmt_duration(fc.secs_to_reset)
    if fc.secs_to_reset is not None and fc.secs_to_reset >= 0:
        reset = f"{reset} · {_fmt_clock(now + fc.secs_to_reset, now)}"
    return f"{label} {cur} → {proj} {_glyph(fc, warn_pct)} ({reset})"


def _overage_markers(
    current: dict[str, Any] | None, now: float, config: dict[str, Any]
) -> list[str]:
    """When a window is currently AT/OVER the cap, a '💸 <win> +$X' marker.

    ``X`` is the notional $ burned WHILE over the cap during the current period
    (queries.overage_spend_usd, scoped to this period's start). Computed only when
    actually over, so the statusline stays quiet the rest of the time.
    """
    if not current:
        return []
    cap = float(alerts._cfg(config, "over_limit_pct"))
    out: list[str] = []
    for which in alerts.WINDOWS:
        used_col, reset_col = alerts._COLS[which]
        used = current.get(used_col)
        reset = current.get(reset_col)
        if used is None or reset is None or float(used) < cap:
            continue
        win_len = float(alerts._window_length(config, which))
        period_start = float(reset) - win_len
        spend = queries.overage_spend_usd(
            which, over_pct=cap, since=period_start, now=now
        )
        if spend > 0:
            out.append(f"💸 {which} +${spend:,.2f}")
    return out


def _evaluate_alerts(now: float, config: dict[str, Any]) -> list[str]:
    """Evaluate usage alerts for this tick and return statusline markers.

    Reads recent FRESH samples + the synthesized current reading, runs the pure
    detection engine (fires due desktop notifications, persists state), and adds
    an overage marker when currently over a cap. Fully wrapped: any failure
    degrades to no markers so the statusline never breaks.
    """
    try:
        win_s = float(alerts._cfg(config, "spike_window_seconds"))
        # Pull a little extra history so the trailing-slope window is fully covered.
        samples = queries.usage_timeseries(win_s * 1.5, now=now)
        current = queries.current_reading()
        state = alerts.run_tick(samples, current, config, now)
        return state.markers() + _overage_markers(current, now, config)
    except Exception:
        return []


def render(payload: dict[str, Any], now: float, config: dict[str, Any]) -> str:
    """Build the status line, recording a sample as a side effect."""
    sample = _sample_from_payload(payload, now)
    # Record the sample for history; the projection itself needs no history.
    try:
        db.ingest(sample)
    except Exception:
        pass

    # Both windows project from average pace since the window opened.
    fc_5h = forecast_from_period_start(
        sample.used_pct_5h,
        _get(config, "forecast", "window_5h_seconds", default=18000),
        sample.resets_at_5h,
        now,
    )
    fc_7d = forecast_from_period_start(
        sample.used_pct_7d,
        _get(config, "forecast", "window_7d_seconds", default=604800),
        sample.resets_at_7d,
        now,
    )

    warn = float(_get(config, "alerts", "warn_projected_pct", default=90.0))

    parts: list[str] = []
    model = _get(payload, "model", "display_name") or sample.model
    if model:
        parts.append(str(model))
    if sample.ctx_used_pct is not None:
        parts.append(f"ctx {sample.ctx_used_pct:.0f}%")
    parts.append(_window_segment("5h", fc_5h, warn, now))
    parts.append(_window_segment("7d", fc_7d, warn, now))

    line = "  ·  ".join(parts)

    # Evaluate alerts AFTER ingesting this tick's sample so the current reading is
    # included; prepend any markers. Never lets an alert failure break the line.
    markers = _evaluate_alerts(now, config)
    if markers:
        line = "  ".join(markers) + "  ·  " + line

    return line


def main() -> int:
    now = time.time()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("claude-usage-monitor: (no payload)")
        return 0

    try:
        config = load_config()
        print(render(payload, now, config))
    except Exception as exc:  # never disrupt the session
        print(f"claude-usage-monitor: error ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
