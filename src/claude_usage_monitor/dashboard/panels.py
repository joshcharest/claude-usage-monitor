"""The six dashboard panels. Each renders one tab from cached data + prep."""

from __future__ import annotations

import math
import time

import altair as alt
import pandas as pd
import streamlit as st

from . import data, prep
from .prep import Controls
from ..forecast import forecast_from_period_start


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
            st.markdown(f"**{which} window**")
            if frac is None:
                st.caption("No rate-limit data (Free tier or not reported).")
                continue
            st.progress(frac, text=f"{frac * 100:.0f}% of window elapsed")
            st.metric("Resets in", prep.fmt_duration(pos["remaining_secs"]))


def render_pace(controls: Controls, config: dict) -> None:
    """Window usage over time, stacked by conversation, with reference lines."""
    which = controls.window_focus
    series = data.pace_rows(which, controls.window_seconds)
    st.markdown(f"**{which} window — usage stacked by conversation**")
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
    c1, c2, c3 = st.columns(3)
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
    color_domain = sorted(set(labels.values())) or None
    win_len = prep.window_length(config, which)
    bucket = prep.pace_bucket_seconds(win_len, series)  # size to the focused window
    smooth = prep.pace_smooth_buckets(bucket)  # ~10-min rolling-average window
    df = prep.pace_share_frame(series, labels, bucket, smooth_buckets=smooth)
    warn = float(config.get("alerts", {}).get("warn_projected_pct", 90))

    if df.empty:
        st.caption("Not enough samples yet.")
        return

    resets = prep.pace_reset_times(series)
    st.altair_chart(_stacked_pace_chart(df, warn, resets, color_domain),
                    use_container_width=True)
    avg_min = max(1, round(smooth * bucket / 60))
    reset_note = (
        f" Red line{'s' if len(resets) != 1 else ''} mark where the {which} "
        f"window reset." if resets else ""
    )
    st.caption(
        f"Account-wide {which}-window used % over time (a rolling metric — it rises "
        f"and falls as usage ages out), peak per ~{int(bucket)}s bucket then a "
        f"centered ~{avg_min}-min rolling average. Bands split the used % by the $ "
        f"each conversation spent (a proxy — the % itself is account-wide). Dashed "
        f"lines: warn {warn:.0f}% and ceiling 100%.{reset_note}"
    )

    # Total usage: the current window's rolling fill (0–100 %), reset-scoped.
    st.markdown(f"**Total usage — current {which} window fill (%)**")
    acc = prep.usage_accumulation_frame(
        series, labels, bucket, reset_times=resets,
        current_window_only=True, window_seconds=win_len,
    )
    if acc.empty:
        st.caption("No usage data in this range yet.")
    else:
        st.altair_chart(_cumulative_usage_chart(acc, resets, color_domain),
                        use_container_width=True)
        st.caption(
            f"Current {which} window's fill level (0–100 %): the rolling used % "
            "since its last reset (rises and falls), split by $ spent per "
            "conversation. The red line marks the window reset."
        )


def _conv_color(color_domain):
    legend = alt.Legend(title="conversation")
    if color_domain:
        return alt.Color("conversation:N", legend=legend,
                         scale=alt.Scale(domain=color_domain))
    return alt.Color("conversation:N", legend=legend)


def _cumulative_usage_chart(df, resets=None, color_domain=None):
    area = (
        alt.Chart(df)
        .mark_area()
        .encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("used_pct:Q", stack=True, title="% of window",
                    scale=alt.Scale(domain=[0, 100])),
            color=_conv_color(color_domain),
            tooltip=["conversation:N",
                     alt.Tooltip("used_pct:Q", title="% of window", format=".0f")],
        )
    )
    ceiling = alt.Chart(pd.DataFrame({"y": [100.0]})).mark_rule(
        strokeDash=[6, 4], color="#999"
    ).encode(y="y:Q")
    layers = [area, ceiling]
    if resets:
        vlines = alt.Chart(pd.DataFrame({"time": list(resets)})).mark_rule(
            color="#e4572e", strokeWidth=2
        ).encode(x="time:T")
        layers.append(vlines)
    return alt.layer(*layers).properties(height=200)


