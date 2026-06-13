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


def _resolve_window_reading(
    rows: list[dict],
    used_col: str,
    reset_col: str,
    recent_cut: float,
    freeze_seconds: float,
) -> tuple[Any, Any] | None:
    """Best (used%, resets_at) for one window from interleaved samples.

    ``rows`` is the widened scan (recent + freeze lookback), oldest first.

    The naive "MAX used% among FRESH rows" is robust while the account-wide
    counter RISES (a lagging session under-reports, so the max is the leading
    edge) — but it breaks on a RESET: a hung session keeps re-emitting the
    pre-reset payload (same used%/resets_at, fresh ``ts``), so it passes the
    ``resets_at > ts`` freshness test and its stale-HIGH value wins the max for as
    long as it emits (observed ~50 min). So we additionally drop FROZEN sessions —
    one stuck repeating a single value for >= ``freeze_seconds`` (a hung process,
    not a live one tracking the account) — and read each LIVE session's LATEST
    value, then take the max of those. If every session is frozen (a genuinely
    idle/maxed account, possibly with a lone stuck zombie), fall back to the
    consensus (lower-median) so one stuck high reading can't dominate.

    Returns ``None`` when no fresh sample exists (caller keeps its fallback).
    """
    fresh = [
        r for r in rows
        if r.get(used_col) is not None
        and r.get(reset_col) is not None
        and r[reset_col] > r["ts"]
    ]
    if not fresh:
        return None

    # Frozen = a session repeating ONE used% across fresh history spanning at least
    # freeze_seconds. Needs the widened scan to see the constancy. (fresh is asc.)
    by_session: dict[Any, list[dict]] = {}
    for r in fresh:
        by_session.setdefault(r.get("session_id"), []).append(r)
    frozen = {
        sid for sid, rs in by_session.items()
        if len({rr[used_col] for rr in rs}) == 1
        and (rs[-1]["ts"] - rs[0]["ts"]) >= freeze_seconds
    }

    # Each session's LATEST fresh reading within the recent snapshot window.
    latest: dict[Any, dict] = {}
    for r in fresh:
        if r["ts"] < recent_cut:
            continue
        sid = r.get("session_id")
        if sid not in latest or r["ts"] >= latest[sid]["ts"]:
            latest[sid] = r
    if not latest:  # fresh history exists but nothing in the recent window
        newest = fresh[-1]
        return newest[used_col], newest[reset_col]

    live = [r for sid, r in latest.items() if sid not in frozen]
    if live:
        best = max(live, key=lambda r: r[used_col])
        return best[used_col], best[reset_col]

    # All sources frozen: lower-median over per-session-latest, so a single stuck
    # zombie can't outvote the consensus (e.g. idle right after a reset).
    ordered = sorted(latest.values(), key=lambda r: r[used_col])
    pick = ordered[(len(ordered) - 1) // 2]
    return pick[used_col], pick[reset_col]


def current_reading(
    *, recent_seconds: float = 30.0, freeze_seconds: float = 90.0, budget_conn=None
) -> dict[str, Any] | None:
    """A single canonical 'current usage' reading, robust to interleaving + resets.

    budget.db interleaves concurrent sessions; a single ``ORDER BY ts DESC`` row
    is a lottery (and may be a stale 'zombie'). Over a recent snapshot window
    (``recent_seconds``) we pick each window's best FRESH (``resets_at_* > ts``)
    reading via :func:`_resolve_window_reading`, which drops hung/frozen sessions
    so a post-reset drop is reflected promptly rather than masked by a stuck
    pre-reset payload. The scan is widened by ``freeze_seconds`` so frozen sessions
    can be recognized. Cost/model/effort/ts come from the latest row; falls back to
    that row's values for a window with no fresh sample.

    NOTE: at the exact reset moment this can read lower than the Pace charts (which
    still take a per-bucket max-of-fresh and so briefly show the lingering zombie
    band) — the KPI/alert 'current' value is intentionally the fresher of the two.
    """
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return None
    try:
        max_ts_row = conn.execute("SELECT MAX(ts) AS m FROM samples").fetchone()
        if not max_ts_row or max_ts_row["m"] is None:
            return None
        max_ts = max_ts_row["m"]
        cutoff = max_ts - (recent_seconds + freeze_seconds)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
            )
        ]
        if not rows:
            return None

        out = dict(rows[-1])  # latest row seeds cost/model/effort/ts/session_id
        recent_cut = max_ts - recent_seconds
        for used_col, reset_col in (
            ("used_pct_5h", "resets_at_5h"),
            ("used_pct_7d", "resets_at_7d"),
        ):
            chosen = _resolve_window_reading(
                rows, used_col, reset_col, recent_cut, freeze_seconds
            )
            if chosen is not None:
                out[used_col], out[reset_col] = chosen
            # else: leave the latest row's values (graceful fallback)
        return out
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


