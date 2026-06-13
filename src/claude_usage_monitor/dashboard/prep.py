"""Pure chart-data-prep for the dashboard — no Streamlit, no I/O.

This is the unit-test surface. Every function takes plain query rows and returns
plain structures / pandas DataFrames ready for charting. The Streamlit layer
(`panels.py`, `kpi.py`, `app.py`) only calls these and `st.*`.

`forecast_for_window` and `load_config` are reused so the dashboard's pace
numbers and thresholds match the statusline exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ..forecast import forecast_for_window


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


def month_start_epoch(now: float, tz=None) -> float:
    """Epoch seconds for 00:00 on the 1st of ``now``'s month, in LOCAL time."""
    local = datetime.fromtimestamp(now, tz or _LOCAL_TZ)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()

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
    help: str | None = None
    # Drives the tile's own color (border + tint) via a CSS class in kpi.py:
    # "ok" (on pace) | "warn" | "over" | None (neutral). Replaces the old delta
    # "pill", so every tile is the same height.
    status: str | None = None
    # Optional 0–1 fraction that draws a slim progress bar under the value (used %
    # for the Used tiles, window-elapsed fraction for the Time Left tiles). None =
    # no bar, so non-gauge tiles (Projected, Over-limit Spend) stay text-only.
    progress: float | None = None
    # Cluster id ("5h" / "7d" / "other") used by kpi.py to draw a vertical
    # separator wherever the group changes, so the eye reads the tiles as windows.
    group: str | None = None


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


def build_kpis(
    latest: dict | None,
    config: dict,
    now: float,
    overage_usd: float | None = None,
    recent: list[dict] | None = None,
) -> list[Kpi]:
    """Top-of-page KPIs: a 5h trio, a 7d trio, then an over-limit-spend tile.

    The two Projected tiles carry a ``status`` ("ok"/"warn"/"over") that colors the
    tile itself; ``overage_usd`` (notional $ spent while over the cap) populates the
    far-right tile.

    ``recent`` is the trailing raw sample stream (newest-last) the dashboard fetches
    so the layered projector can fit a slope; when ``None`` (or ``[forecast].model``
    is off) the projection is the legacy ratio model — byte-identical to before — so
    callers that pass no history stay on the old numbers.
    """
    warn = float(_cfg(config, "alerts", "warn_projected_pct", default=90.0))
    money = (f"${overage_usd:,.2f}" if overage_usd is not None else "—")
    if not latest:
        return [
            Kpi("Time Left", "—", group="5h"), Kpi("5 Hour Used", "—", group="5h"),
            Kpi("5 Hour Projected", "—", group="5h"),
            Kpi("Time Left", "—", group="7d"), Kpi("7 Day Used", "—", group="7d"),
            Kpi("7 Day Projected", "—", group="7d"),
            Kpi("Monthly Overage", money, group="other",
            help="notional $ used while over the limit, this calendar month"),
        ]

    fc5 = forecast_for_window(
        "5h", latest.get("used_pct_5h"), latest.get("resets_at_5h"),
        window_length(config, "5h"), now, recent, config)
    fc7 = forecast_for_window(
        "7d", latest.get("used_pct_7d"), latest.get("resets_at_7d"),
        window_length(config, "7d"), now, recent, config)

    def pct(v):
        return f"{v:.0f}%" if v is not None else "—"

    def used_frac(v):
        # Fill the bar by current used %, clamped so an over-limit reading pins
        # the bar full rather than overflowing st.progress's 0–1 contract.
        return None if v is None else max(0.0, min(v / 100.0, 1.0))

    def elapsed_frac(which):
        return window_position(
            latest.get(f"resets_at_{which}"), window_length(config, which), now
        )["elapsed_frac"]

    def proj_status(fc):
        # Tile color: >=100% over (red), >=warn caution (amber), else on pace
        # (green). Judge by the projection when it's meaningful; early in a window
        # (on_pace None) the projection is noisy, so judge by current used% instead
        # so the tile still shows a sensible color rather than going neutral.
        basis = fc.projected_pct if fc.on_pace is not None else fc.current_pct
        if basis is None:
            return None
        if basis >= 100.0:
            return "over"
        if basis >= warn:
            return "warn"
        return "ok"

    def proj_help(fc):
        # Surface the layered model's uncertainty band in the tile tooltip (small +
        # safe: no layout change). The ratio fallback has no band, so we just label
        # the expected projection. TODO: a richer in-tile band visual (e.g. a
        # low–high range under the value) could replace this tooltip later.
        if fc.projected_low is not None and fc.projected_high is not None:
            return (
                f"expected end-of-window usage; likely range "
                f"{fc.projected_low:.0f}–{fc.projected_high:.0f}%"
            )
        return "projected end-of-window usage"

    return [
        Kpi("Time Left", fmt_duration(fc5.secs_to_reset),
            help="until the 5h window resets", progress=elapsed_frac("5h"), group="5h"),
        Kpi("5 Hour Used", pct(fc5.current_pct),
            progress=used_frac(fc5.current_pct), group="5h"),
        Kpi("5 Hour Projected", pct(fc5.projected_pct),
            status=proj_status(fc5), help=proj_help(fc5), group="5h"),
        Kpi("Time Left", fmt_duration(fc7.secs_to_reset),
            help="until the 7d window resets", progress=elapsed_frac("7d"), group="7d"),
        Kpi("7 Day Used", pct(fc7.current_pct),
            progress=used_frac(fc7.current_pct), group="7d"),
        Kpi("7 Day Projected", pct(fc7.projected_pct),
            status=proj_status(fc7), help=proj_help(fc7), group="7d"),
        Kpi("Monthly Overage", money, group="other",
            help="notional $ used while over the limit, this calendar month"),
    ]