def _stacked_pace_chart(df, warn: float, resets=None, color_domain=None):
    area = (
        alt.Chart(df)
        .mark_area()
        .encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("share:Q", stack=True, title="used % of window",
                    scale=alt.Scale(domain=[0, 105])),
            color=_conv_color(color_domain),
            tooltip=["conversation:N",
                     alt.Tooltip("share:Q", title="$-weighted share of used %",
                                 format=".1f")],
        )
    )
    refs = pd.DataFrame({"y": [warn, 100.0],
                         "label": [f"warn {warn:.0f}%", "ceiling 100%"]})
    rules = alt.Chart(refs).mark_rule(strokeDash=[6, 4], color="#999").encode(y="y:Q")
    rule_labels = (
        alt.Chart(refs)
        .mark_text(align="left", dx=5, dy=-5, color="#999")
        .encode(x=alt.value(5), y="y:Q", text="label:N")
    )
    layers = [area, rules, rule_labels]
    if resets:
        vlines = alt.Chart(pd.DataFrame({"time": list(resets)})).mark_rule(
            color="#e4572e", strokeWidth=2
        ).encode(x="time:T")
        layers.append(vlines)
    return alt.layer(*layers).properties(height=260)


def render_usage(controls: Controls, config: dict) -> None:
    """Used % of both windows over time, plus cost over time."""
    rows = data.usage_rows(controls.window_seconds)
    usage = prep.usage_frame(rows)
    cost = prep.cost_frame(rows)
    st.markdown("**Used % over time**")
    if usage.empty:
        st.info("No usage samples in this range yet.")
    else:
        st.line_chart(usage, x="time", y="used_pct", color="window", height=300)
    st.markdown("**Cost over time** (session running total)")
    if cost.empty:
        st.caption("No cost samples in this range.")
    else:
        st.area_chart(cost, x="time", y="cost_usd", height=220)


def render_models(controls: Controls, config: dict) -> None:
    """Model usage breakdown from the transcript index."""
    rows = data.model_rows(data.generation(), controls.window_seconds)
    df = prep.model_frame(rows)
    st.markdown("**Messages by model**")
    if df.empty:
        st.info("No model data — run the indexer (`claude-usage-index`).")
        return
    st.bar_chart(df, x="model", y="messages", height=300)
    st.dataframe(df, hide_index=True, use_container_width=True)


def render_effort(controls: Controls, config: dict) -> None:
    """Effort-level distribution from budget.db samples."""
    rows = data.effort_rows(controls.window_seconds)
    df = prep.effort_frame(rows)
    st.markdown("**Samples by effort level**")
    if df.empty:
        st.info("No effort samples in this range yet.")
        return
    st.bar_chart(df, x="effort", y="samples", height=300)
    total = int(df["samples"].sum())
    fast = int(df["fast_samples"].sum()) if "fast_samples" in df else 0
    pct = (fast / total * 100) if total else 0
    st.metric("Fast-mode samples", f"{fast} / {total} ({pct:.0f}%)")


def render_conversations(controls: Controls, config: dict) -> None:
    """Filterable conversation table with per-session drill-down."""
    gen = data.generation()
    rows = data.conversations(gen, controls.repo, controls.search)
    df = prep.conversations_frame(rows)
    st.markdown(f"**Conversations** ({len(df)})")
    if df.empty:
        st.info("No conversations indexed yet — run `claude-usage-index`.")
        return

    event = st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
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
        st.line_chart(usage, x="time", y="used_pct", color="window", height=240)
    cost = prep.cost_frame(series)
    if not cost.empty:
        st.area_chart(cost, x="time", y="cost_usd", height=180)


def _pct(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{v:.0f}%"