_OVERAGE_COLS = {
    "5h": ("used_pct_5h", "resets_at_5h"),
    "7d": ("used_pct_7d", "resets_at_7d"),
}


def overage_spend_usd(
    which: str = "7d",
    *,
    over_pct: float = 100.0,
    since: float | None = None,
    now: float,
    budget_conn=None,
) -> float:
    """Notional $ accrued while the ``which`` window was AT/OVER ``over_pct``.

    ``cost_usd`` is a per-session running total (monotonic), so the spend in an
    interval is the positive diff of consecutive samples of the SAME session.
    ``used_pct_*`` is the ACCOUNT-WIDE rolling counter, reported (identically,
    modulo staleness) by every concurrent session — so each sample's own fresh
    reading tells us whether the account was over the cap at that moment. We sum
    each session's positive cost delta only across samples that are FRESH
    (``reset > ts`` — drops lagging zombie snapshots) and AT/OVER the cap.

    Computed in SQL (one indexed scan + window function) so the statusline can
    afford it every tick even for the 7d period. ``since`` bounds the scan to the
    current period start; ``None`` means all history. Returns 0.0 on any miss.
    """
    used_col, reset_col = _OVERAGE_COLS.get(which, _OVERAGE_COLS["7d"])
    conn, own = _open_budget(budget_conn)
    if conn is None:
        return 0.0
    try:
        where = [
            "prev IS NOT NULL",
            f"{used_col} >= ?",
            f"{reset_col} IS NOT NULL",
            f"{reset_col} > ts",
            "cost_usd IS NOT NULL",
        ]
        params: list[Any] = [over_pct]
        ts_filter = ""
        if since is not None:
            ts_filter = "WHERE ts >= ?"
            params.insert(0, since)
        sql = (
            f"WITH s AS (SELECT ts, {used_col}, {reset_col}, cost_usd, "
            f"  LAG(cost_usd) OVER (PARTITION BY session_id ORDER BY ts) AS prev "
            f"  FROM samples {ts_filter}) "
            f"SELECT COALESCE(SUM(MAX(cost_usd - prev, 0)), 0) AS overage "
            f"FROM s WHERE {' AND '.join(where)}"
        )
        row = conn.execute(sql, params).fetchone()
        return float(row["overage"]) if row and row["overage"] is not None else 0.0
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
                "session_id": row.get("session_id"),
                "used_pct": fc.current_pct,
                "projected_pct": fc.projected_pct,
                "on_pace": fc.on_pace,
                "reset_at": row[reset_col],
                "cost_usd": row.get("cost_usd"),
            }
        )
    return out


def session_labels(*, index_conn=None) -> dict[str, str]:
    """Map ``session_id -> human label`` for charts.

    Prefers the ai-title, then the git branch (recognizable for untitled
    worktree/side sessions), then a short session id.
    """
    own = index_conn is None
    index_conn = index_conn or index_db.connect()
    try:
        rows = index_conn.execute(
            "SELECT session_id, title, git_branch FROM conversations"
        ).fetchall()
        # Prefer the git branch (matches the user's branch/worktree tab naming),
        # then the ai-title, then a short session id.
        return {
            r["session_id"]: (r["git_branch"] or r["title"] or r["session_id"][:8])
            for r in rows
        }
    finally:
        if own:
            index_conn.close()


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
