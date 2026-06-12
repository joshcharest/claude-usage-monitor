"""The six dashboard panels. Each renders one tab from cached data + prep."""

from __future__ import annotations

import hashlib
import math
import time

import altair as alt
import pandas as pd
import streamlit as st

from . import data, prep
from .prep import Controls
from .. import alerts, queries
from ..forecast import forecast_from_period_start


def _render_alert_banner(config: dict, now: float) -> None:
    """Visualize active usage alerts at the top of the Pace tab.

    Read-only: uses the PURE detection (``alerts.evaluate``), so the dashboard
    never fires desktop notifications or perturbs the statusline's cooldown
    state — the statusline owns delivery, the dashboard only shows what's active.
    Best-effort; never breaks the page.
    """
    try:
        win_s = float(alerts._cfg(config, "spike_window_seconds"))
        samples = queries.usage_timeseries(win_s * 1.5, now=now)
        state = alerts.evaluate(samples, queries.current_reading(), config, now, prior={})
        for wa in state.windows.values():
            if wa.over and wa.projected_pct is not None:
                st.error(f"🚨 **{wa.window}** window projected to **{wa.projected_pct:.0f}%** "
                         "before reset — well over budget.")
            if wa.spiking and wa.slope_pp_per_min is not None:
                st.warning(f"🚨 **{wa.window}** usage spiking **+{wa.slope_pp_per_min:.1f} "
                           f"pp/min** (fresh samples, last {win_s / 60:.0f} min).")
    except Exception:
        pass

# ----------------------------------------------------------------- chart theme
# "modern-soft" dark Altair theme. Registered once at import; every chart in this
# module inherits transparent canvas, soft grid, and muted axis/legend text so
# the Vega-Lite charts blend into the Streamlit dark shell. Tokens mirror the
# injected CSS in app.py (accent #6366f1, muted text #9aa1b1, hairline grid).
_ACCENT = "#6366f1"
_AXIS_LABEL = "#9aa1b1"
_AXIS_TITLE = "#c7ccd9"
_GRID = "#2a2e3a"


@alt.theme.register("kato_modern_soft", enable=True)
def _kato_modern_soft_theme() -> alt.theme.ThemeConfig:
    return {
        "config": {
            "background": "transparent",
            "view": {"stroke": "transparent", "continuousWidth": 400,
                     "continuousHeight": 240},
            "font": "-apple-system, 'Segoe UI', Roboto, sans-serif",
            "axis": {
                "gridColor": _GRID, "gridOpacity": 0.7, "gridWidth": 1,
                "gridDash": [2, 3], "domainColor": "#3a3f4d",
                "tickColor": "#3a3f4d", "labelColor": _AXIS_LABEL,
                "titleColor": _AXIS_TITLE, "labelFontSize": 11, "titleFontSize": 12,
                "titleFontWeight": 500, "titlePadding": 8,
            },
            "axisX": {"grid": False},
            "legend": {
                "labelColor": _AXIS_LABEL, "titleColor": _AXIS_TITLE,
                "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": 600,
                "symbolType": "circle", "symbolSize": 70, "padding": 6,
            },
            "title": {"color": _AXIS_TITLE, "fontSize": 13, "fontWeight": 600},
        }
    }


def render_time(controls: Controls, config: dict) -> None:
    """Where you are in each rolling window: elapsed vs remaining + countdown."""
    latest = data.latest_sample()
    if not latest:
        st.info("No samples yet — enable the statusline monitor to populate this.")
        return
    now = time.time()
    cols = st.columns(2)
    for col, which, reset_key in zip(
        cols, ("5h", "7d"), ("resets_at_5h", "resets_at_7d")
    ):
        with col:
            win_len = prep.window_length(config, which)
            pos = prep.window_position(latest.get(reset_key), win_len, now)
            frac = pos["elapsed_frac"]
            st.subheader(f"{which} window")
            if frac is None:
                st.caption("No rate-limit data (Free tier or not reported).")
                continue
            st.progress(frac, text=f"{frac * 100:.0f}% of window elapsed")
            st.metric("Resets in", prep.fmt_duration(pos["remaining_secs"]))


