"""Pure chart-data-prep for the dashboard — no Streamlit, no I/O.

This is the unit-test surface. Every function takes plain query rows and returns
plain structures / pandas DataFrames ready for charting. The Streamlit layer
(`panels.py`, `kpi.py`, `app.py`) only calls these and `st.*`.

`forecast_from_period_start` and `load_config` are reused so the dashboard's
pace numbers and thresholds match the statusline exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..forecast import forecast_from_period_start

# Sidebar time-range options → window length in seconds (None = all history).
RANGES: dict[str, float | None] = {
    "5h": 18000.0,
    "24h": 86400.0,
    "7d": 604800.0,
    "All": None,
}


@dataclass
class Controls:
    range_label: str = "24h"
    window_focus: str = "5h"  # "5h" or "7d"
    repo: str | None = None
    search: str | None = None
    live: bool = True

    @property
    def window_seconds(self) -> float | None:
        return RANGES.get(self.range_label, 86400.0)


@dataclass
class Kpi:
    label: str
    value: str
    delta: str | None = None
    delta_color: str = "normal"  # "normal" | "inverse" | "off"
    help: str | None = None


def _cfg(config: dict, *path: str, default: Any = None) -> Any:
    cur = config
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
    return cur


def window_length(config: dict, which: str) -> float:
    if which == "7d":
        return float(_cfg(config, "forecast", "window_7d_seconds", default=604800))
    return float(_cfg(config, "forecast", "window_5h_seconds", default=18000))


def fmt_duration(secs: float | None) -> str:
    """Human duration, matching the statusline format (e.g. 2d3h, 4h12m)."""
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


def window_position(
    resets_at: float | None, window_len: float, now: float
) -> dict[str, Any]:
    """Where we are within a rolling window: elapsed vs remaining."""
    if resets_at is None:
        return {"elapsed_frac": None, "remaining_secs": None, "elapsed_secs": None}
    remaining = resets_at - now
    period_start = resets_at - window_len
    elapsed = now - period_start
    frac = max(0.0, min(elapsed / window_len, 1.0)) if window_len > 0 else None
    return {
        "elapsed_frac": frac,
        "remaining_secs": remaining,
        "elapsed_secs": elapsed,
    }


def build_kpis(latest: dict | None, config: dict, now: float) -> list[Kpi]:
    """Top-of-page KPI metrics from the latest sample + projections."""
    warn = float(_cfg(config, "alerts", "warn_projected_pct", default=90.0))
    if not latest:
        return [Kpi("5h used", "—"), Kpi("7d used", "—"), Kpi("Projected", "—"),
                Kpi("Session $", "—"), Kpi("Active", "no samples yet")]

    fc5 = forecast_from_period_start(
        latest.get("used_pct_5h"), window_length(config, "5h"),
        latest.get("resets_at_5h"), now)
    fc7 = forecast_from_period_start(
        latest.get("used_pct_7d"), window_length(config, "7d"),
        latest.get("resets_at_7d"), now)

    def pct(v):
        return f"{v:.0f}%" if v is not None else "—"

    def proj_delta(fc):
        if fc.projected_pct is None or fc.current_pct is None:
            return None, "off"
        sign = fc.projected_pct - fc.current_pct
        return f"proj {fc.projected_pct:.0f}%", ("inverse" if sign >= 0 else "normal")

    d5, c5 = proj_delta(fc5)
    d7, c7 = proj_delta(fc7)
    cost = latest.get("cost_usd")
    model = latest.get("model") or "—"
    effort = latest.get("effort")
    active = f"{model}" + (f" · {effort}" if effort else "")
    reset5 = fmt_duration(fc5.secs_to_reset)
    over = (fc5.projected_pct or 0) >= warn

    return [
        Kpi("5h used", pct(fc5.current_pct), d5, c5, f"resets in {reset5}"),
        Kpi("7d used", pct(fc7.current_pct), d7, c7,
            f"resets in {fmt_duration(fc7.secs_to_reset)}"),
        Kpi("Projected 5h", pct(fc5.projected_pct),
            "over budget" if over else "on pace",
            "inverse" if over else "normal"),
        Kpi("Session $", f"${cost:.2f}" if isinstance(cost, (int, float)) else "—"),
        Kpi("Active", active, f"5h resets in {reset5}"),
    ]


# ---------------------------------------------------------------- chart frames


def _to_dt(rows: list[dict], col: str = "ts"):
    return pd.to_datetime([r[col] for r in rows], unit="s")


def usage_frame(rows: list[dict]) -> pd.DataFrame:
    """5h/7d used % over time (long format for a multi-series line)."""
    if not rows:
        return pd.DataFrame(columns=["time", "window", "used_pct"])
    dt = _to_dt(rows)
    records = []
    for t, r in zip(dt, rows):
        if r.get("used_pct_5h") is not None:
            records.append({"time": t, "window": "5h", "used_pct": r["used_pct_5h"]})
        if r.get("used_pct_7d") is not None:
            records.append({"time": t, "window": "7d", "used_pct": r["used_pct_7d"]})
    return pd.DataFrame(records)


def cost_frame(rows: list[dict]) -> pd.DataFrame:
    """Cost over time (session running total per tick)."""
    rows = [r for r in rows if r.get("cost_usd") is not None]
    if not rows:
        return pd.DataFrame(columns=["time", "cost_usd"])
    return pd.DataFrame({"time": _to_dt(rows),
                         "cost_usd": [r["cost_usd"] for r in rows]})


_PACE_COLS = ["time", "conversation", "used_pct", "projected_pct"]


def pace_bucket_seconds(
    window_seconds: float | None, series: list[dict], target_points: int = 150
) -> float:
    """Pick a smoothing bucket size so the chart has ~``target_points`` per series."""
    if window_seconds:
        span = window_seconds
    elif series and len(series) > 1:
        ts = [s["ts"] for s in series]
        span = max(ts) - min(ts)
    else:
        span = 0.0
    if span <= 0:
        return 30.0
    return max(30.0, span / target_points)


def pace_conversation_frame(
    series: list[dict],
    titles: dict[str, str] | None = None,
    bucket_seconds: float | None = None,
) -> pd.DataFrame:
    """Actual + projected used % over time, labeled by conversation and smoothed.

    Each row carries the active conversation's title (or a short session id), so
    the chart can color the pace line per conversation. When ``bucket_seconds``
    is given, the dense ~300ms samples are resampled (mean) into time buckets
    per conversation to smooth the line.
    """
    if not series:
        return pd.DataFrame(columns=_PACE_COLS)

    titles = titles or {}
    df = pd.DataFrame(series)
    for col in ("used_pct", "projected_pct", "session_id"):
        if col not in df.columns:
            df[col] = None
    df[["used_pct", "projected_pct"]] = df[["used_pct", "projected_pct"]].apply(
        pd.to_numeric, errors="coerce"
    )
    df["conversation"] = df["session_id"].map(
        lambda s: titles.get(s) or (str(s)[:8] if s else "unknown")
    )
    df["time"] = pd.to_datetime(df["ts"], unit="s")

    if bucket_seconds:
        df = (
            df.set_index("time")
            .groupby("conversation")[["used_pct", "projected_pct"]]
            .resample(f"{int(bucket_seconds)}s")
            .mean()
            .reset_index()
            .dropna(subset=["used_pct", "projected_pct"], how="all")
        )
    return df[_PACE_COLS]


def model_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["model", "messages", "sessions"])
    return pd.DataFrame(rows)


def effort_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["effort", "samples", "fast_samples"])
    return pd.DataFrame(rows)


# Friendly column order/labels for the conversations table.
_CONV_COLUMNS = [
    "title", "repo", "git_branch", "started_at", "duration_secs",
    "message_count", "turn_count", "models_csv", "cost_usd", "session_id",
]


def conversations_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=_CONV_COLUMNS)
    df = pd.DataFrame(rows)
    for col in _CONV_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[_CONV_COLUMNS].copy()
    df["started_at"] = pd.to_datetime(df["started_at"], unit="s")
    return df
