"""Read-only query API the dashboard calls.

Two data tiers are joined by ``session_id``:
- the transcript-derived index (``index_db``): conversations + per-model rollups.
- the live ``budget.db`` ``samples`` time-series: pace, usage %, effort, cost.

We merge in Python (rather than SQLite ATTACH) to avoid cross-DB URI/locking
edge cases and to degrade cleanly when ``budget.db`` does not exist yet (the
monitor may never have run). Conversations are the spine: a session with no live
samples still appears, just without pace/usage fields.
"""

from __future__ import annotations

from typing import Any

from . import db, index_db
from .config import load_config
from .forecast import forecast_from_period_start

_ORDER_SQL = {
    "started_desc": "started_at DESC",
    "started_asc": "started_at ASC",
    "cost_desc": "cost_usd DESC",
    "duration_desc": "duration_secs DESC",
    "messages_desc": "message_count DESC",
}


# ---------------------------------------------------------------- conversations


def list_conversations(
    *,
    repo: str | None = None,
    model: str | None = None,
    search: str | None = None,
    since: float | None = None,
    until: float | None = None,
    limit: int = 200,
    order: str = "started_desc",
    index_conn=None,
    budget_conn=None,
) -> list[dict[str, Any]]:
    """Return conversation rows (newest first by default), merged with samples."""
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        sql = "SELECT * FROM conversations WHERE 1=1"
        params: list[Any] = []
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        if since is not None:
            sql += " AND started_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND started_at <= ?"
            params.append(until)
        if search:
            sql += " AND (title LIKE ? OR cwd LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        if model:
            sql += " AND models_csv LIKE ?"
            params.append(f"%{model}%")
        sql += f" ORDER BY {_ORDER_SQL.get(order, 'started_at DESC')} LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in index_conn.execute(sql, params)]
    finally:
        if own:
            index_conn.close()

    aggs = _sample_aggs_by_session(budget_conn)
    for row in rows:
        agg = aggs.get(row["session_id"])
        if agg:
            row.update(agg)
    return rows


def get_conversation(session_id: str, *, index_conn=None, budget_conn=None):
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        row = index_conn.execute(
            "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
    finally:
        if own:
            index_conn.close()
    if row is None:
        return None
    out = dict(row)
    agg = _sample_aggs_by_session(budget_conn).get(session_id)
    if agg:
        out.update(agg)
    return out


def distinct_repos(*, index_conn=None) -> list[str]:
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        rows = index_conn.execute(
            "SELECT DISTINCT repo FROM conversations WHERE repo IS NOT NULL ORDER BY repo"
        ).fetchall()
        return [r["repo"] for r in rows]
    finally:
        if own:
            index_conn.close()


# --------------------------------------------------------------------- samples


def _budget_available(conn) -> bool:
    return conn is not None or db.db_path().exists()


def _open_budget(conn):
    """Return (conn, own) or (None, False) if budget.db doesn't exist."""
    if conn is not None:
        return conn, False
    if not db.db_path().exists():
        return None, False
    return db.connect(), True


def _sample_aggs_by_session(conn=None) -> dict[str, dict[str, Any]]:
    conn, own = _open_budget(conn)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            """SELECT session_id,
                      MAX(used_pct_5h) AS peak_5h,
                      MAX(used_pct_7d) AS peak_7d,
                      MAX(cost_usd)    AS sample_cost,
                      MIN(ts)          AS sample_first_ts,
                      MAX(ts)          AS sample_last_ts,
                      COUNT(*)         AS sample_count
               FROM samples WHERE session_id IS NOT NULL GROUP BY session_id"""
        ).fetchall()
        return {
            r["session_id"]: {
                "peak_5h": r["peak_5h"],
                "peak_7d": r["peak_7d"],
                "sample_cost": r["sample_cost"],
                "sample_first_ts": r["sample_first_ts"],
                "sample_last_ts": r["sample_last_ts"],
                "sample_count": r["sample_count"],
            }
            for r in rows
        }
    finally:
        if own:
            conn.close()