def render_pace(controls: Controls, config: dict) -> None:
    """Window usage over time, stacked by conversation, with reference lines."""
    which = controls.window_focus
    _render_alert_banner(config, time.time())
    series = data.pace_rows(which, controls.window_seconds)
    st.subheader(f"{which} window — usage stacked by conversation")
    if not series:
        st.info("No pace data in this range yet — the live statusline monitor "
                "needs to record samples first.")
        return

    # Derive the KPIs from the canonical current reading (max-of-fresh) and a
    # live projection, so they agree with the chart top instead of reading one
    # arbitrary interleaved sample.
    reading = data.current_reading() or {}
    fc = forecast_from_period_start(
        reading.get(f"used_pct_{which}"), prep.window_length(config, which),
        reading.get(f"resets_at_{which}"), time.time())
    c1, c2, c3 = st.columns([1, 1.4, 1], gap="medium")
    c1.metric("Current", _pct(fc.current_pct))
    c2.metric("Projected (worst-case)", _pct(fc.projected_pct))
    on_pace = fc.on_pace
    c3.metric("Status",
              "on pace" if on_pace else ("over budget" if on_pace is False else "—"))

    # Keep branch labels fresh during live refresh (throttled to ~15s).
    try:
        data.refresh_index()
    except Exception:
        pass
    labels = data.session_labels(data.generation())
    win_len = prep.window_length(config, which)
    bucket = prep.pace_bucket_seconds(win_len, series)  # size to the focused window
    smooth = prep.pace_smooth_buckets(bucket)  # ~10-min rolling-average window
    df = prep.pace_share_frame(series, labels, bucket, smooth_buckets=smooth)
    warn = float(config.get("alerts", {}).get("warn_projected_pct", 90))

    if df.empty:
        st.caption("Not enough samples yet.")
        return

    resets = prep.pace_reset_times(series)
    st.altair_chart(_stacked_pace_chart(df, warn, resets), width="stretch")
    avg_min = max(1, round(smooth * bucket / 60))
    reset_note = (
        f" Cyan line{'s' if len(resets) != 1 else ''} mark where the {which} "
        f"window reset." if resets else ""
    )
    with st.expander("Chart notes"):
        st.caption(
            f"Account-wide {which}-window used % over time (a rolling metric — it "
            f"rises and falls as usage ages out), peak per ~{int(bucket)}s bucket "
            f"then a centered ~{avg_min}-min rolling average. Bands split the used % "
            f"by the $ each conversation spent (a proxy — the % itself is "
            f"account-wide). Dashed lines: amber warn {warn:.0f}% and slate ceiling "
            f"100%.{reset_note}"
        )

    # Total usage: the current window's rolling fill (0–100 %), reset-scoped.
    st.subheader(f"Total usage — current {which} window fill (%)")
    acc = prep.usage_accumulation_frame(
        series, labels, bucket, reset_times=resets,
        current_window_only=True, window_seconds=win_len,
    )
    if acc.empty:
        st.caption("No usage data in this range yet.")
    else:
        st.altair_chart(_cumulative_usage_chart(acc, resets), width="stretch")
        with st.expander("Chart notes"):
            st.caption(
                f"Current {which} window's fill level (0–100 %): the rolling used % "
                "since its last reset (rises and falls), split by $ spent per "
                "conversation. The cyan line marks the window reset."
            )


# A fixed categorical palette; each conversation maps to a slot by a hash of its
# name, so colors are stable per branch across refreshes without listing every
# known conversation in the legend. Ten saturated Tailwind-400 hues, each
# geometrically spaced and verified >=6.8:1 on the dark canvas (#0e1117) — no
# light tints, so adjacent stacked bands stay distinct in dark mode. The hash
# wraps mod len(_PALETTE); the 11th conversation reuses blue (acceptable).
_PALETTE = [
    "#60a5fa",  # blue
    "#fb923c",  # orange
    "#4ade80",  # green
    "#facc15",  # yellow
    "#22d3ee",  # cyan
    "#f87171",  # red
    "#a78bfa",  # violet
    "#34d399",  # emerald
    "#f472b6",  # pink
    "#fbbf24",  # amber
]


def _color_for(name: str) -> str:
    idx = int(hashlib.md5(str(name).encode()).hexdigest(), 16) % len(_PALETTE)
    return _PALETTE[idx]


