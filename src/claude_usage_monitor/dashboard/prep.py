"""Pure chart-data-prep for the dashboard — no Streamlit, no I/O.

This is the unit-test surface. Every function takes plain query rows and returns
plain structures / pandas DataFrames ready for charting. The Streamlit layer
(`panels.py`, `kpi.py`, `app.py`) only calls these and `st.*`.

`forecast_from_period_start` and `load_config` are reused so the dashboard's
pace numbers and thresholds match the statusline exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ..forecast import forecast_from_period_start


def _local_tz():
    """Best-effort IANA local zone (DST-correct), falling back to the fixed offset."""
    key = os.environ.get("TZ")
    if not key:
        try:
            link = os.readlink("/etc/localtime")  # .../zoneinfo/Area/City
            if "zoneinfo/" in link:
                key = link.split("zoneinfo/", 1)[1]
        except OSError:
            key = None
    if key:
        try:
            return ZoneInfo(key)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


_LOCAL_TZ = _local_tz()


def to_local(ts):
    """Convert epoch seconds to tz-naive LOCAL wall-clock datetimes for display.

    Charts/tables render naive datetimes as-is, so converting UTC epoch to local
    wall-clock here makes axes and timestamps show computer/local time.
    """
    out = pd.to_datetime(ts, unit="s", utc=True)
    if isinstance(out, pd.Series):
        return out.dt.tz_convert(_LOCAL_TZ).dt.tz_localize(None)
    return out.tz_convert(_LOCAL_TZ).tz_localize(None)

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
    return to_local([r[col] for r in rows])


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


_PACE_COLS = ["time", "conversation", "share"]


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


def pace_smooth_buckets(
    series: list[dict], bucket_seconds: float, fraction: float = 0.12,
    minimum: int = 5, maximum: int = 90,
) -> int:
    """Adaptive rolling-average window (in buckets) ~``fraction`` of the range."""
    if not series or len(series) < 2 or not bucket_seconds:
        return minimum
    span = series[-1]["ts"] - series[0]["ts"]
    n_buckets = max(1, int(span / bucket_seconds))
    return max(minimum, min(maximum, round(n_buckets * fraction)))


def _label(session_id, titles: dict[str, str]) -> str:
    return titles.get(session_id) or (str(session_id)[:8] if session_id else "unknown")


def pace_share_frame(
    series: list[dict],
    labels: dict[str, str] | None = None,
    bucket_seconds: float | None = None,
    smooth_buckets: int = 1,
) -> pd.DataFrame:
    """Actual window used %, split among the conversations active in each bucket.

    ``used_pct`` is an account-wide ROLLING metric (it rises and falls as usage
    ages out), so per-conversation increments must NOT be cumulatively summed
    (that overcounts). Instead, within each time bucket we take the actual mean
    used % and allocate it across the active conversations in proportion to how
    many samples each contributed. Stacking these shares makes the stack top
    equal the true used % — bounded, never an overcount.

    ``smooth_buckets`` > 1 applies a centered rolling mean of that many buckets
    to each conversation's share series, smoothing the spiky rolling metric. The
    stack top stays the smoothed total used %.

    Returns long-format ``(time, conversation, share)`` for a stacked area.
    """
    if not series:
        return pd.DataFrame(columns=_PACE_COLS)

    labels = labels or {}
    df = pd.DataFrame(series)
    if "used_pct" not in df.columns or "session_id" not in df.columns:
        return pd.DataFrame(columns=_PACE_COLS)
    df["used_pct"] = pd.to_numeric(df["used_pct"], errors="coerce")
    df = df.dropna(subset=["used_pct"]).sort_values("ts")
    if df.empty:
        return pd.DataFrame(columns=_PACE_COLS)

    df["conversation"] = df["session_id"].map(lambda s: _label(s, labels))
    df["time"] = to_local(df["ts"])
    df["bucket"] = (
        df["time"].dt.floor(f"{int(bucket_seconds)}s") if bucket_seconds else df["time"]
    )

    rows = []
    for bucket_time, group in df.groupby("bucket"):
        total = float(group["used_pct"].mean())
        counts = group["conversation"].value_counts()
        n = int(counts.sum())
        for conv, c in counts.items():
            rows.append({"time": bucket_time, "conversation": conv,
                         "share": total * c / n})
    long = pd.DataFrame(rows, columns=_PACE_COLS)
    if long.empty or smooth_buckets <= 1:
        return long

    # Smooth each conversation's share across buckets (centered rolling mean).
    wide = long.pivot_table(
        index="time", columns="conversation", values="share", aggfunc="sum"
    ).fillna(0.0)
    wide = wide.rolling(int(smooth_buckets), center=True, min_periods=1).mean()
    return (
        wide.reset_index()
        .melt(id_vars="time", var_name="conversation", value_name="share")[_PACE_COLS]
    )


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
    df["started_at"] = to_local(df["started_at"])
    return df
