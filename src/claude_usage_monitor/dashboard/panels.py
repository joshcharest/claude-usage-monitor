"""The six dashboard panels. Each renders one tab from cached data + prep."""

from __future__ import annotations

import time

import streamlit as st

from . import data, prep
from .prep import Controls


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
    """Pace over time for the focused window, smoothed and split by conversation."""
    which = controls.window_focus
    series = data.pace_rows(which, controls.window_seconds)
    st.markdown(f"**{which} window — pace by conversation**")
    if not series:
        st.info("No pace data in this range yet — the live statusline monitor "
                "needs to record samples first.")
        return

    last = series[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Current", _pct(last.get("used_pct")))
    c2.metric("Projected", _pct(last.get("projected_pct")))
    on_pace = last.get("on_pace")
    c3.metric("Status",
              "on pace" if on_pace else ("over budget" if on_pace is False else "—"))

    titles = data.session_titles(data.generation())
    bucket = prep.pace_bucket_seconds(controls.window_seconds, series)
    df = prep.pace_conversation_frame(series, titles, bucket)

    warn = config.get("alerts", {}).get("warn_projected_pct", 90)
    st.caption(f"Budget-burndown projection (used% ÷ fraction of window elapsed), "
               f"smoothed to ~{int(bucket)}s buckets. Each color is one conversation. "
               f"Warn {warn}%, ceiling 100%.")
    st.markdown("**Projected pace**")
    st.line_chart(df, x="time", y="projected_pct", color="conversation", height=320)
    st.markdown("**Actual usage**")
    st.line_chart(df, x="time", y="used_pct", color="conversation", height=240)


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
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"
