"""Orchestrates incremental indexing of transcripts into the conversation index.

Discovers `~/.claude/projects/*/*.jsonl`, decides which files changed using a
cheap stat-vs-bookmark comparison (no file opened for unchanged files), parses
only the new/changed ones (newest first), and writes `conversations` +
`model_usage` rows. Work per call is capped by file count and/or wall-clock so an
auto-refreshing dashboard never re-parses the whole corpus.

Never touches `budget.db` for writing; the statusline writer path is independent.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from . import index_db, statusline_cost, transcript_parser
from .transcript_parser import ParsedSession

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass
class IndexResult:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    rows_written: int = 0
    errors: int = 0
    elapsed_secs: float = 0.0
    capped: bool = False


def projects_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    return Path(override) if override else _PROJECTS_DIR


def discover_transcripts(
    root: Path | str | None = None,
) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Yield ``(path, slug, stat)`` for every transcript file. Cheap (scandir)."""
    base = projects_root(root)
    if not base.exists():
        return
    for proj in os.scandir(base):
        if not proj.is_dir():
            continue
        for entry in os.scandir(proj.path):
            if entry.is_file() and entry.name.endswith(".jsonl"):
                yield Path(entry.path), proj.name, entry.stat()


def needs_reindex(stat: os.stat_result, row) -> Literal["skip", "append", "full"]:
    """Decide how a file must be (re)indexed from its stat vs the bookmark row."""
    if row is None:
        return "full"
    if stat.st_size < row["size_bytes"] or stat.st_mtime_ns < row["mtime_ns"]:
        return "full"  # shrank / rewritten / rotated
    if stat.st_size == row["size_bytes"] and stat.st_mtime_ns == row["mtime_ns"]:
        return "skip"
    if stat.st_size > row["size_bytes"]:
        return "append"
    return "full"  # same size, newer mtime => content changed in place


def reindex(
    *,
    conn=None,
    max_files: int | None = None,
    max_seconds: float | None = None,
    now: float | None = None,
    root: Path | str | None = None,
    capture_path: Path | None = None,
) -> IndexResult:
    """Index changed/new transcripts, capped by ``max_files`` / ``max_seconds``."""
    own = conn is None
    conn = conn or index_db.connect()
    now = now if now is not None else time.time()
    started = time.monotonic()
    result = IndexResult()

    bookmarks = {
        r["path"]: r for r in conn.execute("SELECT * FROM index_files")
    }

    todo: list[tuple[Path, str, os.stat_result, str]] = []
    for path, slug, stat in discover_transcripts(root):
        result.files_seen += 1
        mode = needs_reindex(stat, bookmarks.get(str(path)))
        if mode == "skip":
            result.files_skipped += 1
        else:
            todo.append((path, slug, stat, mode))

    todo.sort(key=lambda t: t[2].st_mtime_ns, reverse=True)  # freshest first

    any_written = False
    for path, slug, stat, mode in todo:
        if max_files is not None and result.files_indexed >= max_files:
            result.capped = True
            break
        if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
            result.capped = True
            break
        bookmark = bookmarks.get(str(path))
        try:
            wrote = _reindex_one(conn, path, slug, stat, mode, bookmark, now)
            result.rows_written += wrote
            result.files_indexed += 1
            any_written = any_written or wrote > 0
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - record and continue
            result.errors += 1
            _record_error(conn, path, stat, str(exc), now)
            conn.commit()

    if any_written:
        costs = statusline_cost.load_session_costs(capture_path)
        if costs:
            enrich_costs(conn, costs)
        index_db.bump_generation(conn)
        conn.commit()

    result.elapsed_secs = time.monotonic() - started
    if own:
        conn.close()
    return result


def _reindex_one(conn, path, slug, stat, mode, bookmark, now) -> int:
    start_offset = 0 if mode == "full" else int(bookmark["offset_bytes"])
    with open(path, "rb") as fh:
        delta = transcript_parser.parse_stream(fh, start_offset=start_offset)

    session_id = delta.session_id or path.stem
    has_signal = (
        delta.message_count
        or delta.sidechain_count
        or delta.title
        or delta.started_at is not None
    )

    if mode == "full":
        conn.execute("DELETE FROM model_usage WHERE session_id = ?", (session_id,))
        if has_signal:
            _write_conversation_full(conn, session_id, slug, delta, now)
            _upsert_models(conn, session_id, delta)
            _refresh_models_csv(conn, session_id)
    else:  # append
        if has_signal:
            _apply_conversation_delta(conn, session_id, delta, now)
            _upsert_models(conn, session_id, delta)
            _refresh_models_csv(conn, session_id)

    _write_bookmark(conn, path, session_id, stat, delta.bytes_consumed, now)
    return 1 if has_signal else 0


def _write_conversation_full(conn, session_id, slug, s: ParsedSession, now) -> None:
    conn.execute(
        """
        INSERT INTO conversations (
            session_id, title, cwd, git_branch, slug, started_at, ended_at,
            duration_secs, active_secs, message_count, sidechain_count,
            turn_count, version, updated_at
        ) VALUES (:sid,:title,:cwd,:branch,:slug,:started,:ended,:dur,:active,
                  :msg,:side,:turns,:ver,:now)
        ON CONFLICT(session_id) DO UPDATE SET
            title=excluded.title, cwd=excluded.cwd, git_branch=excluded.git_branch,
            slug=excluded.slug, started_at=excluded.started_at,
            ended_at=excluded.ended_at, duration_secs=excluded.duration_secs,
            active_secs=excluded.active_secs, message_count=excluded.message_count,
            sidechain_count=excluded.sidechain_count, turn_count=excluded.turn_count,
            version=excluded.version, updated_at=excluded.updated_at
        """,
        {
            "sid": session_id, "title": s.title, "cwd": s.cwd,
            "branch": s.git_branch, "slug": slug, "started": s.started_at,
            "ended": s.ended_at, "dur": transcript_parser.duration_secs(s),
            "active": s.active_secs, "msg": s.message_count,
            "side": s.sidechain_count, "turns": s.turn_count,
            "ver": s.version, "now": now,
        },
    )


