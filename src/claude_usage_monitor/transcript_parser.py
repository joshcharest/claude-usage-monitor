"""Pure, streaming parser for a single Claude Code transcript (``.jsonl``).

No I/O of its own beyond reading the file handle it is given, and no DB access —
so it is trivially unit-testable. The indexer owns file discovery and DB writes.

Transcripts are append-only JSONL, one record per line. We make a single pass,
extracting only the few fields a usage dashboard needs and never loading the
whole file into memory. Token counts are intentionally ignored (unreliable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, BinaryIO

# Record types we care about; everything else is skipped harmlessly.
_TYPE_USER = "user"
_TYPE_ASSISTANT = "assistant"
_TYPE_SYSTEM = "system"
_TYPE_AI_TITLE = "ai-title"


@dataclass
class ModelAgg:
    message_count: int = 0
    first_ts: float | None = None
    last_ts: float | None = None


@dataclass
class ParsedSession:
    session_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    active_secs: float = 0.0
    message_count: int = 0
    sidechain_count: int = 0
    turn_count: int = 0
    # (model, is_sidechain) -> aggregate
    models: dict[tuple[str, bool], ModelAgg] = field(default_factory=dict)
    bytes_consumed: int = 0


def _parse_ts(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp (``...Z``) to epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _merge_ts_bounds(session: ParsedSession, ts: float | None) -> None:
    if ts is None:
        return
    if session.started_at is None or ts < session.started_at:
        session.started_at = ts
    if session.ended_at is None or ts > session.ended_at:
        session.ended_at = ts


def parse_stream(
    fh: BinaryIO, *, start_offset: int = 0, into: ParsedSession | None = None
) -> ParsedSession:
    """Parse records from a BINARY file handle starting at ``start_offset``.

    A binary handle is required so ``bytes_consumed`` is a real byte offset that
    can be passed back as ``start_offset`` for an incremental tail-parse (text
    mode ``seek`` does not accept arbitrary byte offsets).

    Pass an existing ``ParsedSession`` via ``into`` to MERGE appended records
    into a prior parse; otherwise a fresh session is returned. ``bytes_consumed``
    advances only to the last COMPLETE line, so a partial trailing line (file
    mid-append) is re-read next time.
    """
    session = into if into is not None else ParsedSession()
    fh.seek(start_offset)
    consumed = start_offset

    for raw in fh:
        # Only count a line once we know it ended in a newline (complete record).
        if not raw.endswith(b"\n"):
            break
        consumed += len(raw)

        try:
            stripped = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not stripped:
            continue
        try:
            rec = _loads(stripped)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue

        _ingest_record(session, rec)

    session.bytes_consumed = consumed
    return session


def _loads(text: str) -> Any:
    import json

    return json.loads(text)


def _ingest_record(session: ParsedSession, rec: dict[str, Any]) -> None:
    rtype = rec.get("type")
    is_sidechain = bool(rec.get("isSidechain"))

    if session.session_id is None and isinstance(rec.get("sessionId"), str):
        session.session_id = rec["sessionId"]

    if rtype == _TYPE_AI_TITLE:
        title = rec.get("aiTitle")
        if isinstance(title, str) and title:
            session.title = title  # last-wins
        return

    ts = _parse_ts(rec.get("timestamp"))
    _merge_ts_bounds(session, ts)

    # Main-agent context wins; sidechain records must not clobber cwd/branch.
    if not is_sidechain:
        if isinstance(rec.get("cwd"), str):
            session.cwd = rec["cwd"]
        if isinstance(rec.get("gitBranch"), str):
            session.git_branch = rec["gitBranch"]
        if isinstance(rec.get("version"), str):
            session.version = rec["version"]

    if rtype == _TYPE_ASSISTANT:
        if is_sidechain:
            session.sidechain_count += 1
        else:
            session.message_count += 1
        model = (rec.get("message") or {}).get("model")
        if isinstance(model, str) and model:
            agg = session.models.setdefault((model, is_sidechain), ModelAgg())
            agg.message_count += 1
            if ts is not None:
                if agg.first_ts is None or ts < agg.first_ts:
                    agg.first_ts = ts
                if agg.last_ts is None or ts > agg.last_ts:
                    agg.last_ts = ts
    elif rtype == _TYPE_USER:
        if is_sidechain:
            session.sidechain_count += 1
        else:
            session.message_count += 1
    elif rtype == _TYPE_SYSTEM:
        if rec.get("subtype") == "turn_duration":
            session.turn_count += 1
            duration_ms = rec.get("durationMs")
            if isinstance(duration_ms, (int, float)):
                session.active_secs += duration_ms / 1000.0


def models_csv(session: ParsedSession) -> str:
    """Comma-joined distinct model names (main + sidechain), sorted."""
    names = sorted({model for (model, _sc) in session.models})
    return ",".join(names)


def duration_secs(session: ParsedSession) -> float | None:
    if session.started_at is None or session.ended_at is None:
        return None
    return session.ended_at - session.started_at