def _conv_color(df):
    """Color encoding whose legend lists only the conversations PRESENT in df,
    each with a deterministic (hash-based) color that's stable across refreshes."""
    legend = alt.Legend(
        title="conversation", orient="bottom", direction="horizontal",
        columns=4, symbolSize=60, labelFontSize=10, labelLimit=120,
    )
    present = sorted(df["conversation"].dropna().unique()) if "conversation" in df else []
    if not present:
        return alt.Color("conversation:N", legend=legend)
    return alt.Color(
        "conversation:N", legend=legend,
        scale=alt.Scale(domain=present, range=[_color_for(c) for c in present]),
    )


# Per-rule visual encoding so advisory vs. boundary read at a glance:
#   warn      amber, short dash      (caution threshold)
#   ceiling   slate, long dash       (hard 100% reference boundary)
#   reset     cyan, fine dash        (window-reset event marker — cool, no hue
#             overlap with any conversation band)
_WARN_COLOR = "#f59e0b"      # amber-500
_CEILING_COLOR = "#64748b"   # slate-500
_RESET_COLOR = "#06b6d4"     # cyan-500
_ANNOTATION = "#cbd5e1"      # slate-300, lifts labels above grid text


def _time_axis(df) -> alt.Axis:
    """X-axis whose tick format adapts to the data span (intra-day vs multi-day)."""
    fmt = "%H:%M"
    try:
        span = df["time"].max() - df["time"].min()
        if pd.notna(span) and span >= pd.Timedelta(hours=24):
            fmt = "%b %d %H:%M"
    except Exception:
        pass
    return alt.Axis(format=fmt, labelAngle=0, tickCount=6, grid=False)


def _reset_lines(resets):
    return (
        alt.Chart(pd.DataFrame({"time": list(resets)}))
        .mark_rule(color=_RESET_COLOR, strokeWidth=1.5, strokeDash=[3, 2])
        .encode(x="time:T")
    )