def _apply_conversation_delta(conn, session_id, d: ParsedSession, now) -> None:
    existing = conn.execute(
        "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
    ).fetchone()
    if existing is None:
        # No prior row (e.g. earlier pass had no signal) -> treat delta as full.
        conn.execute(
            """INSERT INTO conversations (
                   session_id, title, cwd, git_branch, started_at, ended_at,
                   duration_secs, active_secs, message_count, sidechain_count,
                   turn_count, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, d.title, d.cwd, d.git_branch, d.started_at, d.ended_at,
             transcript_parser.duration_secs(d), d.active_secs, d.message_count,
             d.sidechain_count, d.turn_count, d.version, now),
        )
        return

    started = existing["started_at"]
    ended = _max(existing["ended_at"], d.ended_at)
    duration = (ended - started) if (started is not None and ended is not None) else None
    conn.execute(
        """UPDATE conversations SET
               title = COALESCE(?, title),
               cwd = COALESCE(?, cwd),
               git_branch = COALESCE(?, git_branch),
               version = COALESCE(?, version),
               ended_at = ?,
               duration_secs = ?,
               active_secs = COALESCE(active_secs, 0) + ?,
               message_count = COALESCE(message_count, 0) + ?,
               sidechain_count = COALESCE(sidechain_count, 0) + ?,
               turn_count = COALESCE(turn_count, 0) + ?,
               updated_at = ?
           WHERE session_id = ?""",
        (d.title, d.cwd, d.git_branch, d.version, ended, duration, d.active_secs,
         d.message_count, d.sidechain_count, d.turn_count, now, session_id),
    )


def _upsert_models(conn, session_id, s: ParsedSession) -> None:
    for (model, is_side), agg in s.models.items():
        conn.execute(
            """INSERT INTO model_usage
                   (session_id, model, is_sidechain, message_count, first_ts, last_ts)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id, model, is_sidechain) DO UPDATE SET
                   message_count = message_count + excluded.message_count,
                   first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts),
                   last_ts = MAX(COALESCE(last_ts, excluded.last_ts), excluded.last_ts)""",
            (session_id, model, 1 if is_side else 0, agg.message_count,
             agg.first_ts, agg.last_ts),
        )


def _refresh_models_csv(conn, session_id) -> None:
    rows = conn.execute(
        "SELECT DISTINCT model FROM model_usage WHERE session_id = ? ORDER BY model",
        (session_id,),
    ).fetchall()
    csv = ",".join(r["model"] for r in rows)
    conn.execute(
        "UPDATE conversations SET models_csv = ? WHERE session_id = ?",
        (csv, session_id),
    )


def _write_bookmark(conn, path, session_id, stat, offset, now) -> None:
    conn.execute(
        """INSERT INTO index_files
               (path, session_id, size_bytes, mtime_ns, offset_bytes, last_indexed, parse_error)
           VALUES (?,?,?,?,?,?,NULL)
           ON CONFLICT(path) DO UPDATE SET
               session_id=excluded.session_id, size_bytes=excluded.size_bytes,
               mtime_ns=excluded.mtime_ns, offset_bytes=excluded.offset_bytes,
               last_indexed=excluded.last_indexed, parse_error=NULL""",
        (str(path), session_id, stat.st_size, stat.st_mtime_ns, offset, now),
    )


def _record_error(conn, path, stat, message, now) -> None:
    conn.execute(
        """INSERT INTO index_files
               (path, session_id, size_bytes, mtime_ns, offset_bytes, last_indexed, parse_error)
           VALUES (?,NULL,?,?,0,?,?)
           ON CONFLICT(path) DO UPDATE SET
               last_indexed=excluded.last_indexed, parse_error=excluded.parse_error""",
        (str(path), stat.st_size, stat.st_mtime_ns, now, message),
    )


def enrich_costs(conn, costs: dict[str, statusline_cost.SessionCost]) -> None:
    """Fill ``cost_usd`` / ``repo`` on conversations from statusline-capture."""
    for sid, sc in costs.items():
        conn.execute(
            "UPDATE conversations SET cost_usd = ?, repo = COALESCE(?, repo) "
            "WHERE session_id = ?",
            (sc.cost_usd, sc.repo, sid),
        )


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="claude-usage-index",
        description="Build/refresh the conversation index from Claude Code transcripts.",
    )
    parser.add_argument("--max-files", type=int, default=None,
                        help="Cap files indexed this run (default: no cap).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Cap wall-clock for this run (default: no cap).")
    args = parser.parse_args(argv)

    result = reindex(max_files=args.max_files, max_seconds=args.max_seconds)
    print(
        f"indexed {result.files_indexed}/{result.files_seen} files "
        f"({result.files_skipped} skipped, {result.errors} errors), "
        f"{result.rows_written} rows in {result.elapsed_secs:.1f}s, "
        f"capped={result.capped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
