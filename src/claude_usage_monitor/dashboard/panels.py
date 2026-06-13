"""The six dashboard panels. Each renders one tab from cached data + prep."""

from __future__ import annotations

import hashlib
import time

import altair as alt
import pandas as pd
import streamlit as st

from . import data, prep
from .prep import Controls
from .. import alerts, queries


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


def render_pace(controls: Controls, config: dict, which: str) -> None:
    """Pace for one window ("5h" or "7d"): a rolling worst-case projection (left)
    and the accumulated fill of the CURRENT window (right). The two Pace tabs call
    this with their window, so the layouts are identical bar the time scale.

    The reference line marks where the current period STARTED — taken authorita-
    tively from the live reading (``resets_at - window``), not from reset-event
    detection (which records messy historical boundaries)."""
    _render_alert_banner(config, time.time())
    now = time.time()
    series = data.pace_rows(which, controls.window_seconds)
    if not series:
        st.info("No pace data in this range yet — the live statusline monitor "
                "needs to record samples first.")
        return

    # Keep branch labels fresh during live refresh (throttled to ~15s).
    try:
        data.refresh_index()
    except Exception:
        pass
    labels = data.session_labels(data.generation())
    win_len = prep.window_length(config, which)
    bucket = prep.pace_bucket_seconds(win_len, series)
    smooth = prep.pace_smooth_buckets(bucket)
    warn = float(config.get("alerts", {}).get("warn_projected_pct", 90))
    multiday = win_len >= 86400  # 7d -> the time axis needs dates, not just clock

    # Authoritative current-period start from the live reading: resets_at - window.
    latest = data.current_reading()
    reset_at = latest.get(f"resets_at_{which}") if latest else None
    period_start = prep.to_local([float(reset_at) - win_len])[0] if reset_at else None
    reset_end = prep.to_local([float(reset_at)])[0] if reset_at else None
    reset_lines = [period_start] if period_start is not None else []

    # LEFT: worst-case projection over a ROLLING last-`win_len` view.
    proj = prep.pace_projection_frame(series, bucket, smooth_buckets=smooth)
    proj_domain = (prep.to_local([now - win_len])[0], prep.to_local([now])[0])
    if not proj.empty:
        proj = proj[proj["time"] >= proj_domain[0]]

    # RIGHT: accumulated fill of the CURRENT window [period_start, reset]. The
    # period start is the accumulation cutoff so the fill restarts at the reset.
    acc = prep.usage_accumulation_frame(
        series, labels, bucket, reset_times=reset_lines,
        current_window_only=True, window_seconds=win_len, cumulative=True,
    )
    win_domain = (period_start, reset_end) if period_start is not None else None

    if proj.empty and acc.empty:
        st.caption("Not enough samples yet.")
        return

    avg_min = max(1, round(smooth * bucket / 60))
    reset_note = " The cyan line marks where the current period started." if reset_lines else ""

    # Fixed shared y-axis top (0–150) so both charts ALIGN: identical gridlines
    # and y-label widths, which makes their plot areas — and thus x-axis lengths —
    # equal under width="stretch". A projection above 150% clips at the top.
    y_top = 150.0

    # Side-by-side so both charts fit above the fold. Only the right chart is
    # split by conversation, so its legend is suppressed and drawn once below.
    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown(f"**{which} window — projected usage (worst-case)**")
        if proj.empty:
            st.caption("Not enough samples yet.")
        else:
            st.altair_chart(
                _projection_chart(proj, warn, reset_lines, x_domain=proj_domain,
                                  y_top=y_top, with_date=multiday), width="stretch")
    with right:
        st.markdown(f"**Total usage — accumulated over the current {which} window (%)**")
        if acc.empty:
            st.caption("No usage data in this range yet.")
        else:
            st.altair_chart(
                _cumulative_usage_chart(acc, reset_lines, legend=False,
                                        x_domain=win_domain, y_top=y_top,
                                        with_date=multiday), width="stretch")

    legend = _shared_conv_legend(acc)
    if legend is not None:
        st.altair_chart(legend, width="stretch")

    with st.expander("Chart notes"):
        st.caption(
            f"**Left — projected usage.** Account-wide worst-case projection of the "
            f"{which}-window used % over a rolling last-{which} view: each sample's "
            f"run-rate forecast to the window end, peak per ~{int(bucket)}s bucket "
            f"then a centered ~{avg_min}-min rolling average. Dashed lines: amber "
            f"warn {warn:.0f}% and slate ceiling 100%.{reset_note}"
        )
        st.caption(
            f"**Right — total usage.** Accumulated used % over the current {which} "
            "window: the running high-water mark since the period started, so it "
            "only climbs, split by each conversation's share of the cumulative $ "
            "spent. The x-axis spans the full window, so the gap to the right edge "
            "is the time left until reset; the cyan line marks the period start."
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


def _conv_color(df, *, legend: bool = True):
    """Color encoding for the conversation field. With legend=False the scale
    (and thus the colors) is preserved but no legend is drawn, so two charts can
    share ONE legend rendered separately. Each conversation keeps its
    deterministic (hash-based) color that's stable across refreshes."""
    leg = alt.Legend(
        title="conversation", orient="bottom", direction="horizontal",
        columns=4, symbolSize=60, labelFontSize=10, labelLimit=120,
    ) if legend else None
    present = sorted(df["conversation"].dropna().unique()) if "conversation" in df else []
    if not present:
        return alt.Color("conversation:N", legend=leg)
    return alt.Color(
        "conversation:N", legend=leg,
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

# Shared height for the two side-by-side Pace charts: tall enough to fill the
# viewport once the KPI tiles are compacted, while still fitting without scroll.
_CHART_H = 540


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


def _clock_axis(*, with_date: bool = False) -> alt.Axis:
    """X time axis as a 12-hour clock (never military). ``with_date`` adds the
    month/day for multi-day spans (the 7d window); otherwise it's time-only
    (e.g. '3:45 PM'). Leading zeros on hour/day are stripped via a Vega expr."""
    if with_date:
        expr = "replace(timeFormat(datum.value, '%b %d %I%p'), / 0/g, ' ')"  # 'Jun 6 6PM'
    else:
        expr = "replace(timeFormat(datum.value, '%I:%M %p'), /^0/, '')"      # '3:45 PM'
    return alt.Axis(labelExpr=expr, labelAngle=0, tickCount=6, grid=False)


def _reset_lines(resets):
    return (
        alt.Chart(pd.DataFrame({"time": list(resets)}))
        .mark_rule(color=_RESET_COLOR, strokeWidth=1.5, strokeDash=[3, 2])
        .encode(x="time:T")
    )


def _shared_conv_legend(*frames):
    """A standalone bottom legend covering every conversation in `frames`, drawn
    once beneath the two side-by-side charts. Uses the same hash-based colors as
    the charts, so it is a faithful key for both. Building it from the UNION of
    the frames matters: the cumulative frame is current-window-only (a subset),
    so a per-frame legend could omit an entry shown in the stacked chart."""
    names = sorted({
        c for f in frames if f is not None and "conversation" in f
        for c in f["conversation"].dropna().unique()
    })
    if not names:
        return None
    leg_df = pd.DataFrame({"conversation": names})
    return (
        alt.Chart(leg_df)
        .mark_point(filled=True, size=50, opacity=0)  # invisible marks; legend only
        .encode(
            x=alt.value(0), y=alt.value(0),
            color=alt.Color(
                "conversation:N",
                scale=alt.Scale(domain=names, range=[_color_for(c) for c in names]),
                legend=alt.Legend(
                    title=None, orient="bottom", direction="horizontal",
                    columns=6, symbolSize=50, labelFontSize=10, labelLimit=120,
                    rowPadding=0, padding=2,
                ),
            ),
        )
        .properties(width="container", height=1)
    )


def _cumulative_usage_chart(df, resets=None, *, legend: bool = True, x_domain=None,
                            y_top=None, with_date=False):
    # Pin x to the FULL current window when given (so the chart shows the whole
    # 5h span — elapsed fill plus the remaining time to reset); otherwise fall
    # back to the data's own span.
    if x_domain is not None and x_domain[0] is not None and x_domain[1] is not None:
        x_min, x_max = x_domain
    else:
        x_min, x_max = df["time"].min(), df["time"].max()
    y_max = float(y_top) if y_top is not None else 100.0
    area = (
        alt.Chart(df)
        .mark_area(fillOpacity=0.55, strokeOpacity=0.9, strokeWidth=0.5)
        .encode(
            x=alt.X("time:T", title=None, axis=_clock_axis(with_date=with_date),
                    scale=alt.Scale(domain=[x_min, x_max], nice=False)),
            y=alt.Y("used_pct:Q", stack=True, title="used %",
                    scale=alt.Scale(domain=[0, y_max]),
                    axis=alt.Axis(format="d", tickCount=5)),
            color=_conv_color(df, legend=legend),
            tooltip=[
                alt.Tooltip("time:T", title="time",
                            format="%b %d, %I:%M %p" if with_date else "%I:%M %p"),
                alt.Tooltip("conversation:N", title="conversation"),
                alt.Tooltip("used_pct:Q", title="used %", format=".0f"),
            ],
        )
    )
    ceiling = alt.Chart(pd.DataFrame({"y": [100.0]})).mark_rule(
        strokeDash=[8, 4], color=_CEILING_COLOR
    ).encode(y="y:Q")
    layers = [area, ceiling]
    in_window = [r for r in (resets or []) if x_min <= r <= x_max]
    if in_window:
        layers.append(_reset_lines(in_window))
    return alt.layer(*layers).properties(width="container", height=_CHART_H)


def _projection_chart(df, warn: float, resets=None, *, x_domain=None, y_top=None,
                      with_date=False):
    """Account-wide worst-case PROJECTED used % over time — one trajectory (not
    split by conversation), with the warn and ceiling reference lines. ``x_domain``
    pins the time axis (e.g. a rolling window); the axis is a 12-hour clock, with
    the date added when ``with_date`` (multi-day 7d view). ``y_top`` pins the
    y-axis so it can be shared with the fill chart for aligned gridlines."""
    # Headroom above the data so a projection over 100% stays readable, but cap
    # it so a freak spike can't flatten the whole series against the floor.
    top = float(y_top) if y_top is not None else (
        min(200.0, max(105.0, float(df["projected_pct"].max()) + 5.0))
    )
    x_scale = (
        alt.Scale(domain=list(x_domain), nice=False)
        if x_domain is not None and x_domain[0] is not None and x_domain[1] is not None
        else alt.Undefined
    )
    area = (
        alt.Chart(df)
        .mark_area(
            color=_ACCENT, fillOpacity=0.18,
            line={"color": _ACCENT, "strokeWidth": 2},
        )
        .encode(
            x=alt.X("time:T", title=None, axis=_clock_axis(with_date=with_date),
                    scale=x_scale),
            y=alt.Y("projected_pct:Q", title="projected %",
                    scale=alt.Scale(domain=[0, top]),
                    axis=alt.Axis(format="d", tickCount=5)),
            tooltip=[
                alt.Tooltip("time:T", title="time",
                            format="%b %d, %I:%M %p" if with_date else "%I:%M %p"),
                alt.Tooltip("projected_pct:Q", title="projected %", format=".0f"),
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
    rs = resets or []
    if x_scale is not alt.Undefined:
        lo, hi = x_domain
        rs = [r for r in rs if lo <= r <= hi]  # only resets inside the pinned window
    if rs:
        layers.append(_reset_lines(rs))
    return alt.layer(*layers).properties(width="container", height=_CHART_H)


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

    _render_overage(controls, config)


def _render_overage(controls: Controls, config: dict) -> None:
    """Notional $ burned while a window was at/over the limit, for this range."""
    cap = float(config.get("alerts", {}).get("over_limit_pct", 100.0))
    o5 = data.overage("5h", controls.window_seconds, cap)
    o7 = data.overage("7d", controls.window_seconds, cap)
    st.subheader("💸 Overage — spend while over the limit")
    st.caption(
        f"Notional $ (token-cost equivalent) burned while a window was at/over "
        f"{cap:.0f}% used, within the selected range. Subscription plans don't "
        f"bill this — it's how much usage you packed in past the cap."
    )
    c5, c7 = st.columns(2)
    c5.metric("5h overage", f"${o5:,.2f}")
    c7.metric("7d overage", f"${o7:,.2f}")


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
