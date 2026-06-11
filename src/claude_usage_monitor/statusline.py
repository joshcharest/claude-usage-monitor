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
from typing import Any

from . import db
from .config import load_config
from .forecast import WindowForecast, forecast_from_period_start
from .policy import recommend


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


def _glyph(fc: WindowForecast, warn_pct: float) -> str:
    if fc.on_pace is None:
        return "·"
    if not fc.on_pace:
        return "⛔"
    if fc.projected_pct is not None and fc.projected_pct >= warn_pct:
        return "⚠"
    return "✅"


def _window_segment(label: str, fc: WindowForecast, warn_pct: float) -> str:
    if fc.current_pct is None:
        return f"{label} —"
    cur = f"{fc.current_pct:.0f}%"
    if fc.projected_pct is None or fc.on_pace is None:
        return f"{label} {cur}"
    proj = f"proj {fc.projected_pct:.0f}%"
    reset = _fmt_duration(fc.secs_to_reset)
    return f"{label} {cur} → {proj} {_glyph(fc, warn_pct)} ({reset})"


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
    parts.append(_window_segment("5h", fc_5h, warn))
    parts.append(_window_segment("7d", fc_7d, warn))

    rec = recommend(fc_5h.current_pct, config)
    if rec is not None:
        parts.append(f"→ {rec.model}/{rec.effort}")

    return "  ·  ".join(parts)


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