def _cumulative_usage_chart(df, resets=None):
    area = (
        alt.Chart(df)
        .mark_area(fillOpacity=0.55, strokeOpacity=0.9, strokeWidth=0.5)
        .encode(
            x=alt.X("time:T", title=None, axis=_time_axis(df)),
            y=alt.Y("used_pct:Q", stack=True, title="used %",
                    scale=alt.Scale(domain=[0, 100]),
                    axis=alt.Axis(format="d", tickCount=5)),
            color=_conv_color(df),
            tooltip=[
                alt.Tooltip("time:T", title="time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip("conversation:N", title="conversation"),
                alt.Tooltip("used_pct:Q", title="used %", format=".0f"),
            ],
        )
    )
    ceiling = alt.Chart(pd.DataFrame({"y": [100.0]})).mark_rule(
        strokeDash=[8, 4], color=_CEILING_COLOR
    ).encode(y="y:Q")
    layers = [area, ceiling]
    if resets:
        layers.append(_reset_lines(resets))
    return alt.layer(*layers).properties(width="container", height=240)


def _stacked_pace_chart(df, warn: float, resets=None):
    area = (
        alt.Chart(df)
        .mark_area(fillOpacity=0.55, strokeOpacity=0.9, strokeWidth=0.5)
        .encode(
            x=alt.X("time:T", title=None, axis=_time_axis(df)),
            y=alt.Y("share:Q", stack=True, title="used %",
                    scale=alt.Scale(domain=[0, 105]),
                    axis=alt.Axis(format="d", tickCount=5)),
            color=_conv_color(df),
            tooltip=[
                alt.Tooltip("time:T", title="time", format="%Y-%m-%d %H:%M"),
                alt.Tooltip("conversation:N", title="conversation"),
                alt.Tooltip("share:Q", title="$-weighted share of used %",
                            format=".1f"),
            ],
        )
    )
    # Warn (amber, advisory) and ceiling (slate, boundary) get distinct encoding.
    warn_df = pd.DataFrame({"y": [warn], "label": [f"warn {warn:.0f}%"]})
    ceil_df = pd.DataFrame({"y": [100.0], "label": ["ceiling 100%"]})
    warn_rule = alt.Chart(warn_df).mark_rule(
        strokeDash=[4, 3], color=_WARN_COLOR).encode(y="y:Q")
    ceil_rule = alt.Chart(ceil_df).mark_rule(
        strokeDash=[8, 4], color=_CEILING_COLOR).encode(y="y:Q")
    warn_label = (
        alt.Chart(warn_df)
        .mark_text(align="left", dx=6, dy=-5, fontSize=10, color=_WARN_COLOR)
        .encode(x=alt.value(6), y="y:Q", text="label:N")
    )
    ceil_label = (
        alt.Chart(ceil_df)
        .mark_text(align="left", dx=6, dy=-5, fontSize=10, color=_ANNOTATION)
        .encode(x=alt.value(6), y="y:Q", text="label:N")
    )
    layers = [area, warn_rule, ceil_rule, warn_label, ceil_label]
    if resets:
        layers.append(_reset_lines(resets))
    return alt.layer(*layers).properties(width="container", height=260)


def render_usage(controls: Controls, config: dict) -> None:
    """Used % of both windows over time, plus cost over time."""
    rows = data.usage_rows(controls.window_seconds)
    usage = prep.usage_frame(rows)
    cost = prep.cost_frame(rows)
    st.subheader("Used % over time")
    if usage.empty:
        st.info("No usage samples in this range yet.")
    else:
        st.line_chart(usage, x="time", y="used_pct", color="window",
                      height=300, width="stretch")
    st.subheader("Cost over time")
    st.caption("Session running total.")
    if cost.empty:
        st.caption("No cost samples in this range.")
    else:
        st.area_chart(cost, x="time", y="cost_usd", height=220, width="stretch")


def render_models(controls: Controls, config: dict) -> None:
    """Model usage breakdown from the transcript index."""
    rows = data.model_rows(data.generation(), controls.window_seconds)
    df = prep.model_frame(rows)
    st.subheader("Messages by model")
    if df.empty:
        st.info("No model data — run the indexer (`claude-usage-index`).")
        return
    st.bar_chart(df, x="model", y="messages", height=300, width="stretch")
    st.dataframe(df, hide_index=True, width="stretch")


def render_effort(controls: Controls, config: dict) -> None:
    """Effort-level distribution from budget.db samples."""
    rows = data.effort_rows(controls.window_seconds)
    df = prep.effort_frame(rows)
    st.subheader("Samples by effort level")
    if df.empty:
        st.info("No effort samples in this range yet.")
        return
    st.bar_chart(df, x="effort", y="samples", height=300, width="stretch")
    total = int(df["samples"].sum())
    fast = int(df["fast_samples"].sum()) if "fast_samples" in df else 0
    pct = (fast / total * 100) if total else 0
    st.metric("Fast-mode samples", f"{fast} / {total} ({pct:.0f}%)")


def render_conversations(controls: Controls, config: dict) -> None:
    """Filterable conversation table with per-session drill-down."""
    gen = data.generation()
    rows = data.conversations(gen, controls.repo, controls.search)
    df = prep.conversations_frame(rows)
    st.subheader(f"Conversations ({len(df)})")
    if df.empty:
        st.info("No conversations indexed yet — run `claude-usage-index`.")
        return

    event = st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        selection_mode="single-row",
        on_select="rerun",
        column_config={
            "started_at": st.column_config.DatetimeColumn("started"),
            "duration_secs": st.column_config.NumberColumn("dur (s)", format="%d"),
            "cost_usd": st.column_config.NumberColumn("cost", format="$%.2f"),
            "models_csv": st.column_config.TextColumn("models"),
            "git_branch": st.column_config.TextColumn("branch"),
            "session_id": st.column_config.TextColumn("session", width="small"),
        },
    )
    selected = getattr(event, "selection", None)
    rows_sel = selected.get("rows", []) if isinstance(selected, dict) else getattr(selected, "rows", [])
    if rows_sel:
        sid = df.iloc[rows_sel[0]]["session_id"]
        _render_session_detail(gen, sid)


def _render_session_detail(gen: str, session_id: str) -> None:
    st.divider()
    st.markdown(f"**Session detail** · `{session_id}`")
    series = data.session_series(gen, session_id)
    if not series:
        st.caption("No live samples recorded for this session.")
        return
    usage = prep.usage_frame(series)
    if not usage.empty:
        st.line_chart(usage, x="time", y="used_pct", color="window",
                      height=260, width="stretch")
    cost = prep.cost_frame(series)
    if not cost.empty:
        st.area_chart(cost, x="time", y="cost_usd", height=200, width="stretch")


def _pct(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{v:.0f}%"