def latest_sample(*, budget_conn=None) -> dict[str, Any] | None:
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def session_timeseries(session_id: str, *, budget_conn=None) -> list[dict[str, Any]]:
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM samples WHERE session_id = ? ORDER BY ts ASC", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def usage_timeseries(
    window_seconds: float | None = None, *, now: float, budget_conn=None
) -> list[dict[str, Any]]:
    """Samples within the last ``window_seconds`` (or all if None), oldest first."""
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return []
    try:
        if window_seconds is None:
            rows = conn.execute("SELECT * FROM samples ORDER BY ts ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC",
                (now - window_seconds,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def pace_timeseries(
    which: str = "5h",
    window_seconds: float | None = None,
    *,
    now: float,
    config: dict | None = None,
    budget_conn=None,
) -> list[dict[str, Any]]:
    """Historical pace: per sample, the projection computed *as of that sample*.

    Reuses ``forecast_from_period_start`` so the dashboard's pace matches the
    statusline exactly. ``which`` is "5h" or "7d".
    """
    config = config if config is not None else load_config()
    if which == "7d":
        length = float(_get(config, "forecast", "window_7d_seconds", default=604800))
        used_col, reset_col = "used_pct_7d", "resets_at_7d"
    else:
        length = float(_get(config, "forecast", "window_5h_seconds", default=18000))
        used_col, reset_col = "used_pct_5h", "resets_at_5h"

    out = []
    for row in usage_timeseries(window_seconds, now=now, budget_conn=budget_conn):
        fc = forecast_from_period_start(row[used_col], length, row[reset_col], row["ts"])
        out.append(
            {
                "ts": row["ts"],
                "used_pct": fc.current_pct,
                "projected_pct": fc.projected_pct,
                "on_pace": fc.on_pace,
            }
        )
    return out


# ----------------------------------------------------------------- breakdowns


def model_breakdown(
    *, since: float | None = None, until: float | None = None,
    include_sidechain: bool = False, index_conn=None,
) -> list[dict[str, Any]]:
    """Per-model message + session counts from the transcript index."""
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        sql = ("SELECT model, SUM(message_count) AS messages, "
               "COUNT(DISTINCT session_id) AS sessions "
               "FROM model_usage WHERE 1=1")
        params: list[Any] = []
        if not include_sidechain:
            sql += " AND is_sidechain = 0"
        if since is not None:
            sql += " AND COALESCE(last_ts, first_ts) >= ?"
            params.append(since)
        if until is not None:
            sql += " AND COALESCE(first_ts, last_ts) <= ?"
            params.append(until)
        sql += " GROUP BY model ORDER BY messages DESC"
        return [dict(r) for r in index_conn.execute(sql, params)]
    finally:
        if own:
            index_conn.close()


def effort_breakdown(
    *, since: float | None = None, until: float | None = None, budget_conn=None
) -> list[dict[str, Any]]:
    """Sample counts per effort level from budget.db (effort isn't in transcripts)."""
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return []
    try:
        sql = ("SELECT COALESCE(effort, 'unknown') AS effort, COUNT(*) AS samples, "
               "SUM(CASE WHEN fast_mode = 1 THEN 1 ELSE 0 END) AS fast_samples "
               "FROM samples WHERE 1=1")
        params: list[Any] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " GROUP BY effort ORDER BY samples DESC"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------- status


def index_status(*, index_conn=None) -> dict[str, Any]:
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        conv = index_conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        files = index_conn.execute(
            "SELECT COUNT(*) AS n, MAX(last_indexed) AS last FROM index_files"
        ).fetchone()
        gen = index_db.get_meta(index_conn, "generation", "0")
        return {
            "conversations": conv,
            "files": files["n"],
            "last_indexed": files["last"],
            "generation": gen,
        }
    finally:
        if own:
            index_conn.close()


def _get(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key, default)
    return cur
