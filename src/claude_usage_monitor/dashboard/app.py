"""Streamlit entry point for the claude-usage-monitor dashboard.

Run with:  uv run --extra dashboard claude-usage-dashboard
       or:  uv run --extra dashboard streamlit run <path-to-this-file>
"""

from __future__ import annotations

import time

import streamlit as st

# Absolute imports: Streamlit execs this file as a top-level script (no package
# context), so relative imports would fail. The package is installed, so these
# resolve; the submodules below keep their own relative imports.
from claude_usage_monitor.config import load_config
from claude_usage_monitor.db import db_path
from claude_usage_monitor.dashboard import data, kpi, panels, prep

st.set_page_config(page_title="Claude Usage Monitor", layout="wide")

# Reclaim the large default top padding and tighten vertical spacing so the
# whole dashboard fits on screen without scrolling.
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 1rem; }
      [data-testid="stHeader"] { display: none; }
      [data-testid="stToolbar"] { display: none; }
      [data-testid="stDecoration"] { display: none; }
      #MainMenu { display: none; }
      [data-testid="stVerticalBlock"] { gap: 0.4rem; }
      [data-testid="stMetric"] { padding: 2px 0; }
      h1, h2, h3 { margin: 0.2rem 0; padding: 0; }
      .stCaption, [data-testid="stCaptionContainer"] { margin-top: -0.3rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _sidebar(config: dict) -> prep.Controls:
    st.sidebar.title("Claude Usage")

    range_label = st.sidebar.radio("Range", list(prep.RANGES), index=1, horizontal=True)
    window_focus = st.sidebar.radio("Pace window", ["5h", "7d"], horizontal=True)

    gen = data.generation()
    repo_options = ["All", *data.repos(gen)]
    repo = st.sidebar.selectbox("Repo", repo_options, index=0)
    search = st.sidebar.text_input("Search sessions", value="")
    live = st.sidebar.toggle("Live (5s)", value=True)

    if st.sidebar.button("Reindex now"):
        data.refresh_index.clear()
        st.cache_data.clear()

    status = data.status()
    last = status.get("last_indexed")
    last_str = (
        f"{prep.fmt_duration(time.time() - last)} ago" if last else "never"
    )
    st.sidebar.caption(
        f"{status.get('conversations', 0)} conversations · {status.get('files', 0)} "
        f"files indexed · last {last_str}"
    )
    latest = data.latest_sample()
    if latest:
        age = prep.fmt_duration(time.time() - latest["ts"])
        st.sidebar.caption(f"Latest sample {age} ago")
    else:
        st.sidebar.caption("No live samples yet — wire up the statusline monitor.")
    st.sidebar.caption(f"DB: {db_path()}")

    return prep.Controls(
        range_label=range_label,
        window_focus=window_focus,
        repo=None if repo == "All" else repo,
        search=search or None,
        live=live,
    )


def main_render() -> None:
    config = load_config()
    # Keep the index warm (throttled to ~every 15s by its cache TTL).
    try:
        data.refresh_index()
    except Exception:  # never let indexing break the page
        pass

    controls = _sidebar(config)
    refresh = "5s" if controls.live else None

    st.markdown("### Claude Usage Monitor")

    @st.fragment(run_every=refresh)
    def _kpi_row():
        kpi.render_kpis(prep.build_kpis(data.latest_sample(), config, time.time()))

    _kpi_row()

    tab_time, tab_pace, tab_usage, tab_models, tab_effort, tab_conv = st.tabs(
        ["Time", "Pace", "Usage", "Models", "Effort", "Conversations"]
    )

    with tab_time:
        @st.fragment(run_every=refresh)
        def _time():
            panels.render_time(controls, config)
        _time()

    with tab_pace:
        @st.fragment(run_every=refresh)
        def _pace():
            panels.render_pace(controls, config)
        _pace()

    with tab_usage:
        @st.fragment(run_every="30s" if controls.live else None)
        def _usage():
            panels.render_usage(controls, config)
        _usage()

    with tab_models:
        panels.render_models(controls, config)

    with tab_effort:
        panels.render_effort(controls, config)

    with tab_conv:
        panels.render_conversations(controls, config)


main_render()