# ---------------------------------------------------------------- chart frames


def _to_dt(rows: list[dict], col: str = "ts"):
    return to_local([r[col] for r in rows])


# Cap on points fed to the Usage-tab line/area charts. The raw per-sample stream
# is huge over wide ranges (tens of thousands of points → very slow to render),
# so frames downsample by time bucket to ~this many points. ~800 keeps lines
# smooth while staying responsive.
_USAGE_MAX_POINTS = 800


def _downsample_by_time(df: pd.DataFrame, max_points: int, ts_col: str = "ts"):
    """Reduce a per-sample frame to ~``max_points`` rows by time-bucketing, keeping
    the last non-null value per bucket. No-op when already small enough."""
    n = len(df)
    if n <= max_points:
        return df
    df = df.sort_values(ts_col)
    span = float(df[ts_col].iloc[-1] - df[ts_col].iloc[0])
    if span <= 0:
        return df.iloc[:: max(1, n // max_points)]
    bucket = (df[ts_col] // (span / max_points)).astype("int64")
    return df.groupby(bucket, sort=True).last().reset_index(drop=True)


def usage_frame(rows: list[dict], max_points: int = _USAGE_MAX_POINTS) -> pd.DataFrame:
    """5h/7d used % over time (long format for a multi-series line).

    Time-bucketed down to ~``max_points`` per window so wide ranges stay fast.
    """
    cols = ["time", "window", "used_pct"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = _downsample_by_time(pd.DataFrame(rows), max_points)
    if "ts" not in df.columns:
        return pd.DataFrame(columns=cols)
    df = df.assign(time=to_local(df["ts"]))
    parts = []
    for window, col in (("5h", "used_pct_5h"), ("7d", "used_pct_7d")):
        if col in df.columns:
            sub = df.loc[df[col].notna(), ["time", col]].rename(columns={col: "used_pct"})
            sub["window"] = window
            parts.append(sub[cols])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)


def cost_frame(rows: list[dict], max_points: int = _USAGE_MAX_POINTS) -> pd.DataFrame:
    """Cost over time (session running total per tick), time-bucketed for speed."""
    cols = ["time", "cost_usd"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows)
    if "cost_usd" not in df.columns or "ts" not in df.columns:
        return pd.DataFrame(columns=cols)
    df = df[df["cost_usd"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = _downsample_by_time(df, max_points)
    # to_numpy() on both so a non-contiguous index (from the notna filter) can't
    # misalign the two columns during DataFrame construction.
    return pd.DataFrame({"time": to_local(df["ts"]).to_numpy(),
                         "cost_usd": df["cost_usd"].to_numpy()})


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
    bucket_seconds: float, target_seconds: float = 600.0, maximum: int = 120,
) -> int:
    """Rolling-average window (in buckets) approximating ``target_seconds``.

    A fixed time target (~10 min) rather than a fraction of the range, so the
    smoothing stays the same regardless of how wide the view is.
    """
    if not bucket_seconds or bucket_seconds <= 0:
        return 1
    return max(1, min(maximum, round(target_seconds / bucket_seconds)))


def pace_reset_times(series: list[dict], min_advance_seconds: float = 3600.0):
    """Local times where the focused window actually reset.

    budget.db interleaves multiple concurrent sessions, each reporting its own
    (sometimes stale) ``reset_at`` snapshot, so the raw value flaps. A genuine
    reset is the window's reset time advancing past its running maximum by more
    than ``min_advance_seconds`` (a new block, not sub-hour jitter); the reset
    happened at the boundary that was crossed (the old running max).
    """
    resets: list[float] = []
    running_max: float | None = None
    for row in series:
        ra = row.get("reset_at")
        # Skip stale snapshots: a real reset time is in the FUTURE relative to
        # its sample. Lagging concurrent sessions report past reset_at values.
        if ra is None or ra <= row.get("ts", ra):
            continue
        if running_max is None:
            running_max = ra
        elif ra > running_max + min_advance_seconds:
            resets.append(running_max)  # boundary crossed = reset moment
            running_max = ra
        elif ra > running_max:
            running_max = ra  # small advance = snapshot jitter, not a reset
    uniq = sorted(set(resets))
    return list(to_local(uniq)) if uniq else []


def _label(session_id, titles: dict[str, str]) -> str:
    return titles.get(session_id) or (str(session_id)[:8] if session_id else "unknown")


def _bucketed(
    series: list[dict], labels: dict[str, str] | None, bucket_seconds: float | None
) -> pd.DataFrame:
    """Per-sample df (conversation, time, bucket, cost_usd) with STALE rows dropped.

    Drops snapshots whose ``reset_at`` is already in the past relative to the
    sample's own ``ts`` (same predicate as :func:`pace_reset_times`). Lagging or
    frozen concurrent sessions report stale account-wide values that would
    otherwise dominate the per-bucket ``max``. Returns an empty df if nothing
    usable remains.
    """
    if not series:
        return pd.DataFrame()
    labels = labels or {}
    df = pd.DataFrame(series)
    if "used_pct" not in df.columns or "session_id" not in df.columns:
        return pd.DataFrame()
    df["used_pct"] = pd.to_numeric(df["used_pct"], errors="coerce")
    df = df.dropna(subset=["used_pct"]).sort_values("ts")
    if "reset_at" in df.columns and "ts" in df.columns:
        df = df[df["reset_at"].isna() | (df["reset_at"] > df["ts"])]
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["conversation"] = df["session_id"].map(lambda s: _label(s, labels))
    df["time"] = to_local(df["ts"])
    df["bucket"] = (
        df["time"].dt.floor(f"{int(bucket_seconds)}s") if bucket_seconds else df["time"]
    )
    df["cost_usd"] = (
        pd.to_numeric(df["cost_usd"], errors="coerce")
        if "cost_usd" in df.columns else float("nan")
    )
    return df


def _spend_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(spend, present) wide frames indexed by bucket, columns = conversation.

    ``spend`` = $ spent IN each bucket per conversation (``cost_usd`` is a session
    running total, so per session: last value per bucket, forward-fill, positive
    diff, aggregated by conversation). ``present`` = 1.0 where a conversation has
    any sample in a bucket, else 0.0 (for the equal-split fallback).
    """
    buckets = sorted(df["bucket"].unique())
    convs = sorted(df["conversation"].unique())
    spend = pd.DataFrame(0.0, index=buckets, columns=convs)
    sess_to_conv = dict(zip(df["session_id"], df["conversation"]))
    sess_last = df.groupby(["session_id", "bucket"])["cost_usd"].last()
    for sid, s in sess_last.groupby(level=0):
        vals = s.droplevel(0).reindex(buckets).ffill()
        delta = vals.diff().clip(lower=0).fillna(0.0)
        conv = sess_to_conv.get(sid)
        if conv in spend.columns:
            spend[conv] = spend[conv].add(delta, fill_value=0.0)
    present = (
        df.groupby(["bucket", "conversation"]).size().unstack(fill_value=0)
        .reindex(index=buckets, columns=convs, fill_value=0).gt(0).astype(float)
    )
    return spend, present


def _normalize_weights(spend: pd.DataFrame, present: pd.DataFrame) -> pd.DataFrame:
    """Row-normalize ``spend`` to weights summing to 1 per bucket; buckets with no
    spend fall back to an EQUAL split among the conversations present."""
    total = spend.sum(axis=1)
    by_cost = spend.div(total.where(total > 0), axis=0)
    pres_n = present.sum(axis=1)
    equal = present.div(pres_n.where(pres_n > 0, 1), axis=0)
    return by_cost.where(total.gt(0), equal)


def _cost_weights(df: pd.DataFrame) -> pd.DataFrame:
    """Per-(bucket, conversation) weight in [0,1], summing to 1 per bucket,
    weighted by the $ spent IN that bucket. Returns a wide frame indexed by
    bucket, columns = conversation."""
    spend, present = _spend_matrix(df)
    return _normalize_weights(spend, present)


def _cost_weights_cumulative(df: pd.DataFrame) -> pd.DataFrame:
    """Like :func:`_cost_weights`, but weighted by CUMULATIVE $ spent up to each
    bucket — so an accumulating total is attributed by each conversation's share
    of the running spend. Before any spend, falls back to an equal split."""
    spend, present = _spend_matrix(df)
    return _normalize_weights(spend.cumsum(axis=0), present)


def pace_share_frame(
    series: list[dict],
    labels: dict[str, str] | None = None,
    bucket_seconds: float | None = None,
    smooth_buckets: int = 1,
) -> pd.DataFrame:
    """Account-wide window used % over time, split by conversation, for a stack.

    ``used_pct`` is an account-wide ROLLING metric (rises and falls as usage ages
    out), reported by several concurrent sessions including stale ones. Per bucket
    we take the MAX over FRESH samples (stale dropped in :func:`_bucketed`) = the
    true reading, then split it across conversations weighted by the $ each spent
    in that bucket (:func:`_cost_weights`). Stacking sums to the true used %.

    ``smooth_buckets`` > 1 applies a centered rolling mean per conversation.
    Returns long-format ``(time, conversation, share)``.
    """
    df = _bucketed(series, labels, bucket_seconds)
    if df.empty:
        return pd.DataFrame(columns=_PACE_COLS)

    peak = df.groupby("bucket")["used_pct"].max().sort_index()
    weights = _cost_weights(df).reindex(peak.index).fillna(0.0)
    band = weights.mul(peak, axis=0)  # bucket × conversation, sums to peak
    long = (
        band.reset_index().melt(id_vars="bucket", var_name="conversation",
                                value_name="share")
        .rename(columns={"bucket": "time"})[_PACE_COLS]
    )
    if long.empty or smooth_buckets <= 1:
        return long

    wide = long.pivot_table(
        index="time", columns="conversation", values="share", aggfunc="sum"
    ).fillna(0.0)
    wide = wide.rolling(int(smooth_buckets), center=True, min_periods=1).mean()
    return (
        wide.reset_index()
        .melt(id_vars="time", var_name="conversation", value_name="share")[_PACE_COLS]
    )


_USAGE_COLS = ["time", "conversation", "used_pct"]


def _current_window_cutoff(data_max, reset_times, window_seconds):
    """Start of the CURRENT window: the most recent reset boundary that has
    already occurred (<= ``data_max``), else ``data_max - window_seconds``, else
    None. A future boundary (flaky/advanced resets_at) is ignored so it can't
    clip the whole series away."""
    past_resets = sorted(
        r for r in (pd.Timestamp(t) for t in (reset_times or [])) if r <= data_max
    )
    if past_resets:
        return past_resets[-1]
    if window_seconds:
        return data_max - pd.Timedelta(seconds=float(window_seconds))
    return None


def usage_accumulation_frame(
    series: list[dict],
    labels: dict[str, str] | None = None,
    bucket_seconds: float | None = None,
    reset_times=None,
    current_window_only: bool = False,
    window_seconds: float | None = None,
    cumulative: bool = False,
) -> pd.DataFrame:
    """Fill of the focused window (0–100 %), split by conversation.

    Same peak-of-fresh + cost-weighted split as :func:`pace_share_frame`. By
    default the value is the window's rolling fill (it rises AND falls — no
    high-water latch). With ``cumulative=True`` it instead ACCUMULATES over the
    window: the running-max of the total used % (so it only climbs), attributed by
    each conversation's share of the CUMULATIVE $ spent.

    ``current_window_only`` clips to the current window: buckets at/after the most
    recent reset boundary that has ALREADY occurred, or the last ``window_seconds``
    if no past reset was detected. A boundary in the future (flaky/advanced
    ``resets_at`` data) is ignored — it can't start the current window, and using
    it would clip the whole series away (empty chart). Returns long-format
    ``(time, conversation, used_pct)``.
    """
    df = _bucketed(series, labels, bucket_seconds)
    if df.empty:
        return pd.DataFrame(columns=_USAGE_COLS)

    if cumulative:
        # Clip to the current window FIRST so accumulation starts at the reset
        # (not carrying the prior window's high-water), then take the running max.
        if current_window_only:
            cutoff = _current_window_cutoff(
                df["bucket"].max(), reset_times, window_seconds
            )
            if cutoff is not None:
                df = df[df["bucket"] >= cutoff]
            if df.empty:
                return pd.DataFrame(columns=_USAGE_COLS)
        peak = df.groupby("bucket")["used_pct"].max().sort_index().cummax()
        weights = _cost_weights_cumulative(df).reindex(peak.index).fillna(0.0)
        band = weights.mul(peak, axis=0)
        return (
            band.reset_index().melt(id_vars="bucket", var_name="conversation",
                                    value_name="used_pct")
            .rename(columns={"bucket": "time"})[_USAGE_COLS]
        )

    peak = df.groupby("bucket")["used_pct"].max().sort_index()
    weights = _cost_weights(df).reindex(peak.index).fillna(0.0)
    band = weights.mul(peak, axis=0)
    long = (
        band.reset_index().melt(id_vars="bucket", var_name="conversation",
                                value_name="used_pct")
        .rename(columns={"bucket": "time"})[_USAGE_COLS]
    )

    if current_window_only and not long.empty:
        cutoff = _current_window_cutoff(
            long["time"].max(), reset_times, window_seconds
        )
        if cutoff is not None:
            long = long[long["time"] >= cutoff]
    return long[_USAGE_COLS]


_PROJ_COLS = ["time", "projected_pct"]


def pace_projection_frame(
    series: list[dict],
    bucket_seconds: float | None = None,
    smooth_buckets: int = 1,
) -> pd.DataFrame:
    """Account-wide worst-case PROJECTED used % over time — a single trajectory.

    Uses the same freshness/bucketing as :func:`pace_share_frame` but tracks the
    forecast (``projected_pct``) rather than realized usage and does NOT split by
    conversation: the projection is an account-wide figure. Per bucket we take the
    MAX over fresh samples, then optionally a centered rolling mean. Returns
    long-format ``(time, projected_pct)``.
    """
    df = _bucketed(series, None, bucket_seconds)
    if df.empty or "projected_pct" not in df.columns:
        return pd.DataFrame(columns=_PROJ_COLS)
    df = df.copy()
    df["projected_pct"] = pd.to_numeric(df["projected_pct"], errors="coerce")
    df = df.dropna(subset=["projected_pct"])
    if df.empty:
        return pd.DataFrame(columns=_PROJ_COLS)
    peak = df.groupby("bucket")["projected_pct"].max().sort_index()
    if smooth_buckets and smooth_buckets > 1:
        peak = peak.rolling(int(smooth_buckets), center=True, min_periods=1).mean()
    out = peak.reset_index()
    out.columns = _PROJ_COLS
    return out[_PROJ_COLS]


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
