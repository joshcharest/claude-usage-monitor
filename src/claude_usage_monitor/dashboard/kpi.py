"""KPI metric row rendering (thin Streamlit layer over prep.build_kpis)."""

from __future__ import annotations

import streamlit as st

from .prep import Kpi


def render_kpis(kpis: list[Kpi]) -> None:
    cols = st.columns(len(kpis), gap="medium")
    for i, (col, k) in enumerate(zip(cols, kpis)):
        with col:
            # Wrap in a keyed container so app.py CSS can color the tile by status
            # (the key becomes an `st-key-…` class). Index keeps keys unique.
            with st.container(key=f"kpitile-{k.status or 'none'}-{i}"):
                st.metric(k.label, k.value, help=k.help)
