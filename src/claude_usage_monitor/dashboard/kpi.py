"""KPI metric row rendering (thin Streamlit layer over prep.build_kpis)."""

from __future__ import annotations

import streamlit as st

from .prep import Kpi


def render_kpis(kpis: list[Kpi]) -> None:
    # Interleave a slim separator column wherever the window group changes (5h ->
    # 7d -> monthly), so the seven tiles read as distinct clusters. `spec` holds
    # the column weights; `layout` the matching ("sep" | Kpi) render plan.
    spec: list[float] = []
    layout: list[Kpi | None] = []  # None marks a separator column
    prev_group = None
    for k in kpis:
        if prev_group is not None and k.group != prev_group:
            spec.append(0.12)
            layout.append(None)
        spec.append(1.0)
        layout.append(k)
        prev_group = k.group

    cols = st.columns(spec, gap="medium")
    tile_i = 0
    for col, item in zip(cols, layout):
        with col:
            if item is None:
                st.markdown('<div class="km-kpi-sep"></div>', unsafe_allow_html=True)
                continue
            # Wrap in a keyed container so app.py CSS can color the tile by status
            # (the key becomes an `st-key-…` class). Index keeps keys unique.
            with st.container(key=f"kpitile-{item.status or 'none'}-{tile_i}"):
                st.metric(item.label, item.value, help=item.help)
                # Slim gauge under the value: used % on the Used tiles, window
                # elapsed on the Time Left tiles. Other tiles leave progress None.
                if item.progress is not None:
                    st.progress(item.progress)
            tile_i += 1
