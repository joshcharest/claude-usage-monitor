"""KPI metric row rendering (thin Streamlit layer over prep.build_kpis)."""

from __future__ import annotations

import streamlit as st

from .prep import Kpi


def render_kpis(kpis: list[Kpi]) -> None:
    cols = st.columns(len(kpis), gap="medium")
    for col, k in zip(cols, kpis):
        with col:
            st.metric(
                k.label,
                k.value,
                delta=k.delta,
                delta_color=k.delta_color,
                help=k.help,
            )
