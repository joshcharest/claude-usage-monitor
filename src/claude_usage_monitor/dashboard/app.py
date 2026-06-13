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

# ---------------------------------------------------------------------------
# "modern-soft" design system — injected CSS.
#
# Design tokens (kept in sync with the Altair theme in panels.py):
#   accent           #6366f1  (indigo-500)  — primary actions / focus
#   accent-soft      rgba(99,102,241,.16)   — hover / selected tint
#   surface          rgba(255,255,255,.04)  — card / chip background (dark)
#   surface-hover    rgba(255,255,255,.07)
#   border-soft      rgba(255,255,255,.09)  — hairline card borders
#   text-strong      #e6e8ef                — values / headings
#   text-muted       #9aa1b1                — labels / captions
#   radius           14px (cards) / 10px (chips) / 8px (controls)
#   shadow           0 1px 2px rgba(0,0,0,.25), 0 4px 16px rgba(0,0,0,.18)
#
# The hidden-toolbar / tight-top rules are deliberate and preserved.
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
      :root {
        --km-accent: #6366f1;
        --km-accent-soft: rgba(99, 102, 241, 0.16);
        --km-surface: rgba(255, 255, 255, 0.04);
        --km-surface-hover: rgba(255, 255, 255, 0.07);
        --km-border: rgba(255, 255, 255, 0.09);
        --km-text-strong: #e6e8ef;
        --km-text-muted: #9aa1b1;
        --km-radius-card: 14px;
        --km-radius-chip: 10px;
        --km-shadow: 0 1px 2px rgba(0, 0, 0, 0.25), 0 4px 16px rgba(0, 0, 0, 0.18);
      }

      /* Reclaim default top padding; hide chrome (deliberate). */
      .block-container { padding-top: 1.1rem; padding-bottom: 1.4rem; max-width: 1700px; }
      [data-testid="stHeader"] { display: none; }
      [data-testid="stToolbar"] { display: none; }
      [data-testid="stDecoration"] { display: none; }
      #MainMenu { display: none; }

      /* Friendly type: comfortable headings, muted captions. */
      h1, h2, h3, h4 { letter-spacing: -0.01em; }
      h1, h2, h3 { margin: 0.2rem 0; padding: 0; }
      .stCaption, [data-testid="stCaptionContainer"] {
        color: var(--km-text-muted); line-height: 1.45;
      }

      /* Page title: anchor the top of the page with a touch more weight. */
      .km-title h2 {
        margin: 0.1rem 0 0.5rem 0;
        font-weight: 700;
        font-size: 1.5rem;
        color: var(--km-text-strong);
      }

      /* Default vertical rhythm is tight; tabs get breathing room (below). */
      [data-testid="stVerticalBlock"] { gap: 0.45rem; }
      .stTabs [data-testid="stVerticalBlock"] { gap: 0.8rem; }

      /* ---- KPI chips: subtle soft cards ---------------------------------- */
      [data-testid="stMetric"] {
        background: var(--km-surface);
        border: 1px solid var(--km-border);
        border-radius: var(--km-radius-chip);
        padding: 6px 14px;
        box-shadow: var(--km-shadow);
        transition: background 120ms ease, transform 120ms ease;
      }
      [data-testid="stMetric"]:hover {
        background: var(--km-surface-hover);
        transform: translateY(-1px);
      }
      [data-testid="stMetricLabel"] { opacity: 0.85; }
      [data-testid="stMetricLabel"] p { color: var(--km-text-muted); font-weight: 500; }
      [data-testid="stMetricValue"] {
        color: var(--km-text-strong); font-weight: 650;
        font-size: 1.55rem; line-height: 1.15;
      }

      /* KPI status coloring: the tile itself (border + faint tint) signals the
         projection status, replacing the old "on pace" delta pill so every tile
         is the same height. Keyed containers from kpi.py expose st-key-… classes. */
      [class*="st-key-kpitile-ok-"] [data-testid="stMetric"] {
        border-color: rgba(34, 197, 94, 0.45);
        background: rgba(34, 197, 94, 0.08);
      }
      [class*="st-key-kpitile-warn-"] [data-testid="stMetric"] {
        border-color: rgba(245, 158, 11, 0.50);
        background: rgba(245, 158, 11, 0.10);
      }
      [class*="st-key-kpitile-over-"] [data-testid="stMetric"] {
        border-color: rgba(239, 68, 68, 0.55);
        background: rgba(239, 68, 68, 0.12);
      }

      /* Vertical hairline between KPI window groups (5h | 7d | monthly). Sits in
         its own narrow column; height roughly matches a tile + its progress bar. */
      .km-kpi-sep {
        width: 1px;
        height: 76px;
        margin: 4px auto 0;
        background: linear-gradient(
          to bottom, transparent, var(--km-border) 18%,
          var(--km-border) 82%, transparent);
      }

      /* In-tab context KPIs (e.g. the Pace Current/Projected/Status row) get an
         accent left-rail so the eye ties them to the chart that follows. */
      .stTabs [data-testid="stMetric"] {
        border-left: 3px solid var(--km-accent);
        border-radius: 8px var(--km-radius-chip) var(--km-radius-chip) 8px;
      }

      /* ---- Tabs: pill-style, rounded, accent on the active tab ----------- */
      .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        border-bottom: 1px solid var(--km-border);
        padding-bottom: 2px;
      }
      .stTabs [data-baseweb="tab"] {
        border-radius: 9px 9px 0 0;
        padding: 6px 14px;
        color: var(--km-text-muted);
      }
      .stTabs [data-baseweb="tab"]:hover { background: var(--km-surface); }
      .stTabs [aria-selected="true"] { color: var(--km-text-strong); }

      /* ---- Charts / dataframes: soft framing ---------------------------- */
      [data-testid="stVegaLiteChart"], .stDataFrame, [data-testid="stTable"] {
        border-radius: var(--km-radius-card);
      }

      /* ---- Sidebar polish ------------------------------------------------ */
      [data-testid="stSidebar"] { border-right: 1px solid var(--km-border); }
      [data-testid="stSidebar"] .stCaption { line-height: 1.55; }

      /* Buttons / toggles pick up the accent for a cohesive product feel. */
      .stButton > button {
        border-radius: 9px;
        border: 1px solid var(--km-border);
        transition: border-color 120ms ease, background 120ms ease;
      }
      .stButton > button:hover {
        border-color: var(--km-accent);
        background: var(--km-accent-soft);
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def _sidebar(config: dict) -> prep.Controls:
    st.sidebar.title("Claude Usage")

    range_label = st.sidebar.radio("Range", list(prep.RANGES), index=1, horizontal=True)

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
    latest = data.latest_sample()
    if latest:
        sample_line = (
            f"Latest sample {prep.fmt_duration(time.time() - latest['ts'])} ago"
        )
    else:
        sample_line = "No live samples yet — wire up the statusline monitor."

    st.sidebar.divider()
    st.sidebar.markdown("###### Status")
    st.sidebar.caption(
        f"{status.get('conversations', 0)} conversations · {status.get('files', 0)} "
        f"files indexed · last {last_str}\n\n{sample_line}"
    )
    with st.sidebar.expander("DB path"):
        st.caption(f"`{db_path()}`")

    return prep.Controls(
        range_label=range_label,
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

    st.markdown(
        '<div class="km-title"><h2>Claude Usage Monitor</h2></div>',
        unsafe_allow_html=True,
    )

    @st.fragment(run_every=refresh)
    def _kpi_row():
        now = time.time()
        cap = float(config.get("alerts", {}).get("over_limit_pct", 100.0))
        # Notional $ used while over the cap on EITHER window, THIS calendar month.
        overage = data.overage_since("any", prep.month_start_epoch(now), cap)
        # Window-spanning trailing history so the layered projector can fit a slope
        # (recent) AND apply the roll-off decay (needs ~one full 7d window back);
        # build_kpis falls back to the ratio model when this is thin/off.
        hist_secs = float(config.get("forecast", {}).get("window_7d_seconds", 604800))
        recent = data.usage_rows(hist_secs * 1.05)
        kpi.render_kpis(
            prep.build_kpis(data.current_reading(), config, now, overage, recent))

    _kpi_row()

    # Pace first so it's the default tab on load (Streamlit selects tab[0]);
    # split into a 5h and a 7d tab (identical layout, per window).
    (tab_pace5, tab_pace7, tab_usage,
     tab_models, tab_effort, tab_conv) = st.tabs(
        ["5h Pace", "7d Pace", "Usage", "Models", "Effort", "Conversations"]
    )

    with tab_pace5:
        @st.fragment(run_every=refresh)
        def _pace5():
            panels.render_pace(controls, config, "5h")
        _pace5()

    with tab_pace7:
        @st.fragment(run_every=refresh)
        def _pace7():
            panels.render_pace(controls, config, "7d")
        _pace7()

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
