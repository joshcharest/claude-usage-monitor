"""Cached Streamlit wrappers around the read-only query API.

Cache TTLs keep the live KPIs fresh while the expensive conversation index sits
behind a longer cache, keyed on the index ``generation`` so it invalidates
exactly when re-indexing writes new data.
"""

from __future__ import annotations

import time

import streamlit as st

from .. import indexer, queries


def generation() -> str:
    """Current index generation (cache-busting key). Cheap, uncached."""
    return str(queries.index_status().get("generation", "0"))


@st.cache_data(ttl=15)
def refresh_index() -> dict:
    """Run a capped incremental reindex at most every ~15s (throttled by TTL)."""
    result = indexer.reindex(max_files=80, max_seconds=2.0)
    return {"indexed": result.files_indexed, "capped": result.capped,
            "seen": result.files_seen}


@st.cache_data(ttl=4)
def latest_sample() -> dict | None:
    return queries.latest_sample()


@st.cache_data(ttl=4)
def usage_rows(window_seconds: float | None) -> list[dict]:
    return queries.usage_timeseries(window_seconds, now=time.time())


@st.cache_data(ttl=4)
def pace_rows(which: str, window_seconds: float | None) -> list[dict]:
    return queries.pace_timeseries(which, window_seconds, now=time.time())


@st.cache_data(ttl=30)
def model_rows(_gen: str, window_seconds: float | None) -> list[dict]:
    since = (time.time() - window_seconds) if window_seconds else None
    return queries.model_breakdown(since=since)


@st.cache_data(ttl=30)
def effort_rows(window_seconds: float | None) -> list[dict]:
    since = (time.time() - window_seconds) if window_seconds else None
    return queries.effort_breakdown(since=since)


@st.cache_data(ttl=300)
def conversations(_gen: str, repo: str | None, search: str | None) -> list[dict]:
    return queries.list_conversations(
        repo=repo or None, search=search or None, limit=500
    )


@st.cache_data(ttl=300)
def session_series(_gen: str, session_id: str) -> list[dict]:
    return queries.session_timeseries(session_id)


@st.cache_data(ttl=300)
def repos(_gen: str) -> list[str]:
    return queries.distinct_repos()


def status() -> dict:
    return queries.index_status()
